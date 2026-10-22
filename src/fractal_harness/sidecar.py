"""The sidecar: where the model lives, outside the repository it describes.

Harness state must not infect the target repo, so the model is kept in `<git common dir>/fractal/model.db`:
inside `.git`, which git never tracks, never in the working tree where build tools, IDEs and test collectors
can see it, shared by every linked worktree, and gone when the clone is. This module writes nothing else, and
nothing anywhere in the working tree.

Two layers in one SQLite file:

  * **snapshots** (the source of truth): every model ever saved, as canonical JSON keyed by its content hash,
    plus an append-only `log` of which snapshot was saved when and under what label. The head is the last
    log entry. Saving an unchanged model under the same label as the head is a no-op.
  * **projection** (derived): the head model as relations (`node`, `port`, `edge`, `invariant`, ...) and
    views over them (`feeds`, the gap views). It is rebuilt from the head on every save and whenever the
    schema version moves, so it can always be dropped. `projected()` reads the model back out of it, which is
    how the tests show the projection loses nothing.

`save` refuses a model that does not validate unless told it is a draft: the head is what the harness
treats as the accepted structure. Gaps never block a save; they are what the model is for reporting.

Queries (`query`) run on a separate read-only connection, so a query can never change the store.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import time
from pathlib import Path

from .model import (Discharge, Edge, Gap, Invariant, Model, Node, Port, Preserve, Strength, parent_of, split_ref)

SIDECAR_DIR = "fractal"
DB_NAME = "model.db"
SCHEMA_VERSION = 2


class SidecarError(RuntimeError):
    pass


def sidecar_dir(root: Path | str) -> Path:
    """`<git common dir>/fractal` for the checkout at `root`: one per clone, shared by its linked worktrees."""
    try:
        out = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=root,
                             capture_output=True, text=True, check=True).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError, NotADirectoryError) as e:
        raise SidecarError(f"{root} is not inside a git repository; the model lives in its .git directory") from e
    if not out or not Path(out).is_absolute() or not Path(out).is_dir():
        raise SidecarError(f"git reported {out!r} as the git directory of {root}; refusing to guess where to write")
    return Path(out) / SIDECAR_DIR


STORE = """
CREATE TABLE IF NOT EXISTS snapshots(key TEXT PRIMARY KEY, doc TEXT NOT NULL, ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS log(seq INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL REFERENCES snapshots(key),
                               label TEXT NOT NULL, ts REAL NOT NULL);
"""

PROJECTION_TABLES = ("node", "owns", "port", "port_inv", "invariant", "assembled", "preserve", "edge")
PROJECTION_VIEWS = ("feeds", "gap_port", "gap_edge", "gap_invariant", "gap_claim")

PROJECTION = """
CREATE TABLE node(id TEXT, parent TEXT, depth INT, leaf INT, state INT, intent TEXT);
CREATE TABLE owns(node TEXT, entry TEXT);
CREATE TABLE port(node TEXT, name TEXT, direction TEXT, type TEXT, many INT, binds TEXT);
CREATE TABLE port_inv(node TEXT, port TEXT, direction TEXT, ref TEXT);
CREATE TABLE invariant(ref TEXT, owner TEXT, name TEXT, statement TEXT, strength INT, pointer TEXT);
CREATE TABLE assembled(ref TEXT, from_ref TEXT);
CREATE TABLE preserve(node TEXT, ref TEXT, strength INT, pointer TEXT);
CREATE TABLE edge(src_node TEXT, src_port TEXT, dst_node TEXT, dst_port TEXT, container TEXT, kind TEXT);

-- `a` feeds `b` through sibling edges, transitively. Cycles (through state nodes) terminate: UNION dedupes.
CREATE VIEW feeds AS
  WITH RECURSIVE r(a, b) AS (
    SELECT src_node, dst_node FROM edge WHERE kind = 'sibling'
    UNION SELECT r.a, e.dst_node FROM r JOIN edge e ON e.kind = 'sibling' AND e.src_node = r.b)
  SELECT a, b FROM r;

-- Below the top level, an input nothing feeds and an output nothing consumes. (The top level faces the outside.)
CREATE VIEW gap_port AS
  SELECT 'unfed-input' AS kind, p.node || '.' || p.name AS subject
  FROM port p JOIN node n ON n.id = p.node
  WHERE p.direction = 'in' AND n.parent <> ''
    AND NOT EXISTS (SELECT 1 FROM edge e WHERE e.dst_node = p.node AND e.dst_port = p.name)
  UNION
  SELECT 'unused-output', p.node || '.' || p.name
  FROM port p JOIN node n ON n.id = p.node
  WHERE p.direction = 'out' AND n.parent <> ''
    AND NOT EXISTS (SELECT 1 FROM edge e WHERE e.src_node = p.node AND e.src_port = p.name);

-- A port requires an invariant the port feeding it does not provide. (A required invariant implies the
-- requiring port exists; the feeding port may not, and then the edge is malformed, not a gap.)
CREATE VIEW gap_edge AS
  SELECT 'edge-assumption' AS kind,
         e.src_node || '.' || e.src_port || ' -> ' || e.dst_node || '.' || e.dst_port AS subject
  FROM edge e
  WHERE e.kind IS NOT NULL
    AND EXISTS (SELECT 1 FROM port p WHERE p.node = e.src_node AND p.name = e.src_port)
    AND EXISTS (SELECT 1 FROM port_inv d WHERE d.node = e.dst_node AND d.port = e.dst_port
                AND NOT EXISTS (SELECT 1 FROM port_inv s WHERE s.node = e.src_node AND s.port = e.src_port AND s.ref = d.ref));

-- An invariant nothing discharges: unjudged if it is assembled, undischarged if not.
CREATE VIEW gap_invariant AS
  SELECT CASE WHEN EXISTS (SELECT 1 FROM assembled a WHERE a.ref = i.ref) THEN 'assembly-unjudged' ELSE 'undischarged' END AS kind,
         i.ref AS subject
  FROM invariant i WHERE i.strength IS NULL;

-- A leaf carries, on an output port, an invariant it does not own, and records no discharge for it.
CREATE VIEW gap_claim AS
  SELECT DISTINCT 'claim-undischarged' AS kind, pi.node || '.' || pi.port AS subject
  FROM port_inv pi JOIN node n ON n.id = pi.node AND n.leaf = 1
  WHERE pi.direction = 'out' AND substr(pi.ref, 1, instr(pi.ref, '#') - 1) <> pi.node
    AND NOT EXISTS (SELECT 1 FROM preserve p WHERE p.node = pi.node AND p.ref = pi.ref AND p.strength IS NOT NULL);
"""

WEAK = """
SELECT 'weak', ref FROM invariant WHERE strength IS NOT NULL AND strength < :floor
UNION
SELECT 'weak', pi.node || '.' || pi.port
FROM port_inv pi JOIN node n ON n.id = pi.node AND n.leaf = 1
WHERE pi.direction = 'out' AND substr(pi.ref, 1, instr(pi.ref, '#') - 1) <> pi.node
  AND EXISTS (SELECT 1 FROM preserve p WHERE p.node = pi.node AND p.ref = pi.ref
              AND p.strength < :floor)
"""


def canonical(model: Model) -> str:
    return json.dumps(model.to_dict(), sort_keys=True, separators=(",", ":"))


def snapshot_key(model: Model) -> str:
    import hashlib
    return hashlib.sha256(canonical(model).encode()).hexdigest()[:16]


class Sidecar:
    """The model store of one git clone. Open with `Sidecar(root)`; use as a context manager or `close()` it."""

    def __init__(self, root: Path | str, directory: Path | None = None):
        self.dir = Path(directory) if directory else sidecar_dir(root)
        if not self.dir.is_absolute():
            raise SidecarError(f"{self.dir} is not absolute; a relative store would land in whatever directory this runs from")
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / DB_NAME
        self.db = sqlite3.connect(self.path, timeout=30)
        self.db.executescript(STORE)
        self._ro: sqlite3.Connection | None = None
        self._ensure_projection()

    def __enter__(self) -> "Sidecar":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        if self._ro is not None:
            self._ro.close()
        self.db.close()

    # ---- snapshots -----------------------------------------------------------------------------------

    def save(self, model: Model, label: str = "", draft: bool = False) -> str:
        """Record `model` as the new head and return its snapshot key. Refuses a model that does not validate
        unless `draft`. Saving what is already the head under the same label changes nothing."""
        if not draft and (bad := model.validate()):
            shown = "; ".join(f"{v.rule} {v.subject}" for v in bad[:3])
            raise SidecarError(f"the model does not validate ({len(bad)} violations: {shown}); save it as a draft or fix it")
        key = snapshot_key(model)
        head = self._head_row()
        if head and head[0] == key and head[1] == label:
            return key
        now = time.time()
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO snapshots VALUES (?, ?, ?)", (key, canonical(model), now))
            self.db.execute("INSERT INTO log(key, label, ts) VALUES (?, ?, ?)", (key, label, now))
            self._project(model)
        return key

    def _head_row(self) -> tuple[str, str] | None:
        return self.db.execute("SELECT key, label FROM log ORDER BY seq DESC LIMIT 1").fetchone()

    def head_key(self) -> str | None:
        row = self._head_row()
        return row[0] if row else None

    def head(self) -> Model | None:
        key = self.head_key()
        return self.load(key) if key else None

    def load(self, key: str) -> Model:
        row = self.db.execute("SELECT doc FROM snapshots WHERE key = ?", (key,)).fetchone()
        if row is None:
            raise SidecarError(f"no snapshot {key}")
        return Model.from_dict(json.loads(row[0]))

    def history(self) -> list[dict]:
        """Every save, oldest first."""
        return [{"seq": s, "key": k, "label": l, "ts": t}
                for s, k, l, t in self.db.execute("SELECT seq, key, label, ts FROM log ORDER BY seq")]

    # ---- projection ----------------------------------------------------------------------------------

    def _ensure_projection(self) -> None:
        if self.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION:
            return
        drops = "".join(f"DROP VIEW IF EXISTS {v};" for v in PROJECTION_VIEWS)
        drops += "".join(f"DROP TABLE IF EXISTS {t};" for t in PROJECTION_TABLES)
        self.db.executescript(drops + PROJECTION)
        head = self.head()
        if head is not None:
            with self.db:
                self._project(head)
        self.db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _project(self, m: Model) -> None:
        for t in PROJECTION_TABLES:
            self.db.execute(f"DELETE FROM {t}")
        leaves = set(m.leaves())
        for n in m.nodes:
            self.db.execute("INSERT INTO node VALUES (?, ?, ?, ?, ?, ?)",
                            (n.id, parent_of(n.id), n.id.count("/"), int(n.id in leaves), int(n.state), n.intent))
            self.db.executemany("INSERT INTO owns VALUES (?, ?)", [(n.id, o) for o in n.owns])
            for p in n.ports:
                self.db.execute("INSERT INTO port VALUES (?, ?, ?, ?, ?, ?)",
                                (n.id, p.name, p.direction, p.type, int(p.many), p.binds))
                self.db.executemany("INSERT INTO port_inv VALUES (?, ?, ?, ?)",
                                    [(n.id, p.name, p.direction, r) for r in p.invariants])
            for i in n.invariants:
                ref = f"{n.id}#{i.name}"
                d = i.discharge
                self.db.execute("INSERT INTO invariant VALUES (?, ?, ?, ?, ?, ?)",
                                (ref, n.id, i.name, i.statement, int(d.strength) if d else None, d.pointer if d else None))
                self.db.executemany("INSERT INTO assembled VALUES (?, ?)", [(ref, r) for r in i.assembled_from])
            for pr in n.preserves:
                d = pr.discharge
                self.db.execute("INSERT INTO preserve VALUES (?, ?, ?, ?)",
                                (n.id, pr.ref, int(d.strength) if d else None, d.pointer if d else None))
        for e in m.edges:
            wire = m.wire(e)
            self.db.execute("INSERT INTO edge VALUES (?, ?, ?, ?, ?, ?)",
                            (e.src[0], e.src[1], e.dst[0], e.dst[1], wire[0] if wire else None, wire[1] if wire else None))

    def projected(self) -> Model:
        """The model as the projection holds it. Equal to `head()` unless the projection lost something."""
        q = self.db.execute
        ports: dict[str, list[Port]] = {}
        for node, name, direction, type_, many, binds in q("SELECT node, name, direction, type, many, binds FROM port"):
            refs = tuple(r for (r,) in q("SELECT ref FROM port_inv WHERE node = ? AND port = ? AND direction = ?",
                                         (node, name, direction)))
            ports.setdefault(node, []).append(Port(name, direction, type_, refs, bool(many), binds))
        nodes = []
        for nid, state, intent in q("SELECT id, state, intent FROM node"):
            invs = []
            for ref, name, statement, strength, pointer in q(
                    "SELECT ref, name, statement, strength, pointer FROM invariant WHERE owner = ?", (nid,)):
                invs.append(Invariant(name, statement, Discharge(Strength(strength), pointer) if strength is not None else None,
                                      tuple(r for (r,) in q("SELECT from_ref FROM assembled WHERE ref = ?", (ref,)))))
            keeps = tuple(Preserve(ref, Discharge(Strength(s), ptr) if s is not None else None)
                          for ref, s, ptr in q("SELECT ref, strength, pointer FROM preserve WHERE node = ?", (nid,)))
            nodes.append(Node(nid, intent, tuple(ports.get(nid, ())), tuple(invs),
                              tuple(o for (o,) in q("SELECT entry FROM owns WHERE node = ?", (nid,))), bool(state), keeps))
        edges = [Edge((a, b), (c, d)) for a, b, c, d in q("SELECT src_node, src_port, dst_node, dst_port FROM edge")]
        return Model.of(nodes, edges)

    # ---- queries -------------------------------------------------------------------------------------

    def query(self, sql: str, params: tuple | dict = ()) -> list[tuple]:
        """Run a read-only query against the projection. The connection cannot write."""
        if self._ro is None:
            self._ro = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=30)
        return self._ro.execute(sql, params).fetchall()

    def gap_pairs(self, floor: Strength | None = None) -> set[tuple[str, str]]:
        """(kind, subject) of every gap in the head model, from the views: the relational twin of `Model.gaps`."""
        out = set()
        for view in ("gap_port", "gap_edge", "gap_invariant", "gap_claim"):
            out |= {(k, s) for k, s in self.query(f"SELECT kind, subject FROM {view}")}
        if floor is not None:
            out |= {(k, s) for k, s in self.query(WEAK, {"floor": int(floor)})}
        return out

    def feeds(self, node: str) -> list[str]:
        """Everything `node` feeds, directly or through other siblings."""
        return [b for (b,) in self.query("SELECT b FROM feeds WHERE a = ? ORDER BY b", (node,))]

    def fed_by(self, node: str) -> list[str]:
        return [a for (a,) in self.query("SELECT a FROM feeds WHERE b = ? ORDER BY a", (node,))]

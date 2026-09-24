"""SQLite persistence for edges, dependencies, and telemetry events."""

from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS edges (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    pre         TEXT NOT NULL,
    post        TEXT NOT NULL,
    reads       TEXT NOT NULL,   -- json list of repo-relative paths
    writes      TEXT NOT NULL,   -- json list of repo-relative paths/globs
    probe       TEXT,            -- json probe, or NULL for trusted-only claims
    status      TEXT NOT NULL,
    fingerprint TEXT,            -- fingerprint at the last verdict
    hashes      TEXT NOT NULL DEFAULT '{}',  -- json {path: sha} at the last verdict
    anchors     TEXT NOT NULL DEFAULT '[]',  -- json keyframe: lines the probe matched, with context
    repair      TEXT,                        -- json {kind, residuals} when a repair is needed
    delta_count INTEGER NOT NULL DEFAULT 0,  -- delta repairs since the last full verification
    level       INTEGER,                     -- zoom level: 1 coarse (~10K lines) .. 3 fine (~100 lines)
    region      TEXT NOT NULL DEFAULT '[]',  -- json globs of the code region this claim describes
    detail      TEXT NOT NULL DEFAULT '',
    parent_id   TEXT,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS deps (
    edge_id    TEXT NOT NULL,
    depends_on TEXT NOT NULL,
    PRIMARY KEY (edge_id, depends_on)
);
CREATE INDEX IF NOT EXISTS deps_by_target ON deps(depends_on);
-- Per-line hashes of source files at verification time, keyed by file content hash and
-- shared across claims. Used to measure how much of a file changed (scene-cut detection).
CREATE TABLE IF NOT EXISTS snapshots (
    sha   TEXT PRIMARY KEY,
    lines TEXT NOT NULL
);
-- Ranked keyword search over claim text and read paths (porter: complete ~ completion).
CREATE VIRTUAL TABLE IF NOT EXISTS edges_fts USING fts5(id UNINDEXED, body, tokenize='porter unicode61');
CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL NOT NULL,
    kind    TEXT NOT NULL,
    edge_id TEXT,
    data    TEXT NOT NULL
);
"""

# pending: planned, not yet generated. verified: probe passed at fingerprint.
# trusted: no probe (or depends on a trusted edge). stale: sources or upstream
# changed since the verdict. failed: probe did not pass.
STATUSES = ("pending", "verified", "trusted", "stale", "failed")
GOOD = ("verified", "trusted")

STOPWORDS = frozenset("""
a an and are as at be but by can do does for from how i if in into is it its me my of on or
so that the their them then there these this to use used uses using was what when where
which who why will with you your app code file files""".split())


def tokens(text: str) -> list[str]:
    """Search terms: alphanumeric runs (FTS5 unicode61 splits on the same boundaries)."""
    seen: dict[str, None] = {}
    for t in re.findall(r"[A-Za-z0-9]+", text.lower()):
        if t not in STOPWORDS and len(t) > 1:
            seen.setdefault(t)
    return list(seen)


def _body(e: "Edge") -> str:
    return " ".join([e.pre, e.post, *e.reads])


@dataclass
class Edge:
    id: str
    kind: str
    pre: str
    post: str
    reads: list[str]
    writes: list[str]
    probe: dict | None
    status: str
    fingerprint: str | None
    hashes: dict[str, str]
    detail: str
    parent_id: str | None
    created_at: float
    updated_at: float
    depends_on: list[str] = field(default_factory=list)
    anchors: list[dict] = field(default_factory=list)
    repair: dict | None = None
    delta_count: int = 0
    level: int | None = None
    region: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "pre": self.pre,
            "post": self.post,
            "reads": self.reads,
            "writes": self.writes,
            "probe": self.probe,
            "status": self.status,
            "detail": self.detail,
            "parent_id": self.parent_id,
            "depends_on": self.depends_on,
            "repair": self.repair,
            "delta_count": self.delta_count,
            "level": self.level,
            "region": self.region,
        }


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)  # callers serialize access
        self.db.row_factory = sqlite3.Row
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(edges)")}
        for col, ddl in (("anchors", "TEXT NOT NULL DEFAULT '[]'"), ("repair", "TEXT"),
                         ("delta_count", "INTEGER NOT NULL DEFAULT 0"), ("level", "INTEGER"),
                         ("region", "TEXT NOT NULL DEFAULT '[]'")):
            if cols and col not in cols:  # stores created before motion estimation
                self.db.execute(f"ALTER TABLE edges ADD COLUMN {col} {ddl}")
        fts_sql = self.db.execute("SELECT sql FROM sqlite_master WHERE name = 'edges_fts'").fetchone()
        if fts_sql and "porter" not in fts_sql[0]:  # index built before stemming: rebuild it
            self.db.execute("DROP TABLE edges_fts")
        self.db.executescript(SCHEMA)
        n_edges = self.db.execute("SELECT count(*) FROM edges").fetchone()[0]
        n_fts = self.db.execute("SELECT count(*) FROM edges_fts").fetchone()[0]
        if n_edges != n_fts:  # stores created before search indexing existed
            with self.db:
                self.db.execute("DELETE FROM edges_fts")
                for e in self.all():
                    self.db.execute("INSERT INTO edges_fts (id, body) VALUES (?, ?)", (e.id, _body(e)))

    def close(self) -> None:
        self.db.close()

    # --- edges -------------------------------------------------------------

    def _edge(self, row: sqlite3.Row) -> Edge:
        deps = [r[0] for r in self.db.execute(
            "SELECT depends_on FROM deps WHERE edge_id = ? ORDER BY depends_on", (row["id"],)
        )]
        return Edge(
            id=row["id"],
            kind=row["kind"],
            pre=row["pre"],
            post=row["post"],
            reads=json.loads(row["reads"]),
            writes=json.loads(row["writes"]),
            probe=json.loads(row["probe"]) if row["probe"] else None,
            status=row["status"],
            fingerprint=row["fingerprint"],
            hashes=json.loads(row["hashes"]),
            detail=row["detail"],
            parent_id=row["parent_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            depends_on=deps,
            anchors=json.loads(row["anchors"]),
            repair=json.loads(row["repair"]) if row["repair"] else None,
            delta_count=row["delta_count"],
            level=row["level"],
            region=json.loads(row["region"]),
        )

    def get(self, edge_id: str) -> Edge | None:
        row = self.db.execute("SELECT * FROM edges WHERE id = ?", (edge_id,)).fetchone()
        return self._edge(row) if row else None

    def all(self) -> list[Edge]:
        return [self._edge(r) for r in self.db.execute("SELECT * FROM edges ORDER BY created_at")]

    def upsert(self, e: Edge) -> None:
        now = time.time()
        with self.db:
            self.db.execute(
                """INSERT INTO edges (id, kind, pre, post, reads, writes, probe, status,
                                      fingerprint, hashes, detail, parent_id, created_at, updated_at,
                                      anchors, repair, delta_count, level, region)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     reads=excluded.reads, writes=excluded.writes, probe=excluded.probe,
                     status=excluded.status, fingerprint=excluded.fingerprint,
                     hashes=excluded.hashes,
                     detail=excluded.detail, parent_id=excluded.parent_id,
                     updated_at=excluded.updated_at, anchors=excluded.anchors,
                     repair=excluded.repair, delta_count=excluded.delta_count,
                     level=excluded.level, region=excluded.region""",
                (e.id, e.kind, e.pre, e.post, json.dumps(e.reads), json.dumps(e.writes),
                 json.dumps(e.probe) if e.probe else None, e.status, e.fingerprint,
                 json.dumps(e.hashes), e.detail, e.parent_id, now, now,
                 json.dumps(e.anchors), json.dumps(e.repair) if e.repair else None, e.delta_count,
                 e.level, json.dumps(e.region)),
            )
            self.db.execute("DELETE FROM edges_fts WHERE id = ?", (e.id,))
            self.db.execute("INSERT INTO edges_fts (id, body) VALUES (?, ?)", (e.id, _body(e)))
            self.db.execute("DELETE FROM deps WHERE edge_id = ?", (e.id,))
            self.db.executemany(
                "INSERT INTO deps (edge_id, depends_on) VALUES (?, ?)",
                [(e.id, d) for d in e.depends_on],
            )

    def set_status(self, edge_id: str, status: str, detail: str,
                   fingerprint: str | None = None, hashes: dict[str, str] | None = None) -> None:
        with self.db:
            if fingerprint is None:
                self.db.execute(
                    "UPDATE edges SET status=?, detail=?, updated_at=? WHERE id=?",
                    (status, detail, time.time(), edge_id),
                )
            else:
                self.db.execute(
                    "UPDATE edges SET status=?, detail=?, fingerprint=?, hashes=?, updated_at=? WHERE id=?",
                    (status, detail, fingerprint, json.dumps(hashes or {}), time.time(), edge_id),
                )

    def set_motion(self, edge_id: str, anchors: list[dict] | None, repair: dict | None) -> None:
        """Store new anchors (None = keep) and the pending repair (None = clear)."""
        with self.db:
            if anchors is not None:
                self.db.execute("UPDATE edges SET anchors=? WHERE id=?", (json.dumps(anchors), edge_id))
            self.db.execute("UPDATE edges SET repair=? WHERE id=?",
                            (json.dumps(repair) if repair else None, edge_id))

    def put_snapshot(self, sha: str, line_hashes: list[str]) -> None:
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO snapshots (sha, lines) VALUES (?, ?)",
                            (sha, json.dumps(line_hashes)))

    def snapshot(self, sha: str) -> list[str] | None:
        row = self.db.execute("SELECT lines FROM snapshots WHERE sha = ?", (sha,)).fetchone()
        return json.loads(row[0]) if row else None

    def delete(self, edge_id: str) -> None:
        with self.db:
            self.db.execute("DELETE FROM edges WHERE id = ?", (edge_id,))
            self.db.execute("DELETE FROM edges_fts WHERE id = ?", (edge_id,))
            self.db.execute("DELETE FROM deps WHERE edge_id = ? OR depends_on = ?", (edge_id, edge_id))

    def dependents(self, edge_id: str) -> list[str]:
        return [r[0] for r in self.db.execute(
            "SELECT edge_id FROM deps WHERE depends_on = ?", (edge_id,)
        )]

    def ranked(self, text: str, min_matches: int = 1) -> list[tuple[str, float, int]]:
        """(id, bm25 score, distinct matched terms) for claims matching any term, best first.

        Scores are FTS5 bm25: negative, and more negative is more relevant.
        """
        terms = tokens(text)
        if not terms:
            return []
        # Distinct terms matched per claim, counted by FTS itself so stemming applies.
        matched: dict[str, int] = {}
        for t in terms:
            for (eid,) in self.db.execute("SELECT id FROM edges_fts WHERE edges_fts MATCH ?", (f'"{t}"',)):
                matched[eid] = matched.get(eid, 0) + 1
        query = " OR ".join(f'"{t}"' for t in terms)
        return [(row["id"], row["score"], matched.get(row["id"], 0)) for row in self.db.execute(
            "SELECT id, bm25(edges_fts) AS score FROM edges_fts WHERE edges_fts MATCH ? ORDER BY score",
            (query,),
        ) if matched.get(row["id"], 0) >= min_matches]

    def search(self, text: str = "", paths: list[str] | None = None,
               statuses: list[str] | None = None, limit: int = 50,
               min_matches: int = 1) -> list[Edge]:
        """Ranked: any query term may match (BM25 over claim text and read paths).

        Claims matching both the text and a path rank first, then text-only by BM25,
        then path-only. `min_matches` drops text hits sharing fewer distinct terms.
        """
        by_path: set[str] = set()
        if paths:
            by_path = {e.id for e in self.all() if any(_overlaps(p, e.reads) for p in paths)}
        if tokens(text):
            ranked = [i for i, _, _ in self.ranked(text, min_matches)]
            ids = [i for i in ranked if i in by_path] + [i for i in ranked if i not in by_path] \
                + sorted(by_path - set(ranked))
        elif paths:
            ids = sorted(by_path)
        elif text.strip():
            ids = []  # only stopwords: nothing specific to match
        else:
            ids = [r[0] for r in self.db.execute("SELECT id FROM edges ORDER BY updated_at DESC")]
        edges = [e for e in (self.get(i) for i in ids) if e is not None]
        if statuses:
            edges = [e for e in edges if e.status in statuses]
        return edges[:limit]

    # --- telemetry -----------------------------------------------------------

    def log(self, kind: str, edge_id: str | None = None, **data: object) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO events (ts, kind, edge_id, data) VALUES (?, ?, ?, ?)",
                (time.time(), kind, edge_id, json.dumps(data)),
            )

    def events(self, kind: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM events", []
        if kind:
            sql, args = sql + " WHERE kind = ?", [kind]
        return [
            {"ts": r["ts"], "kind": r["kind"], "edge_id": r["edge_id"], **json.loads(r["data"])}
            for r in self.db.execute(sql + " ORDER BY id", args)
        ]


def _overlaps(path: str, reads: list[str]) -> bool:
    """A query path matches an edge if either is a prefix of the other (dirs or files).

    A glob read is compared by its literal directory prefix (`app/lib/**/*.dart` -> `app/lib`).
    """
    path = path.rstrip("/")
    for r in reads:
        if any(c in r for c in "*?["):
            r = "/".join(part for part in r.split("/")[: next(
                i for i, part in enumerate(r.split("/")) if any(c in part for c in "*?["))])
            if not r or path == r or path.startswith(r + "/") or r.startswith(path + "/"):
                return True
        elif r == path or r.startswith(path + "/") or path.startswith(r + "/"):
            return True
    return False

"""The declared node graph as relations, and the graph-check rules as SQL over them.

A spike: does `graph_check`'s hand-written loops read better as rules? The graph is loaded into an
in-memory SQLite database as facts (node edges, exports, mentions, test files), and `dead`, `missing` and
`phantom` are views over them, the same questions `graph_check.check` asks. `check` here returns the same
three keys, and the tests diff the two. What SQL adds is recursion: `reaches` is the transitive closure of
the `parents:` edges, so a layer (a node's depth above the foundations) and a dependency cycle are one query
each, where no check looks at either today.

Two kinds of answer, and they are not equally trustworthy:

  * sound over the declared edges: `reaches`, `cycles`, `layers`. They follow `parents:`/`after:` exactly as
    written; if the headers are right the answer is right.
  * heuristic, inherited: `dead`, `missing`, `phantom`. The facts they read (`mention`) are the word-match
    index of refindex.py, so they are as unsound as `graph_check` is, and no more. The rules add no error of
    their own; the diff tests are what show that.

Extraction stays in Python (header parsing, the word-match index, `.fractal-public` globs, a Python module's
use of its own helper). Those become facts; the rules never see source text.
"""

from __future__ import annotations

import itertools
import sqlite3
from pathlib import Path

from .cache import ClaimCache
from .graph_check import ENTRY_NAMES, MIN_NAME, _is_public, _public_patterns, _refs
from .nodes import _symbol_keys, parse_header
from .refindex import Index, is_test, load_trees
from .symbols import at

SCHEMA = """
CREATE TABLE file(path TEXT, test INT);                 -- source files of this tree
CREATE TABLE mention(name TEXT, file TEXT);             -- word match: `name` is a token of `file`
CREATE TABLE node(name TEXT);
CREATE TABLE uses(seq INT, child TEXT, parent TEXT);    -- `parents:`: child relies on parent's contract
CREATE TABLE after(child TEXT, parent TEXT);            -- `after:`: sequencing only
CREATE TABLE export(node TEXT, path TEXT, name TEXT);   -- top-level symbol of an export ref
CREATE TABLE leaf(node TEXT, name TEXT);                -- last segment of an export ref (`f.rs::T::m` -> m)
CREATE TABLE export_file(node TEXT, file TEXT);         -- file part of an export ref
CREATE TABLE planned(node TEXT, path TEXT, name TEXT);  -- exported ahead of its code, on purpose
CREATE TABLE mutant_file(node TEXT, file TEXT);         -- files the node's mutants were recorded in
CREATE TABLE public_symbol(node TEXT, path TEXT, name TEXT);  -- marked public API (header or .fractal-public)
CREATE TABLE self_use(node TEXT, path TEXT, name TEXT);       -- a Python def its own module also calls

-- Exports the rules look at: short names and entry points are never reported.
CREATE VIEW tracked AS
  SELECT * FROM export WHERE length(name) >= {min_name} AND name NOT IN ({entries});

CREATE VIEW live AS
  SELECT t.* FROM tracked t WHERE EXISTS (SELECT 1 FROM mention m WHERE m.name = t.name AND m.file = t.path);

-- A stale spec: the code no longer defines the symbol (unless the spec is written ahead of it).
CREATE VIEW missing AS
  SELECT t.* FROM tracked t
  WHERE NOT EXISTS (SELECT 1 FROM live l WHERE l.node = t.node AND l.path = t.path AND l.name = t.name)
    AND NOT EXISTS (SELECT 1 FROM planned p WHERE p.node = t.node AND p.path = t.path AND p.name = t.name);

-- Every file that uses an export from outside its definition, plus the defining file when it uses it itself.
CREATE VIEW ref AS
  SELECT l.node, l.path, l.name, m.file FROM live l JOIN mention m ON m.name = l.name AND m.file <> l.path
  UNION
  SELECT l.node, l.path, l.name, l.path FROM live l JOIN self_use s
    ON s.node = l.node AND s.path = l.path AND s.name = l.name;

CREATE VIEW dead AS
  SELECT l.node, l.path, l.name,
         EXISTS (SELECT 1 FROM ref r WHERE r.node = l.node AND r.path = l.path AND r.name = l.name) AS tested_only
  FROM live l
  WHERE NOT EXISTS (SELECT 1 FROM ref r JOIN file f ON f.path = r.file
                    WHERE r.node = l.node AND r.path = l.path AND r.name = l.name AND NOT f.test)
    AND NOT EXISTS (SELECT 1 FROM public_symbol p WHERE p.node = l.node AND p.path = l.path AND p.name = l.name);

-- Where a node's own code can be: its exported files, its mutants' files, and the tests naming its exports.
CREATE VIEW scope AS
  SELECT node, file FROM export_file
  UNION SELECT node, file FROM mutant_file
  UNION SELECT e.node, m.file FROM export e
        JOIN mention m ON m.name = e.name JOIN file f ON f.path = m.file AND f.test
        WHERE length(e.name) >= {min_name};

CREATE VIEW parent_name AS
  SELECT u.seq, u.child, u.parent, l.name FROM uses u JOIN leaf l ON l.node = u.parent
  WHERE length(l.name) >= {min_name};

-- A parent edge the child never touches: none of the parent's exported names is mentioned in its scope.
CREATE VIEW phantom AS
  SELECT DISTINCT u.seq, u.child, u.parent FROM uses u
  WHERE EXISTS (SELECT 1 FROM parent_name p WHERE p.seq = u.seq)
    AND NOT EXISTS (SELECT 1 FROM parent_name p JOIN scope s ON s.node = p.child
                    JOIN mention m ON m.name = p.name AND m.file = s.file WHERE p.seq = u.seq);

-- Sound over the declared edges: the closure of `uses`, and of `uses` plus `after` for ordering.
CREATE VIEW reaches AS
  WITH RECURSIVE r(a, b) AS (
    SELECT child, parent FROM uses
    UNION SELECT r.a, u.parent FROM r JOIN uses u ON u.child = r.b)
  SELECT a, b FROM r;

CREATE VIEW before AS
  WITH RECURSIVE r(a, b) AS (
    SELECT child, parent FROM uses UNION SELECT child, parent FROM after
    UNION SELECT r.a, e.parent FROM r JOIN (SELECT child, parent FROM uses UNION SELECT child, parent FROM after) e
      ON e.child = r.b)
  SELECT a, b FROM r;

CREATE VIEW cyclic AS SELECT DISTINCT a AS node FROM before WHERE a = b;

-- A layer is the longest chain of `parents:` below a node: 0 for a foundation. Nodes in or above a cycle have none.
CREATE VIEW layer AS
  WITH RECURSIVE ground(node) AS (
    SELECT name FROM node UNION SELECT parent FROM uses),
  depth(node, d) AS (
    SELECT node, 0 FROM ground WHERE node NOT IN (SELECT child FROM uses)
    UNION SELECT u.child, depth.d + 1 FROM depth JOIN uses u ON u.parent = depth.node
    WHERE depth.d <= (SELECT count(*) FROM ground))
  SELECT node, max(d) AS layer FROM depth
  WHERE node NOT IN (SELECT a FROM before WHERE b IN (SELECT node FROM cyclic))     -- a cyclic node reaches itself
  GROUP BY node;
"""


def facts(cache: ClaimCache, root: Path) -> sqlite3.Connection:
    """The declared graph and the word-match index of this checkout, as relations."""
    db = sqlite3.connect(":memory:")
    db.executescript(SCHEMA.format(min_name=MIN_NAME, entries=", ".join(repr(e) for e in sorted(ENTRY_NAMES))))
    nodes = cache.store.all_nodes()
    here = next((t for t in load_trees(root) if t.current), None)
    texts = here.texts if here else {}
    idx = here.index if here else Index({})
    headers = {n: parse_header(at(root, i["spec"]).read_text(errors="replace"))
               for n, i in nodes.items() if at(root, i["spec"]).is_file()}
    public = _public_patterns(root, headers)
    db.executemany("INSERT INTO file VALUES (?, ?)", [(p, is_test(p)) for p in texts])
    db.executemany("INSERT INTO node VALUES (?)", [(n,) for n in nodes])
    names: set[str] = set()
    seq = itertools.count()
    for n, h in headers.items():
        if h is None:
            continue
        db.executemany("INSERT INTO uses VALUES (?, ?, ?)", [(next(seq), n, p) for p in h.parents])
        db.executemany("INSERT INTO after VALUES (?, ?)", [(n, p) for p in h.after])
        db.executemany("INSERT INTO export_file VALUES (?, ?)", [(n, r.partition("::")[0]) for r in h.exports])
        db.executemany("INSERT INTO mutant_file VALUES (?, ?)", [(n, m["file"]) for m in cache.store.mutants(n)])
        db.executemany("INSERT INTO leaf VALUES (?, ?)", [(n, r.rpartition("::")[2]) for r in h.exports])
        for table, refs in (("export", h.exports), ("planned", h.planned)):
            for key in sorted(_symbol_keys(refs)):
                path, name = key.split("::", 1)
                db.execute(f"INSERT INTO {table} VALUES (?, ?, ?)", (n, path, name))
        for key in sorted(_symbol_keys(h.exports)):
            path, name = key.split("::", 1)
            if _is_public(key, public):
                db.execute("INSERT INTO public_symbol VALUES (?, ?, ?)", (n, path, name))
            if path in _refs(idx, texts, path, name):
                db.execute("INSERT INTO self_use VALUES (?, ?, ?)", (n, path, name))
        names |= {r.rpartition("::")[2] for r in h.exports} | {k.split("::", 1)[1] for k in _symbol_keys(h.exports)}
    for name in sorted(names):
        db.executemany("INSERT INTO mention VALUES (?, ?)", [(name, f) for f in idx.files(name)])
    return db


def check(cache: ClaimCache, root: Path, only: str | None = None) -> dict:
    """`graph_check.check`'s `dead`, `phantom` and `missing`, from the rules."""
    db = facts(cache, root)
    where, args = ("AND node = ?", [only]) if only else ("", [])
    key = "path || '::' || name"
    dead = [{"node": n, "symbol": s, "tested_only": bool(t)} for n, s, t in db.execute(
        f"SELECT node, {key}, tested_only FROM dead WHERE 1 {where} ORDER BY node, {key}", args)]
    missing = [{"node": n, "symbol": s} for n, s in db.execute(
        f"SELECT node, {key} FROM missing WHERE 1 {where} ORDER BY node, {key}", args)]
    w2, a2 = ("AND child = ?", [only]) if only else ("", [])
    phantom = [{"node": c, "parent": p, "looked_in": sorted(
        f for (f,) in db.execute("SELECT s.file FROM scope s JOIN file f ON f.path = s.file WHERE s.node = ?", (c,)))}
        for _, c, p in db.execute(f"SELECT seq, child, parent FROM phantom WHERE 1 {w2} ORDER BY child, seq", a2)]
    return {"dead": dead, "phantom": phantom, "missing": missing}


def layers(cache: ClaimCache, root: Path) -> dict:
    """The layering the `parents:` edges declare: `layer` (node -> depth above the foundations), `cycles`
    (nodes that, following `parents:` and `after:`, wait on themselves) and `reaches` (node -> everything
    it relies on, transitively)."""
    db = facts(cache, root)
    reach: dict[str, list[str]] = {}
    for a, b in db.execute("SELECT a, b FROM reaches ORDER BY a, b"):
        reach.setdefault(a, []).append(b)
    return {"layer": dict(db.execute("SELECT node, layer FROM layer ORDER BY layer, node")),
            "cycles": [n for (n,) in db.execute("SELECT node FROM cyclic ORDER BY node")],
            "reaches": reach}

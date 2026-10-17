"""`fractal graph-check`: the declared node graph against what the code actually references.

The header lint catches *undeclared* edges (code uses a module no parent exports). This reports the other
two cases, both warnings, never a gate:

  * dead exports: a node exports a top-level symbol that no non-test file outside its defining file
    mentions (`tested only` when a test does). The node may have many declared children and the symbol
    still be an orphan in the code.
  * phantom edges: a `parents:` (uses) edge none of whose exports the child mentions. The child's code is
    its exported files, its recorded mutant files, and the test files that name its exports.
    `after:` edges are sequencing, not use, so they are never reported.

The graph is only as complete as the specs, so it also reports what no spec covers:

  * public items in the code that no registered node exports and nothing else references (the "scan every
    public item" check: an orphan needs no declaration to be an orphan);
  * spec files with a header that were never registered with `fractal ledger node`;
  * exports a spec lists that the code no longer defines.

Intended public surface that nothing in the repo uses (a library's API, test helpers) is marked, not
reported: a node's `public:` header field, or a repo-level `.fractal-public` file, one entry per line
(`#` comments). An entry is `file::Name` (fnmatch: `src/*.rs::*`), a path or glob (`src/testkit/**`),
or a bare `Name`. With those, `fractal graph-check --strict` can be a commit probe.

References are word matches in `.rs`/`.py` files, so a shared generic name clears a report that a
type-aware tool would make: expect missed warnings before wrong ones.
"""

from __future__ import annotations

import re
from pathlib import Path

from .cache import ClaimCache
from .nodes import _symbol_keys, parse_header
from .refindex import Index, is_test, load_trees

MIN_NAME = 3
ENTRY_NAMES = {"main", "run", "cli"}
RUST_PUB = re.compile(r"^pub(?:\([^)]*\))?\s+(?:async\s+|unsafe\s+|const\s+|extern\s+\"[^\"]*\"\s+)*"
                      r"(?:struct|enum|trait|fn|type|const|static|union)\s+(\w+)", re.M)
PY_DEF = re.compile(r"^(?:async\s+)?(?:def|class)\s+([A-Za-z]\w*)", re.M)


def public_items(path: str, text: str) -> list[str]:
    """Top-level public names defined in a source file (Rust `pub` items before any `#[cfg(test)]`; Python
    non-underscore defs and classes)."""
    if path.endswith(".rs"):
        return RUST_PUB.findall(text.split("#[cfg(test)]")[0])
    return [n for n in PY_DEF.findall(text) if not n.startswith("test")]


PUBLIC_FILE = ".fractal-public"


def _public_patterns(root: Path, headers: dict) -> list[str]:
    pats = [x for h in headers.values() if h for x in h.public]
    f = root / PUBLIC_FILE
    if f.is_file():
        pats += [l.split("#")[0].strip() for l in f.read_text(errors="replace").splitlines() if l.split("#")[0].strip()]
    return pats


def _is_public(key: str, patterns: list[str]) -> bool:
    import fnmatch
    from .regions import matches
    path, name = key.split("::", 1)
    for pat in patterns:
        if "::" in pat:
            ok = fnmatch.fnmatchcase(key, pat)
        elif any(c in pat for c in "/.*"):
            ok = matches(pat, path)
        else:
            ok = pat == name
        if ok:
            return True
    return False


def _refs(idx: Index, texts: dict[str, str], path: str, name: str) -> list[str]:
    """Files other than the definition that use `name`. In Python a def has no `pub`: a module-level helper
    the module itself calls is internal, not an orphan, so a use inside the defining file counts there. In
    Rust `pub` is a promise of API, and a `pub` item only its own file uses is the smell this looks for."""
    files = idx.files(name, path)
    if path.endswith(".py"):
        text = texts.get(path, "")
        defs = len(re.findall(rf"^[ \t]*(?:async\s+)?(?:def|class)\s+{re.escape(name)}\b", text, re.M))
        if len(re.findall(rf"(?<!\w){re.escape(name)}(?!\w)", text)) > defs:
            files = sorted({*files, path})
    return files


def check(cache: ClaimCache, root: Path, only: str | None = None) -> dict:
    import subprocess
    nodes = cache.store.all_nodes()
    here = next((t for t in load_trees(root) if t.current), None)
    texts = here.texts if here else {}
    idx = here.index if here else Index({})
    headers = {}
    for n, info in nodes.items():
        if (s := root / info["spec"]).is_file():
            headers[n] = parse_header(s.read_text(errors="replace"))
    public = _public_patterns(root, headers)
    dead, phantom, missing = [], [], []
    for n, h in sorted(headers.items()):
        if h is None or (only and n != only):
            continue
        for key in sorted(_symbol_keys(h.exports)):
            path, name = key.split("::", 1)
            if len(name) < MIN_NAME or name in ENTRY_NAMES:
                continue
            if path not in texts or not idx.mentions(name, path):
                missing.append({"node": n, "symbol": key})
                continue
            others = _refs(idx, texts, path, name)
            if not any(not is_test(f) for f in others) and not _is_public(key, public):
                dead.append({"node": n, "symbol": key, "tested_only": bool(others)})
        for p in h.parents:
            ph = headers.get(p)
            names = {r.rpartition("::")[2] for r in (ph.exports if ph else [])}
            names = {x for x in names if len(x) >= MIN_NAME}
            if not names:
                continue
            mine = {r.partition("::")[0] for r in h.exports}
            mine |= {r["file"] for r in cache.store.mutants(n)}
            own_names = {k.split("::", 1)[1] for k in _symbol_keys(h.exports)}
            mine |= {f for f in texts if is_test(f) and any(idx.mentions(x, f) for x in own_names if len(x) >= MIN_NAME)}
            if not any(idx.mentions(x, f) for x in names for f in mine):
                phantom.append({"node": n, "parent": p, "looked_in": sorted(mine & set(texts))})
    result = {"dead": dead, "phantom": phantom, "missing": missing, "undeclared_dead": [], "unregistered": []}
    if only:
        return result
    declared = {k for h in headers.values() if h for k in _symbol_keys(h.exports)}
    for path, text in sorted(texts.items()):
        if is_test(path):
            continue
        for name in dict.fromkeys(public_items(path, text)):
            if (len(name) < MIN_NAME or name in ENTRY_NAMES or f"{path}::{name}" in declared
                    or _is_public(f"{path}::{name}", public)):
                continue
            others = _refs(idx, texts, path, name)
            if not any(not is_test(f) for f in others):
                result["undeclared_dead"].append({"symbol": f"{path}::{name}", "tested_only": bool(others)})
    registered = {i["spec"] for i in nodes.values()}
    try:
        tracked = subprocess.run(["git", "ls-files", "*.md"], cwd=root, capture_output=True, text=True).stdout.split()
    except FileNotFoundError:
        tracked = []
    for f in tracked:
        if f not in registered and (root / f).is_file():
            try:
                h = parse_header((root / f).read_text(errors="replace"))
            except Exception:
                continue
            if h is not None and (h.exports or h.guarantee or h.parents):
                result["unregistered"].append({"spec": f, "exports": h.exports})
    return result


def render(r: dict) -> str:
    if not any(r.get(k) for k in ("dead", "phantom", "missing", "undeclared_dead", "unregistered")):
        return "declared graph and code references agree\n"
    out = []
    if r.get("unregistered"):
        out += ["## Specs with a header that are not registered (the graph cannot see their exports)"]
        out += [f"- {d['spec']}: `fractal ledger node <name> --spec {d['spec']}`" for d in r["unregistered"]]
        out += [""]
    if r.get("missing"):
        out += ["## Exports the code no longer defines (a stale spec)"]
        out += [f"- {d['node']}: {d['symbol']}" for d in r["missing"]]
        out += ["", "Remove it from `exports:`, or restore the symbol.", ""]
    if r["dead"]:
        out += ["## Dead exports (nothing outside the defining file, tests aside, mentions them)"]
        out += [f"- {d['node']}: {d['symbol']}" + (" (tested only)" if d["tested_only"] else "") for d in r["dead"]]
        out += ["", "Remove it, or use it; a node can have children and still export an orphan.", ""]
    if r.get("undeclared_dead"):
        out += ["## Public items no spec exports and nothing references"]
        out += [f"- {d['symbol']}" + (" (tested only)" if d["tested_only"] else "") for d in r["undeclared_dead"]]
        out += ["", "Delete it, or declare it under a node's `exports:` if it is meant to be API.", ""]
    if r["phantom"]:
        out += ["## Phantom edges (a declared parent none of whose exports the child mentions)"]
        out += [f"- {d['node']} -> {d['parent']} (looked in {', '.join(d['looked_in']) or 'no files'})"
                for d in r["phantom"]]
        out += ["", "Drop the parent, or move it to `after:` if it is only sequencing.", ""]
    return "\n".join(out)

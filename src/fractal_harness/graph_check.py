"""`fractal graph-check`: the declared node graph against what the code actually references.

The header lint catches *undeclared* edges (code uses a module no parent exports). This reports the other
two cases, both warnings, never a gate:

  * dead exports: a node exports a top-level symbol that no non-test file outside its defining file
    mentions (`tested only` when a test does). The node may have many declared children and the symbol
    still be an orphan in the code.
  * phantom edges: a `parents:` (uses) edge none of whose exports the child mentions. The child's code is
    its exported files, its recorded mutant files, and the test files that name its exports.
    `after:` edges are sequencing, not use, so they are never reported.

References are word matches in `.rs`/`.py` files, so a shared generic name clears a report that a
type-aware tool would make: expect missed warnings before wrong ones.
"""

from __future__ import annotations

import re
from pathlib import Path

from .cache import ClaimCache
from .nodes import _files, _symbol_keys, parse_header

MIN_NAME = 3
TEST_PATH = re.compile(r"(^|/)(tests?|test_[^/]*|[^/]*_tests?\.\w+)(/|$)")
ENTRY_NAMES = {"main", "run", "cli"}


def _word(name: str, text: str) -> bool:
    return bool(re.search(rf"(?<![\w]){re.escape(name)}(?![\w])", text))


def _is_test(path: str) -> bool:
    return bool(TEST_PATH.search(path))


def check(cache: ClaimCache, root: Path, only: str | None = None) -> dict:
    nodes = cache.store.all_nodes()
    texts = {f: (root / f).read_text(errors="replace") for f in _files(root) if (root / f).is_file()}
    headers = {}
    for n, info in nodes.items():
        if (s := root / info["spec"]).is_file():
            headers[n] = parse_header(s.read_text(errors="replace"))
    dead, phantom = [], []
    for n, h in sorted(headers.items()):
        if h is None or (only and n != only):
            continue
        for key in sorted(_symbol_keys(h.exports)):
            path, name = key.split("::", 1)
            if len(name) < MIN_NAME or name in ENTRY_NAMES or path not in texts:
                continue
            others = {f: t for f, t in texts.items() if f != path and _word(name, t)}
            if not any(not _is_test(f) for f in others):
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
            mine |= {f for f, t in texts.items() if _is_test(f) and any(_word(x, t) for x in own_names if len(x) >= MIN_NAME)}
            code = "\n".join(texts[f] for f in mine if f in texts)
            if not any(_word(x, code) for x in names):
                phantom.append({"node": n, "parent": p, "looked_in": sorted(mine & set(texts))})
    return {"dead": dead, "phantom": phantom}


def render(r: dict) -> str:
    if not r["dead"] and not r["phantom"]:
        return "declared graph and code references agree\n"
    out = []
    if r["dead"]:
        out += ["## Dead exports (nothing outside the defining file, tests aside, mentions them)"]
        out += [f"- {d['node']}: {d['symbol']}" + (" (tested only)" if d["tested_only"] else "") for d in r["dead"]]
        out += ["", "Remove it, or use it; a node can have children and still export an orphan.", ""]
    if r["phantom"]:
        out += ["## Phantom edges (a declared parent none of whose exports the child mentions)"]
        out += [f"- {d['node']} -> {d['parent']} (looked in {', '.join(d['looked_in']) or 'no files'})"
                for d in r["phantom"]]
        out += ["", "Drop the parent, or move it to `after:` if it is only sequencing.", ""]
    return "\n".join(out)

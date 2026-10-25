"""Task maps: the agent's framework for a task, read off the behavioral graph.

Instead of gating after the fact, the graph shapes how the agent cuts and does the work.
For a task, `task_map` finds its footprint on the graph (deterministically, in milliseconds):

  regions        where the work is: owner regions of the files the task names, the files that
                 define identifiers it mentions, and the files behind matching facts
  contracts      per region, the tests that directly exercise its files: the definition of done
  relies on      upstream regions the involved files import, with their contracts: assumptions
  must not break downstream contracts that transitively depend on the involved files
  rules          enforced invariants governing the involved files
  weak spots     involved regions with no direct contract (nothing would catch a mistake there)

The agent (or its subagents, one per region) works region by region: change contracts
first, keep every other contract holding, done = its region's contracts pass.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from .behavior import dart_imports, dart_package, detect_runner, python_imports
from . import interfaces
from .cache import ClaimCache, dependencies
from .interfaces import DEF_RXS
from .regions import affects, build, governs, names_file, owner, placement, tracked_files
from .store import Edge, tokens

IDENT = re.compile(r"`?\b([A-Z][A-Za-z0-9]*[a-z][A-Za-z0-9]*|[a-z]+(?:[A-Z][a-z0-9]*)+|[a-z0-9]+_[a-z0-9_]+)\b`?")
# A relative path the task writes; an absolute (`/tmp/x/y.out`) or home (`~/x/y.md`) path is outside the repo, so a
# match may not start mid-path.
NAMED_PATH = re.compile(r"(?<![\w./~-])[\w.-]+(?:/[\w.-]+)+\.\w+")
MAX_DEF_FILES = 6            # an identifier defined in more files than this is too generic to route by
MAX_REGIONS = 5
MAX_CONTRACTS_PER_REGION = 4
MAX_CHARS = 3500
OMITTED_ROOM = len("\n(+9999 more lines omitted)")
# What the neighbours' signatures may add to a map, all regions together; 0 turns them off (for A/B arms).
MAX_IFACE_CHARS = int(os.environ.get("FRACTAL_IFACE_CHARS") or 1200)


@dataclass
class Region:
    path: str
    files: list[str] = field(default_factory=list)
    why: list[str] = field(default_factory=list)
    contracts: list[Edge] = field(default_factory=list)       # tests directly exercising these files
    relies_on: dict[str, list[Edge]] = field(default_factory=dict)   # upstream file -> its contracts
    interfaces: dict[str, list[str]] = field(default_factory=dict)   # upstream file -> signatures of what this region uses
    rules: list[Edge] = field(default_factory=list)
    facts: list[Edge] = field(default_factory=list)


@dataclass
class TaskMap:
    regions: list[Region]
    downstream: list[Edge]         # contracts that transitively depend on the involved files
    weak: list[str]                # involved regions with no direct contract

    @property
    def empty(self) -> bool:
        return not self.regions


def _language(root: Path) -> str | None:
    runner = detect_runner(root)
    return None if runner is None else ("dart" if runner in ("flutter", "dart") else "python")


def _direct(root: Path, rel: str, files: set[str], language: str | None, package: str | None) -> set[str]:
    if language == "dart":
        return dart_imports(root, rel, package, files)
    if language == "python":
        return python_imports(root, rel, files)
    return set()


def definition_index(root: Path, source: list[str]) -> dict[str, list[str]]:
    """name -> files that define it (one regex pass per file)."""
    index: dict[str, list[str]] = {}
    for f in source:
        try:
            text = (root / f).read_text(errors="replace")
        except OSError:
            continue
        for rx in DEF_RXS:
            for name in set(rx.findall(text)):
                index.setdefault(name, []).append(f)
    return index


def _read(root: Path, rel: str) -> str:
    try:
        return (root / rel).read_text(errors="replace")
    except OSError:
        return ""


def _test_count(e: Edge) -> str:
    m = re.match(r"(\d+) passed", e.detail or "")
    return f"{m.group(1)} tests" if m else e.status


def task_map(root: Path, prompt: str, capacity: int = 1000) -> TaskMap:
    root = root.resolve()
    files = tracked_files(root)
    fileset = set(files)
    language = _language(root)
    package = dart_package(root) if language == "dart" else None
    source = [f for f in files if f.endswith((".dart", ".py", ".ts", ".tsx", ".js")) and not f.startswith("test")
              and "/test" not in f]

    # -- seeds: files the task is about -------------------------------------------------------
    seeds: dict[str, list[str]] = {}
    for f in files:
        if names_file(prompt, f):
            seeds.setdefault(f, []).append("named in the task")
    for m in NAMED_PATH.findall(prompt):
        if m not in fileset and ".." not in m.split("/"):   # a new file the task asks for: seed its directory's neighbours
            seeds.setdefault(m, []).append("new file named in the task")
    index = definition_index(root, source)
    idents = list(dict.fromkeys(m for m in IDENT.findall(prompt) if len(m) >= 5))
    # plain words ("quad", "retry") may name functions too; the index only holds definitions
    # ...but only when the task writes them as code (`quad`, quad()), or English words would route
    plain = [w for w in dict.fromkeys(re.findall(r"`([a-z][a-z0-9]{3,})`|\b([a-z][a-z0-9]{3,})\(", prompt))
             for w in w if w and w not in idents]
    for name in idents + plain:
        defs = index.get(name, [])
        if 0 < len(defs) <= MAX_DEF_FILES:
            for f in defs:   # a name the task writes as code is a deliberate reference
                seeds.setdefault(f, []).append(f"defines `{name}`" if name in plain else f"defines {name}")

    cache = ClaimCache(root, run_slow=False)
    try:
        claims = cache.store.all()
        behavior = [e for e in claims if e.kind == "behavior"]
        rules = [e for e in claims if e.enforced]
        facts = []
        if len(tokens(prompt)) >= 2:
            scored = cache.store.ranked(prompt, 2)
            if scored:
                best = scored[0][1]
                facts = [cache.get(i) for i, s, _ in scored if s <= best * 0.5][:6]
                facts = [e for e in facts if e and e.status == "verified" and e.kind not in ("invariant", "behavior")]
        for e in facts:
            for r in e.reads:
                if r in fileset and not r.startswith("test"):
                    seeds.setdefault(r, []).append("matches a verified fact")
    finally:
        cache.close()
    if not seeds:
        return TaskMap([], [], [])

    # -- regions, contracts, assumptions ------------------------------------------------------------
    tree = build(root, files)
    direct_tests: dict[str, set[str]] = {}     # test file -> app files it imports directly
    for e in behavior:
        t = e.probe["file"]
        direct_tests[t] = _direct(root, t, fileset, language, package)
    by_test = {e.probe["file"]: e for e in behavior}

    regions: dict[str, Region] = {}
    for f, why in seeds.items():
        path = owner(tree, f, capacity) if f in fileset else str(PurePosixPath(f).parent)
        reg = regions.setdefault(path, Region(path))
        reg.files.append(f)
        reg.why.extend(w for w in why if w not in reg.why)
    def score(r: Region) -> int:
        """Evidence strength: named in the task 3, defines a type the task names 2, anything else 1."""
        total = 0
        for w in r.why:
            if w.startswith(("named in the task", "new file named")):
                total += 3
            elif w.startswith("defines `") or (w.startswith("defines ") and w[8:9].isupper()):
                total += 2
            else:
                total += 1
        return total

    ranked = sorted((r for r in regions.values() if score(r) >= 2), key=lambda r: -score(r))[:MAX_REGIONS]

    involved: set[str] = set()
    for reg in ranked:
        in_region = [f for f in files if governs(reg.path, f)] or reg.files
        involved.update(f for f in reg.files if f in fileset)
        reg.contracts = [by_test[t] for t, imps in direct_tests.items()
                         if imps & set(in_region) or any(governs(reg.path, i) for i in imps)]
        reg.contracts.sort(key=lambda e: e.probe["file"])
        for f in reg.files:
            if f not in fileset:
                continue
            for up in sorted(_direct(root, f, fileset, language, package)):
                if governs(reg.path, up):
                    continue
                reg.relies_on[up] = [by_test[t] for t, imps in direct_tests.items() if up in imps][:2]
        if MAX_IFACE_CHARS > 0:
            used = interfaces.usage(_read(root, f) for f in reg.files if f in fileset)
            for up in reg.relies_on:
                if sigs := interfaces.interface_lines(_read(root, up), used):
                    reg.interfaces[up] = sigs
        reg.facts = [e for e in facts if any(governs(reg.path, r) for r in e.reads)]

    rule_place = {e.id: placement(dependencies(e), files) for e in rules}
    for reg in ranked:
        # a rule governs the region if its probe scans one of the involved files, or it is
        # placed at an ancestor of them (jurisdiction)
        reg.rules = [e for e in rules if any(affects(dependencies(e), f) or governs(rule_place[e.id], f)
                                             for f in reg.files)]
    local = {e.id for reg in ranked for e in reg.contracts}
    downstream = [e for e in behavior if e.id not in local and any(f in e.reads for f in involved)]
    weak = [reg.path for reg in ranked if not reg.contracts]
    return TaskMap(ranked, downstream, weak)


HEADER = ("TASK MAP: where this task sits in the repo's behavioral graph (tests = contracts, "
          "verified against the current code).")
HOW_TO = (
    "How to work from this map: (1) if the task changes behavior, change or add the contracts (tests) of the "
    "affected region first; (2) work region by region, relying only on the listed upstream contracts; for "
    "several independent regions, a subagent per region is fine; (3) a region is done when its contracts pass; "
    "(4) the downstream contracts must keep passing (`fractal behavior` runs exactly the affected ones). Enforced "
    "rules are human-owned: never work around them."
)
# Said only when signatures are shown: text that is not needed is tokens that are not saved.
IFACE_NOTE = ("Signatures under 'relies on' are read from the current source (a word match: it can miss a use). "
              "Open an upstream file only for what its signature does not show.")


def render(m: TaskMap, budget: int = MAX_CHARS, primed: bool = False) -> str:
    """The map as text. The static lines come first and task-specific lines follow. Over budget, whole lines
    are dropped from the end (neighbour signatures first, then facts, then the rest), so trimming never cuts
    the static start or a line in half. (Where a prompt hook's text lands in a request, a static start is not
    a cache prefix: see measure.py.) `primed`: the session already holds the how-to (see hook.session_hook), so
    only the one-line header is repeated."""
    if m.empty:
        return ""
    body: list[tuple[str, str]] = []           # (line, kind): kind decides what is dropped first
    iface_chars = 0
    for i, reg in enumerate(m.regions, 1):
        body.append((f"{i}. {reg.path or '(repo root)'}: {'; '.join(reg.why[:3])}", "core"))
        if reg.contracts:
            shown = ", ".join(f"{e.probe['file']} ({_test_count(e)})" for e in reg.contracts[:MAX_CONTRACTS_PER_REGION])
            more = len(reg.contracts) - MAX_CONTRACTS_PER_REGION
            body.append((f"   contracts (definition of done): {shown}" + (f" +{more} more" if more > 0 else ""), "core"))
        else:
            body.append(("   contracts: NONE directly cover this region (weak spot: add a test for what you change)", "core"))
        shown_ups = list(reg.relies_on.items())[:4]
        if shown_ups:
            ups = [f"{up} [{', '.join(e.probe['file'] for e in es) or 'no direct contract'}]" for up, es in shown_ups]
            body.append((f"   relies on: {'; '.join(ups)}", "core"))
            for up, _ in shown_ups:
                for sig in reg.interfaces.get(up, ()):
                    line = f"      {up}: {sig}"
                    if iface_chars + len(line) > MAX_IFACE_CHARS:
                        break
                    iface_chars += len(line)
                    body.append((line, "iface"))
        for e in reg.rules[:3]:
            body.append((f"   RULE (enforced): {e.post}", "core"))
        for e in reg.facts[:2]:
            body.append((f"   fact: {e.post}", "fact"))
    if m.downstream:
        files = sorted({e.probe["file"] for e in m.downstream})
        body.append((f"Must not break: {len(files)} downstream contracts depend on these files "
                     f"(e.g. {', '.join(files[:4])}).", "core"))

    def head() -> list[str]:
        if primed:
            return [HEADER]
        return [HEADER, HOW_TO] + ([IFACE_NOTE] if any(k == "iface" for _, k in body) else [])

    def size() -> int:
        return len("\n".join(head() + [line for line, _ in body]))

    for kind in ("iface", "fact"):
        while size() > budget and any(k == kind for _, k in body):
            body.pop(max(i for i, (_, k) in enumerate(body) if k == kind))
    dropped = 0
    if size() > budget:                         # still over: drop from the end, leaving room for the marker
        while body and size() + OMITTED_ROOM > budget:
            body.pop()
            dropped += 1
        body.append((f"(+{dropped} more lines omitted)", "core"))
    return "\n".join(head() + [line for line, _ in body])

"""Executable probes: deterministic checks that re-verify a claim without an LLM.

Probe shapes:
  {"type": "command", "run": "pytest -q tests/test_x.py", "expect_exit": 0, "timeout": 120}
  {"type": "grep", "pattern": "auth_middleware", "paths": ["src/**/*.py"],
   "exclude": "^\\s*(#|//)",  # optional: skip lines matching this (e.g. comments)
   "exclude_paths": ["src/legacy/*.py"],  # optional: files the rule does not cover
   "expect": "present" | "absent" | {"count": 3} | {"min": 1}}

  {"type": "only_in", "pattern": "fn rank_key", "paths": ["src/**/*.rs"], "files": ["src/rank.rs"],
   "concept": "ranking expression"}   # single ownership: the pattern appears in `files` and nowhere else
  {"type": "only_in", "kind": "struct", "name": "Nearest\\w*", "paths": ["src/**/*.rs"], "files": ["src/nearest.rs"]}
      # the same claim for a symbol kind (struct|enum|trait|class|fn|def): every definition whose
      # name matches `name` is in `files`. `decision` (an ID or a list) names the decisions the concept owns:
      # the ownership lint on nodes' exports applies to those IDs only (IDs in `concept` count too). Optional `exclude`/`exclude_paths` as for grep. Passes when
      # there is at least one match in `files` (`"min": 0` allows none) and none elsewhere.

  {"type": "all", "probes": [<probe>, ...]}  # passes only if every sub-probe passes
  {"type": "test", "runner": "flutter" | "dart" | "pytest", "file": "test/x_test.dart"}
      # a behavioral probe: the test file passes. Slow (seconds): never run in the
      # millisecond hooks; batched per runner at turn end and at commit (see behavior.py)

  {"type": "staged_paths", "forbid": "proptest-regressions/", "only": ["src/**", "tests/**"]}
      # a gate probe over the paths staged for commit: fails if any staged path matches the
      # `forbid` regex, or matches none of the `only` globs (either key is optional, one is required)
  {"type": "commit_message", "forbid": "\\b[ABD][0-9]+\\b", "require": "^Node: "}
      # a gate probe over the message being committed (comment lines stripped): fails if the
      # `forbid` regex matches or the `require` regex does not (multiline; either key optional)

  {"type": "section", "ref": "docs/DECISIONS.md::D16", "sha": "<digest>"}
  {"type": "symbol", "ref": "src/profile.rs::Profile::len", "sha": "<digest>"}
      # the extracted markdown section / code interface still hashes to `sha` (see symbols.py);
      # written by `fractal put --section/--symbol`, so a claim carrying that text goes stale
      # (and fails) when the text changes

Gate probes depend on the commit in flight, not on files, so they are never memoized: the cache
records them as holding vacuously, and the commit gates (`fractal check`, `fractal check
--message FILE`) evaluate them with the real staged paths / message (`ctx`).

Evidence class (see `evidence_class`): what a passing probe establishes, shown beside every rule and fact.
A probe's class follows from its type; `"behavioral": true` marks a rule about what the code does,
which `evidence_gap` then holds to a test-class mechanism; only a human, writing the probe, can declare a grep exact with
`"evidence": "exact"` (a pattern that decides the property, not one that approximates it).

grep is line-based: it counts matching lines, so `^`/`$` anchor to each line.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ProbeResult:
    passed: bool
    detail: str
    matches: list[tuple[str, int]] = field(default_factory=list)  # (repo-relative file, 0-based line)


class ProbeError(ValueError):
    pass


RUNNERS = {"flutter", "dart", "pytest"}
KINDS = {"struct", "enum", "trait", "class", "fn", "def", "type"}


def only_in_pattern(probe: dict) -> str:
    """The regex an `only_in` probe looks for: its `pattern`, or the definitions of `kind` named `name`."""
    if "pattern" in probe:
        return probe["pattern"]
    name = probe.get("name", r"\w+").removeprefix("^").removesuffix("$")    # `name` is the whole identifier already
    return (rf"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+|unsafe\s+|const\s+)*{probe['kind']}\s+"
            rf"(?:{name})\b")


def is_slow(probe: dict | None) -> bool:
    """Probes that take seconds (tests, commands): kept out of the millisecond hooks."""
    if not probe:
        return False
    if probe.get("type") == "all":
        return any(is_slow(p) for p in probe["probes"])
    return probe.get("type") in ("test", "command")


GATE_TYPES = {"staged_paths", "commit_message"}

EVIDENCE = ("exact", "proxy", "test", "doc-pinned")     # strongest first: an `all` is as weak as its weakest member
EVIDENCE_MEANING = {
    "exact": "the probe decides the property",
    "proxy": "a pattern approximates the property",
    "test": "a test passes; only as strong as the test",
    "doc-pinned": "the text is unchanged; the code is not checked",
}


def evidence_class(probe: dict | None) -> str | None:
    """What a passing probe establishes, or None for a claim with no probe (no mechanism at all).

    exact: only_in, the commit gates, or a grep its author declared `"evidence": "exact"`.
    proxy: any other grep, and a command probe declared `"evidence": "proxy"`.
    test: a test or command probe. doc-pinned: section/symbol, which only notice the text changing.
    A declared value is honored only where a deterministic check can back it (grep, command)."""
    if not probe:
        return None
    kind = probe.get("type")
    if kind == "all":
        return max((evidence_class(p) or "doc-pinned" for p in probe["probes"]), key=EVIDENCE.index)
    if kind in GATE_TYPES or kind == "only_in":
        return "exact"
    if kind in ("section", "symbol"):
        return "doc-pinned"
    if kind == "grep":
        return "exact" if probe.get("evidence") == "exact" else "proxy"
    return "proxy" if probe.get("evidence") == "proxy" else "test"


def gate_leaves(probe: dict | None) -> list[dict]:
    """The gate probes inside `probe` (itself, or the members of an `all`)."""
    if not probe:
        return []
    if probe.get("type") == "all":
        return [g for sub in probe["probes"] for g in gate_leaves(sub)]
    return [probe] if probe.get("type") in GATE_TYPES else []


def evidence_gap(probe: dict | None) -> str | None:
    """Why a rule's mechanism does not establish it, or None. A rule with no probe or a doc-pinned one checks
    nothing about the code; a rule its author marked `"behavioral": true` (it constrains what the code does,
    not how it is written) needs more than a pattern."""
    cls = evidence_class(probe)
    if cls is None:
        return "no probe: nothing checks the code"
    if cls == "doc-pinned":
        return "doc-pinned: only the rule's text is checked, not the code"
    if cls == "proxy" and probe.get("behavioral"):
        return "behavioral rule checked only by a pattern; it needs a test"
    return None


def validate(probe: dict) -> None:
    kind = probe.get("type")
    if not isinstance(probe.get("behavioral", False), bool):
        raise ProbeError('"behavioral" must be true or false')
    declared = probe.get("evidence")
    if declared is not None and not (declared == "exact" and kind == "grep" or declared == "proxy" and kind == "command"):
        raise ProbeError('"evidence" may only be "exact" on a grep probe or "proxy" on a command probe')
    if kind == "command":
        if not isinstance(probe.get("run"), str) or not probe["run"].strip():
            raise ProbeError("command probe needs a non-empty 'run' string")
    elif kind == "grep":
        if not isinstance(probe.get("pattern"), str):
            raise ProbeError("grep probe needs a 'pattern' string")
        try:
            re.compile(probe["pattern"])
        except re.error as e:
            raise ProbeError(f"invalid regex: {e}") from e
        if "exclude" in probe:
            try:
                re.compile(probe["exclude"])
            except (re.error, TypeError) as e:
                raise ProbeError(f"invalid exclude regex: {e}") from e
        if not probe.get("paths"):
            raise ProbeError("grep probe needs 'paths' (list of globs)")
        if not isinstance(probe.get("exclude_paths", []), list):
            raise ProbeError("'exclude_paths' must be a list of globs")
        _expectation(probe.get("expect", "present"))
    elif kind == "only_in":
        if ("pattern" in probe) == ("kind" in probe):
            raise ProbeError("only_in probe needs exactly one of 'pattern' or 'kind' (+ optional 'name')")
        if "kind" in probe and probe["kind"] not in KINDS:
            raise ProbeError(f"only_in kind must be one of {sorted(KINDS)}")
        try:
            re.compile(only_in_pattern(probe))
            if "exclude" in probe:
                re.compile(probe["exclude"])
        except (re.error, TypeError) as e:
            raise ProbeError(f"invalid regex: {e}") from e
        for key in ("paths", "files"):
            if not isinstance(probe.get(key), list) or not probe[key] or not all(isinstance(g, str) for g in probe[key]):
                raise ProbeError(f"only_in probe needs '{key}' (a non-empty list of globs/paths)")
        if not isinstance(probe.get("exclude_paths", []), list) or not isinstance(probe.get("min", 1), int):
            raise ProbeError("'exclude_paths' must be a list of globs and 'min' an integer")
        d = probe.get("decision", [])
        if not isinstance(d, (str, list)) or not all(isinstance(x, str) for x in ([d] if isinstance(d, str) else d)):
            raise ProbeError("'decision' must be a decision ID or a list of them")
    elif kind in GATE_TYPES:
        keys = ("forbid", "only") if kind == "staged_paths" else ("forbid", "require")
        if not any(k in probe for k in keys):
            raise ProbeError(f"{kind} probe needs one of {', '.join(repr(k) for k in keys)}")
        for k in (k for k in keys if k in probe):
            if k == "only":
                if not isinstance(probe[k], list) or not all(isinstance(g, str) for g in probe[k]):
                    raise ProbeError("'only' must be a list of globs")
            else:
                try:
                    re.compile(probe[k])
                except (re.error, TypeError) as e:
                    raise ProbeError(f"invalid {k} regex: {e}") from e
    elif kind in ("section", "symbol"):
        from .symbols import SymbolError, split_ref
        try:
            split_ref(probe.get("ref") or "")
        except SymbolError as e:
            raise ProbeError(str(e)) from e
        if not isinstance(probe.get("sha"), str):
            raise ProbeError(f"{kind} probe needs a 'sha' string")
    elif kind == "test":
        if probe.get("runner") not in RUNNERS:
            raise ProbeError(f"test probe runner must be one of {sorted(RUNNERS)}")
        if not isinstance(probe.get("file"), str) or not probe["file"]:
            raise ProbeError("test probe needs a 'file'")
    elif kind == "all":
        subs = probe.get("probes")
        if not isinstance(subs, list) or not subs:
            raise ProbeError("all probe needs a non-empty 'probes' list")
        for sub in subs:
            validate(sub)
    else:
        raise ProbeError(f"unknown probe type: {kind!r}")


def run(probe: dict, root: Path, overlay: dict[str, str] | None = None,
        ctx: dict | None = None) -> ProbeResult:
    """Run a probe. `overlay` maps repo-relative paths to substitute contents (grep only),
    so audits can test probes against mutated code without touching the working tree. `ctx` is
    the commit in flight for gate probes: {"staged": [paths], "message": text}; a gate probe
    whose input is absent from `ctx` holds vacuously."""
    validate(probe)
    if probe["type"] in GATE_TYPES:
        return _run_gate(probe, ctx or {})
    if probe["type"] in ("section", "symbol"):
        return _run_extract(probe, root)
    if probe["type"] == "command":
        return _run_command(probe, root)
    if probe["type"] == "test":
        from .behavior import run_tests
        result = run_tests(root, probe["runner"], [probe["file"]]).get(probe["file"])
        return result or ProbeResult(False, "test runner produced no result")
    if probe["type"] == "all":
        results = [run(sub, root, overlay, ctx) for sub in probe["probes"]]
        matches = [m for r in results for m in r.matches]
        failed = [f"#{i}: {r.detail}" for i, r in enumerate(results) if not r.passed]
        if failed:
            return ProbeResult(False, "; ".join(failed), matches)
        return ProbeResult(True, f"all {len(results)} probes passed", matches)
    if probe["type"] == "only_in":
        return _run_only_in(probe, root, overlay)
    return _run_grep(probe, root, overlay)


def _run_command(probe: dict, root: Path) -> ProbeResult:
    expect = probe.get("expect_exit", 0)
    try:
        proc = subprocess.run(
            probe["run"],
            shell=True,
            cwd=root,
            capture_output=True,
            text=True,
            timeout=probe.get("timeout", 120),
        )
    except subprocess.TimeoutExpired:
        return ProbeResult(False, "timed out")
    tail = (proc.stdout + proc.stderr).strip()[-500:]
    return ProbeResult(proc.returncode == expect, f"exit {proc.returncode} (expected {expect}): {tail}")


def _expectation(expect: object) -> tuple[str, int]:
    if expect == "present":
        return "min", 1
    if expect == "absent":
        return "count", 0
    if isinstance(expect, dict) and len(expect) == 1:
        (op, n), = expect.items()
        if op in ("count", "min", "max") and isinstance(n, int):
            return op, n
    raise ProbeError(f"invalid grep expectation: {expect!r}")


def _scan(pattern: str, probe: dict, root: Path, overlay: dict[str, str] | None) -> tuple[int, list[tuple[str, int]]]:
    """(files scanned, every (file, 0-based line) matching `pattern`) over the probe's paths."""
    rx = re.compile(pattern)
    exclude = re.compile(probe["exclude"]) if probe.get("exclude") else None
    files: set[Path] = set()
    for glob in probe["paths"]:
        files.update(p for p in root.glob(glob) if p.is_file())
    from .regions import matches
    if overlay:  # in-memory files (e.g. a Write creating a new file) count if a glob covers them
        files.update(root / rel for rel in overlay if any(matches(g, rel) for g in probe["paths"]))
    if probe.get("exclude_paths"):
        files = {f for f in files if not any(matches(g, f.relative_to(root).as_posix())
                                             for g in probe["exclude_paths"])}
    hits: list[tuple[str, int]] = []
    for path in sorted(files):
        rel = path.relative_to(root).as_posix()
        try:
            text = overlay[rel] if overlay and rel in overlay else path.read_text(errors="replace")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines()):
            if rx.search(line) and not (exclude and exclude.search(line)):
                hits.append((rel, i))
    return len(files), hits


def _run_grep(probe: dict, root: Path, overlay: dict[str, str] | None = None) -> ProbeResult:
    op, n = _expectation(probe.get("expect", "present"))
    nfiles, hits = _scan(probe["pattern"], probe, root, overlay)
    count = len(hits)
    ok = {"count": count == n, "min": count >= n, "max": count <= n}[op]
    # An "absent" probe has no positive matches to anchor on; its anchors come from violations.
    return ProbeResult(ok, f"{count} matching lines in {nfiles} files (expected {op} {n})", hits[:20])


def _run_only_in(probe: dict, root: Path, overlay: dict[str, str] | None = None) -> ProbeResult:
    from .regions import matches
    nfiles, hits = _scan(only_in_pattern(probe), probe, root, overlay)
    inside = [h for h in hits if any(matches(g, h[0]) for g in probe["files"])]
    outside = [h for h in hits if h not in inside]
    what = probe.get("concept") or probe.get("pattern") or f"{probe['kind']} {probe.get('name', '*')}"
    if outside:
        where = ", ".join(sorted({f"{f}:{i + 1}" for f, i in outside})[:5])
        return ProbeResult(False, f"{what!r} must appear only in {', '.join(probe['files'])} but also appears at "
                                  f"{where}", outside[:20])
    if len(inside) < probe.get("min", 1):
        return ProbeResult(False, f"{what!r} no longer appears in {', '.join(probe['files'])} "
                                  f"({len(inside)} of at least {probe.get('min', 1)}): moved or renamed?")
    return ProbeResult(True, f"{what!r}: {len(inside)} match(es), all in {', '.join(probe['files'])}", inside[:20])


def _run_gate(probe: dict, ctx: dict) -> ProbeResult:
    if probe["type"] == "staged_paths":
        if ctx.get("staged") is None:
            return ProbeResult(True, "gate probe: checked at commit (no staged paths in scope)")
        from .regions import matches
        bad = []
        for rel in ctx["staged"]:
            if probe.get("forbid") and re.search(probe["forbid"], rel):
                bad.append(f"{rel} (forbidden: /{probe['forbid']}/)")
            elif "only" in probe and not any(matches(g, rel) for g in probe["only"]):
                bad.append(f"{rel} (outside {probe['only']})")
        if bad:
            return ProbeResult(False, "staged: " + "; ".join(bad[:10]))
        return ProbeResult(True, f"{len(ctx['staged'])} staged paths allowed")
    if ctx.get("message") is None:
        return ProbeResult(True, "gate probe: checked at commit (no message in scope)")
    text = "\n".join(l for l in ctx["message"].splitlines() if not l.startswith("#"))
    if probe.get("forbid") and (m := re.search(probe["forbid"], text, re.M)):
        return ProbeResult(False, f"commit message contains {m.group(0)!r} (forbidden: /{probe['forbid']}/)")
    if probe.get("require") and not re.search(probe["require"], text, re.M):
        return ProbeResult(False, f"commit message lacks /{probe['require']}/")
    return ProbeResult(True, "commit message allowed")


def _run_extract(probe: dict, root: Path) -> ProbeResult:
    from .symbols import SymbolError, digest, section_of, symbol_of
    try:
        text = (section_of if probe["type"] == "section" else symbol_of)(root, probe["ref"])
    except SymbolError as e:
        return ProbeResult(False, str(e))
    if digest(text) != probe["sha"]:
        return ProbeResult(False, f"{probe['type']} {probe['ref']} changed since the claim was written")
    return ProbeResult(True, f"{probe['type']} {probe['ref']} unchanged")

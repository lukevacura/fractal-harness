"""Executable probes: deterministic checks that re-verify a claim without an LLM.

Probe shapes:
  {"type": "command", "run": "pytest -q tests/test_x.py", "expect_exit": 0, "timeout": 120}
  {"type": "grep", "pattern": "auth_middleware", "paths": ["src/**/*.py", "!src/legacy.py"],  # `!` excludes
   "exclude": "^\\s*(#|//)",  # optional: skip lines matching this (e.g. comments)
   "exclude_paths": ["src/legacy/*.py"],  # optional: files the rule does not cover
   "skip_tests": true,  # optional: test code is not counted (test files, Rust #[cfg(test)] items; see test_lines)
   "expect": "present" | "absent" | {"count": 3} | {"min": 1}}

  {"type": "only_in", "pattern": "fn rank_key", "paths": ["src/**/*.rs"], "files": ["src/rank.rs"],
   "concept": "ranking expression"}   # single ownership: the pattern appears in `files` and nowhere else
  {"type": "only_in", "kind": "struct", "name": "Nearest\\w*", "paths": ["src/**/*.rs"], "files": ["src/nearest.rs"]}
      # the same claim for a symbol kind (struct|enum|trait|class|fn|def): every definition whose
      # name matches `name` is in `files`. `decision` (an ID or a list) names the decisions the concept owns:
      # the ownership lint on nodes' exports applies to those IDs only (IDs in `concept` count too). Optional `exclude`/`exclude_paths`/`skip_tests` as for grep. Passes when
      # there is at least one match in `files` (`"min": 0` allows none) and none elsewhere.

  {"type": "paths", "paths": ["**/proptest-regressions/**"], "expect": "absent"}
  {"type": "paths", "paths": ["db/migrations/*"], "unless_name": "^[0-9]{3}_[a-z0-9_]+\\.sql$", "expect": "absent"}
  {"type": "paths", "paths": ["db/migrations/*.sql"], "unique": "^[0-9]{3}"}
      # counts FILES, not lines: those matching `paths` (globs, `!` excludes, `exclude_paths`), as a grep selects
      # them: the repo's files (tracked, or untracked and not ignored; files a pending edit creates count). Optional:
      # `unless_name` counts only files whose name does NOT fully match the regex (a naming convention);
      # `unique` counts files whose name's match of the regex is shared with another file (duplicates). Both count
      # violations and default `expect` to "absent"; otherwise `expect` is as for grep (default "present").

  {"type": "all", "probes": [<probe>, ...]}  # passes only if every sub-probe passes
  {"type": "test", "runner": "flutter" | "dart" | "pytest", "file": "test/x_test.dart"}
      # a behavioral probe: the test file passes. Slow (seconds): never run in the
      # millisecond hooks; batched per runner at turn end and at commit (see behavior.py)

  {"type": "staged_paths", "forbid": "proptest-regressions/", "only": ["src/**", "tests/**"],
   "frozen": ["db/migrations/*.sql"]}
      # a gate probe over the paths staged for commit: fails if any staged path matches the
      # `forbid` regex, or matches none of the `only` globs, or is an existing file under a `frozen` glob that is
      # modified, deleted or renamed (adding new files there is allowed: append-only). One key is required.
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
    vacuous: bool = False    # passed only because it scanned nothing: absence of scope, not evidence of absence


VACUOUS = "vacuous: "


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

    exact: only_in, paths, the commit gates, or a grep its author declared `"evidence": "exact"`.
    proxy: any other grep, and a command probe declared `"evidence": "proxy"`.
    test: a test or command probe. doc-pinned: section/symbol, which only notice the text changing.
    A declared value is honored only where a deterministic check can back it (grep, command)."""
    if not probe:
        return None
    kind = probe.get("type")
    if kind == "all":
        return max((evidence_class(p) or "doc-pinned" for p in probe["probes"]), key=EVIDENCE.index)
    if kind in GATE_TYPES or kind in ("only_in", "paths"):
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


_COMMON_KEYS = {"type", "behavioral", "evidence", "debt_target", "concept"}
KEYS = {
    "grep": {"pattern", "paths", "expect", "exclude", "exclude_paths", "skip_tests"},
    "only_in": {"pattern", "kind", "name", "paths", "files", "exclude", "exclude_paths", "min", "decision", "skip_tests"},
    "paths": {"paths", "expect", "unless_name", "unique"},
    "command": {"run", "expect_exit", "timeout"},
    "test": {"runner", "file"},
    "section": {"ref", "sha"},
    "symbol": {"ref", "sha"},
    "staged_paths": {"forbid", "only", "frozen"},
    "commit_message": {"forbid", "require"},
    "all": {"probes"},
}


def _check_keys(probe: dict, kind) -> None:
    """A key the probe does not read is an error: a misspelled `expect` would leave the default in force, and the
    rule would pass while checking something other than what its author wrote."""
    if kind not in KEYS:
        return
    allowed = KEYS[kind] | _COMMON_KEYS
    if unknown := sorted(set(probe) - allowed):
        import difflib
        hint = ""
        if near := difflib.get_close_matches(unknown[0], sorted(allowed), n=1):
            hint = f" (did you mean {near[0]!r}?)"
        elif unknown[0] in ("absent", "present"):
            hint = f' (did you mean "expect": "{unknown[0]}"?)'
        raise ProbeError(f"{kind} probe does not read {', '.join(repr(k) for k in unknown)}{hint}; "
                         f"its keys are {', '.join(sorted(KEYS[kind]))}")


def validate(probe: dict) -> None:
    kind = probe.get("type")
    _check_keys(probe, kind)
    if kind in ("grep", "only_in", "paths") and isinstance(probe.get("paths"), list):
        bad = [g for g in probe["paths"] if isinstance(g, str) and not g.startswith("!")
               and (g.startswith("/") or g.endswith("/"))]
        if bad:      # the walk matches nothing for them: the rule would check nothing, silently
            raise ProbeError(f"paths are repo-relative globs without a leading or trailing '/': {bad[0]!r} "
                             f"(for a directory, use 'dir/**')")
    if not isinstance(probe.get("behavioral", False), bool):
        raise ProbeError('"behavioral" must be true or false')
    if not isinstance(probe.get("skip_tests", False), bool):
        raise ProbeError('"skip_tests" must be true or false')
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
        if not probe.get("paths") or all(g.startswith("!") for g in probe["paths"]):
            raise ProbeError("grep probe needs 'paths' (list of globs, at least one not starting with `!`)")
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
    elif kind == "paths":
        if not isinstance(probe.get("paths"), list) or not probe["paths"] or not all(isinstance(g, str) for g in probe["paths"]):
            raise ProbeError("paths probe needs a non-empty 'paths' list of globs")
        for k in ("unless_name", "unique"):
            if k in probe:
                try:
                    re.compile(probe[k])
                except (re.error, TypeError) as e:
                    raise ProbeError(f"invalid {k} regex: {e}") from e
        if "unless_name" in probe and "unique" in probe:
            raise ProbeError("a paths probe takes 'unless_name' or 'unique', not both (use an `all` probe)")
        _expectation(paths_expect(probe))
    elif kind in GATE_TYPES:
        keys = ("forbid", "only", "frozen") if kind == "staged_paths" else ("forbid", "require")
        if not any(k in probe for k in keys):
            raise ProbeError(f"{kind} probe needs one of {', '.join(repr(k) for k in keys)}")
        for k in (k for k in keys if k in probe):
            if k in ("only", "frozen"):
                if not isinstance(probe[k], list) or not all(isinstance(g, str) for g in probe[k]):
                    raise ProbeError(f"'{k}' must be a list of globs")
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
    """Run a probe. `overlay` maps repo-relative paths to substitute contents (grep, only_in, paths, section,
    symbol), so audits and reclamation can test probes against changed code without touching the working tree. `ctx` is
    the commit in flight for gate probes: {"staged": [paths], "message": text}; a gate probe
    whose input is absent from `ctx` holds vacuously."""
    validate(probe)
    if probe["type"] in GATE_TYPES:
        return _run_gate(probe, ctx or {})
    if probe["type"] in ("section", "symbol"):
        return _run_extract(probe, root, overlay)
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
        empty = [f"#{i}: {r.detail}" for i, r in enumerate(results) if r.vacuous]
        if empty:
            return ProbeResult(True, VACUOUS + "; ".join(empty), matches, vacuous=True)
        return ProbeResult(True, f"all {len(results)} probes passed", matches)
    if probe["type"] == "only_in":
        return _run_only_in(probe, root, overlay)
    if probe["type"] == "paths":
        return _run_paths(probe, root, overlay)
    return _run_grep(probe, root, overlay)


_PATHLIKE = re.compile(r"[\w.-]+(?:/[\w.-]+)+|[\w-]+\.[A-Za-z]\w*")


def _absent_here(run: str, root: Path) -> list[str]:
    """Paths the command names that this checkout lacks but the main checkout has: ignored files, which a git
    worktree does not get. The command cannot run here, which says nothing about the rule."""
    import shlex
    try:
        words = shlex.split(run, comments=True)
    except ValueError:
        words = run.split()
    missing = [w for w in words if _PATHLIKE.fullmatch(w) and not (root / w).exists()]
    if not missing:
        return []
    from .cache import store_root
    main = store_root(root)
    return [w for w in missing if (main / w).exists()]


def _run_command(probe: dict, root: Path) -> ProbeResult:
    expect = probe.get("expect_exit", 0)
    if absent := _absent_here(probe["run"], root):
        return ProbeResult(True, f"{VACUOUS}not applicable in this worktree: {', '.join(absent)} "
                           f"{'is' if len(absent) == 1 else 'are'} only in the main checkout (ignored files are not checked out here); the main checkout checks it",
                           vacuous=True)
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


def _selected(probe: dict, root: Path, overlay: dict[str, str] | None) -> set[Path]:
    """The files a grep/only_in/paths probe covers: its `paths` globs, minus `!` paths and `exclude_paths`, plus
    in-memory files (e.g. a Write creating a new file) a glob covers."""
    from .hashing import expand_glob, visible
    from .regions import matches
    files: set[Path] = set()
    include = [g for g in probe["paths"] if not g.startswith("!")]
    skip = [*probe.get("exclude_paths", []), *(g[1:] for g in probe["paths"] if g.startswith("!"))]
    for glob in include:
        files.update(root / f for f in expand_glob(root, glob))
    if overlay:
        files.update(root / rel for rel in overlay if any(visible(root, g, rel) for g in include))
    if skip:
        files = {f for f in files if not any(matches(g, f.relative_to(root).as_posix()) for g in skip)}
    return files


def count(probe: dict, root: Path) -> int:
    """What a grep (matching lines) or paths (files) probe counts on the current tree."""
    if probe.get("type") == "grep":
        return len(_scan(probe["pattern"], probe, root, None)[1])
    if probe.get("type") == "paths":
        return len(_paths_counted(probe, root, None)[1])
    raise ProbeError(f"a {probe.get('type')!r} probe has no count")


def _paths_counted(probe: dict, root: Path, overlay: dict[str, str] | None) -> tuple[list[str], list[str], str]:
    """(the files the probe selects, the ones it counts, what they are). Files git ignores do not count (a Finder
    `.DS_Store`, a local build artifact): a paths rule is about the files that are, or could be, in the repo."""
    files = sorted(f.relative_to(root).as_posix() for f in _selected(probe, root, overlay))
    if "unless_name" in probe:
        rx = re.compile(probe["unless_name"])
        counted, what = [f for f in files if not rx.fullmatch(f.rsplit("/", 1)[-1])], f"not named /{probe['unless_name']}/"
    elif "unique" in probe:
        rx = re.compile(probe["unique"])
        keys: dict[str, list[str]] = {}
        for f in files:
            if m := rx.search(f.rsplit("/", 1)[-1]):
                keys.setdefault(m.group(0), []).append(f)
        counted, what = [f for fs in keys.values() if len(fs) > 1 for f in fs], f"sharing a /{probe['unique']}/ key"
    else:
        counted, what = files, "matching"
    return files, counted, what


def paths_expect(probe: dict):
    """A paths probe's expectation. `unless_name` and `unique` count violations, so they default to "absent"."""
    return probe.get("expect", "absent" if "unless_name" in probe or "unique" in probe else "present")


def _run_paths(probe: dict, root: Path, overlay: dict[str, str] | None = None) -> ProbeResult:
    files, counted, what = _paths_counted(probe, root, overlay)
    op, n = _expectation(paths_expect(probe))
    count = len(counted)
    ok = {"count": count == n, "min": count >= n, "max": count <= n}[op]
    hits = [(f, 0) for f in counted[:20]]
    if ok and not files and ("unless_name" in probe or "unique" in probe):
        return ProbeResult(True, f"{VACUOUS}the paths match no files (expected {op} {n})", [], vacuous=True)
    return ProbeResult(ok, f"{count} file(s) {what} of {len(files)} (expected {op} {n})"
                           + (f": {', '.join(counted[:5])}" if counted and not ok else ""), hits)


TEST_FILE = re.compile(r"(?:^|/)(?:tests?|__tests__)/|(?:^|/)(?:test_[^/]*\.py|conftest\.py|tests\.rs)$"
                       r"|_test\.\w+$|\.(?:test|spec)\.\w+$")
_CFG_TEST = re.compile(r"^\s*#\[cfg\(test\)\]")
_RUST_NOISE = re.compile(r"//.*$|\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])'")


def test_lines(rel: str, text: str) -> set[int] | None:
    """The 0-based lines of `text` that are test code by convention, or None when the whole file is: a file under
    a `test/`, `tests/` or `__tests__/` folder or named like a test (`test_*.py`, `*_test.*`, `*.test.*`,
    `*.spec.*`, `conftest.py`, Rust `tests.rs`); in a Rust file, the item a `#[cfg(test)]` attribute marks, to
    its closing brace (braces in strings, chars and `//` comments not counted)."""
    if TEST_FILE.search(rel):
        return None
    out: set[int] = set()
    if not rel.endswith(".rs") or "cfg(test)" not in text:
        return out
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        if not _CFG_TEST.match(lines[i]):
            i += 1
            continue
        depth, parens, opened, j = 0, 0, False, i
        while j < len(lines):
            code = _RUST_NOISE.sub("", lines[j]) if j > i else ""
            for ch in code:
                if ch == "{":
                    depth, opened = depth + 1, True
                elif ch == "}":
                    depth -= 1
                elif ch in "([":
                    parens += 1
                elif ch in ")]":
                    parens -= 1
            if not opened and depth < 0:          # the enclosing block closed: the item (a field, an arm) ended before
                j -= 1
                break
            if opened and depth <= 0 or not opened and parens <= 0 and code.rstrip().endswith((";", ",")):
                break
            j += 1
        out.update(range(i, min(j, len(lines) - 1) + 1))
        i = j + 1
    return out


def _scan(pattern: str, probe: dict, root: Path, overlay: dict[str, str] | None) -> tuple[int, list[tuple[str, int]]]:
    """(files scanned, every (file, 0-based line) matching `pattern`) over the probe's paths."""
    rx = re.compile(pattern)
    exclude = re.compile(probe["exclude"]) if probe.get("exclude") else None
    skip_tests = bool(probe.get("skip_tests"))
    files = _selected(probe, root, overlay)
    from .hashing import scoped
    hits: list[tuple[str, int]] = []

    def scan_text(rel: str, text: str) -> list[tuple[str, int]]:
        tests = test_lines(rel, text) if skip_tests else set()
        if tests is None:
            return []
        return [(rel, i) for i, line in enumerate(text.splitlines())
                if rx.search(line) and not (exclude and exclude.search(line)) and i not in tests]

    def scan_file(path: Path, rel: str) -> list[tuple[str, int]]:
        try:
            return scan_text(rel, path.read_text(errors="replace"))
        except OSError:
            return []
    for path in sorted(files):
        rel = path.relative_to(root).as_posix()
        if overlay and rel in overlay:
            hits.extend(scan_text(rel, overlay[rel]))
        else:       # within a stable_files() scope a file on disk is scanned once per pattern
            hits.extend(scoped(("scan", str(root), pattern, probe.get("exclude"), skip_tests, rel),
                               lambda: scan_file(path, rel)))
    return len(files), hits


def _run_grep(probe: dict, root: Path, overlay: dict[str, str] | None = None) -> ProbeResult:
    op, n = _expectation(probe.get("expect", "present"))
    nfiles, hits = _scan(probe["pattern"], probe, root, overlay)
    count = len(hits)
    ok = {"count": count == n, "min": count >= n, "max": count <= n}[op]
    # An "absent" probe has no positive matches to anchor on; its anchors come from violations.
    if ok and nfiles == 0:    # a glob that matches nothing (a typo, a moved directory) must not read as "none found"
        return ProbeResult(True, f"{VACUOUS}the paths match no files (expected {op} {n})", [], vacuous=True)
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
        changes = ctx.get("staged_status") or {}
        for rel, how in sorted(changes.items()):
            if how != "A" and any(matches(g, rel) for g in probe.get("frozen", [])):
                bad.append(f"{rel} ({ {'M': 'modified', 'D': 'deleted', 'R': 'renamed'}.get(how, how) } but frozen: "
                           "files there may only be added)")
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


def _run_extract(probe: dict, root: Path, overlay: dict[str, str] | None = None) -> ProbeResult:
    from .symbols import SymbolError, digest, section_of, symbol_of
    extract = section_of if probe["type"] == "section" else symbol_of
    rel = probe["ref"].partition("::")[0]
    try:
        if overlay and rel in overlay:              # the file's pending content, read from a scratch copy of it alone
            import tempfile
            with tempfile.TemporaryDirectory() as d:
                (Path(d) / rel).parent.mkdir(parents=True, exist_ok=True)
                (Path(d) / rel).write_text(overlay[rel])
                text = extract(Path(d), probe["ref"])
        else:
            text = extract(root, probe["ref"])
    except SymbolError as e:
        return ProbeResult(False, str(e))
    if digest(text) != probe["sha"]:
        return ProbeResult(False, f"{probe['type']} {probe['ref']} changed since the claim was written")
    return ProbeResult(True, f"{probe['type']} {probe['ref']} unchanged")

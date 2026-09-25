"""Behavioral contracts: the codebase as a fractal graph of regions and the behavior they guarantee.

Every test file is a behavioral claim about the code it exercises. `import_tests` reads each
test's transitive imports inside the repo, so a claim depends on exactly the files whose
behavior it checks; the region tree places it at the smallest region containing them. Edits
then map to the contracts they can break (test-impact analysis), and invalidation walks the
dependency edges.

Behavior is checked by slow `test` probes, batched per runner (one `flutter test f1 f2 …`
compiles once). They never run in the millisecond hooks: they run at the end of an agent's
turn (the Stop gate) and at commit, only for claims whose dependencies changed.

A regression is a behavioral claim that passed at its last verdict and fails now. The gate
blocks on regressions and on violated enforced invariants; tests that were already failing
are reported, never blocking.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path, PurePosixPath

from .cache import ClaimCache, dependencies
from .probes import ProbeResult, is_slow
from .regions import affects, tracked_files

GATE_TIMEOUT_S = 600      # time budget for one turn-end gate run
MAX_GATE_BLOCKS = 2       # per session: after this many blocks the gate reports instead of blocking

# --- import graphs ---------------------------------------------------------------------------

DART_DIRECTIVE = re.compile(r"""^\s*(?:import|export|part)\s+['"]([^'"]+)['"]""", re.M)
# names stay on one line unless parenthesized: `from a import (b,\n c)`
PY_IMPORT = re.compile(r"^[ \t]*(?:from[ \t]+(\.*[\w.]*)[ \t]+import[ \t]+(\([^)]*\)|[\w*, \t]+)|import[ \t]+([\w., \t]+))", re.M)


def dart_package(root: Path) -> str | None:
    try:
        m = re.search(r"^name:\s*(\S+)", (root / "pubspec.yaml").read_text(), re.M)
    except OSError:
        return None
    return m.group(1) if m else None


def dart_imports(root: Path, rel: str, package: str | None, files: set[str]) -> set[str]:
    try:
        text = (root / rel).read_text(errors="replace")
    except OSError:
        return set()
    out = set()
    for target in DART_DIRECTIVE.findall(text):
        if target.startswith("dart:"):
            continue
        if target.startswith("package:"):
            pkg, _, path = target[len("package:"):].partition("/")
            if pkg != package:
                continue
            cand = f"lib/{path}"
        else:
            cand = str(PurePosixPath(rel).parent / target)
        cand = _normpath(cand)
        if cand in files:
            out.add(cand)
    return out


def python_imports(root: Path, rel: str, files: set[str]) -> set[str]:
    try:
        text = (root / rel).read_text(errors="replace")
    except OSError:
        return set()
    here = PurePosixPath(rel).parent
    out = set()
    for frm, names, plain in PY_IMPORT.findall(text):
        modules = []
        if plain:
            modules = [m.strip().split(" as ")[0] for m in plain.split(",")]
        elif frm:
            dots = len(frm) - len(frm.lstrip("."))
            base = frm.lstrip(".")
            if dots:
                anchor = here
                for _ in range(dots - 1):
                    anchor = anchor.parent
                prefix = str(anchor).replace("/", ".") if str(anchor) != "." else ""
                base = ".".join(p for p in (prefix, base) if p)
            modules = [base] + [f"{base}.{n.strip()}" if base else n.strip()
                                for n in names.replace("(", "").replace(")", "").split(",") if n.strip() != "*"]
        for mod in modules:
            path = mod.replace(".", "/")
            for cand in (f"{path}.py", f"{path}/__init__.py", f"src/{path}.py", f"src/{path}/__init__.py"):
                if cand in files:
                    out.add(cand)
    return out


def _normpath(p: str) -> str:
    parts: list[str] = []
    for part in p.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if parts:
                parts.pop()
        else:
            parts.append(part)
    return "/".join(parts)


PATH_LITERAL = re.compile(r"""['"]((?:\.\./)?(?:lib|src|app)(?:/[\w./-]*)?)['"]""")


def scanned_paths(root: Path, rel: str, files: set[str]) -> set[str]:
    """Files a test reads by path instead of importing (source-scanning tests): string literals
    naming a repo file, or a directory it walks (every file under it)."""
    try:
        text = (root / rel).read_text(errors="replace")
    except OSError:
        return set()
    out = set()
    for lit in PATH_LITERAL.findall(text):
        lit = _normpath(lit.removeprefix("../"))
        if lit in files:
            out.add(lit)
        elif lit and any(f.startswith(lit.rstrip("/") + "/") for f in files):
            out.update(f for f in files if f.startswith(lit.rstrip("/") + "/"))
    return out


def closure(root: Path, start: str, files: set[str], language: str, package: str | None = None) -> set[str]:
    """Every repo file reachable from `start` through imports (excluding `start`)."""
    seen: set[str] = set()
    stack = [start]
    while stack:
        cur = stack.pop()
        direct = dart_imports(root, cur, package, files) if language == "dart" else python_imports(root, cur, files)
        for d in direct - seen:
            seen.add(d)
            stack.append(d)
    seen.discard(start)
    return seen


# --- discovery -----------------------------------------------------------------------------------

def detect_runner(root: Path) -> str | None:
    pubspec = root / "pubspec.yaml"
    if pubspec.exists():
        return "flutter" if re.search(r"^\s*sdk:\s*flutter", pubspec.read_text(), re.M) else "dart"
    if any((root / f).exists() for f in ("pyproject.toml", "setup.py", "setup.cfg", "pytest.ini")):
        return "pytest"
    return None


def discover_tests(files: list[str], runner: str) -> list[str]:
    if runner in ("flutter", "dart"):
        return [f for f in files if f.startswith("test/") and f.endswith("_test.dart")]
    return [f for f in files if f.endswith(".py") and PurePosixPath(f).name.startswith("test_")
            or f.endswith("_test.py")]


def _summary(root: Path, test: str) -> str:
    """A readable contract statement from the test's group/describe names."""
    try:
        text = (root / test).read_text(errors="replace")
    except OSError:
        text = ""
    names = re.findall(r"""\b(?:group|describe)\(\s*['"]([^'"]{3,120})['"]""", text)
    names = names or re.findall(r"^(?:class|def)\s+(Test\w+|test_\w+)", text, re.M)[:3]
    about = "; ".join(dict.fromkeys(names))[:240]
    return f"Behavior checked by {test}" + (f": {about}" if about else "")


def import_tests(root: Path, runner: str | None = None, limit: int | None = None) -> dict:
    """Create one behavioral claim per test file. Deferred: no test runs here."""
    root = root.resolve()
    runner = runner or detect_runner(root)
    if runner is None:
        raise ValueError("could not detect a test runner (pass --runner)")
    files = tracked_files(root)
    fileset = set(files)
    language = "dart" if runner in ("flutter", "dart") else "python"
    package = dart_package(root) if language == "dart" else None
    tests = discover_tests(files, runner)[:limit] if limit else discover_tests(files, runner)
    cache = ClaimCache(root)
    created = updated = 0
    vacuous = []
    try:
        for t in tests:
            deps = sorted(closure(root, t, fileset, language, package) | scanned_paths(root, t, fileset) | {t})
            if deps == [t]:
                vacuous.append(t)   # exercises no repo code: can never catch a regression
            before = cache.get_by_probe_file(t)
            e = cache.put(_summary(root, t), deps, kind="behavior",
                          probe={"type": "test", "runner": runner, "file": t}, check=False)
            created += before is None
            updated += before is not None
            if before is not None and before.id != e.id:
                cache.delete(before.id)
        cache.store.log("import_tests", None, runner=runner, tests=len(tests))
    finally:
        cache.close()
    return {"runner": runner, "tests": len(tests), "created": created, "updated": updated,
            "vacuous": len(vacuous), "vacuous_tests": vacuous}


# --- running -------------------------------------------------------------------------------------

def run_tests(root: Path, runner: str, files: list[str], timeout: int = GATE_TIMEOUT_S) -> dict[str, ProbeResult]:
    """Run test files in one invocation; per-file results."""
    if not files:
        return {}
    if runner in ("flutter", "dart"):
        cmd = [runner, "test", "--reporter", "json", *files]
    else:
        cmd = ["python", "-m", "pytest", "-q", "-rA", "--tb=line", "-p", "no:cacheprovider", *files]
    import os
    # No bytecode: an edit within the same second that keeps the file size would otherwise
    # run stale .pyc files and hide a regression.
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    try:
        proc = subprocess.run(cmd, cwd=root, capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return {f: ProbeResult(False, f"timed out after {timeout}s") for f in files}
    except FileNotFoundError:
        return {f: ProbeResult(False, f"runner not found: {cmd[0]}") for f in files}
    if runner in ("flutter", "dart"):
        return _parse_dart_json(root, proc.stdout, files)
    return _parse_pytest(proc.stdout + proc.stderr, files, proc.returncode)


def _parse_dart_json(root: Path, out: str, files: list[str]) -> dict[str, ProbeResult]:
    suites: dict[int, str] = {}
    tests: dict[int, int] = {}
    passed = {f: 0 for f in files}
    failed: dict[str, list[str]] = {f: [] for f in files}
    names: dict[int, str] = {}
    for line in out.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = ev.get("type")
        if kind == "suite":
            path = ev["suite"].get("path") or ""
            try:
                path = Path(path).resolve().relative_to(root.resolve()).as_posix()
            except ValueError:
                pass
            suites[ev["suite"]["id"]] = path
        elif kind == "testStart":
            tests[ev["test"]["id"]] = ev["test"].get("suiteID")
            names[ev["test"]["id"]] = ev["test"].get("name", "")
        elif kind == "testDone" and not ev.get("hidden"):
            f = suites.get(tests.get(ev["testID"]), "")
            if f not in passed:
                continue
            if ev.get("result") == "success":
                passed[f] += 0 if ev.get("skipped") else 1
            else:
                failed[f].append(names.get(ev["testID"], "?"))
    return {f: ProbeResult(not failed[f] and passed[f] > 0,
                           f"{passed[f]} passed" + (f", {len(failed[f])} failed: {'; '.join(failed[f][:3])}"
                                                    if failed[f] else "") if passed[f] or failed[f]
                           else "no tests ran (compile or load error)")
            for f in files}


def _parse_pytest(out: str, files: list[str], code: int) -> dict[str, ProbeResult]:
    passed = {f: 0 for f in files}
    failed: dict[str, list[str]] = {f: [] for f in files}
    for status, nodeid in re.findall(r"^(PASSED|FAILED|ERROR)\s+(\S+)", out, re.M):
        f = nodeid.split("::")[0]
        if f in passed:
            if status == "PASSED":
                passed[f] += 1
            else:
                failed[f].append(nodeid.split("::", 1)[-1])
    return {f: ProbeResult(not failed[f] and passed[f] > 0,
                           f"{passed[f]} passed" + (f", {len(failed[f])} failed: {'; '.join(failed[f][:3])}"
                                                    if failed[f] else "") if passed[f] or failed[f]
                           else "no tests ran (collection error)")
            for f in files}


def slow_claims(cache: ClaimCache) -> list:
    """Behavioral claims and enforced invariants whose probes are tests."""
    return [e for e in cache.store.all() if e.probe and e.probe.get("type") == "test"
            and (e.kind == "behavior" or e.enforced)]


def run_claims(cache: ClaimCache, claims: list, timeout: int = GATE_TIMEOUT_S,
               as_baseline: bool = False) -> dict[str, ProbeResult]:
    """Batch-run test-probe claims (one invocation per runner) and record each verdict.

    `as_baseline` marks the verdicts as the reference that later runs are judged against
    (a regression is a claim whose *baseline* verdict passed and that fails now)."""
    by_runner: dict[str, list] = {}
    for e in claims:
        by_runner.setdefault(e.probe["runner"], []).append(e)
    out: dict[str, ProbeResult] = {}
    for runner, group in by_runner.items():
        files = sorted({e.probe["file"] for e in group})
        results = run_tests(cache.root, runner, files, timeout)
        for e in group:
            r = results.get(e.probe["file"], ProbeResult(False, "no result"))
            cache.set_verdict(e.id, r.passed, r.detail)
            cache.store.log("behavior_verdict", e.id, passed=r.passed, baseline=as_baseline)
            out[e.id] = r
    return out


def last_verdicts(cache: ClaimCache, baseline_only: bool = True) -> dict[str, bool]:
    """Latest verdict per claim; by default only baseline verdicts (the regression reference)."""
    return {ev["edge_id"]: ev["passed"] for ev in cache.store.events("behavior_verdict")
            if ev.get("baseline") or not baseline_only}


def promote_baseline(cache: ClaimCache, results: dict[str, ProbeResult]) -> None:
    """Passing results become the new baseline (e.g. once the commit gate lets a commit through)."""
    for eid, r in results.items():
        if r.passed:
            cache.store.log("behavior_verdict", eid, passed=True, baseline=True)


def changed_files(root: Path) -> list[str]:
    """Uncommitted changes (tracked and untracked, not ignored)."""
    out = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=root,
                         capture_output=True, text=True).stdout
    paths = {line[3:].split(" -> ")[-1].strip() for line in out.splitlines() if line[3:].strip()}
    return sorted(p for p in paths if not p.startswith(".fractal/") and "__pycache__/" not in p)


def affected(cache: ClaimCache, files: list[str]) -> list:
    return [e for e in slow_claims(cache) if any(affects(dependencies(e), f) for f in files)]


def gate(root: Path, files: list[str] | None = None, timeout: int = GATE_TIMEOUT_S) -> dict:
    """Run the behavioral claims the changes can affect; classify the outcome."""
    cache = ClaimCache(root)
    try:
        files = changed_files(cache.root) if files is None else files
        claims = affected(cache, files)
        prior = last_verdicts(cache)
        t0 = time.time()
        results = run_claims(cache, claims, timeout) if claims else {}
        by_id = {e.id: e for e in claims}
        regressions = [by_id[i] for i, r in results.items()
                       if not r.passed and prior.get(i) is True and by_id[i].kind == "behavior"]
        violations = [by_id[i] for i, r in results.items() if not r.passed and by_id[i].enforced]
        still_failing = [by_id[i] for i, r in results.items()
                         if not r.passed and by_id[i] not in regressions and by_id[i] not in violations]
        summary = {"changed": len(files), "checked": len(claims), "seconds": round(time.time() - t0, 1),
                   "regressions": regressions, "violations": violations, "still_failing": still_failing,
                   "results": results}
        cache.store.log("gate", None, changed=len(files), checked=len(claims), regressions=len(regressions),
                        violations=len(violations), seconds=summary["seconds"])
        return summary
    finally:
        cache.close()


def baseline(root: Path, timeout: int = 3600) -> dict:
    """Run every behavioral claim once to establish verdicts (what counts as a regression later)."""
    cache = ClaimCache(root)
    try:
        claims = slow_claims(cache)
        t0 = time.time()
        results = run_claims(cache, claims, timeout, as_baseline=True)
        return {"checked": len(claims), "passed": sum(r.passed for r in results.values()),
                "failed": sum(not r.passed for r in results.values()), "seconds": round(time.time() - t0, 1)}
    finally:
        cache.close()

"""Executable probes: deterministic checks that re-verify a claim without an LLM.

Probe shapes:
  {"type": "command", "run": "pytest -q tests/test_x.py", "expect_exit": 0, "timeout": 120}
  {"type": "grep", "pattern": "auth_middleware", "paths": ["src/**/*.py"],
   "exclude": "^\\s*(#|//)",  # optional: skip lines matching this (e.g. comments)
   "expect": "present" | "absent" | {"count": 3} | {"min": 1}}

  {"type": "all", "probes": [<probe>, ...]}  # passes only if every sub-probe passes

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


def validate(probe: dict) -> None:
    kind = probe.get("type")
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
        _expectation(probe.get("expect", "present"))
    elif kind == "all":
        subs = probe.get("probes")
        if not isinstance(subs, list) or not subs:
            raise ProbeError("all probe needs a non-empty 'probes' list")
        for sub in subs:
            validate(sub)
    else:
        raise ProbeError(f"unknown probe type: {kind!r}")


def run(probe: dict, root: Path, overlay: dict[str, str] | None = None) -> ProbeResult:
    """Run a probe. `overlay` maps repo-relative paths to substitute contents (grep only),
    so audits can test probes against mutated code without touching the working tree."""
    validate(probe)
    if probe["type"] == "command":
        return _run_command(probe, root)
    if probe["type"] == "all":
        results = [run(sub, root, overlay) for sub in probe["probes"]]
        matches = [m for r in results for m in r.matches]
        failed = [f"#{i}: {r.detail}" for i, r in enumerate(results) if not r.passed]
        if failed:
            return ProbeResult(False, "; ".join(failed), matches)
        return ProbeResult(True, f"all {len(results)} probes passed", matches)
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


def _run_grep(probe: dict, root: Path, overlay: dict[str, str] | None = None) -> ProbeResult:
    pattern = re.compile(probe["pattern"])
    exclude = re.compile(probe["exclude"]) if probe.get("exclude") else None
    op, n = _expectation(probe.get("expect", "present"))
    files: set[Path] = set()
    for glob in probe["paths"]:
        files.update(p for p in root.glob(glob) if p.is_file())
    count = 0
    matches: list[tuple[str, int]] = []
    for path in sorted(files):
        rel = path.relative_to(root).as_posix()
        try:
            text = overlay[rel] if overlay and rel in overlay else path.read_text(errors="replace")
        except OSError:
            continue
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if pattern.search(line) and not (exclude and exclude.search(line)):
                count += 1
                if len(matches) < 20:
                    matches.append((rel, i))
    ok = {"count": count == n, "min": count >= n, "max": count <= n}[op]
    # An "absent" probe has no positive matches to anchor on; its anchors come from violations.
    return ProbeResult(ok, f"{count} matching lines in {len(files)} files (expected {op} {n})", matches)

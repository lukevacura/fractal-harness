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
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ProbeResult:
    passed: bool
    detail: str


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


def run(probe: dict, root: Path) -> ProbeResult:
    validate(probe)
    if probe["type"] == "command":
        return _run_command(probe, root)
    if probe["type"] == "all":
        results = [run(sub, root) for sub in probe["probes"]]
        failed = [f"#{i}: {r.detail}" for i, r in enumerate(results) if not r.passed]
        if failed:
            return ProbeResult(False, "; ".join(failed))
        return ProbeResult(True, f"all {len(results)} probes passed")
    return _run_grep(probe, root)


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


def _run_grep(probe: dict, root: Path) -> ProbeResult:
    pattern = re.compile(probe["pattern"])
    exclude = re.compile(probe["exclude"]) if probe.get("exclude") else None
    op, n = _expectation(probe.get("expect", "present"))
    files: set[Path] = set()
    for glob in probe["paths"]:
        files.update(p for p in root.glob(glob) if p.is_file())
    count = 0
    for path in sorted(files):
        try:
            lines = path.read_text(errors="replace").splitlines()
        except OSError:
            continue
        count += sum(1 for line in lines
                     if pattern.search(line) and not (exclude and exclude.search(line)))
    ok = {"count": count == n, "min": count >= n, "max": count <= n}[op]
    return ProbeResult(ok, f"{count} matching lines in {len(files)} files (expected {op} {n})")

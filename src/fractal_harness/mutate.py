"""`fractal mutate <node>`: run a verifier's mutants and record the verdicts.

The split follows the thesis: choosing a mutant is the scout's (the verifier's) job; applying it, running the
tests and recording the outcome is the judge's, and deterministic. The verifier hands over small patterns:

    [{"file": "src/x.py", "span": "40-42", "tests": ["test_edge"],
      "find": "<", "replace": "<="}                       # replace text inside the span, or
     {"file": "src/x.py", "span": "57", "tests": ["test_clamp"], "replacement": ""}]   # replace the span's lines

Each runs in its own scratch worktree of the node's commit (see executor.py). Before any mutant, the same
command must pass on the unmutated commit, or nothing is recorded. A test failure is a kill; passing tests is
a survivor (recorded `survived`: whether it is equivalent is an argument the verifier makes with
`ledger record`). A mutant that does not build is reported invalid and not recorded; without `--build-cmd` a
build error cannot be told from a kill, so give one.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import ledger
from .cache import ClaimCache
from .executor import Job, Outcome, run_all
from .symbols import SymbolError


@dataclass
class Mutant:
    file: str
    start: int
    end: int
    name: str
    tests: list[str]
    find: str | None = None
    replace: str = ""
    replacement: str | None = None

    @property
    def span(self) -> str:
        return str(self.start) if self.start == self.end else f"{self.start}-{self.end}"


def parse(items: list[dict]) -> list[Mutant]:
    out = []
    for i, it in enumerate(items):
        where = f"mutant #{i + 1}"
        if not (it.get("file") and it.get("span")):
            raise SymbolError(f"{where} needs file and span")
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", str(it["span"]).strip())
        if not m or int(m.group(1)) < 1 or int(m.group(2) or m.group(1)) < int(m.group(1)):
            raise SymbolError(f"{where}: bad span {it['span']!r} (N or N-M, 1-based lines)")
        if ("find" in it) == ("replacement" in it):
            raise SymbolError(f"{where} needs exactly one of `find`+`replace` or `replacement`")
        tests = it.get("tests") or []
        if isinstance(tests, str):
            tests = [t.strip() for t in tests.split(",") if t.strip()]
        if not tests:
            raise SymbolError(f"{where} needs `tests`: the tests that should kill it (they are run, and recorded)")
        start, end = int(m.group(1)), int(m.group(2) or m.group(1))
        if "find" in it:
            name = it.get("mutant") or f"{it['find']} -> {it.get('replace', '')}"
        else:
            name = it.get("mutant") or ("delete lines" if not it["replacement"] else "replace lines")
        out.append(Mutant(it["file"], start, end, name, tests, it.get("find"), it.get("replace", ""), it.get("replacement")))
    return out


def apply(text: str, m: Mutant) -> str:
    lines = text.splitlines(keepends=True)
    if m.end > len(lines):
        raise SymbolError(f"{m.file}: span {m.span} is past the end ({len(lines)} lines)")
    block = "".join(lines[m.start - 1:m.end])
    if m.find is not None:
        if m.find not in block:
            raise SymbolError(f"{m.file}:{m.span}: `{m.find}` does not occur in the span")
        new = block.replace(m.find, m.replace, 1)
    else:
        new = m.replacement + ("\n" if m.replacement and not m.replacement.endswith("\n") else "")
    return "".join(lines[:m.start - 1]) + new + "".join(lines[m.end:])


def _show(root: Path, commit: str, file: str) -> str:
    p = subprocess.run(["git", "show", f"{commit}:{file}"], cwd=root, capture_output=True, text=True)
    if p.returncode:
        raise SymbolError(f"{file} is not in {commit[:10]}")
    return p.stdout


def run(cache: ClaimCache, root: Path, node: str, items: list[dict], *, cmd: str, build_cmd: str = "",
        env: dict[str, str] | None = None, commit: str | None = None, workers: int = 4, timeout: int = 600,
        record: bool = True, use_memo: bool = True) -> dict:
    if "{tests}" not in cmd:
        cmd += " {tests}"
    mutants = parse(items)
    sha = ledger.node_commit(cache, root, node, commit)
    env = env or {}

    def job(tests: list[str], edits: dict[str, str], label: str) -> Job:
        return Job(edits, cmd.replace("{tests}", " ".join(tests)), build_cmd, env, timeout, label)

    # the judge's precondition: the tests pass with nothing changed
    selections = {tuple(m.tests): m.tests for m in mutants}
    base = run_all(cache.store, root, sha, [job(t, {}, "baseline") for t in selections.values()], workers, use_memo)
    for t, b in zip(selections, base):
        if b.status != "passed":
            raise SymbolError(f"the tests do not pass on the unmutated commit {sha[:10]} ({' '.join(t)}): "
                              f"{b.status}\n{b.tail}")
    sources: dict[str, str] = {}
    jobs = []
    for m in mutants:
        sources.setdefault(m.file, _show(root, sha, m.file))
        jobs.append(job(m.tests, {m.file: apply(sources[m.file], m)}, m.name))
    outcomes = run_all(cache.store, root, sha, jobs, workers, use_memo)
    rows = []
    for m, o in zip(mutants, outcomes):
        verdict = {"failed": "killed", "passed": "survived", "build_failed": "invalid", "timeout": "timeout"}[o.status]
        if record and verdict in ("killed", "survived"):
            ledger.record(cache, root, node, file=m.file, span=m.span, mutant=m.name, result=verdict,
                          commit=sha, tests=m.tests if verdict == "killed" else [])
        rows.append({"file": m.file, "span": m.span, "mutant": m.name, "verdict": verdict, "tests": m.tests,
                     "seconds": round(o.seconds, 2), "cached": o.cached, "tail": o.tail if verdict != "killed" else ""})
    return {"node": node, "commit": sha, "recorded": record, "rows": rows,
            "baseline_seconds": round(sum(b.seconds for b in base), 2)}


def render(r: dict) -> str:
    out = [f"# Mutants for {r['node']} at {r['commit'][:10]}", ""]
    for x in r["rows"]:
        out.append(f"- {x['file']}:{x['span']}  {x['mutant']}: {x['verdict'].upper()}  ({x['seconds']}s"
                   + (", cached" if x["cached"] else "") + ")")
        if x["verdict"] in ("invalid", "timeout") and x["tail"]:
            out.append("    " + x["tail"].strip().splitlines()[-1][:160])
    count = lambda v: sum(1 for x in r["rows"] if x["verdict"] == v)
    spent = sum(x["seconds"] for x in r["rows"] if not x["cached"])
    out += ["", f"{count('killed')} killed, {count('survived')} survived, {count('invalid')} invalid (do not build), "
                f"{count('timeout')} timed out; {sum(1 for x in r['rows'] if x['cached'])} answered from the memo; "
                f"{spent:.1f}s spent running." + ("" if r["recorded"] else " Nothing recorded (--no-record).")]
    if count("survived"):
        out.append("Survivors are recorded `survived` (unresolved): argue equivalence with `ledger record`, or strengthen the tests.")
    return "\n".join(out) + "\n"

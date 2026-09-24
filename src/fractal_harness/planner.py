"""Contract-first parallel implementation (prototype).

The streaming model applied to code generation:

  plan    (keyframe, sequential)  one agent writes a *skeleton commit*: interface stubs, one
          unit test per edge that fakes the edge's dependencies at their interfaces (so each
          edge is independently checkable: a closed GOP), and an integration test. Edges own
          disjoint write sets. Probe-first: an edge whose test already passes is dropped.
  expand  (chunks, parallel)      each edge runs in its own git worktree off the skeleton,
          may touch only its write set, and is done when its own test passes.
  merge                           apply every passing edge's patch (disjoint writes: no
          conflicts), then run all edge tests + the integration test. A composition failure
          means a contract at some cut was incomplete (the frame problem).

`sequential` runs the same skeleton through one agent: the baseline that isolates
"parallel chunks vs. sequential expansion".
"""

from __future__ import annotations

import fnmatch
import json
import os
import shlex
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .cache import STORE_DIR
from .record import NO_HOOKS_ENV

PLAN_FILE = ".fractal-plan.json"


class BudgetExceeded(RuntimeError):
    pass


class Budget:
    """Process-wide spend limit for agent calls. Checked before every call; each call's cost
    is added after it returns, so a run can overshoot by at most the calls already in flight."""

    def __init__(self) -> None:
        import threading
        self.limit: float | None = None
        self.spent = 0.0
        self._lock = threading.Lock()

    def set(self, limit: float | None) -> None:
        with self._lock:
            self.limit, self.spent = limit, 0.0

    def check(self) -> None:
        with self._lock:
            if self.limit is not None and self.spent >= self.limit:
                raise BudgetExceeded(f"budget ${self.limit:.2f} reached (spent ${self.spent:.2f})")

    def add(self, cost: float) -> None:
        with self._lock:
            self.spent += cost or 0.0


BUDGET = Budget()
# An edge is not done while any stub in its write set is left unimplemented, even if its
# test passes: the test may not exercise every stub (a contract claiming more than it checks).
STUB_MARKERS = ("raise NotImplementedError",)

PLAN_PROMPT = """\
You are the planner for a contract-first parallel implementation. Do NOT implement the task.

Task:
{task}

Produce a skeleton that lets {edge_count} agents implement the task in parallel, each
working alone in its own copy of the repo:

1. Explore the repo enough to understand where the change goes.
2. Split the task into edges. Each edge owns a disjoint set of files (its write set). Cut
   where the interface between pieces is small and checkable: function signatures, types,
   and a docstring contract.
3. Write the skeleton directly in the repo:
   - Interface stubs for every new or changed public function/class: full signature, type
     hints, and a docstring stating the contract precisely (inputs, outputs, errors, edge
     cases). Body: `raise NotImplementedError`. Each stub lives in the write set of the edge
     that implements it. Do not implement any bodies.
   - For each edge, a unit test file `tests/test_edge_<id>.py` checking that edge's contract
     ONLY. Where the edge calls another edge's function, the test must replace it with a fake
     (monkeypatch) so the test passes once this edge alone is implemented, whatever the
     other edges do.
   - One integration test `tests/test_integration_<name>.py` exercising the whole feature
     through the real code, with no fakes.
4. Write `{plan_file}`:
   {{"edges": [{{"id": "<short-id>", "post": "<the edge's contract in 1-3 sentences>",
                 "writes": ["<file>", ...], "test": "tests/test_edge_<id>.py",
                 "depends_on": ["<edge ids whose functions this edge calls>"]}}],
    "integration_test": "tests/test_integration_<name>.py"}}
   Write sets must be disjoint and must not contain test files.
5. Verify with `{test_cmd}` (replace {{test}} with a test path): every edge test and the
   integration test must FAIL now, and only because of NotImplementedError, never because of
   import or syntax errors. Fix the skeleton until that holds.

Finish with a one-line summary.
"""

EDGE_PROMPT = """\
You implement ONE edge of a planned change. Other agents are implementing the other edges at
the same time, in separate copies of the repo; you will never see their code.

Overall task (context only):
{task}

The plan. Other edges' functions exist in the code as stubs with docstring contracts; rely
only on those signatures and contracts:
{plan}

YOUR EDGE: {id}
Contract: {post}
Files you may modify: {writes}
Do not modify any other file. In particular, never modify tests.

You are done when `{cmd}` passes. That test fakes the other edges, so it does not depend on
their implementations. Implement EVERY stub in your files fully and correctly per its
docstring (production quality, not just enough to pass the test): your edge is rejected if any
`raise NotImplementedError` remains in your files, even if the test passes.
Finish with a one-line summary.
"""

DIRECT_PROMPT = """\
Implement this task in the repo. Add or update tests as appropriate.

Task:
{task}

Keep the existing test suite passing (`{cmd}`). Finish with a one-line summary.
"""

SEQUENTIAL_PROMPT = """\
Implement this planned change. The skeleton (stubs with docstring contracts and tests) is
already in the repo.

Task:
{task}

The plan:
{plan}

Implement every edge. Files you may modify: {writes}. Never modify tests.
You are done when all of these pass: {cmds}
Finish with a one-line summary.
"""


@dataclass
class EdgeSpec:
    id: str
    post: str
    writes: list[str]
    test: str
    depends_on: list[str] = field(default_factory=list)


@dataclass
class Plan:
    id: str
    task: str
    root_commit: str
    skeleton: str               # commit sha of the skeleton
    edges: list[EdgeSpec]
    integration_test: str
    test_cmd: str               # shell template with {test}
    dropped: list[dict] = field(default_factory=list)   # probe-first: already-passing edges
    cost_usd: float = 0.0
    duration_s: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Plan":
        return Plan(**{**d, "edges": [EdgeSpec(**e) for e in d["edges"]]})

    def summary(self) -> str:
        return "\n".join(f"- {e.id} (writes {', '.join(e.writes)}; depends on {', '.join(e.depends_on) or '-'}): "
                         f"{e.post}" for e in self.edges)


class PlanError(ValueError):
    pass


# --- git + agent plumbing -------------------------------------------------------

def git(cwd: Path, *args: str, check: bool = True) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=check).stdout.strip()


def _worktree(root: Path, name: str, commit: str) -> Path:
    wt = root / STORE_DIR / "wt" / name
    if wt.exists():
        git(root, "worktree", "remove", "--force", str(wt), check=False)
    wt.parent.mkdir(parents=True, exist_ok=True)
    git(root, "worktree", "add", "-q", "--detach", str(wt), commit)
    return wt


def _drop_worktree(root: Path, wt: Path) -> None:
    git(root, "worktree", "remove", "--force", str(wt), check=False)


def _exclude_store(root: Path) -> None:
    exclude = Path(git(root, "rev-parse", "--git-path", "info/exclude"))
    exclude = exclude if exclude.is_absolute() else root / exclude
    text = exclude.read_text() if exclude.exists() else ""
    if f"{STORE_DIR}/" not in text.split():
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text(text + ("" if text.endswith("\n") or not text else "\n") + f"{STORE_DIR}/\n")


def _agent(cwd: Path, prompt: str, test_cmd: str, model: str, max_turns: int = 60,
           edit: bool = True) -> dict:
    runner = shlex.split(test_cmd.replace("{test}", ""))[0]
    tools = ["Read", "Grep", "Glob", "Bash(ls:*)"]
    if edit:
        tools += ["Edit", "Write", f"Bash({runner}:*)"]
    BUDGET.check()
    cmd = ["claude", "-p", prompt, "--output-format", "json", "--model", model,
           "--strict-mcp-config", "--permission-mode", "acceptEdits" if edit else "default",
           "--allowedTools", *tools, "--no-session-persistence", "--max-turns", str(max_turns)]
    t0 = time.time()
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=3600,
                          env={**os.environ, NO_HOOKS_ENV: "1"})
    try:
        r = json.loads(proc.stdout)
    except json.JSONDecodeError:
        r = {"result": proc.stderr[-500:]}
    BUDGET.add(r.get("total_cost_usd") or 0.0)
    return {"cost_usd": r.get("total_cost_usd") or 0.0, "turns": r.get("num_turns"),
            "duration_s": round(time.time() - t0, 1), "summary": (r.get("result") or "")[-400:],
            "result": r.get("result") or ""}


def run_test(cwd: Path, test_cmd: str, test: str) -> tuple[bool, str]:
    proc = subprocess.run(test_cmd.replace("{test}", shlex.quote(test) if test else ""), shell=True, cwd=cwd,
                          capture_output=True, text=True, timeout=600)
    return proc.returncode == 0, (proc.stdout + proc.stderr).strip()[-600:]


def changed_files(wt: Path, base: str) -> list[str]:
    git(wt, "add", "-A")
    return [f for f in git(wt, "diff", "--cached", "--name-only", base).splitlines() if f]


def leftover_stubs(wt: Path, writes: list[str]) -> list[str]:
    """`file:line` of stub markers still present in the write set."""
    out = []
    for w in writes:
        for path in (wt.glob(w) if any(c in w for c in "*?[") else [wt / w]):
            if not path.is_file():
                continue
            for i, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
                code = line.split("#", 1)[0]
                if any(m in code for m in STUB_MARKERS):
                    out.append(f"{path.relative_to(wt).as_posix()}:{i}")
    return out


def outside(files: list[str], writes: list[str]) -> list[str]:
    return [f for f in files if not any(f == w or fnmatch.fnmatch(f, w) for w in writes)]


# --- plan -----------------------------------------------------------------------

def validate(raw: dict, wt: Path) -> tuple[list[EdgeSpec], str]:
    try:
        edges = [EdgeSpec(id=str(e["id"]), post=e["post"], writes=list(e["writes"]), test=e["test"],
                          depends_on=list(e.get("depends_on", []))) for e in raw["edges"]]
        integration = raw["integration_test"]
    except (KeyError, TypeError) as ex:
        raise PlanError(f"malformed plan: {ex}") from ex
    ids = [e.id for e in edges]
    if len(set(ids)) != len(ids) or len(edges) < 1:
        raise PlanError("edge ids must be unique and non-empty")
    owner: dict[str, str] = {}
    for e in edges:
        for w in e.writes:
            if w in owner:
                raise PlanError(f"write sets overlap on {w}: {owner[w]} and {e.id}")
            if w.startswith("tests/") or "/test_" in w:
                raise PlanError(f"edge {e.id} may not own test file {w}")
            owner[w] = e.id
        for d in e.depends_on:
            if d not in ids:
                raise PlanError(f"edge {e.id} depends on unknown edge {d}")
        if not (wt / e.test).is_file():
            raise PlanError(f"edge {e.id} test {e.test} missing")
    if not (wt / integration).is_file():
        raise PlanError(f"integration test {integration} missing")
    seen, stack = set(), set()

    def visit(i: str) -> None:
        if i in stack:
            raise PlanError(f"dependency cycle through {i}")
        if i in seen:
            return
        stack.add(i)
        for d in next(e for e in edges if e.id == i).depends_on:
            visit(d)
        stack.discard(i)
        seen.add(i)

    for i in ids:
        visit(i)
    return edges, integration


def plan(root: Path, task: str, test_cmd: str, model: str = "sonnet", max_edges: int = 4,
         exact_edges: int | None = None) -> Plan:
    root = root.resolve()
    _exclude_store(root)
    pid = uuid.uuid4().hex[:8]
    head = git(root, "rev-parse", "HEAD")
    wt = _worktree(root, f"plan-{pid}", head)
    try:
        count = f"exactly {exact_edges}" if exact_edges else f"2 to {max_edges}"
        prompt = PLAN_PROMPT.format(task=task, edge_count=count, plan_file=PLAN_FILE, test_cmd=test_cmd)
        run = _agent(wt, prompt, test_cmd, model, max_turns=80)
        path = wt / PLAN_FILE
        if not path.exists():
            raise PlanError(f"planner wrote no {PLAN_FILE}: {run['summary']}")
        edges, integration = validate(json.loads(path.read_text()), wt)
        # Probe-first pruning: an edge whose contract already holds needs no work.
        dropped, kept = [], []
        for e in edges:
            ok, out = run_test(wt, test_cmd, e.test)
            (dropped if ok else kept).append(e)
        dropped_info = [{"id": e.id, "reason": "test already passes on the skeleton"} for e in dropped]
        git(wt, "add", "-A")
        git(wt, "-c", "user.name=fractal", "-c", "user.email=fractal@localhost",
            "commit", "-q", "-m", f"fractal skeleton {pid}: {task[:60]}")
        skeleton = git(wt, "rev-parse", "HEAD")
        git(root, "update-ref", f"refs/fractal/{pid}/skeleton", skeleton)
    finally:
        _drop_worktree(root, wt)
    p = Plan(id=pid, task=task, root_commit=head, skeleton=skeleton, edges=kept, integration_test=integration,
             test_cmd=test_cmd, dropped=dropped_info, cost_usd=run["cost_usd"], duration_s=run["duration_s"])
    save(root, p)
    return p


def save(root: Path, p: Plan) -> Path:
    path = root / STORE_DIR / "plans" / f"{p.id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(p.to_dict(), indent=2))
    return path


def load(root: Path, pid: str) -> Plan:
    return Plan.from_dict(json.loads((root / STORE_DIR / "plans" / f"{pid}.json").read_text()))


# --- expand + merge ---------------------------------------------------------------

def expand_edge(root: Path, p: Plan, e: EdgeSpec, model: str) -> dict:
    wt = _worktree(root, f"{p.id}-{e.id}", p.skeleton)
    try:
        cmd = p.test_cmd.replace("{test}", e.test)
        run = _agent(wt, EDGE_PROMPT.format(task=p.task, plan=p.summary(), id=e.id, post=e.post,
                                            writes=", ".join(e.writes), cmd=cmd), p.test_cmd, model)
        test_ok, out = run_test(wt, p.test_cmd, e.test)
        stubs = leftover_stubs(wt, e.writes)
        files = changed_files(wt, p.skeleton)
        patch = git(wt, "diff", "--cached", "--binary", p.skeleton)
    finally:
        _drop_worktree(root, wt)
    passed = test_ok and not stubs
    return {"edge": e.id, "passed": passed, "test_passed": test_ok, "leftover_stubs": stubs,
            "violations": outside(files, e.writes), "files": files,
            "test_output": "" if test_ok else out, "patch": patch + "\n" if patch else "", **run}


def pytest_counts(output: str) -> dict:
    """{"passed": n, "failed": m} from a pytest summary line (errors count as failed)."""
    import re
    counts = {k: int(n) for n, k in re.findall(r"(\d+) (passed|failed|error)", output)}
    return {"passed": counts.get("passed", 0), "failed": counts.get("failed", 0) + counts.get("error", 0)}


def _accept(wt: Path, test_cmd: str, acceptance: str | None) -> dict | None:
    """Run an external (hidden) acceptance test file against the tree in `wt`: pass/fail plus counts."""
    if not acceptance:
        return None
    ok, out = run_test(wt, test_cmd.replace(" -q ", " -q -rN "), acceptance)
    c = pytest_counts(out)
    total = c["passed"] + c["failed"]
    return {"ok": ok, **c, "score": round(c["passed"] / total, 3) if total else 0.0}


def merge(root: Path, p: Plan, results: list[dict], label: str = "merged",
          acceptance: str | None = None) -> dict:
    wt = _worktree(root, f"{p.id}-{label}", p.skeleton)
    try:
        applied, failed = [], []
        for r in results:
            if not (r["passed"] and not r["violations"] and r["patch"]):
                continue
            proc = subprocess.run(["git", "apply", "--index", "-"], cwd=wt, input=r["patch"], text=True,
                                  capture_output=True)
            (applied if proc.returncode == 0 else failed).append(r["edge"])
        tests = {e.id: run_test(wt, p.test_cmd, e.test)[0] for e in p.edges}
        integration, out = run_test(wt, p.test_cmd, p.integration_test)
        full, _ = run_test(wt, p.test_cmd, "")
        accepted = _accept(wt, p.test_cmd, acceptance)
        if applied:
            git(wt, "-c", "user.name=fractal", "-c", "user.email=fractal@localhost",
                "commit", "-q", "-m", f"fractal {label} {p.id}")
            git(root, "update-ref", f"refs/fractal/{p.id}/{label}", git(wt, "rev-parse", "HEAD"))
    finally:
        _drop_worktree(root, wt)
    return {"applied": applied, "apply_failed": failed, "edge_tests": tests, "integration": integration,
            "integration_output": "" if integration else out, "full_suite": full, "acceptance": accepted}


def run_parallel(root: Path, p: Plan, model: str = "sonnet", workers: int = 4,
                 acceptance: str | None = None) -> dict:
    t0 = time.time()
    with ThreadPoolExecutor(max(1, workers)) as pool:
        results = list(pool.map(lambda e: expand_edge(root, p, e, model), p.edges))
    wall = time.time() - t0
    m = merge(root, p, results, "parallel", acceptance)
    return {"mode": "parallel", "wall_s": round(wall, 1), "cost_usd": sum(r["cost_usd"] for r in results),
            "edges": [{k: v for k, v in r.items() if k != "patch"} for r in results], "merge": m}


def run_sequential(root: Path, p: Plan, model: str = "sonnet", acceptance: str | None = None) -> dict:
    wt = _worktree(root, f"{p.id}-seq", p.skeleton)
    writes = [w for e in p.edges for w in e.writes]
    try:
        cmds = "; ".join(p.test_cmd.replace("{test}", t) for t in [e.test for e in p.edges] + [p.integration_test])
        run = _agent(wt, SEQUENTIAL_PROMPT.format(task=p.task, plan=p.summary(), writes=", ".join(writes),
                                                  cmds=cmds), p.test_cmd, model, max_turns=120)
        tests = {e.id: run_test(wt, p.test_cmd, e.test)[0] for e in p.edges}
        integration, out = run_test(wt, p.test_cmd, p.integration_test)
        full, _ = run_test(wt, p.test_cmd, "")
        accepted = _accept(wt, p.test_cmd, acceptance)
        files = changed_files(wt, p.skeleton)
    finally:
        _drop_worktree(root, wt)
    return {"mode": "sequential", "wall_s": run["duration_s"], "cost_usd": run["cost_usd"], "turns": run["turns"],
            "violations": outside(files, writes), "merge": {"edge_tests": tests, "integration": integration,
                                                             "integration_output": "" if integration else out,
                                                             "full_suite": full, "acceptance": accepted}}


def run_direct(root: Path, task: str, test_cmd: str, model: str = "sonnet",
               acceptance: str | None = None) -> dict:
    """Baseline without a plan: one agent implements the task from the original HEAD."""
    root = root.resolve()
    _exclude_store(root)
    name = f"direct-{uuid.uuid4().hex[:6]}"
    wt = _worktree(root, name, git(root, "rev-parse", "HEAD"))
    try:
        run = _agent(wt, DIRECT_PROMPT.format(task=task, cmd=test_cmd.replace("{test}", "")), test_cmd, model,
                     max_turns=120)
        full, _ = run_test(wt, test_cmd, "")
        accepted = _accept(wt, test_cmd, acceptance)
        files = changed_files(wt, git(root, "rev-parse", "HEAD"))
    finally:
        _drop_worktree(root, wt)
    return {"mode": "direct", "wall_s": run["duration_s"], "cost_usd": run["cost_usd"], "turns": run["turns"],
            "files": files, "merge": {"full_suite": full, "acceptance": accepted}}

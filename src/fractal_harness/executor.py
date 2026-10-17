"""The scratch-worktree executor: apply a change to a commit somewhere disposable, run a command, report.

The judge's one primitive. A job is a set of file edits against a commit plus a command; the executor
checks the commit out into its own throwaway worktree, writes the edits there, builds and runs, and removes
the worktree whatever happens. The real checkout is never touched, so a killed or stalled run cannot leave a
mutant applied to anyone's tree. Outcomes are deterministic functions of (the commit's tree, the edits, the
commands, the environment), so they are memoized by a hash of exactly that: a repeated job costs nothing.

`mutate.py` is the first user (mutants are edits). Reclamation (deleting what nothing needs) is the second.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from .store import Store
from .symbols import SymbolError

TAIL = 1500
_git_lock = threading.Lock()      # `git worktree add/remove` take repository locks: do them one at a time


@dataclass
class Job:
    edits: dict[str, str]                       # repo-relative path -> the new full text
    cmd: str                                    # the command whose exit status is the verdict
    build_cmd: str = ""                         # run first: failing here means the edit does not build
    env: dict[str, str] = field(default_factory=dict)   # values may use {job}, the job's number, e.g. a target dir
    timeout: int = 600
    label: str = ""


@dataclass
class Outcome:
    status: str                                 # passed | failed | build_failed | timeout
    code: int
    tail: str
    seconds: float
    cached: bool = False


def tree_of(root: Path, commit: str) -> str:
    p = subprocess.run(["git", "rev-parse", "--verify", f"{commit}^{{tree}}"], cwd=root, capture_output=True, text=True)
    if p.returncode:
        raise SymbolError(p.stderr.strip() or f"no such commit: {commit}")
    return p.stdout.strip()


def memo_key(tree: str, job: Job) -> str:
    blob = json.dumps({"tree": tree, "edits": {k: hashlib.sha256(v.encode()).hexdigest() for k, v in sorted(job.edits.items())},
                       "cmd": job.cmd, "build": job.build_cmd, "env": sorted(job.env.items())}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def _sh(cmd: str, cwd: Path, env: dict, timeout: int) -> tuple[int | None, str]:
    """(exit code or None on timeout, tail of the output). The whole process group is killed on timeout."""
    p = subprocess.Popen(cmd, shell=True, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, start_new_session=True)
    try:
        out, _ = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGKILL)
        out, _ = p.communicate()
        return None, (out or "")[-TAIL:]
    return p.returncode, (out or "")[-TAIL:]


def execute(root: Path, commit: str, job: Job, n: int = 0) -> Outcome:
    """Run one job in a fresh worktree of `commit`; always removes it."""
    start = time.time()
    tmp = Path(tempfile.mkdtemp(prefix="fractal-scratch-"))
    wt = tmp / "wt"
    try:
        with _git_lock:
            subprocess.run(["git", "worktree", "add", "--detach", "-q", str(wt), commit], cwd=root,
                           capture_output=True, text=True, check=True)
        for rel, text in job.edits.items():
            f = wt / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text)
        env = {**os.environ, **{k: v.replace("{job}", str(n)) for k, v in job.env.items()}, "FRACTAL_SCRATCH": "1"}
        for cmd, fail in ((job.build_cmd, "build_failed"), (job.cmd, "failed")):
            if not cmd:
                continue
            code, tail = _sh(cmd, wt, env, job.timeout)
            if code is None:
                return Outcome("timeout", -1, tail, time.time() - start)
            if code:
                return Outcome(fail, code, tail, time.time() - start)
        return Outcome("passed", 0, tail if job.cmd or job.build_cmd else "", time.time() - start)
    finally:
        with _git_lock:
            subprocess.run(["git", "worktree", "remove", "--force", str(wt)], cwd=root, capture_output=True)
            subprocess.run(["git", "worktree", "prune"], cwd=root, capture_output=True)
        shutil.rmtree(tmp, ignore_errors=True)


def run_all(store: Store, root: Path, commit: str, jobs: list[Job], workers: int = 4, use_memo: bool = True) -> list[Outcome]:
    """Outcomes for `jobs`, in order. Memoized outcomes are answered without running; timeouts are never
    memoized. The store is touched only from this thread."""
    tree = tree_of(root, commit)
    keys = [memo_key(tree, j) for j in jobs]
    out: list[Outcome | None] = [None] * len(jobs)
    todo: dict[str, list[int]] = {}
    for i, k in enumerate(keys):
        if use_memo and (m := store.get_run(k)):
            out[i] = Outcome(m["status"], m["code"], m["tail"], m["seconds"], cached=True)
        else:
            todo.setdefault(k, []).append(i)           # identical jobs in one batch run once
    firsts = [idx[0] for idx in todo.values()]
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        results = list(pool.map(lambda i: execute(root, commit, jobs[i], i), firsts))
    for (k, idx), res in zip(todo.items(), results):
        if res.status != "timeout":
            store.put_run(k, res.status, res.code, res.tail, res.seconds)
        for i in idx:
            out[i] = res if i == idx[0] else Outcome(res.status, res.code, res.tail, res.seconds, cached=True)
    return out  # type: ignore[return-value]

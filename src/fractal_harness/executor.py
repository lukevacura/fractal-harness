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
import shlex
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
    tail: int = TAIL                            # how much of the output the outcome keeps (not part of the memo key)
    log: str = ""                               # a file the output is streamed to as it appears (not part of the key)


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


def memo_key(tree: str, job: Job, backend: str = "local") -> str:
    """What an outcome is a function of. The local backend's key is what it always was (existing memos stay
    valid); another backend runs in another environment, so its name is part of the key."""
    d = {"tree": tree, "edits": {k: hashlib.sha256(v.encode()).hexdigest() for k, v in sorted(job.edits.items())},
         "cmd": job.cmd, "build": job.build_cmd, "env": sorted(job.env.items())}
    if backend != "local":
        d["backend"] = backend
    return hashlib.sha256(json.dumps(d, sort_keys=True).encode()).hexdigest()


def _sh(cmd: str | list[str], cwd: Path, env: dict, timeout: int, tail: int = TAIL, log: str = "",
        stdin: str = "") -> tuple[int | None, str]:
    """(exit code or None on timeout, tail of the output). A string runs in a shell, a list is an argv. The
    whole process group is killed on timeout. With `log` the output goes to that file as it appears."""
    sink = open(log, "ab") if log else subprocess.PIPE
    src = open(stdin, "rb") if stdin else None
    try:
        p = subprocess.Popen(cmd, shell=isinstance(cmd, str), cwd=cwd, env=env, stdin=src, stdout=sink,
                             stderr=subprocess.STDOUT, start_new_session=True, **({} if log else {"text": True}))
        try:
            out, _ = p.communicate(timeout=timeout)
            code: int | None = p.returncode
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
            out, _ = p.communicate()
            code = None
    finally:
        if log:
            sink.close()                                        # type: ignore[union-attr]
        if src:
            src.close()
    if log:
        with open(log, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - tail * 4))
            out = fh.read().decode(errors="replace")
    return code, (out or "")[-tail:]


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
            code, tail = _sh(cmd, wt, env, job.timeout, job.tail, job.log)
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


class BackendError(SymbolError):
    """The backend itself failed (could not reach, sync or run), as opposed to the job failing. Never memoized."""


class Backend:
    """Where a job runs. `run` returns an Outcome for `job` against `commit`; it must not touch the real checkout."""
    name = "?"

    def run(self, root: Path, commit: str, job: Job, n: int = 0) -> Outcome:
        raise NotImplementedError

    def doctor(self, root: Path) -> list[tuple[bool, str]]:
        return []


class LocalBackend(Backend):
    """A scratch worktree of the commit on this machine (what the executor has always done)."""
    name = "local"

    def run(self, root: Path, commit: str, job: Job, n: int = 0) -> Outcome:
        return execute(root, commit, job, n)


MARK = "__FRACTAL_EXIT__="


class SshBackend(Backend):
    """Sync the commit's tree (plus the job's edits) to `host:dir`, run the command there, fetch output and exit
    code. `prefix` wraps the command (e.g. `nix develop -c`); `ssh` is the client command (default `ssh`).
    The exit code travels in-band (a marker line) so a failure of ssh itself is never mistaken for the job's.
    On timeout the local ssh client is killed; a remote process that ignores the hangup may linger."""
    name = "ssh"

    def __init__(self, host: str, dir: str = "", prefix: str = "", ssh: str = "ssh", options: list[str] | None = None):
        self.host, self.dir, self.prefix = host, dir or "fractal-jobs", prefix
        self.ssh = shlex.split(ssh) + list(options or [])

    @classmethod
    def from_config(cls, cfg) -> "SshBackend":
        c = cfg.jobs.get("ssh", {})
        if not c.get("host"):
            raise BackendError("backend ssh needs [jobs.ssh] host in fractal.toml")
        return cls(c["host"], c.get("dir", ""), c.get("prefix", ""), c.get("ssh", "ssh"), c.get("options"))

    def _ssh(self, remote: str, timeout: int, tail: int = TAIL, log: str = "", stdin: str = "") -> tuple[int | None, str]:
        return _sh([*self.ssh, self.host, remote], Path.cwd(), os.environ.copy(), timeout, tail, log, stdin)

    def _step(self, rdir: str, cmd: str, env: dict, job: Job, log: str) -> tuple[int | None, str]:
        run = f"{shlex.quote(cmd)}"
        envs = "env " + " ".join(shlex.quote(f"{k}={v}") for k, v in sorted(env.items())) + " " if env else ""
        remote = (f"cd {shlex.quote(rdir)} && {envs}{self.prefix + ' ' if self.prefix else ''}sh -c {run}; "
                  f"printf '\\n{MARK}%s\\n' $?")
        code, out = self._ssh(remote, job.timeout, job.tail + 200, log)
        if code is None:
            return None, out[-job.tail:]
        at = out.rfind(MARK)
        if code or at < 0:
            raise BackendError(f"ssh to {self.host} failed (exit {code}): {out.strip()[-300:]}")
        try:
            rc = int(out[at + len(MARK):].split()[0])
        except (ValueError, IndexError):
            raise BackendError(f"ssh to {self.host}: unreadable exit marker") from None
        return rc, out[:at].rstrip("\n")[-job.tail:]

    def run(self, root: Path, commit: str, job: Job, n: int = 0) -> Outcome:
        start = time.time()
        tmp = Path(tempfile.mkdtemp(prefix="fractal-ssh-"))
        src, tarball = tmp / "src", tmp / "tree.tar"
        rdir = f"{self.dir.rstrip('/')}/job-{os.getpid()}-{n}-{int(start * 1000)}"
        try:
            src.mkdir()
            sh = lambda argv: subprocess.run(argv, cwd=root, capture_output=True, text=True)
            p = sh(["git", "archive", "--format=tar", "-o", str(tarball), commit])
            if p.returncode:
                raise BackendError(f"git archive {commit[:10]}: {p.stderr.strip()}")
            subprocess.run(["tar", "-xf", str(tarball), "-C", str(src)], check=True)
            for rel, text in job.edits.items():
                f = src / rel
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text(text)
            subprocess.run(["tar", "-cf", str(tarball), "-C", str(src), "."], check=True)
            code, out = self._ssh(f"rm -rf {shlex.quote(rdir)} && mkdir -p {shlex.quote(rdir)} && tar -xf - -C {shlex.quote(rdir)}",
                                  min(job.timeout, 600), stdin=str(tarball))
            if code != 0:
                raise BackendError(f"sync to {self.host}:{rdir} failed ({'timeout' if code is None else code}): {out.strip()[-300:]}")
            env = {**{k: v.replace("{job}", str(n)) for k, v in job.env.items()}, "FRACTAL_SCRATCH": "1"}
            tail = ""
            for cmd, fail in ((job.build_cmd, "build_failed"), (job.cmd, "failed")):
                if not cmd:
                    continue
                rc, tail = self._step(rdir, cmd, env, job, job.log)
                if rc is None:
                    return Outcome("timeout", -1, tail, time.time() - start)
                if rc:
                    return Outcome(fail, rc, tail, time.time() - start)
            return Outcome("passed", 0, tail if job.cmd or job.build_cmd else "", time.time() - start)
        finally:
            self._ssh(f"rm -rf {shlex.quote(rdir)}", 60)
            shutil.rmtree(tmp, ignore_errors=True)

    def doctor(self, root: Path) -> list[tuple[bool, str]]:
        """[sound] each check reports exactly what it ran: client present, host answers now, remote dir writable."""
        out = []
        client = shutil.which(self.ssh[0])
        out.append((client is not None, f"jobs backend ssh [sound]: client `{self.ssh[0]}` " + (f"at {client}" if client else "not on PATH")))
        if not client:
            return out
        code, msg = self._ssh("true", 15)
        out.append((code == 0, f"jobs backend ssh [sound]: {self.host} " + ("reachable" if code == 0 else
                    "NOT reachable (" + ("timeout" if code is None else msg.strip().splitlines()[-1][:120] if msg.strip() else f"exit {code}") + ")")))
        if code == 0:
            d = shlex.quote(self.dir)
            code, msg = self._ssh(f"mkdir -p {d} && test -w {d}", 15)
            out.append((code == 0, f"jobs backend ssh [sound]: remote dir {self.dir} " + ("writable" if code == 0 else "NOT writable")))
            if self.prefix:
                head = shlex.quote(shlex.split(self.prefix)[0])
                code, _ = self._ssh(f"command -v {head}", 15)
                out.append((code == 0, f"jobs backend ssh [sound]: prefix command {head} " + ("found on the host" if code == 0 else "NOT found on the host")))
        return out


BACKENDS = {"local": lambda cfg: LocalBackend(), "ssh": SshBackend.from_config}   # name -> factory(Config); tests add fakes


def get_backend(root: Path, name: str | None = None) -> Backend:
    """The configured backend (`[jobs] backend`), else local. `name` overrides (`fractal run --backend`)."""
    from .config import load
    cfg = load(root)
    name = name or cfg.jobs.get("backend", "local")
    if name not in BACKENDS:
        raise BackendError(f"unknown jobs backend {name!r} (known: {', '.join(sorted(BACKENDS))})")
    return BACKENDS[name](cfg)


def run_all(store: Store, root: Path, commit: str, jobs: list[Job], workers: int = 4, use_memo: bool = True,
            backend: Backend | None = None) -> list[Outcome]:
    """Outcomes for `jobs`, in order. Memoized outcomes are answered without running; timeouts are never
    memoized. The store is touched only from this thread. `backend` defaults to local (today's behavior)."""
    backend = backend or LocalBackend()
    tree = tree_of(root, commit)
    keys = [memo_key(tree, j, backend.name) for j in jobs]
    out: list[Outcome | None] = [None] * len(jobs)
    todo: dict[str, list[int]] = {}
    for i, k in enumerate(keys):
        if use_memo and (m := store.get_run(k)):
            out[i] = Outcome(m["status"], m["code"], m["tail"], m["seconds"], cached=True)
        else:
            todo.setdefault(k, []).append(i)           # identical jobs in one batch run once
    firsts = [idx[0] for idx in todo.values()]
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        results = list(pool.map(lambda i: backend.run(root, commit, jobs[i], i), firsts))
    for (k, idx), res in zip(todo.items(), results):
        if res.status != "timeout":
            store.put_run(k, res.status, res.code, res.tail, res.seconds)
        for i in idx:
            out[i] = res if i == idx[0] else Outcome(res.status, res.code, res.tail, res.seconds, cached=True)
    return out  # type: ignore[return-value]

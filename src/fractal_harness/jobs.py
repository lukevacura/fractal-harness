"""`fractal run`: a command run at a commit on the configured backend, pollable without waiting.

A heavy or long job should not hold an agent (or a human) in a notification loop. `fractal run --detach` starts
the job in a background process and returns an id at once; `fractal run status <id>` and `fractal run log <id>`
are cheap reads. Records live under the store root's `.fractal/runs/<id>/` (untracked):

    meta.json    what was asked: command, node, commit, backend, timeout
    pid          the worker's pid, written by the worker
    log          the job's output, streamed as it appears
    result.json  written once, atomically, when the job ends: status, exit code, seconds, whether memoized

The job runs through `executor.run_all`, so it is memoized by (tree hash, command) per backend and runs on
whatever `[jobs]` selects (local scratch worktree by default). State is derived from those files:
SOUND for finished jobs (result.json is the outcome the backend reported); `running` vs `lost` for a job with
no result rests on whether the worker's pid is alive, which is HEURISTIC (pids are reused).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from .cache import STORE_DIR, ClaimCache, store_root
from .executor import BackendError, Job, get_backend, run_all
from .symbols import SymbolError

DEFAULT_TIMEOUT = 3600
LOG_TAIL = 20000
STARTING_GRACE = 30          # seconds a record may exist before its worker has written a pid


def runs_dir(root: Path) -> Path:
    return store_root(root) / STORE_DIR / "runs"


def _dir(root: Path, run_id: str) -> Path:
    if not run_id or not all(c.isalnum() or c in "-_" for c in run_id):
        raise SymbolError(f"bad run id {run_id!r}")
    d = runs_dir(root) / run_id
    if not (d / "meta.json").is_file():
        raise SymbolError(f"no such run: {run_id}")
    return d


def _write(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    os.replace(tmp, path)


def create(cache: ClaimCache, root: Path, cmd: str, *, node: str | None = None, backend: str | None = None,
           timeout: int = DEFAULT_TIMEOUT) -> str:
    """Record a run to be done: its id. Resolves the commit now (the node's, else HEAD)."""
    from . import ledger
    commit = ledger.node_commit(cache, root, node) if node else ledger.resolve_commit(root, "HEAD")
    be = get_backend(root, backend)                 # fail before any record exists if the backend is unknown/incomplete
    run_id = hashlib.sha1(f"{cmd}\0{commit}\0{time.time_ns()}".encode()).hexdigest()[:8]
    d = runs_dir(root) / run_id
    d.mkdir(parents=True)
    _write(d / "meta.json", {"id": run_id, "cmd": cmd, "node": node, "commit": commit, "backend": be.name,
                             "timeout": timeout, "created": time.time()})
    return run_id


def execute_run(root: Path, run_id: str) -> dict:
    """Do the run (in this process): stream to the log, then write result.json. Returns the result."""
    d = _dir(root, run_id)
    meta = json.loads((d / "meta.json").read_text())
    (d / "pid").write_text(str(os.getpid()))
    cache = ClaimCache(root)
    t0 = time.time()
    try:
        job = Job({}, meta["cmd"], timeout=meta["timeout"], label=run_id, tail=LOG_TAIL, log=str(d / "log"))
        o = run_all(cache.store, root, meta["commit"], [job], 1, backend=get_backend(root, meta["backend"]))[0]
        if o.cached:
            with open(d / "log", "ab") as fh:
                fh.write(f"[memoized outcome for this tree and command; not run again]\n{o.tail}".encode())
        res = {"status": o.status, "code": o.code, "seconds": round(o.seconds, 2), "cached": o.cached}
    except Exception as e:                  # whatever went wrong is the run's result, not a silent dead worker
        res = {"status": "error", "code": -1, "seconds": round(time.time() - t0, 2), "cached": False, "error": f"{type(e).__name__}: {e}"}
        with open(d / "log", "ab") as fh:
            fh.write(f"[error] {type(e).__name__}: {e}\n".encode())
    finally:
        cache.close()
    res["finished"] = time.time()
    _write(d / "result.json", res)
    return res


def spawn(root: Path, run_id: str) -> None:
    """Start the worker detached (own session, no inherited streams)."""
    subprocess.Popen([sys.executable, "-m", "fractal_harness.jobs", "--root", str(root), run_id],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True, close_fds=True)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def status(root: Path, run_id: str, tail_lines: int = 12) -> dict:
    d = _dir(root, run_id)
    meta = json.loads((d / "meta.json").read_text())
    out = {k: meta[k] for k in ("id", "cmd", "node", "commit", "backend", "timeout")}
    log = (d / "log").read_text(errors="replace") if (d / "log").is_file() else ""
    out["tail"] = "\n".join(log.splitlines()[-tail_lines:])
    if (d / "result.json").is_file():
        r = json.loads((d / "result.json").read_text())
        out.update(state=r["status"], code=r["code"], seconds=r["seconds"], cached=r["cached"],
                   **({"error": r["error"]} if "error" in r else {}))
        return out
    pidf = d / "pid"
    if pidf.is_file() and _alive(int(pidf.read_text() or 0)):
        out.update(state="running", seconds=round(time.time() - meta["created"], 1))
    elif not pidf.is_file() and time.time() - meta["created"] < STARTING_GRACE:
        out.update(state="starting", seconds=round(time.time() - meta["created"], 1))
    else:
        out.update(state="lost", seconds=round(time.time() - meta["created"], 1),
                   error="the worker is gone and wrote no result (killed?)")
    return out


def log_text(root: Path, run_id: str, tail: int | None = None) -> str:
    d = _dir(root, run_id)
    text = (d / "log").read_text(errors="replace") if (d / "log").is_file() else ""
    return "\n".join(text.splitlines()[-tail:]) + "\n" if tail else text


def render(s: dict) -> str:
    head = f"run {s['id']}: {s['state']}"
    if s["state"] not in ("running", "starting", "lost"):
        head += f" (exit {s['code']}, {s['seconds']}s" + (", memoized" if s.get("cached") else "") + ")"
    elif s["state"] == "running":
        head += f" ({s['seconds']}s so far)"
    out = [head, f"  cmd: {s['cmd']}", f"  at {s['commit'][:10]} on {s['backend']}" + (f", node {s['node']}" if s["node"] else "")]
    if s.get("error"):
        out.append(f"  error: {s['error']}")
    if s["state"] in ("running", "starting"):
        out.append(f"  poll again with: fractal run status {s['id']}   (output so far: fractal run log {s['id']})")
    if s["tail"]:
        out += ["  --- last output ---", *("  " + ln for ln in s["tail"].splitlines())]
    return "\n".join(out) + "\n"


def doctor_checks(root: Path) -> list[tuple[bool, str]]:
    """The configured jobs backend: nothing when there is no `[jobs]` section (behavior unchanged)."""
    from .config import ConfigError, load
    try:
        cfg = load(root)
        if not cfg.jobs:
            return []
        be = get_backend(root)
    except (ConfigError, BackendError) as e:
        return [(False, f"jobs backend [sound]: {e}")]
    if be.name == "local":
        p = subprocess.run(["git", "worktree", "list"], cwd=root, capture_output=True, text=True)
        return [(p.returncode == 0, "jobs backend local [sound]: `git worktree` works here (scratch worktrees)")]
    return be.doctor(root)


def main(argv: list[str] | None = None) -> int:
    """Worker entry point (`python -m fractal_harness.jobs --root R <id>`); not a user command."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("run_id")
    a = ap.parse_args(argv)
    execute_run(Path(a.root), a.run_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())

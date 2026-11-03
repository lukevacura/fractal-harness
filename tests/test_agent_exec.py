"""Phase 5: `fractal brief`, `report file|list`, the executor's backends, `fractal run`, and the doctor checks."""
import hashlib
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from fractal_harness import brief, executor, jobs, ledger, reports
from fractal_harness.cache import ClaimCache
from fractal_harness.cli import main
from fractal_harness.config import ConfigError, ConfigFileError, load
from fractal_harness.executor import Backend, BackendError, Job, Outcome, SshBackend, memo_key, run_all
from fractal_harness.symbols import SymbolError

from test_config import _cfg, _two_dirs
from test_nodes import _repo as nodes_repo
from test_mutate import _repo as mutate_repo


def _fake_ssh(tmp_path: Path, body: str = 'shift\nexec sh -c "$*"\n') -> str:
    f = tmp_path / "fakessh"
    f.write_text("#!/bin/sh\n" + body)
    f.chmod(f.stat().st_mode | stat.S_IEXEC)
    return str(f)


class Fake(Backend):
    name = "fake"

    def __init__(self, outcomes=None):
        self.calls, self.outcomes = [], list(outcomes or [])

    def run(self, root, commit, job, n=0):
        self.calls.append(job.cmd)
        o = self.outcomes.pop(0) if self.outcomes else Outcome("passed", 0, "ok", 0.1)
        if job.log:                                    # like a real backend, stream the output to the log
            Path(job.log).write_text(o.tail)
        return o


# --- brief ----------------------------------------------------------------------------------

def test_brief_is_deterministic_and_has_the_root_block_first(tmp_path):
    c = nodes_repo(tmp_path)
    a, b = brief.build(c, tmp_path, "child"), brief.build(c, tmp_path, "child")
    assert a == b and a["text"].endswith(f"brief-digest: {a['digest']}\n")
    t = a["text"]
    root_at = t.index("## Repo root")
    assert root_at < t.index("## Why this node is due") < t.index("## Task and acceptance") < t.index("## Spec:")
    assert str(tmp_path.resolve()) in t[root_at:t.index("## Why")] and "git rev-parse --show-toplevel" in t
    assert t.index("## Reporting") < t.index("## Heavy runs")


def test_brief_carries_only_this_nodes_premises_not_the_graph(tmp_path):
    c = nodes_repo(tmp_path)
    t = brief.build(c, tmp_path, "child")["text"]
    assert "Reproduce the ops bit for bit" in t                  # D18, which child implements
    assert "FMA changes rounding" not in t                       # D17: in the store, not child's premise
    assert "Exact windowed mean" in t                            # parent stats' contract
    assert "Grandparent promise" not in t and "Reference implementation" not in t   # not its parent's parent, not a `see`
    assert "fractal mutate child" in t and "fractal ledger record child" in t        # no private mutant scripts
    assert "fractal run" in t and "fractal report file" in t


def test_brief_says_why_due_from_status(tmp_path):
    c = nodes_repo(tmp_path)
    d = brief.build(c, tmp_path, "child")
    assert d["state"] == "pending" and "no verdicts recorded" in d["text"]


def test_brief_sections_are_overridable_and_extendable(tmp_path):
    c = nodes_repo(tmp_path)
    _cfg(tmp_path, '[brief]\ntask = "Just fix {node}."\nheavy_extra = "Use box B."\nreporting_extra = "Be brief."\n'
                   'report_dir = "scratch-out"\n')
    t = brief.build(c, tmp_path, "child")["text"]
    assert "Just fix child." in t and "Choose mutants" not in t              # replaced
    assert "Do not wait on long local background jobs" in t and "Use box B." in t   # extended
    assert "Be brief." in t
    p = brief.report_path(tmp_path, "child")
    assert p == (tmp_path / "scratch-out" / "child.md").resolve() and str(p) in t


def test_brief_report_path_default_is_untracked_and_absolute(tmp_path):
    nodes_repo(tmp_path)
    p = brief.report_path(tmp_path, "n")
    assert p.is_absolute() and p == (tmp_path / ".fractal/reports/scratch/n.md").resolve()


def test_brief_rejects_unregistered_and_bad_names(tmp_path):
    c = nodes_repo(tmp_path)
    with pytest.raises(SymbolError, match="not registered"):
        brief.build(c, tmp_path, "nope")
    with pytest.raises(SymbolError, match="bad node"):
        brief.build(c, tmp_path, "../x")


def test_brief_cli_out_json_and_digest_recorded(tmp_path, capsys):
    nodes_repo(tmp_path).close()
    out = tmp_path / "b.md"
    assert main(["--root", str(tmp_path), "brief", "child", "--out", str(out)]) == 0
    digest = out.read_text().rsplit("brief-digest: ", 1)[1].strip()
    assert brief.last_digest(tmp_path, "child") == digest
    capsys.readouterr()
    assert main(["--root", str(tmp_path), "brief", "child", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["digest"] == digest


# --- reports --------------------------------------------------------------------------------

def _filed(tmp_path):
    c, repo, docs = _two_dirs(tmp_path)
    _cfg(repo, '[roots]\ndocs = "../docs"\n[brief]\nreport_to = "docs:reports/{node}"\n')
    scratch = repo / ".fractal" / "reports" / "scratch"
    scratch.mkdir(parents=True)
    (scratch / "child.md").write_text("# Findings\nall good\n")
    return repo, docs, c, scratch / "child.md"


def test_report_file_copies_into_the_docs_root_with_provenance(tmp_path):
    repo, docs, c, f = _filed(tmp_path)
    brief.remember(repo, "child", "abc123")
    row = reports.file_report(c, repo, str(f), "child")
    dest = docs / "reports" / "child" / "child.md"
    assert Path(row["path"]) == dest.resolve()
    t = dest.read_text()
    assert t.startswith("<!-- fractal-report") and "node: child" in t and "brief-digest: abc123" in t
    assert f"commit: {ledger.resolve_commit(repo, 'HEAD')}" in t and "filed: 20" in t
    assert t.endswith("# Findings\nall good\n")
    assert [r["path"] for r in reports.list_reports(repo, "child")] == [str(dest.resolve())]
    assert reports.list_reports(repo, "other") == []


def test_report_file_refuses_escapes_and_overwrites(tmp_path):
    repo, docs, c, f = _filed(tmp_path)
    for to in ("docs:../evil.md", "docs:reports/../../evil.md", "../evil.md", "docs:/abs.md"):
        with pytest.raises(ConfigError):
            reports.file_report(c, repo, str(f), "child", to=to)
    assert not (docs.parent / "evil.md").exists() and not (repo.parent / "evil.md").exists()
    with pytest.raises(ConfigError, match="unknown root"):
        reports.file_report(c, repo, str(f), "child", to="nope:x.md")
    with pytest.raises(SymbolError, match="bad node"):
        reports.file_report(c, repo, str(f), "../child")
    reports.file_report(c, repo, str(f), "child")
    with pytest.raises(SymbolError, match="exists"):
        reports.file_report(c, repo, str(f), "child")
    reports.file_report(c, repo, str(f), "child", force=True)
    with pytest.raises(SymbolError, match="no such report"):
        reports.file_report(c, repo, str(repo / "missing.md"), "child")


def test_report_file_explicit_destination_forms_and_default(tmp_path):
    repo, docs, c, f = _filed(tmp_path)
    a = reports.file_report(c, repo, str(f), "child", to="docs:notes/final.md")
    assert Path(a["path"]) == (docs / "notes" / "final.md").resolve()
    b = reports.file_report(c, repo, str(f), "child", to="docs:notes/")
    assert Path(b["path"]) == (docs / "notes" / "child.md").resolve()
    (repo / "fractal.toml").write_text("")                         # no report_to: untracked default inside the repo
    d = reports.file_report(c, repo, str(f), "child")
    assert Path(d["path"]) == (repo / ".fractal/filed/child/child.md").resolve()


def test_report_cli(tmp_path, capsys):
    repo, docs, c, f = _filed(tmp_path)
    c.close()
    assert main(["--root", str(repo), "report", "file", str(f), "--node", "child"]) == 0
    assert (docs / "reports/child/child.md").is_file()
    assert main(["--root", str(repo), "report", "file", str(f), "--node", "child", "--to", "docs:../x.md"]) == 1
    capsys.readouterr()
    assert main(["--root", str(repo), "report", "list", "--node", "child"]) == 0
    assert "reports/child/child.md" in capsys.readouterr().out


# --- executor: local unchanged, memo, fake backend ------------------------------------------

def test_local_memo_key_is_the_legacy_key():
    j = Job({"a.py": "x"}, "pytest", "build", {"K": "v"})
    legacy = hashlib.sha256(json.dumps({"tree": "T", "edits": {"a.py": hashlib.sha256(b"x").hexdigest()}, "cmd": "pytest",
                                        "build": "build", "env": [("K", "v")]}, sort_keys=True).encode()).hexdigest()
    assert memo_key("T", j) == memo_key("T", j, "local") == legacy
    assert memo_key("T", j, "ssh") != legacy and memo_key("T", j, "ssh") != memo_key("T", j, "other")
    assert memo_key("T", j, "fake") == memo_key("T", Job({"a.py": "x"}, "pytest", "build", {"K": "v"}, tail=9, log="/x"), "fake")


def test_default_backend_is_local_and_still_memoizes(tmp_path):
    c = mutate_repo(tmp_path)
    sha = ledger.resolve_commit(tmp_path, "HEAD")
    j = Job({}, "python3 check.py", timeout=60)
    a = run_all(c.store, tmp_path, sha, [j])[0]
    b = run_all(c.store, tmp_path, sha, [j])[0]
    assert (a.status, a.cached, b.status, b.cached) == ("passed", False, "passed", True)


def test_fake_backend_runs_once_per_tree_and_keeps_namespaces_apart(tmp_path):
    c = mutate_repo(tmp_path)
    sha = ledger.resolve_commit(tmp_path, "HEAD")
    fb, other = Fake(), Fake()
    other.name = "fake2"
    j = Job({}, "echo hi")
    assert run_all(c.store, tmp_path, sha, [j], backend=fb)[0].cached is False
    assert run_all(c.store, tmp_path, sha, [j], backend=fb)[0].cached is True
    assert fb.calls == ["echo hi"]
    assert run_all(c.store, tmp_path, sha, [j], backend=other)[0].cached is False     # another backend: another environment
    assert run_all(c.store, tmp_path, sha, [j])[0].cached is False                    # and not the local memo
    run_all(c.store, tmp_path, sha, [j], backend=fb, use_memo=False)
    assert len(fb.calls) == 2


def test_timeouts_are_not_memoized(tmp_path):
    c = mutate_repo(tmp_path)
    sha = ledger.resolve_commit(tmp_path, "HEAD")
    fb = Fake([Outcome("timeout", -1, "", 1.0), Outcome("passed", 0, "", 1.0)])
    j = Job({}, "slow")
    assert run_all(c.store, tmp_path, sha, [j], backend=fb)[0].status == "timeout"
    r = run_all(c.store, tmp_path, sha, [j], backend=fb)[0]
    assert (r.status, r.cached, len(fb.calls)) == ("passed", False, 2)


def test_get_backend_selection(tmp_path, monkeypatch):
    _repo = mutate_repo(tmp_path)
    assert executor.get_backend(tmp_path).name == "local"
    monkeypatch.setitem(executor.BACKENDS, "fake", lambda cfg: Fake())
    _cfg(tmp_path, '[jobs]\nbackend = "fake"\n')
    assert executor.get_backend(tmp_path).name == "fake" and executor.get_backend(tmp_path, "local").name == "local"
    with pytest.raises(BackendError, match="unknown jobs backend"):
        executor.get_backend(tmp_path, "nope")
    _cfg(tmp_path, '[jobs]\nbackend = "ssh"\n')
    with pytest.raises(BackendError, match="host"):
        executor.get_backend(tmp_path)


def test_jobs_config_validation(tmp_path):
    _cfg(tmp_path, '[jobs]\nbackend = 3\n')
    with pytest.raises(ConfigFileError):
        load(tmp_path)
    _cfg(tmp_path, '[jobs]\nbackend = "ssh"\n[jobs.ssh]\nhost = "h"\noptions = ["-p", "2"]\nbogus = 1\n')
    c = load(tmp_path)
    assert c.jobs["ssh"] == {"host": "h", "options": ["-p", "2"]} and any("bogus" in w for w in c.warnings)
    _cfg(tmp_path, '[jobs.ssh]\nhost = ""\n')
    with pytest.raises(ConfigFileError):
        load(tmp_path)
    _cfg(tmp_path, '[brief]\ntask = 5\n')
    with pytest.raises(ConfigFileError):
        load(tmp_path)


# --- ssh backend, against a fake ssh client that runs the "remote" command locally ------------

def _ssh(tmp_path, body=None, **kw):
    return SshBackend("somehost", str(tmp_path / "remote"), ssh=_fake_ssh(tmp_path, *([body] if body else [])), **kw)


def test_ssh_backend_syncs_the_commit_applies_edits_and_returns_the_exit_code(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    c = mutate_repo(repo)
    sha = ledger.resolve_commit(repo, "HEAD")
    be = _ssh(tmp_path)
    ok = be.run(repo, sha, Job({}, "python3 check.py", timeout=60))
    assert ok.status == "passed"
    bad = be.run(repo, sha, Job({"calc.py": "def add(a, b):\n    return a - b\n\ndef neg(a):\n    return -a\n"},
                                "python3 check.py", timeout=60))
    assert (bad.status, bad.code) == ("failed", 1) and "add" in bad.tail
    nb = be.run(repo, sha, Job({"calc.py": "def (:\n"}, "python3 check.py", "python3 -m py_compile calc.py", timeout=60))
    assert nb.status == "build_failed"
    e = be.run(repo, sha, Job({}, "echo $FRACTAL_SCRATCH $X", env={"X": "a b-{job}"}, timeout=60), n=3)
    assert e.status == "passed" and e.tail.strip() == "1 a b-3"
    assert be.run(repo, sha, Job({}, "exit 255", timeout=60)).code == 255          # a job's 255 is a job failure
    assert list((tmp_path / "remote").iterdir()) == []                              # remote scratch removed
    assert not (repo / "calc.py.orig").exists()


def test_ssh_backend_prefix_and_timeout(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    mutate_repo(repo)
    sha = ledger.resolve_commit(repo, "HEAD")
    be = _ssh(tmp_path, prefix="env WRAP=1")
    assert be.run(repo, sha, Job({}, "echo $WRAP", timeout=60)).tail.strip() == "1"
    t0 = time.time()
    o = _ssh(tmp_path).run(repo, sha, Job({}, "sleep 30", timeout=2))
    assert o.status == "timeout" and time.time() - t0 < 15


def test_ssh_failure_is_a_backend_error_not_a_job_failure(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    mutate_repo(repo)
    sha = ledger.resolve_commit(repo, "HEAD")
    with pytest.raises(BackendError, match="failed"):
        _ssh(tmp_path, 'echo "ssh: Could not resolve hostname" >&2\nexit 255\n').run(repo, sha, Job({}, "true", timeout=30))


def test_ssh_failure_during_the_run_or_a_lost_marker_is_a_backend_error(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    mutate_repo(repo)
    sha = ledger.resolve_commit(repo, "HEAD")
    sync_ok = 'case "$*" in *"sh -c"*) {} ;; esac\nshift\nexec sh -c "$*"\n'
    with pytest.raises(BackendError, match="exit 255"):
        _ssh(tmp_path, sync_ok.format("echo dropped >&2; exit 255")).run(repo, sha, Job({}, "true", timeout=30))
    with pytest.raises(BackendError, match="failed"):       # exit 0 but the exit marker never arrived
        _ssh(tmp_path, sync_ok.format("echo partial; exit 0")).run(repo, sha, Job({}, "true", timeout=30))


def test_brief_report_path_from_a_linked_worktree_points_at_the_main_checkout(tmp_path):
    main_repo = tmp_path / "main"
    main_repo.mkdir()
    mutate_repo(main_repo).close()
    wt = tmp_path / "wt"
    subprocess.run(["git", "worktree", "add", "-q", "--detach", str(wt)], cwd=main_repo, check=True)
    assert brief.report_path(wt, "n") == (main_repo / ".fractal/reports/scratch/n.md").resolve()


def test_ssh_outcomes_memoize_by_tree_under_their_own_key(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    c = mutate_repo(repo)
    sha = ledger.resolve_commit(repo, "HEAD")
    be = _ssh(tmp_path)
    j = Job({}, "python3 check.py", timeout=60)
    assert run_all(c.store, repo, sha, [j], backend=be)[0].cached is False
    assert run_all(c.store, repo, sha, [j], backend=be)[0].cached is True
    assert run_all(c.store, repo, sha, [j])[0].cached is False


# --- fractal run ---------------------------------------------------------------------------

def test_run_records_status_and_log_with_a_fake_backend(tmp_path, monkeypatch):
    c = mutate_repo(tmp_path)
    fb = Fake([Outcome("failed", 3, "boom\nlast line", 2.0)])
    monkeypatch.setitem(executor.BACKENDS, "fake", lambda cfg: fb)
    rid = jobs.create(c, tmp_path, "make heavy", node="n", backend="fake", timeout=99)
    assert jobs.status(tmp_path, rid)["state"] in ("starting", "lost")            # no worker yet
    res = jobs.execute_run(tmp_path, rid)
    s = jobs.status(tmp_path, rid)
    assert res["status"] == "failed" and (s["state"], s["code"], s["backend"], s["node"]) == ("failed", 3, "fake", "n")
    assert fb.calls == ["make heavy"] and "last line" in s["tail"] and "boom" in jobs.log_text(tmp_path, rid)
    rid2 = jobs.create(c, tmp_path, "make heavy", node="n", backend="fake")
    jobs.execute_run(tmp_path, rid2)                                              # failed outcomes are memoized too
    assert jobs.status(tmp_path, rid2)["cached"] is True and len(fb.calls) == 1
    assert "memoized" in jobs.log_text(tmp_path, rid2)
    with pytest.raises(SymbolError, match="no such run"):
        jobs.status(tmp_path, "deadbeef")
    with pytest.raises(SymbolError, match="bad run id"):
        jobs.status(tmp_path, "../x")


def test_run_backend_error_is_recorded_as_error(tmp_path, monkeypatch):
    c = mutate_repo(tmp_path)

    class Boom(Fake):
        def run(self, *a, **k):
            raise BackendError("host down")
    monkeypatch.setitem(executor.BACKENDS, "fake", lambda cfg: Boom())
    rid = jobs.create(c, tmp_path, "x", backend="fake")
    assert jobs.execute_run(tmp_path, rid)["status"] == "error"
    s = jobs.status(tmp_path, rid)
    assert s["state"] == "error" and "host down" in s["error"] and "host down" in jobs.log_text(tmp_path, rid)


def test_run_lost_worker_is_reported(tmp_path):
    c = mutate_repo(tmp_path)
    rid = jobs.create(c, tmp_path, "x")
    d = jobs.runs_dir(tmp_path) / rid
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    (d / "pid").write_text(str(p.pid))
    assert jobs.status(tmp_path, rid)["state"] == "lost"
    (d / "pid").write_text(str(os.getpid()))
    assert jobs.status(tmp_path, rid)["state"] == "running"


def test_run_cli_foreground_detach_and_poll(tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    c = mutate_repo(repo)
    c.close()
    _cfg(repo, f'[jobs]\nbackend = "ssh"\n[jobs.ssh]\nhost = "h"\ndir = "{tmp_path / "remote"}"\nssh = "{_fake_ssh(tmp_path)}"\n')
    R = ["--root", str(repo)]
    assert main([*R, "run", "python3", "check.py"]) == 0
    assert "passed" in capsys.readouterr().out
    assert main([*R, "run", "--timeout", "30", "sh", "-c", "exit 4"]) == 1
    assert "failed (exit 4" in capsys.readouterr().out
    assert main([*R, "run", "--detach", "sh", "-c", "echo begun; sleep 2; echo finished"]) == 0
    rid = capsys.readouterr().out.strip()
    assert main([*R, "run", "status", rid]) == 0
    o = capsys.readouterr().out
    assert any(w in o for w in ("running", "starting", "passed")), o
    for _ in range(100):
        main([*R, "--json", "run", "status", rid])
        s = json.loads(capsys.readouterr().out)
        if s["state"] not in ("starting", "running"):
            break
        time.sleep(0.2)
    assert s["state"] == "passed" and s["backend"] == "ssh"
    assert main([*R, "run", "log", rid]) == 0 and "finished" in capsys.readouterr().out
    assert main([*R, "run", "--tail", "1", "log", rid]) == 0
    assert main([*R, "run", "log", "nosuchid"]) == 1
    assert main([*R, "run"]) == 1


def test_run_unknown_backend_fails_cleanly(tmp_path):
    mutate_repo(tmp_path).close()
    assert main(["--root", str(tmp_path), "run", "--backend", "zzz", "true"]) == 1


# --- doctor ---------------------------------------------------------------------------------

def test_doctor_has_nothing_to_say_without_config(tmp_path):
    mutate_repo(tmp_path)
    assert jobs.doctor_checks(tmp_path) == [] and reports.doctor_checks(tmp_path) == []


def test_doctor_ssh_reachable_and_not(tmp_path):
    mutate_repo(tmp_path)
    rd = tmp_path / "rd"
    _cfg(tmp_path, f'[jobs]\nbackend = "ssh"\n[jobs.ssh]\nhost = "h"\ndir = "{rd}"\nssh = "{_fake_ssh(tmp_path)}"\nprefix = "env"\n')
    ok = jobs.doctor_checks(tmp_path)
    assert all(o for o, _ in ok) and any("reachable" in m for _, m in ok) and any("writable" in m for _, m in ok)
    assert all("[sound]" in m for _, m in ok) and rd.is_dir()
    _cfg(tmp_path, f'[jobs]\nbackend = "ssh"\n[jobs.ssh]\nhost = "h"\nssh = "{_fake_ssh(tmp_path, "echo no route >&2; exit 255")}"\n')
    bad = jobs.doctor_checks(tmp_path)
    assert (False, "jobs backend ssh [sound]: h NOT reachable (no route)") in bad
    _cfg(tmp_path, '[jobs]\nbackend = "ssh"\n[jobs.ssh]\nhost = "h"\nssh = "/no/such/ssh-client"\n')
    assert jobs.doctor_checks(tmp_path)[0][0] is False
    _cfg(tmp_path, '[jobs]\nbackend = "ssh"\n')
    assert jobs.doctor_checks(tmp_path)[0][0] is False


def test_doctor_local_and_report_dir_warning(tmp_path):
    mutate_repo(tmp_path)
    _cfg(tmp_path, '[jobs]\nbackend = "local"\n[brief]\nreport_dir = "out"\n')
    assert jobs.doctor_checks(tmp_path)[0][0] is True
    (ok, msg), = reports.doctor_checks(tmp_path)
    assert ok and "WARN [heuristic]" in msg                                     # warns, never fails
    _cfg(tmp_path, '[brief]\nreport_dir = ".fractal/x"\n')
    assert "is git-ignored" in reports.doctor_checks(tmp_path)[0][1]

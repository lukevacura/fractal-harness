import subprocess
from pathlib import Path

import pytest

from fractal_harness import ledger, reclaim
from fractal_harness.cache import ClaimCache
from fractal_harness.cli import main
from fractal_harness.nodes import parse_header
from fractal_harness.symbols import SymbolError

LIB = '''def used():
    return 1


@staticmethod
def dead_fn(x):
    """Nothing calls this."""
    return x + 1


class Dead:
    def m(self):
        return 2


def after():
    return 3
'''
CHECK = "import lib\nassert lib.used() == 1\nassert lib.after() == 3\n"
SPEC = "---\nguarantee: g\nexports: lib.py::used, lib.py::dead_fn, lib.py::Gone\n---\n# N\n"


def git(cwd, *a):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=cwd, check=True, capture_output=True)


def _repo(tmp_path: Path):
    git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / ".git" / "info" / "exclude").write_text(".fractal/\n")
    (tmp_path / "lib.py").write_text(LIB)
    (tmp_path / "check.py").write_text(CHECK)
    (tmp_path / "main.py").write_text("import lib\nprint(lib.used(), lib.after())\n")
    (tmp_path / "n.md").write_text(SPEC)
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "c1")
    c = ClaimCache(tmp_path)
    ledger.register_node(c, tmp_path, "n", "n.md")
    return c


CLASSES = {"metadata", "code", "store"}


def test_item_spans_cover_decorators_docs_bodies_and_rust_impl_blocks():
    assert reclaim.item_spans("lib.py", LIB, "dead_fn") == [(4, 7)]
    assert reclaim.item_spans("lib.py", LIB, "Dead") == [(10, 12)]
    rs = '/// Doc.\n#[derive(Debug)]\npub struct Pair {\n    a: u32,\n}\n\nimpl Pair {\n    pub fn new() -> Self {\n        Pair { a: 1 }\n    }\n}\n\npub const K: u32 = 3;\npub fn keep() {}\n'
    assert reclaim.item_spans("x.rs", rs, "Pair") == [(0, 4), (6, 10)]
    assert reclaim.remove_spans(rs, reclaim.item_spans("x.rs", rs, "Pair")) == "pub const K: u32 = 3;\npub fn keep() {}\n"
    assert reclaim.item_spans("x.rs", rs, "K") == [(12, 12)] and reclaim.item_spans("x.rs", rs, "Nope") == []


def test_sweep_finds_candidates_and_blocks_what_another_worktree_uses(tmp_path):
    c = _repo(tmp_path)
    wt = tmp_path.parent / (tmp_path.name + "_wt")
    git(tmp_path, "worktree", "add", "-q", "-b", "node-x", str(wt))
    (wt / "other.py").write_text("import lib\nlib.dead_fn(1)\n")                      # a live branch uses dead_fn
    cands = reclaim.sweep(c, tmp_path, CLASSES)
    by = {x.what: x for x in cands}
    assert by["lib.py::Gone"].cls == "metadata" and by["lib.py::Dead"].cls == "code"
    assert "node-x" in by["lib.py::dead_fn"].gate and not by["lib.py::Dead"].gate
    assert "BLOCKED" in reclaim.render(cands)


def test_verify_apply_and_nothing_is_committed(tmp_path):
    c = _repo(tmp_path)
    cands = reclaim.sweep(c, tmp_path, CLASSES)
    info = reclaim.verify(c, tmp_path, cands, cmd="python3 check.py", workers=2)
    verdicts = {x.what: x.verdict for x in cands if x.cls == "code"}
    assert verdicts == {"lib.py::dead_fn": "passed", "lib.py::Dead": "passed"} and info["combined"] == "passed"
    assert (tmp_path / "lib.py").read_text() == LIB                                       # verify never touches the tree
    done = reclaim.apply(c, tmp_path, cands)
    assert "deleted lib.py::Dead" in done and any("Gone" in d for d in done)
    left = (tmp_path / "lib.py").read_text()
    assert "dead_fn" not in left and "Dead" not in left and "staticmethod" not in left and "def after" in left
    assert parse_header((tmp_path / "n.md").read_text()).exports == ["lib.py::used"]
    assert subprocess.run(["python3", "check.py"], cwd=tmp_path).returncode == 0
    assert subprocess.run(["git", "log", "--oneline"], cwd=tmp_path, capture_output=True, text=True).stdout.count("\n") == 1
    assert [a["action"] for a in c.store.audit("n")].count("reclaim") >= 2


def test_a_deletion_the_tests_need_is_rejected_and_not_applied(tmp_path):
    c = _repo(tmp_path)
    (tmp_path / "test_uses.py").write_text("import lib\nlib.dead_fn(1)\n")                # only a test uses it
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "test uses it")
    cands = reclaim.sweep(c, tmp_path, {"code"})
    assert "tested only" in {x.what: x.why for x in cands}["lib.py::dead_fn"]
    reclaim.verify(c, tmp_path, cands, cmd="python3 check.py && python3 test_uses.py", workers=2)
    assert {x.what: x.verdict for x in cands}["lib.py::dead_fn"] == "failed"
    reclaim.apply(c, tmp_path, cands)
    assert "def dead_fn" in (tmp_path / "lib.py").read_text() and "class Dead" not in (tmp_path / "lib.py").read_text()


def test_verify_refuses_a_dirty_tree_or_failing_baseline_and_public_items_are_never_candidates(tmp_path):
    c = _repo(tmp_path)
    (tmp_path / ".fractal-public").write_text("Dead\n")
    assert "lib.py::Dead" not in {x.what for x in reclaim.sweep(c, tmp_path, CLASSES)}
    (tmp_path / "lib.py").write_text(LIB + "\n")
    with pytest.raises(SymbolError, match="uncommitted"):
        reclaim.verify(c, tmp_path, reclaim.sweep(c, tmp_path, {"code"}), cmd="python3 check.py")
    git(tmp_path, "checkout", "-q", "lib.py")
    with pytest.raises(SymbolError, match="do not pass on HEAD"):
        reclaim.verify(c, tmp_path, reclaim.sweep(c, tmp_path, {"code"}), cmd="python3 -c 'raise SystemExit(1)'")


def test_store_candidates_orphan_verdicts_and_missing_spec(tmp_path, capsys):
    c = _repo(tmp_path)
    ledger.record(c, tmp_path, "n", file="lib.py", span="2", mutant="m", result="killed", tests=["t"])
    ledger.record(c, tmp_path, "ghost", file="lib.py", span="2", mutant="m", result="killed", tests=["t"])
    (tmp_path / "n.md").unlink()
    got = {(x.what, x.action) for x in reclaim.sweep(c, tmp_path, {"store"})}
    assert got == {("n", "deregister"), ("ghost", "drop_verdicts")}
    c.close()
    assert main(["--root", str(tmp_path), "reclaim", "--class", "store", "--apply"]) == 0
    assert "applied to the working tree" in capsys.readouterr().out
    c = ClaimCache(tmp_path)
    assert c.store.verdict_nodes() == set() and c.store.get_node("n") is None
    assert {a["action"] for a in c.store.audit()} >= {"reclaim"}


def test_cli_plan_then_apply(tmp_path, capsys):
    c = _repo(tmp_path)
    c.close()
    assert main(["--root", str(tmp_path), "reclaim"]) == 0
    out = capsys.readouterr().out
    assert "Code nothing reaches" in out and "not run" in out and (tmp_path / "lib.py").read_text() == LIB
    assert main(["--root", str(tmp_path), "reclaim", "--apply", "--class", "code"]) == 1          # no test command given
    assert "give the test command" in capsys.readouterr().err
    assert main(["--root", str(tmp_path), "reclaim", "--apply", "--class", "code", "--cmd", "python3 check.py"]) == 0
    assert "def dead_fn" not in (tmp_path / "lib.py").read_text()

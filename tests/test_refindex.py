import subprocess
from pathlib import Path

from fractal_harness.cli import main
from fractal_harness.refindex import Index, RefIndex


def git(cwd, *a):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=cwd, check=True, capture_output=True)


def _repos(tmp_path: Path):
    main_ = tmp_path / "main"
    main_.mkdir()
    git(main_, "init", "-q", "-b", "main")
    (main_ / "src").mkdir()
    (main_ / "src" / "rows.rs").write_text("pub fn row_sum() {}\n")
    (main_ / "src" / "use.rs").write_text("fn a() { row_sum(); }\n")
    (main_ / "notes.md").write_text("row_sum is not source\n")
    git(main_, "add", "-A")
    git(main_, "commit", "-qm", "c1")
    wt = tmp_path / "wt"
    git(main_, "worktree", "add", "-q", "-b", "node-batch", str(wt))
    (wt / "src" / "batch.rs").write_text("fn b() { row_sum(); }\n")          # a new caller, uncommitted and untracked
    git(main_, "branch", "node-idle", "main")
    git(main_, "checkout", "-q", "node-idle")                                 # leave a tip with no worktree of its own
    git(main_, "checkout", "-q", "main")
    return main_, wt


def test_index_matches_whole_identifiers_only():
    idx = Index({"a.rs": "fn row_sum() {}", "b.rs": "fn row_sum_2() {}", "tests/t.rs": "row_sum();"})
    assert idx.files("row_sum") == ["a.rs", "tests/t.rs"]
    assert idx.files("row_sum", exclude="a.rs", tests=False) == []
    assert idx.mentions("row_sum_2", "b.rs") and not idx.mentions("row_sum", "b.rs")


def test_where_sees_each_live_worktree_including_uncommitted_files(tmp_path):
    main_, wt = _repos(tmp_path)
    idx = RefIndex.load(main_)
    assert [t.label for t in idx.trees] == ["main", "node-batch"] and idx.current.label == "main"
    assert idx.where("row_sum", exclude="src/rows.rs") == {"main": ["src/use.rs"],
                                                           "node-batch": ["src/batch.rs", "src/use.rs"]}
    assert idx.elsewhere("row_sum", exclude="src/rows.rs") == {"node-batch": ["src/batch.rs", "src/use.rs"]}
    assert "notes.md" not in str(idx.where("row_sum"))                       # source files only


def test_branch_tips_without_a_worktree_are_read_from_git_on_request(tmp_path):
    main_, wt = _repos(tmp_path)
    git(main_, "checkout", "-q", "-b", "node-tip")
    (main_ / "src" / "tip.rs").write_text("fn t() { row_sum(); }\n")
    git(main_, "add", "-A")
    git(main_, "commit", "-qm", "tip")
    git(main_, "checkout", "-q", "main")
    assert "node-tip" not in RefIndex.load(main_).where("row_sum")
    idx = RefIndex.load(main_, branches=True)
    assert "src/tip.rs" in idx.where("row_sum")["node-tip"] and "node-idle" in idx.where("row_sum")


def test_refs_command(tmp_path, capsys):
    main_, wt = _repos(tmp_path)
    assert main(["--root", str(main_), "refs", "row_sum", "--not-in", "src/rows.rs"]) == 0
    out = capsys.readouterr().out
    assert "main (this checkout): src/use.rs" in out and "node-batch: src/batch.rs, src/use.rs" in out
    assert main(["--root", str(main_), "refs", "nothing_here"]) == 0
    assert "no mentions" in capsys.readouterr().out

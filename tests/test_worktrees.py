import subprocess
from pathlib import Path

from fractal_harness import ledger, worktrees
from fractal_harness.cache import ClaimCache


def g(cwd, *a):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=cwd,
                          capture_output=True, text=True, check=True).stdout.strip()


def _main(tmp_path):
    m = tmp_path / "main"
    m.mkdir()
    g(m, "init", "-q", "-b", "main")
    (m / "a.txt").write_text("1\n")
    g(m, "add", "-A")
    g(m, "commit", "-qm", "c1")
    return m


def _wt(m, name, *, commit=True, extra="x\n", fname="a.txt"):
    """A worktree on branch `name` with one commit of its own (a.txt = extra)."""
    wt = m.parent / name
    g(m, "worktree", "add", "-q", "-b", name, str(wt))
    if commit:
        (wt / fname).write_text(extra)
        g(wt, "add", "-A")
        g(wt, "commit", "-qm", name)
    return wt


def _by(trees):
    return {Path(t.path).name: t for t in trees}


def _merge(m, name):
    g(m, "merge", "-q", "--no-ff", "-m", f"merge {name}", name)


def test_merged_clean_worktree_is_removable_and_unmerged_is_kept(tmp_path):
    m = _main(tmp_path)
    _wt(m, "done")
    _wt(m, "open", extra="y\n")
    _merge(m, "done")
    t = _by(worktrees.plan(ClaimCache(m), m))
    assert t["done"].verdict == "remove" and t["done"].basis == "ancestor"
    assert t["open"].verdict == "keep" and "not in the base" in t["open"].why
    assert t["main"].verdict == "keep"


def test_a_fresh_worktree_at_the_base_is_not_swallowed(tmp_path):
    m = _main(tmp_path)
    _wt(m, "fresh", commit=False)
    t = _by(worktrees.plan(ClaimCache(m), m))
    assert t["fresh"].verdict == "keep" and "just started" in t["fresh"].why


def test_uncommitted_work_not_in_base_keeps_the_worktree(tmp_path):
    m = _main(tmp_path)
    wt = _wt(m, "d")
    _merge(m, "d")
    (wt / "new.txt").write_text("precious\n")            # untracked
    t = _by(worktrees.plan(ClaimCache(m), m))["d"]
    assert t.verdict == "keep" and "not in the base" in t.why and "new.txt" in t.why
    (wt / "new.txt").unlink()
    (wt / "a.txt").write_text("edited after merge\n")    # tracked change the base lacks
    assert _by(worktrees.plan(ClaimCache(m), m))["d"].verdict == "keep"


def test_uncommitted_changes_already_in_base_byte_for_byte_are_swallowed(tmp_path):
    m = _main(tmp_path)
    wt = _wt(m, "d")
    _merge(m, "d")
    (m / "late.txt").write_text("same\n")
    g(m, "add", "-A")
    g(m, "commit", "-qm", "late")                         # base now has late.txt
    (wt / "late.txt").write_text("same\n")                # the worktree's own uncommitted copy
    (wt / "a.txt").write_text("x\n")                      # equals base (no change vs. its tip, merged)
    t = _by(worktrees.plan(ClaimCache(m), m))["d"]
    assert t.verdict == "remove" and "already in the base" in t.why
    (wt / "late.txt").write_text("different\n")           # one byte of difference is enough to keep it
    assert _by(worktrees.plan(ClaimCache(m), m))["d"].verdict == "keep"


def test_a_deletion_is_swallowed_only_if_the_base_deleted_it_too(tmp_path):
    m = _main(tmp_path)
    (m / "gone.txt").write_text("g\n")
    g(m, "add", "-A")
    g(m, "commit", "-qm", "gone")
    wt = _wt(m, "d")
    _merge(m, "d")
    (wt / "gone.txt").unlink()
    assert _by(worktrees.plan(ClaimCache(m), m))["d"].verdict == "keep"      # base still has it
    g(m, "rm", "-q", "gone.txt")
    g(m, "commit", "-qm", "rm")
    assert _by(worktrees.plan(ClaimCache(m), m))["d"].verdict == "remove"


def test_locked_and_current_checkouts_are_kept(tmp_path):
    m = _main(tmp_path)
    a = _wt(m, "locked")
    _merge(m, "locked")
    g(m, "worktree", "lock", str(a))
    assert _by(worktrees.plan(ClaimCache(m), m))["locked"].why == "locked"
    g(m, "worktree", "unlock", str(a))
    t = _by(worktrees.plan(ClaimCache(a), a))                 # running from inside it
    assert t["locked"].verdict == "keep" and "running in" in t["locked"].why


def test_squash_merge_needs_the_flag_and_is_labelled_heuristic(tmp_path):
    m = _main(tmp_path)
    _wt(m, "sq")
    g(m, "merge", "-q", "--squash", "sq")
    g(m, "commit", "-qm", "squashed")
    assert _by(worktrees.plan(ClaimCache(m), m))["sq"].verdict == "keep"
    t = _by(worktrees.plan(ClaimCache(m), m, squashed=True))["sq"]
    assert t.verdict == "remove" and t.basis == "patch-id" and "HEURISTIC" in t.why


def test_into_overrides_the_base(tmp_path):
    m = _main(tmp_path)
    _wt(m, "d")
    _merge(m, "d")
    g(m, "branch", "older", "HEAD~1")
    assert _by(worktrees.plan(ClaimCache(m), m, into="older"))["d"].verdict == "keep"


def test_apply_removes_the_directory_and_keeps_the_branch_by_default(tmp_path):
    m = _main(tmp_path)
    wt = _wt(m, "d")
    other = _wt(m, "open", extra="y\n")
    _merge(m, "d")
    trees = worktrees.plan(ClaimCache(m), m)
    log = worktrees.apply(m, trees)
    assert not wt.exists() and other.exists()
    assert "refs/heads/d" in g(m, "for-each-ref", "refs/heads/d") and "removed worktree" in log[0]


def test_delete_branches_spares_a_branch_a_node_registers(tmp_path):
    m = _main(tmp_path)
    (m / "n.md").write_text("# Node\n\n## Guarantee\ng\n")
    g(m, "add", "-A")
    g(m, "commit", "-qm", "spec")
    wa, wb = _wt(m, "reg"), _wt(m, "plain", extra="z\n", fname="plain.txt")
    c = ClaimCache(m)
    ledger.register_node(c, m, "n", "n.md", branch="reg")
    _merge(m, "reg")
    _merge(m, "plain")
    trees = worktrees.plan(c, m)
    t = _by(trees)
    assert t["reg"].keep_branch and "n" in t["reg"].nodes and not t["plain"].keep_branch
    worktrees.apply(m, trees, delete_branches=True)
    assert not wa.exists() and not wb.exists()
    branches = g(m, "branch", "--format=%(refname:short)").split()
    assert "reg" in branches and "plain" not in branches


def test_a_missing_directory_is_pruned(tmp_path):
    import shutil
    m = _main(tmp_path)
    wt = _wt(m, "d")
    shutil.rmtree(wt)
    trees = worktrees.plan(ClaimCache(m), m)
    assert _by(trees)["d"].basis == "missing"
    worktrees.apply(m, trees)
    assert len(g(m, "worktree", "list").splitlines()) == 1


def test_cli_lists_by_default_and_changes_nothing(tmp_path, capsys):
    from fractal_harness.cli import main
    m = _main(tmp_path)
    wt = _wt(m, "d")
    _merge(m, "d")
    assert main(["--root", str(m), "worktrees"]) == 0
    assert "1 of 1 linked worktree(s) removable" in capsys.readouterr().out and wt.exists()
    assert main(["--root", str(m), "worktrees", "--apply"]) == 0
    assert not wt.exists()


def test_a_staged_rename_is_never_judged_swallowed(tmp_path):
    m = _main(tmp_path)
    wt = _wt(m, "d")
    _merge(m, "d")
    (m / "b.txt").write_text("x\n")                      # the base already has the rename's target, identical
    g(m, "add", "-A")
    g(m, "commit", "-qm", "b")
    g(wt, "mv", "a.txt", "b.txt")
    t = _by(worktrees.plan(ClaimCache(m), m))["d"]
    assert t.verdict == "keep" and "not in the base" in t.why


def test_squashed_requires_every_commit_to_have_a_twin(tmp_path):
    m = _main(tmp_path)
    wt = _wt(m, "two", fname="one.txt")
    (wt / "two.txt").write_text("2\n")
    g(wt, "add", "-A")
    g(wt, "commit", "-qm", "second")
    (m / "moved.txt").write_text("m\n")                   # the base moves on, so a pick is a new commit, not the same hash
    g(m, "add", "-A")
    g(m, "commit", "-qm", "moved")
    first = g(wt, "rev-parse", "HEAD~1")
    g(m, "cherry-pick", first)                            # only the first commit reaches the base
    assert _by(worktrees.plan(ClaimCache(m), m, squashed=True))["two"].verdict == "keep"
    g(m, "cherry-pick", g(wt, "rev-parse", "HEAD"))
    assert _by(worktrees.plan(ClaimCache(m), m, squashed=True))["two"].verdict == "remove"


def test_branches_never_force_deletes_a_branch_git_calls_unmerged(tmp_path):
    m = _main(tmp_path)
    _wt(m, "sq")
    g(m, "merge", "-q", "--squash", "sq")
    g(m, "commit", "-qm", "squashed")
    trees = worktrees.plan(ClaimCache(m), m, squashed=True)
    log = worktrees.apply(m, trees, delete_branches=True)
    assert "sq" in g(m, "branch", "--format=%(refname:short)").split()      # `-d` refuses: not an ancestor
    assert any("kept branch sq" in l for l in log)


def test_a_file_that_appears_after_the_plan_is_not_lost(tmp_path):
    m = _main(tmp_path)
    wt = _wt(m, "d")
    _merge(m, "d")
    trees = worktrees.plan(ClaimCache(m), m)
    (wt / "late.txt").write_text("written after the plan\n")
    log = worktrees.apply(m, trees)
    assert wt.exists() and (wt / "late.txt").exists() and any("git refused" in l for l in log)

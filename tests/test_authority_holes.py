"""Holes a review reproduced: ways an agent reached a root decision, or a change the law forbids, with no human.
Each test is the reviewer's repro, now refused."""
import json
import subprocess

import pytest

from fractal_harness import authority, config, root_signal
from fractal_harness.authority import AuthorityError
from fractal_harness.cache import ClaimCache
from fractal_harness.cli import main
from fractal_harness.store_guard import _human_verb

from test_authority import NO_LEGACY, NO_LEGACY_A, _propose, _repo
from test_store_guard import _bash, _blocked, _config, _policy_blocked, _store, _tool


def _run(tmp_path, *argv):
    return main(["--root", str(tmp_path), *argv])


# --- naming yourself root ----------------------------------------------------------------------------------------

def test_accept_as_root_is_the_human_path_not_an_agent_accept(tmp_path, monkeypatch):
    c = _repo(tmp_path, policy="local")
    p = _propose(c, "no legacy in src", NO_LEGACY)
    with pytest.raises(AuthorityError, match="accepting as root is the human's"):
        authority.accept_as(c, tmp_path, p.id, "root", "local")
    c.close()
    asked = []
    monkeypatch.setattr(root_signal, "confirm", lambda root, what, yes=False: asked.append(what))
    assert _run(tmp_path, "accept", p.id, "--as", "root") == 0
    assert asked, "--as root must ask the human"
    c = ClaimCache(tmp_path)
    assert c.store.rule_history(p.id)[-1]["who"] == "human"
    c.close()


@pytest.mark.parametrize("args", [
    ["accept", "X", "--as", "A", "--as", "root"],
    ["accept", "X", "--as", "A", "--as=root"],
    ["accept", "X", "--as", "$(echo root)"],
    ["accept", "X", "--as", "`echo root`"],
    ["override", "X", "--by", "A", "--by", "root", "--reason", "r"],
    ["change", "X", "--drop", "--by", "A", "--by", "root", "--reason", "r"],
    ["conflict", "resolve", "c1", "keep", "--as", "A", "--as", "root", "--reason", "r"],
])
def test_a_repeated_or_computed_option_is_treated_as_root(args):
    assert _human_verb(args), args


def test_a_single_node_option_is_still_an_agent_verb():
    assert _human_verb(["accept", "X", "--as", "A"]) is None
    assert _human_verb(["change", "X", "--drop", "--by", "A", "--reason", "r"]) is None


def test_ratchet_by_root_is_refused(tmp_path):
    c = _repo(tmp_path, policy=None)
    r = _propose(c, "no legacy (debt)", {**NO_LEGACY, "expect": {"max": 3}, "debt_target": "absent"})
    c.set_rule(r.id, "enforced")
    with pytest.raises(AuthorityError, match="acting as root"):
        authority.judge(c, "root", {r.id: {**r.probe, "expect": {"max": 2}}})
    c.close()


# --- a ratchet never breaks the tree ----------------------------------------------------------------------------

def test_a_ratchet_below_todays_count_or_a_payoff_while_violated_is_refused(tmp_path):
    c = _repo(tmp_path, policy=None)
    (tmp_path / "src" / "old.py").write_text("import legacy\n")
    r = _propose(c, "no legacy (debt)", {**NO_LEGACY, "expect": {"max": 1}, "debt_target": "absent"})
    c.set_rule(r.id, "enforced")
    with pytest.raises(AuthorityError, match="would not hold now"):
        authority.judge(c, "anyone", {r.id: {**r.probe, "expect": {"max": 0}}})
    with pytest.raises(AuthorityError, match="would not hold now"):
        authority.judge(c, "anyone", {r.id: dict(NO_LEGACY)})           # "paid off" while still violated
    (tmp_path / "src" / "old.py").unlink()
    authority.judge(c, "anyone", {r.id: dict(NO_LEGACY)})               # now it is paid off
    c.close()


# --- the human's override stays the human's ---------------------------------------------------------------------

def test_a_node_cannot_undo_the_humans_override(tmp_path):
    c = _repo(tmp_path, policy="local")
    e = _propose(c, "no legacy in mid", NO_LEGACY_A, ["src/mid.py"])
    authority.accept_as(c, tmp_path, e.id, "mid", "local")
    authority.override(c, e.id, "root", "the human says no", "local")
    with pytest.raises(AuthorityError, match="demoted by the human"):
        authority.override(c, e.id, "top", "bring it back", "local", undo=True)
    assert authority.override(c, e.id, "root", "I changed my mind", "local", undo=True).enforced
    c.close()


# --- the hook and case-insensitive file systems ---------------------------------------------------------------

@pytest.mark.parametrize("rel", [".FRACTAL/edges.db", ".Fractal/policy.pin"])
def test_a_store_path_in_another_case_is_blocked(tmp_path, rel):
    root = _store(tmp_path)
    assert _blocked(_tool(root, "Write", {"file_path": str(root / rel), "content": "x"}))
    assert _blocked(_bash(root, f"sqlite3 {rel} 'delete from rule_history'"))


@pytest.mark.parametrize("command", ["printf '[policy]\\naccept_policy = \"local\"\\n' > Fractal.toml",
                                     "echo x >> FRACTAL.TOML"])
def test_the_policy_file_in_another_case_is_blocked(tmp_path, command):
    root = _config(_store(tmp_path))
    assert _policy_blocked(_tool(root, "Write", {"file_path": str(root / "Fractal.toml"), "content": ""}))
    assert _policy_blocked(_bash(root, command))


# --- the policy of a shared store is the main checkout's ---------------------------------------------------------

def _git(cwd, *a):
    r = subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr + r.stdout


def test_a_worktrees_own_fractal_toml_does_not_set_the_shared_policy(tmp_path):
    main_repo = tmp_path / "main"
    main_repo.mkdir()
    c = _repo(main_repo, policy=None)                               # human_only, unpinned
    c.close()
    wt = tmp_path / "wt"
    _git(main_repo, "worktree", "add", "-q", str(wt))
    (wt / "fractal.toml").write_text('[policy]\naccept_policy = "local"\n')
    assert config.load(wt).accept_policy == "human_only"
    (main_repo / "src" / "fractal.toml").write_text('[policy]\naccept_policy = "local"\n')
    assert config.load(main_repo / "src").accept_policy == "human_only"
    (main_repo / "fractal.toml").write_text('[policy]\naccept_policy = "local"\n')
    config._store_dir.cache_clear()
    assert config.load(wt).accept_policy == "local"                 # the main checkout's file governs

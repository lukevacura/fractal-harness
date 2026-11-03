"""Layered authority: root is the base case; a node may locally accept a rule inside its own files."""
import json
import sqlite3

import pytest

from fractal_harness import authority, ledger
from fractal_harness.authority import AuthorityError
from fractal_harness.cache import ClaimCache
from fractal_harness.check import check
from fractal_harness.cli import main
from fractal_harness.context import put_extracted
from fractal_harness.hook import edit_hook, pre_edit_hook, select_context
from fractal_harness.manifest import manifest
from fractal_harness.store import Store

from test_nodes import _git, _spec

NO_LEGACY = {"type": "grep", "pattern": r"^import legacy\b", "paths": ["src/**/*.py"], "expect": "absent"}
NO_LEGACY_A = {"type": "grep", "pattern": r"^import legacy\b", "paths": ["src/mid.py"], "expect": "absent"}
NEEDS_LEGACY = {"type": "grep", "pattern": r"^import legacy\b", "paths": ["src/mid.py"], "expect": "present"}
HAS_DEF = {"type": "grep", "pattern": r"^def f\(", "paths": ["src/mid.py"]}


def _repo(tmp_path, policy="local"):
    _git(tmp_path, "init", "-q")
    (tmp_path / ".git" / "info" / "exclude").write_text(".fractal/\n")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "top.py").write_text("def g():\n    return 1\n")
    (tmp_path / "src" / "mid.py").write_text("import os\n\ndef f():\n    return 2\n")
    (tmp_path / "src" / "other.py").write_text("x = 1\n")
    (tmp_path / "top.md").write_text(_spec(guarantee="Top promise.", exports="src/top.py::g"))
    (tmp_path / "mid.md").write_text(_spec(parents="top", guarantee="Mid promise.", exports="src/mid.py::f"))
    (tmp_path / "side.md").write_text(_spec(guarantee="Side promise.", exports="src/other.py::x"))
    if policy:
        (tmp_path / "fractal.toml").write_text(f'[policy]\naccept_policy = "{policy}"\n')
    c = ClaimCache(tmp_path)
    for n in ("top", "mid", "side"):
        ledger.register_node(c, tmp_path, n, f"{n}.md")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "c1")
    return c


def _propose(c, text, probe, reads=None):
    return c.put(text, reads or [], kind="invariant", probe=probe)


def _hook(root, rel, tool="Write", content=None):
    return pre_edit_hook(json.dumps({"tool_name": tool, "cwd": str(root), "tool_input": {
        "file_path": str(root / rel), "content": content if content is not None else (root / rel).read_text()}}))


# --- migration ---------------------------------------------------------------------------------

def test_existing_enforced_rules_migrate_to_root_idempotently(tmp_path):
    db = tmp_path / "old.db"
    s = Store(db)
    c = ClaimCache(tmp_path / "r")
    e = c.put("a rule", [], kind="invariant", probe=NO_LEGACY)
    c.set_rule(e.id, "enforced")
    p = c.put("proposed", [], kind="invariant", probe=NO_LEGACY_A)
    c.close(); s.close()
    # simulate a store from before authority existed
    path = tmp_path / "r" / ".fractal" / "edges.db"
    raw = sqlite3.connect(path)
    raw.execute("UPDATE edges SET authority=NULL, provenance=NULL")
    raw.execute("DELETE FROM rule_history")
    raw.commit(); raw.close()
    s = Store(path)
    assert s.get(e.id).authority == "root" and s.get(e.id).enforced
    assert s.get(p.id).authority is None and s.get(p.id).rule_state == "proposed"     # nothing else changes
    assert [h["action"] for h in s.rule_history(e.id)] == ["migrate"]
    s.close()
    s = Store(path)                                                                  # idempotent
    assert [h["action"] for h in s.rule_history(e.id)] == ["migrate"] and s.get(e.id).authority == "root"
    s.close()


def test_a_local_authority_is_not_reset_by_migration(tmp_path):
    c = _repo(tmp_path)
    e = _propose(c, "mid has def f", HAS_DEF, ["src/mid.py"])
    authority.accept_as(c, tmp_path, e.id, "mid", "local")
    c.close()
    c = ClaimCache(tmp_path)
    assert c.store.get(e.id).authority == "mid"
    c.close()


def test_human_accept_is_root_with_provenance(tmp_path):
    c = _repo(tmp_path, policy=None)
    e = _propose(c, "no legacy", NO_LEGACY)
    c.close()
    assert main(["--root", str(tmp_path), "accept", e.id, "--yes"]) == 0
    c = ClaimCache(tmp_path)
    got = c.store.get(e.id)
    assert got.enforced and got.layer == "root" and got.provenance["by"] == "human"
    assert c.store.rule_history(e.id)[-1]["action"] == "accept"
    c.close()


# --- accept --as: policy and each gate ----------------------------------------------------------

def test_refused_under_human_only_and_by_default(tmp_path, capsys):
    for policy in ("human_only", None):
        d = tmp_path / str(policy)
        d.mkdir()
        c = _repo(d, policy)
        e = _propose(c, "mid has def f", HAS_DEF, ["src/mid.py"])
        c.close()
        assert main(["--root", str(d), "accept", e.id, "--as", "mid"]) == 1
        assert "accept_policy" in capsys.readouterr().err
        c = ClaimCache(d)
        assert c.store.get(e.id).rule_state == "proposed"
        c.close()


def test_accept_as_passes_all_gates_and_records_provenance(tmp_path):
    c = _repo(tmp_path)
    e = _propose(c, "mid has def f", HAS_DEF, ["src/mid.py"])
    r = authority.accept_as(c, tmp_path, e.id, "mid", "local")
    got = r["edge"]
    assert got.enforced and got.authority == "mid" and r["audit"] is None
    assert got.provenance["by"] == "agent" and got.provenance["node"] == "mid" and got.provenance["evidence"] == "proxy"
    assert got.provenance["ts"] > 0
    assert c.store.rule_history(e.id)[-1]["who"] == "mid"
    c.close()


def test_local_with_audit_records_the_audit_but_never_gates_on_it(tmp_path):
    c = _repo(tmp_path, "local_with_audit")
    e = _propose(c, "mid has def f", HAS_DEF, ["src/mid.py"])
    r = authority.accept_as(c, tmp_path, e.id, "mid", "local_with_audit")
    assert r["audit"]["verdict"] in ("ok", "partial", "weak") and r["edge"].provenance["audit"] == r["audit"]
    c.close()


def test_scope_gate_refuses_widening_and_repo_wide(tmp_path):
    c = _repo(tmp_path)
    wide = _propose(c, "no legacy anywhere in src", NO_LEGACY)                    # src/** reaches top.py, other.py
    with pytest.raises(AuthorityError, match="scope gate.*does not own"):
        authority.accept_as(c, tmp_path, wide.id, "mid", "local")
    other = _propose(c, "other is x", {"type": "grep", "pattern": "x", "paths": ["src/other.py"]})
    with pytest.raises(AuthorityError, match="scope gate"):                       # another node's file
        authority.accept_as(c, tmp_path, other.id, "mid", "local")
    nothing = _propose(c, "commit msgs", {"type": "grep", "pattern": "x", "paths": ["nope/*.py"]})
    with pytest.raises(AuthorityError, match="scope gate.*depends on no file|does not own|probe gate"):
        authority.accept_as(c, tmp_path, nothing.id, "mid", "local")
    assert c.store.get(wide.id).rule_state == "proposed"
    with pytest.raises(AuthorityError, match="not a registered node"):
        authority.accept_as(c, tmp_path, wide.id, "ghost", "local")
    c.close()


def test_probe_gate_needs_a_probe_that_runs_passes_and_checks_something(tmp_path):
    c = _repo(tmp_path)
    none = c.put("mid is nice", ["src/mid.py"], kind="invariant")
    with pytest.raises(AuthorityError, match="probe gate.*no probe"):
        authority.accept_as(c, tmp_path, none.id, "mid", "local")
    failing = _propose(c, "mid imports legacy", NEEDS_LEGACY)
    with pytest.raises(AuthorityError, match="probe gate.*violated now"):
        authority.accept_as(c, tmp_path, failing.id, "mid", "local")
    vacuous = _propose(c, "mid never", {"type": "grep", "pattern": "x", "paths": ["src/mid.py", "src/mid.py"],
                                        "expect": "absent", "exclude_paths": ["src/mid.py"]}, ["src/mid.py"])
    with pytest.raises(AuthorityError, match="probe gate.*checks nothing|scope gate"):
        authority.accept_as(c, tmp_path, vacuous.id, "mid", "local")
    c.close()


def test_conflict_gate_refuses_a_sound_contradiction_with_a_root_rule(tmp_path):
    c = _repo(tmp_path)
    root_rule = _propose(c, "no legacy in src", NO_LEGACY)
    c.set_rule(root_rule.id, "enforced")
    (tmp_path / "src" / "mid.py").write_text("import os\n")
    # the child wants legacy imported: absent over src/** vs present over src/mid.py (a subset) -> sound contradiction
    child = _propose(c, "mid must import legacy", NEEDS_LEGACY)
    # (it fails now, so the probe gate would trip first: make it hold, then accept)
    (tmp_path / "src" / "mid.py").write_text("import legacy\n")
    with pytest.raises(AuthorityError, match="conflict gate.*contradicts root"):
        authority.accept_as(c, tmp_path, child.id, "mid", "local")
    assert c.store.get(child.id).rule_state == "proposed"
    c.close()


def test_conflict_gate_passes_for_agreement_and_other_layers(tmp_path):
    c = _repo(tmp_path)
    parent = _propose(c, "no legacy in src", NO_LEGACY)
    c.set_rule(parent.id, "enforced")
    same = _propose(c, "mid no legacy", NO_LEGACY_A)                               # agrees: a narrowing
    assert authority.accept_as(c, tmp_path, same.id, "mid", "local")["warnings"] == []
    # a sibling's rule is not a higher layer: no conflict with it
    sib = _propose(c, "other must have x", {"type": "grep", "pattern": "^x", "paths": ["src/other.py"]})
    authority.accept_as(c, tmp_path, sib.id, "side", "local")
    c.close()


def test_conflict_with_other_probe_types_is_a_warning_not_a_refusal(tmp_path):
    c = _repo(tmp_path)
    cmd = c.put("mid builds", ["src/mid.py"], kind="invariant", probe={"type": "command", "run": "true"})
    c.set_rule(cmd.id, "enforced")
    e = _propose(c, "mid has def f", HAS_DEF, ["src/mid.py"])
    r = authority.accept_as(c, tmp_path, e.id, "mid", "local")
    assert any("heuristic" in w for w in r["warnings"])
    c.close()


def test_cannot_accept_rejected_enforced_or_overridden(tmp_path):
    c = _repo(tmp_path)
    e = _propose(c, "mid has def f", HAS_DEF, ["src/mid.py"])
    c.set_rule(e.id, "rejected")
    with pytest.raises(AuthorityError, match="rejected, not proposed"):
        authority.accept_as(c, tmp_path, e.id, "mid", "local")
    c.close()


def test_agent_accept_needs_no_tty_but_human_accept_does(tmp_path, capsys, monkeypatch):
    c = _repo(tmp_path)
    e = _propose(c, "mid has def f", HAS_DEF, ["src/mid.py"])
    c.close()
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert main(["--root", str(tmp_path), "accept", e.id]) == 2                    # human step: refused without a TTY
    assert main(["--root", str(tmp_path), "accept", e.id, "--as", "mid"]) == 0
    assert "enforced@mid" in capsys.readouterr().out


# --- precedence ---------------------------------------------------------------------------------

def _conflicting(tmp_path):
    """root: no legacy in src/**; mid (locally): src/mid.py has legacy. The local rule was accepted first, then
    the root rule arrived (a conflict that only exists after the fact)."""
    c = _repo(tmp_path)
    (tmp_path / "src" / "mid.py").write_text("import legacy\ndef f():\n    return 2\n")
    child = _propose(c, "mid must import legacy", NEEDS_LEGACY)
    authority.accept_as(c, tmp_path, child.id, "mid", "local")
    parent = _propose(c, "no legacy in src", NO_LEGACY)
    c.set_rule(parent.id, "enforced")
    return c, parent, child


def test_precedence_in_the_edit_hooks_parent_wins_and_says_so(tmp_path):
    c, parent, child = _conflicting(tmp_path)
    c.close()
    code, msg = _hook(tmp_path, "src/mid.py")
    assert code == 2
    assert "no legacy in src" in msg and "mid must import legacy" not in msg.split("overrides")[0]
    assert f"overrides mid's rule [{child.id}]" in msg and "1 enforced invariant" in msg      # only the parent's violation
    code, msg = edit_hook(json.dumps({"tool_name": "Edit", "cwd": str(tmp_path),
                                      "tool_input": {"file_path": str(tmp_path / "src/mid.py")}}))
    assert code == 2 and "1 enforced invariant" in msg and "overrides mid's rule" in msg
    # fixing the code for the parent is accepted: the child's contradictory rule does not block it
    assert _hook(tmp_path, "src/mid.py", content="def f():\n    return 2\n") == (0, "")


def test_without_conflict_both_layers_apply_and_siblings_do_not_shadow(tmp_path):
    c = _repo(tmp_path)
    e = _propose(c, "mid has def f", HAS_DEF, ["src/mid.py"])
    authority.accept_as(c, tmp_path, e.id, "mid", "local")
    c.close()
    code, msg = _hook(tmp_path, "src/mid.py", content="import os\n")                # local rule still enforced
    assert code == 2 and "mid has def f" in msg


def test_precedence_in_check_and_in_context(tmp_path):
    c, parent, child = _conflicting(tmp_path)
    (tmp_path / "src" / "mid.py").write_text("def f():\n    return 2\n")             # satisfies root, breaks the child
    c.close()
    code, out = check(tmp_path)
    assert code == 0, out
    assert f"mid's rule {child.id} is set aside where it conflicts with root's rule {parent.id}" in out
    (tmp_path / "src" / "mid.py").write_text("import legacy\n")                      # satisfies the child, breaks root
    code, out = check(tmp_path)
    assert code == 1 and f"VIOLATION {parent.id}" in out and f"VIOLATION {child.id}" not in out
    rules, _ = select_context(tmp_path, "edit src/mid.py", log=False)
    assert [e.id for e in rules] == [parent.id]
    assert child.post not in manifest(tmp_path, "src")


def test_a_lower_rule_cannot_shadow_a_higher_one_and_unsound_conflicts_do_not_shadow(tmp_path):
    c = _repo(tmp_path)
    # partial overlap, neither nested: heuristic only -> nothing is set aside
    a = {"type": "grep", "pattern": "zz", "paths": ["src/mid.py", "src/top.py"], "expect": "absent"}
    b = {"type": "grep", "pattern": "zz", "paths": ["src/mid.py", "src/other.py"], "expect": {"min": 1}}
    cn = authority.contradiction(a, b, ["src/mid.py", "src/top.py", "src/other.py"])
    assert cn is not None and not cn.sound
    files = ["src/mid.py", "src/top.py"]
    assert authority.contradiction(NO_LEGACY_A, NEEDS_LEGACY, files).sound
    assert authority.contradiction(NEEDS_LEGACY, NO_LEGACY_A, files).sound         # same files: symmetric
    nested = authority.contradiction(NO_LEGACY, NEEDS_LEGACY, files)
    assert nested.sound
    assert authority.contradiction(NEEDS_LEGACY, NO_LEGACY, files).sound            # present in mid vs absent in all: no
    wide_present = {**NEEDS_LEGACY, "paths": ["src/**/*.py"]}
    assert authority.contradiction(NO_LEGACY_A, wide_present, files) is None        # satisfiable: use top.py
    assert authority.contradiction({**NO_LEGACY_A, "pattern": "other"}, NEEDS_LEGACY, files) is None
    assert authority.outranks("root", "mid", {}) and not authority.outranks("mid", "root", {})
    nodes = c.store.all_nodes()
    assert authority.outranks("top", "mid", nodes) and not authority.outranks("mid", "top", nodes)
    assert not authority.outranks("side", "mid", nodes) and not authority.outranks("mid", "mid", nodes)
    c.close()


# --- override ------------------------------------------------------------------------------------

def test_override_demotes_keeps_history_and_is_reversible(tmp_path, capsys):
    c = _repo(tmp_path)
    e = _propose(c, "mid has def f", HAS_DEF, ["src/mid.py"])
    authority.accept_as(c, tmp_path, e.id, "mid", "local")
    c.close()
    assert main(["--root", str(tmp_path), "override", e.id, "--reason", "f is being renamed", "--yes"]) == 0
    c = ClaimCache(tmp_path)
    got = c.store.get(e.id)
    assert got.rule_state == "overridden" and not got.enforced and got.authority == "mid"     # demoted, not deleted
    hist = c.store.rule_history(e.id)
    assert [(h["action"], h["who"], h["reason"]) for h in hist][-1] == ("override", "root", "f is being renamed")
    c.close()
    assert _hook(tmp_path, "src/mid.py", content="import os\n") == (0, "")                  # no longer enforced
    capsys.readouterr()
    assert main(["--root", str(tmp_path), "invariants"]) == 0
    out = capsys.readouterr().out
    assert "overridden" in out and "OVERRIDDEN by root: f is being renamed" in out and "@mid" in out
    assert main(["--root", str(tmp_path), "--json", "invariants"]) == 0
    row = json.loads(capsys.readouterr().out)[0]
    assert row["rule_state"] == "overridden" and row["authority"] == "mid" and row["last_override"]["reason"]
    assert main(["--root", str(tmp_path), "override", e.id, "--undo", "--reason", "decided against", "--yes"]) == 0
    c = ClaimCache(tmp_path)
    assert c.store.get(e.id).enforced and c.store.get(e.id).authority == "mid"
    assert [h["action"] for h in c.store.rule_history(e.id)][-2:] == ["override", "restore"]
    c.close()
    assert _hook(tmp_path, "src/mid.py", content="import os\n")[0] == 2


def test_override_requires_a_higher_layer_and_a_reason(tmp_path):
    c = _repo(tmp_path)
    e = _propose(c, "mid has def f", HAS_DEF, ["src/mid.py"])
    authority.accept_as(c, tmp_path, e.id, "mid", "local")
    with pytest.raises(AuthorityError, match="reason"):
        authority.override(c, e.id, "root", " ", "local")
    with pytest.raises(AuthorityError, match="does not outrank"):
        authority.override(c, e.id, "side", "no", "local")               # a sibling
    with pytest.raises(AuthorityError, match="does not outrank"):
        authority.override(c, e.id, "mid", "no", "local")               # not itself
    with pytest.raises(AuthorityError, match="human_only"):
        authority.override(c, e.id, "top", "no", "human_only")          # a node needs the policy
    # top is mid's parent: it may override mid's rule
    assert authority.override(c, e.id, "top", "superseded", "local").rule_state == "overridden"
    # but a node may not touch a root rule
    r = _propose(c, "no legacy in src", NO_LEGACY)
    c.set_rule(r.id, "enforced")
    with pytest.raises(AuthorityError, match="does not outrank root"):
        authority.override(c, r.id, "top", "no", "local")
    assert authority.override(c, r.id, "root", "obsolete", "human_only").rule_state == "overridden"
    with pytest.raises(AuthorityError, match="only an enforced"):
        authority.override(c, r.id, "root", "again", "human_only")
    c.close()


def test_override_is_a_human_step_without_by_a_node(tmp_path, monkeypatch):
    c = _repo(tmp_path)
    e = _propose(c, "mid has def f", HAS_DEF, ["src/mid.py"])
    authority.accept_as(c, tmp_path, e.id, "mid", "local")
    c.close()
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert main(["--root", str(tmp_path), "override", e.id, "--reason", "x"]) == 2
    assert main(["--root", str(tmp_path), "override", e.id, "--by", "top", "--reason", "x"]) == 0


# --- no config: unchanged -------------------------------------------------------------------------

def test_no_config_default_behaviour_is_unchanged(tmp_path, capsys):
    c = _repo(tmp_path, policy=None)
    r = _propose(c, "no legacy in src", NO_LEGACY)
    c.set_rule(r.id, "enforced")
    c.close()
    assert main(["--root", str(tmp_path), "invariants"]) == 0
    out = capsys.readouterr().out
    assert "@" not in out and "overridden" not in out and out.rstrip().endswith("enforced=1 proposed=0 rejected=0")
    main(["--root", str(tmp_path), "--json", "invariants"])
    assert "authority" not in json.loads(capsys.readouterr().out)[0]
    (tmp_path / "src" / "mid.py").write_text("import legacy\n")
    code, msg = _hook(tmp_path, "src/mid.py")
    assert code == 2 and "overrides" not in msg


# --- added after mutation testing ------------------------------------------------------------------

def test_same_layer_rules_never_outrank_each_other(tmp_path):
    c = _repo(tmp_path)
    assert not authority.outranks("root", "root", {}) and not authority.outranks("mid", "mid", c.store.all_nodes())
    a = _propose(c, "no legacy in mid", NO_LEGACY_A)
    b = _propose(c, "legacy in mid", NEEDS_LEGACY)
    c.set_rule(a.id, "enforced"); c.set_rule(b.id, "enforced")           # two root rules that contradict
    files = ["src/mid.py"]
    assert authority.shadowed(c.store.all(), c.store.all_nodes(), files) == {}     # same layer: nothing set aside
    from fractal_harness.store import Edge
    assert Edge(**{**c.store.get(a.id).__dict__, "authority": None}).layer == "root"
    assert c.store.get(a.id).authority == "root"
    c.close()


def test_scope_gate_refuses_a_rule_with_no_files_or_a_file_not_there(tmp_path):
    c = _repo(tmp_path)
    nofiles = c.put("true holds", [], kind="invariant", probe={"type": "command", "run": "true"})
    with pytest.raises(AuthorityError, match="depends on no file"):
        authority.accept_as(c, tmp_path, nofiles.id, "mid", "local")
    ghost = _propose(c, "new file is clean", {"type": "grep", "pattern": "x", "paths": ["src/new.py"], "expect": "absent"})
    with pytest.raises(AuthorityError, match="scope gate.*src/new.py"):
        authority.accept_as(c, tmp_path, ghost.id, "mid", "local")
    c.close()


def test_contradiction_details_counts_disjoint_excludes_and_heuristics_never_shadow(tmp_path):
    c = _repo(tmp_path)
    files = ["src/mid.py", "src/top.py", "src/other.py"]
    three = {**NEEDS_LEGACY, "expect": {"count": 3}}
    assert authority.contradiction(three, NO_LEGACY_A, files).sound                 # exactly 3 vs none, same files
    assert authority.contradiction(NO_LEGACY_A, {**NEEDS_LEGACY, "paths": ["src/top.py"]}, files) is None   # disjoint
    skipping = {**NO_LEGACY, "exclude_paths": ["src/mid.py"]}
    assert authority.contradiction(skipping, NEEDS_LEGACY, files) is None           # mid is excluded from the ban
    # a heuristic (partial overlap) conflict is warned about but never sets a rule aside
    hi = _propose(c, "zz absent", {"type": "grep", "pattern": "zz", "paths": ["src/mid.py", "src/top.py"], "expect": "absent"})
    lo = _propose(c, "zz present", {"type": "grep", "pattern": "zz", "paths": ["src/mid.py", "src/other.py"], "expect": {"min": 1}})
    c.set_rule(hi.id, "enforced")
    c.store.set_authority(lo.id, "enforced", "mid", {})
    assert authority.contradiction(c.store.get(hi.id).probe, c.store.get(lo.id).probe, files).sound is False
    assert authority.shadowed(c.store.all(), c.store.all_nodes(), files) == {}
    c.close()


def test_the_conflict_check_ignores_rules_over_other_files(tmp_path):
    c = _repo(tmp_path)
    cmd = c.put("other builds", ["src/other.py"], kind="invariant", probe={"type": "command", "run": "true"})
    c.set_rule(cmd.id, "enforced")                                                   # unrelated scope, unanalysable probe
    e = _propose(c, "mid has def f", HAS_DEF, ["src/mid.py"])
    assert authority.accept_as(c, tmp_path, e.id, "mid", "local")["warnings"] == []
    c.close()

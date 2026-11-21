"""Onboarding shows debt instead of hiding it: a rule the code breaks today is kept with a bound that only goes down
(ratchet), proposals report being violated or contradicting another rule, and a human can lift a command probe to a
typed one through the door."""
import json

import pytest

from fractal_harness import debt, root_signal
from fractal_harness.cache import ClaimCache
from fractal_harness.cli import main

from test_authority import NO_LEGACY, _propose, _repo


def _run(tmp_path, *argv):
    return main(["--root", str(tmp_path), *argv])


def _legacy(tmp_path, n):
    """n files under src/ that `import legacy`."""
    for i in range(n):
        (tmp_path / "src" / f"old{i}.py").write_text("import legacy\n")


def _get(tmp_path, eid):
    c = ClaimCache(tmp_path)
    try:
        return c.store.get(eid)
    finally:
        c.close()


def _only_id(tmp_path, state="proposed"):
    c = ClaimCache(tmp_path)
    try:
        [e] = c.invariants((state,))
        return e.id
    finally:
        c.close()


# --- debt form ---------------------------------------------------------------------------------------------------

def test_to_debt_sets_todays_count_and_keeps_the_target(tmp_path):
    _repo(tmp_path).close()
    _legacy(tmp_path, 3)
    probe, notes = debt.to_debt(NO_LEGACY, tmp_path)
    assert probe["expect"] == {"max": 3} and probe["debt_target"] == "absent" and "3 today" in notes[0]
    assert NO_LEGACY["expect"] == "absent"                  # the input is not modified


def test_to_debt_refuses_a_passing_probe_and_a_presence_rule(tmp_path):
    _repo(tmp_path).close()
    with pytest.raises(debt.DebtError, match="passes now"):
        debt.to_debt(NO_LEGACY, tmp_path)
    with pytest.raises(debt.DebtError, match="cannot be debt"):
        debt.to_debt({"type": "grep", "pattern": "NOPE", "paths": ["src/**/*.py"]}, tmp_path)


def test_to_debt_only_relaxes_the_failing_leaves_of_an_all_probe(tmp_path):
    _repo(tmp_path).close()
    _legacy(tmp_path, 2)
    ok = {"type": "grep", "pattern": "^import os_never", "paths": ["src/**/*.py"], "expect": "absent"}
    probe, _ = debt.to_debt({"type": "all", "probes": [NO_LEGACY, ok]}, tmp_path)
    assert probe["probes"][0]["expect"] == {"max": 2} and probe["probes"][1] == ok


def test_paths_rules_can_be_debt(tmp_path):
    (tmp_path / "m").mkdir()
    for f in ("1_a.sql", "2_b.sql", "003_c.sql"):
        (tmp_path / "m" / f).write_text("")
    rule = {"type": "paths", "paths": ["m/*"], "unless_name": r"[0-9]{3}_\w+\.sql", "expect": "absent"}
    probe, _ = debt.to_debt(rule, tmp_path)
    assert probe["expect"] == {"max": 2}


# --- propose --debt and ratchet ----------------------------------------------------------------------------------

def test_propose_debt_then_ratchet_down_to_the_target(tmp_path, capsys):
    _repo(tmp_path).close()
    _legacy(tmp_path, 3)
    assert _run(tmp_path, "propose", "no legacy imports in src", "--probe", json.dumps(NO_LEGACY), "--debt") == 0
    eid = _only_id(tmp_path)
    assert "debt:" in capsys.readouterr().out
    c = ClaimCache(tmp_path)
    c.set_rule(eid, "enforced")
    c.close()
    assert _get(tmp_path, eid).status == "verified"           # the debt rule holds today
    (tmp_path / "src" / "old0.py").write_text("import modern\n")
    assert _run(tmp_path, "ratchet", eid) == 0
    e = _get(tmp_path, eid)
    assert e.probe["expect"] == {"max": 2} and e.enforced
    c = ClaimCache(tmp_path)
    h = c.store.rule_history(eid)[-1]
    c.close()
    assert h["action"] == "ratchet" and h["who"] == "agent" and h["detail"]["loss"] is False
    for i in (1, 2):
        (tmp_path / "src" / f"old{i}.py").write_text("import modern\n")
    assert _run(tmp_path, "ratchet", eid) == 0
    e = _get(tmp_path, eid)
    assert e.probe["expect"] == "absent" and "debt_target" not in e.probe       # paid off: the original rule
    assert _run(tmp_path, "ratchet", eid) == 1                                    # no longer a debt rule


def test_a_new_violation_breaks_the_debt_rule_and_ratchet_refuses(tmp_path, capsys):
    _repo(tmp_path).close()
    _legacy(tmp_path, 2)
    assert _run(tmp_path, "propose", "no legacy", "--probe", json.dumps(NO_LEGACY), "--debt") == 0
    eid = _only_id(tmp_path)
    _legacy(tmp_path, 3)                                       # one more
    capsys.readouterr()
    assert _run(tmp_path, "ratchet", eid) == 1
    assert "above its bound" in capsys.readouterr().err
    assert _get(tmp_path, eid).probe["expect"] == {"max": 2}


def test_ratchet_with_no_progress_changes_nothing(tmp_path, capsys):
    _repo(tmp_path).close()
    _legacy(tmp_path, 2)
    _run(tmp_path, "propose", "no legacy", "--probe", json.dumps(NO_LEGACY), "--debt")
    eid = _only_id(tmp_path)
    capsys.readouterr()
    assert _run(tmp_path, "ratchet", eid) == 0 and "unchanged" in capsys.readouterr().out


def test_raising_a_debt_bound_is_a_loss(tmp_path):
    from fractal_harness.rules import global_loss
    _repo(tmp_path).close()
    _legacy(tmp_path, 2)
    _run(tmp_path, "propose", "no legacy", "--probe", json.dumps(NO_LEGACY), "--debt")
    eid = _only_id(tmp_path)
    c = ClaimCache(tmp_path)
    c.set_rule(eid, "enforced")
    e = c.store.get(eid)
    looser = {**e.probe, "expect": {"max": 5}}
    assert global_loss(c, e, looser, leaves_enforced=False)[0] is True
    assert global_loss(c, e, {**e.probe, "expect": {"max": 1}}, leaves_enforced=False)[0] is False
    c.close()


def test_invariants_lists_the_debt(tmp_path, capsys):
    _repo(tmp_path).close()
    _legacy(tmp_path, 2)
    _run(tmp_path, "propose", "no legacy", "--probe", json.dumps(NO_LEGACY), "--debt")
    capsys.readouterr()
    assert _run(tmp_path, "invariants") == 0
    assert "debt: " in capsys.readouterr().out


# --- findings at propose time ------------------------------------------------------------------------------------

def test_a_violated_proposal_says_so(tmp_path, capsys):
    _repo(tmp_path).close()
    _legacy(tmp_path, 1)
    capsys.readouterr()
    assert _run(tmp_path, "propose", "no legacy", "--probe", json.dumps(NO_LEGACY)) == 0
    out = capsys.readouterr().out
    assert "VIOLATED NOW" in out and "--debt" in out


def test_a_contradicting_proposal_is_reported_in_propose_and_invariants(tmp_path, capsys):
    c = _repo(tmp_path)
    (tmp_path / "src" / "mid.py").write_text("import legacy\ndef f():\n    return 2\n")
    keep = _propose(c, "mid imports legacy", {**NO_LEGACY, "paths": ["src/mid.py"], "expect": "present"})
    c.set_rule(keep.id, "enforced")
    c.close()
    capsys.readouterr()
    _run(tmp_path, "propose", "no legacy anywhere", "--probe", json.dumps(NO_LEGACY))
    out = capsys.readouterr().out
    assert "CONTRADICTS" in out and keep.id in out
    _run(tmp_path, "invariants", "--state", "proposed")
    assert "CONTRADICTS" in capsys.readouterr().out


def test_a_clean_proposal_reports_nothing(tmp_path, capsys):
    _repo(tmp_path).close()
    capsys.readouterr()
    _run(tmp_path, "propose", "no legacy", "--probe", json.dumps(NO_LEGACY))
    out = capsys.readouterr().out
    assert "VIOLATED" not in out and "CONTRADICTS" not in out and "conflict" not in out


# --- lift -------------------------------------------------------------------------------------------------------

CMD = {"type": "command", "run": "! grep -rq '^import legacy' src"}


def _enforced_command(tmp_path):
    c = _repo(tmp_path)
    e = _propose(c, "no legacy imports", CMD)
    c.set_rule(e.id, "enforced")
    c.close()
    return e.id


def test_lift_replaces_a_command_probe_through_the_door(tmp_path, monkeypatch):
    eid = _enforced_command(tmp_path)
    asked = []
    monkeypatch.setattr(root_signal, "confirm", lambda root, what, yes=False: asked.append(what))
    assert _run(tmp_path, "lift", eid, "--probe", json.dumps(NO_LEGACY), "--reason", "typed") == 0
    assert "old probe" in asked[0] and "new probe" in asked[0] and "GLOBAL LOSS" in asked[0]   # command: unprovable
    e = _get(tmp_path, eid)
    assert e.probe == NO_LEGACY and e.enforced
    c = ClaimCache(tmp_path)
    h = c.store.rule_history(eid)[-1]
    from fractal_harness.rules import drifted
    assert h["action"] == "lift" and h["who"] == "human" and h["detail"]["old_probe"] == CMD
    assert drifted(c.store, [c.store.get(eid)]) == []                  # the new revision is the pinned one
    c.close()


def test_lift_refuses_a_new_probe_that_fails_or_checks_nothing(tmp_path, monkeypatch, capsys):
    eid = _enforced_command(tmp_path)
    monkeypatch.setattr(root_signal, "confirm", lambda *a, **k: pytest.fail("must not ask"))
    _legacy(tmp_path, 0)
    failing = {**NO_LEGACY, "expect": "present"}
    assert _run(tmp_path, "lift", eid, "--probe", json.dumps(failing), "--reason", "x") == 1
    vacuous = {**NO_LEGACY, "paths": ["nothing/**/*.py"]}
    assert _run(tmp_path, "lift", eid, "--probe", json.dumps(vacuous), "--reason", "x") == 1
    assert _get(tmp_path, eid).probe == CMD


def test_lift_is_a_human_verb(tmp_path, monkeypatch):
    eid = _enforced_command(tmp_path)
    monkeypatch.setattr(root_signal, "DEFAULT", "tty")
    monkeypatch.setattr(root_signal, "_open_tty", lambda: None)
    assert _run(tmp_path, "lift", eid, "--probe", json.dumps(NO_LEGACY), "--reason", "x", "--yes") == 2
    assert _get(tmp_path, eid).probe == CMD


# --- review fixes -----------------------------------------------------------------------------------------------

def test_a_proposed_debt_rule_can_be_ratcheted_before_it_is_accepted(tmp_path):
    _repo(tmp_path).close()
    _legacy(tmp_path, 3)
    assert _run(tmp_path, "propose", "no legacy imports in src", "--probe", json.dumps(NO_LEGACY), "--debt") == 0
    eid = _only_id(tmp_path)
    (tmp_path / "src" / "old0.py").write_text("import modern\n")
    assert _run(tmp_path, "ratchet", eid) == 0
    e = _get(tmp_path, eid)
    assert e.probe["expect"] == {"max": 2} and e.rule_state == "proposed"


def test_ratcheting_several_rules_is_all_or_nothing(tmp_path, capsys):
    c = _repo(tmp_path)
    _legacy(tmp_path, 2)
    a = _propose(c, "no legacy (debt)", {**NO_LEGACY, "expect": {"max": 2}, "debt_target": "absent"})
    c.set_rule(a.id, "enforced")
    c.close()
    (tmp_path / "src" / "old0.py").write_text("import modern\n")              # a can go down
    assert _run(tmp_path, "ratchet", a.id, "nope") == 1                       # nope is not a debt rule
    assert _get(tmp_path, a.id).probe["expect"] == {"max": 2}                 # so a is unchanged


def test_lift_of_a_rule_with_no_probe_does_not_crash(tmp_path, monkeypatch):
    c = _repo(tmp_path)
    e = c.put("a rule nothing checks", [], kind="invariant")
    c.set_rule(e.id, "enforced")
    c.close()
    asked = []
    monkeypatch.setattr(root_signal, "confirm", lambda root, what, yes=False: asked.append(what))
    assert _run(tmp_path, "lift", e.id, "--probe", json.dumps(NO_LEGACY), "--reason", "typed") == 0
    assert "no probe" in asked[0] and _get(tmp_path, e.id).probe == NO_LEGACY

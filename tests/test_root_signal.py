"""Root decisions come from outside the agent's reach (configurable: [policy] root_signal), the policy that says so is
itself pinned by a root decision, and rules ruled on before revisions existed can be pinned.

  root_signal = "tty"   (default) confirm on the controlling terminal; --yes is not accepted
  root_signal = "flag"  stdin is a terminal, or --yes (the earlier behaviour; scripts the human controls)
The suite's conftest sets the default to "flag" so older tests keep their meaning; these tests set it explicitly.
"""
import io
import json
import sqlite3

import pytest

from fractal_harness import authority, config, root_signal, rules
from fractal_harness.cache import ClaimCache
from fractal_harness.check import check
from fractal_harness.cli import main
from fractal_harness.hook import pre_edit_hook

from test_authority import HAS_DEF, NO_LEGACY, _propose, _repo

WEAK = {"type": "grep", "pattern": "NEVER_MATCHES_ANYTHING", "paths": ["src/**/*.py"], "expect": "absent"}


class FakeTTY(io.StringIO):
    def __init__(self, answer):
        super().__init__(answer + "\n")
        self.shown = ""

    def write(self, s):
        self.shown += s
        return len(s)


@pytest.fixture
def tty(monkeypatch):
    """Use the real default (tty) and control what the terminal answers; None = no terminal (an agent's shell)."""
    monkeypatch.setattr(root_signal, "DEFAULT", "tty")
    state = {"answer": None, "opened": []}

    def _open():
        if state["answer"] is None:
            return None
        t = FakeTTY(state["answer"])
        state["opened"].append(t)
        return t
    monkeypatch.setattr(root_signal, "_open_tty", _open)
    return state


def _policy(tmp_path, **kv):
    lines = ["[policy]"] + [f"{k} = {json.dumps(v)}" for k, v in kv.items()]
    (tmp_path / "fractal.toml").write_text("\n".join(lines) + "\n")


def _proposal(tmp_path, policy=None):
    c = _repo(tmp_path, policy=policy)
    e = _propose(c, "no legacy", NO_LEGACY)
    c.close()
    return e.id


def _state(tmp_path, eid):
    c = ClaimCache(tmp_path)
    try:
        return c.store.get(eid).rule_state
    finally:
        c.close()


# --- the signal ---------------------------------------------------------------------------------------------

def test_the_shipped_default_is_tty():
    assert root_signal.SHIPPED_DEFAULT == "tty"


def test_tty_refuses_without_a_terminal_even_with_yes(tmp_path, tty, capsys):
    eid = _proposal(tmp_path)
    assert main(["--root", str(tmp_path), "accept", eid, "--yes"]) == 2
    err = capsys.readouterr().err
    assert "terminal" in err and "--yes" in err
    assert _state(tmp_path, eid) == "proposed"


def test_tty_accepts_when_the_terminal_confirms_and_names_the_decision(tmp_path, tty):
    eid = _proposal(tmp_path)
    tty["answer"] = "yes"
    assert main(["--root", str(tmp_path), "accept", eid]) == 0
    assert _state(tmp_path, eid) == "enforced" and eid in tty["opened"][0].shown


def test_tty_refuses_when_the_terminal_declines(tmp_path, tty):
    eid = _proposal(tmp_path)
    tty["answer"] = "no"
    assert main(["--root", str(tmp_path), "accept", eid]) == 2
    assert _state(tmp_path, eid) == "proposed"


@pytest.mark.parametrize("argv", [["reject", "{id}"], ["override", "{id}", "--reason", "r"]])
def test_tty_gates_every_human_verb(tmp_path, tty, argv):
    eid = _proposal(tmp_path)
    if argv[0] == "override":
        c = ClaimCache(tmp_path)
        c.set_rule(eid, "enforced")
        c.close()
    before = _state(tmp_path, eid)
    assert main(["--root", str(tmp_path), *[a.format(id=eid) for a in argv], "--yes"]) == 2
    assert _state(tmp_path, eid) == before


def test_agent_verbs_never_open_the_terminal(tmp_path, tty):
    c = _repo(tmp_path)
    e = _propose(c, "mid has def f", HAS_DEF, ["src/mid.py"])
    c.close()
    tty["answer"] = "yes"
    assert main(["--root", str(tmp_path), "accept", e.id, "--as", "mid"]) == 0
    assert main(["--root", str(tmp_path), "invariants"]) == 0
    assert tty["opened"] == []


def test_flag_keeps_the_earlier_behaviour(tmp_path, monkeypatch):
    monkeypatch.setattr(root_signal, "_open_tty", lambda: pytest.fail("flag must not open the terminal"))
    eid = _proposal(tmp_path)
    _policy(tmp_path, root_signal="flag")
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert main(["--root", str(tmp_path), "accept", eid]) == 2
    assert main(["--root", str(tmp_path), "accept", eid, "--yes"]) == 0
    assert _state(tmp_path, eid) == "enforced"


def test_an_unknown_signal_is_a_config_error(tmp_path):
    _policy(tmp_path, root_signal="telepathy")
    with pytest.raises(config.ConfigFileError, match="root_signal"):
        config.load(tmp_path)


def test_conflict_arbitration_is_gated_and_agent_resolution_is_not(tmp_path, tty, monkeypatch):
    calls = []
    monkeypatch.setattr(root_signal, "confirm", lambda root, what, yes=False: calls.append(what))
    from fractal_harness import cli
    gate = cli._human_gate(tmp_path, "conflict arbitrate c1", False)
    assert calls == [] and callable(gate)        # lazy: nothing is asked until a human decision is needed
    assert gate() is True and calls == ["conflict arbitrate c1"]


# --- fractal pin: older rules and the policy ------------------------------------------------------------------

def _legacy_enforced(tmp_path):
    """An enforced rule whose history has no revision (ruled on before revisions were recorded)."""
    eid = _proposal(tmp_path)
    c = ClaimCache(tmp_path)
    c.set_rule(eid, "enforced")
    c.close()
    db = sqlite3.connect(tmp_path / ".fractal" / "edges.db")
    with db:
        db.execute("UPDATE rule_history SET detail='{}' WHERE edge_id=?", (eid,))
    db.close()
    return eid


def _tamper(tmp_path, eid):
    db = sqlite3.connect(tmp_path / ".fractal" / "edges.db")
    with db:
        db.execute("UPDATE edges SET probe=? WHERE id=?", (json.dumps(WEAK), eid))
    db.close()


def test_pin_records_revisions_for_unpinned_rules_so_drift_is_then_caught(tmp_path):
    eid = _legacy_enforced(tmp_path)
    _tamper(tmp_path, eid)
    assert "CHANGED" not in check(tmp_path)[1]          # unpinned: not checked (documented gap)
    db = sqlite3.connect(tmp_path / ".fractal" / "edges.db")
    with db:
        db.execute("UPDATE edges SET probe=? WHERE id=?", (json.dumps(NO_LEGACY), eid))
    db.close()
    assert main(["--root", str(tmp_path), "pin", "--yes"]) == 0
    c = ClaimCache(tmp_path)
    h = c.store.rule_history(eid)[-1]
    assert h["action"] == "pin" and h["who"] == "human" and h["detail"]["revision"] == rules.revision(c.store.get(eid))
    c.close()
    _tamper(tmp_path, eid)
    code, out = check(tmp_path)
    assert code == 1 and "CHANGED" in out


def test_pin_does_not_bless_a_rule_that_drifted_after_it_was_pinned(tmp_path, capsys):
    eid = _proposal(tmp_path)
    c = ClaimCache(tmp_path)
    c.set_rule(eid, "enforced")
    c.close()
    _tamper(tmp_path, eid)
    capsys.readouterr()
    assert main(["--root", str(tmp_path), "pin", "--yes"]) == 0
    assert "changed outside" in capsys.readouterr().out
    c = ClaimCache(tmp_path)
    assert c.store.rule_history(eid)[-1]["action"] == "accept"
    c.close()
    assert check(tmp_path)[0] == 1


def test_pin_is_a_human_verb(tmp_path, tty):
    _legacy_enforced(tmp_path)
    assert main(["--root", str(tmp_path), "pin", "--yes"]) == 2


def test_a_pinned_policy_holds_until_a_human_repins(tmp_path, capsys):
    _proposal(tmp_path, policy=None)
    _policy(tmp_path, accept_policy="human_only", root_signal="flag")
    assert main(["--root", str(tmp_path), "pin", "--yes"]) == 0
    _policy(tmp_path, accept_policy="local", root_signal="flag")      # an edit nobody pinned
    cfg = config.load(tmp_path)
    assert cfg.accept_policy == "human_only" and "accept_policy" in cfg.policy_drift
    code, out = check(tmp_path)
    assert code == 1 and "accept_policy" in out and "fractal pin" in out
    assert main(["--root", str(tmp_path), "pin", "--yes"]) == 0          # the human takes the edit
    cfg = config.load(tmp_path)
    assert cfg.accept_policy == "local" and not cfg.policy_drift


def test_removing_the_config_does_not_drop_pinned_human_nodes(tmp_path):
    _proposal(tmp_path, policy=None)
    _policy(tmp_path, human_nodes=["core"], root_signal="flag")
    assert main(["--root", str(tmp_path), "pin", "--yes"]) == 0
    (tmp_path / "fractal.toml").unlink()
    cfg = config.load(tmp_path)
    assert cfg.human_nodes == ("core",) and cfg.root_signal == "flag" and "human_nodes" in cfg.policy_drift


def test_without_a_pin_the_file_is_the_policy(tmp_path):
    _proposal(tmp_path, policy="local")
    cfg = config.load(tmp_path)
    assert cfg.accept_policy == "local" and cfg.policy_drift == {}


# --- drift in the edit hooks ------------------------------------------------------------------------------------

def test_pre_edit_hook_blocks_edits_governed_by_a_rule_changed_outside_the_door(tmp_path):
    eid = _proposal(tmp_path)
    c = ClaimCache(tmp_path)
    c.set_rule(eid, "enforced")
    c.close()
    _tamper(tmp_path, eid)
    code, err = pre_edit_hook(json.dumps({"tool_name": "Write", "cwd": str(tmp_path), "tool_input": {
        "file_path": str(tmp_path / "src" / "other.py"), "content": "x = 2\n"}}))
    assert code == 2 and eid in err and "changed outside" in err


def test_pre_edit_hook_unaffected_without_drift(tmp_path):
    eid = _proposal(tmp_path)
    c = ClaimCache(tmp_path)
    c.set_rule(eid, "enforced")
    c.close()
    assert pre_edit_hook(json.dumps({"tool_name": "Write", "cwd": str(tmp_path), "tool_input": {
        "file_path": str(tmp_path / "src" / "other.py"), "content": "x = 2\n"}})) == (0, "")

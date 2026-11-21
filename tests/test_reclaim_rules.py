"""Reclamation of the rule set (compression without loss) and the rule gate on code reclamation (no deletion may break
an enforced rule: the code graph does not know about rules)."""
import pytest

from fractal_harness import authority, reclaim
from fractal_harness.authority import AuthorityError
from fractal_harness.cli import main
from fractal_harness.symbols import SymbolError

from test_authority import HAS_DEF, NO_LEGACY, NO_LEGACY_A, _propose, _repo as _arepo
from test_reclaim import _repo as _rrepo, git


def _enforce(c, text, probe, reads=None):
    e = _propose(c, text, probe, reads)
    c.set_rule(e.id, "enforced")
    return e


def _local(c, tmp_path, text, probe, node="mid", reads=("src/mid.py",)):
    e = _propose(c, text, probe, list(reads))
    authority.accept_as(c, tmp_path, e.id, node, "local")
    return e


def _rules(c, tmp_path):
    return {x.rule_id: x for x in reclaim.sweep(c, tmp_path, {"rules"})}


# --- which rules are redundant --------------------------------------------------------------------------------

def test_a_node_rule_absorbed_by_a_root_rule_is_retirable(tmp_path):
    c = _arepo(tmp_path, policy="local")
    keep = _enforce(c, "no legacy in src", NO_LEGACY)
    dup = _local(c, tmp_path, "no legacy in mid", NO_LEGACY_A)
    got = _rules(c, tmp_path)
    assert set(got) == {dup.id} and got[dup.id].action == "retire" and keep.id in got[dup.id].why
    c.close()


def test_a_rule_that_adds_information_is_not_a_candidate(tmp_path):
    c = _arepo(tmp_path)
    _enforce(c, "no legacy in mid", NO_LEGACY_A)
    _enforce(c, "no legacy in src", NO_LEGACY)            # wider: adds information
    got = _rules(c, tmp_path)
    assert all(x.action != "retire" for x in got.values())
    assert [x for x in got.values() if x.action == "human"] and len(got) == 1   # only the narrower root rule
    c.close()


def test_two_identical_rules_are_not_both_retired(tmp_path):
    c = _arepo(tmp_path)
    a = _enforce(c, "no legacy in src", NO_LEGACY)
    b = _enforce(c, "no legacy imports in src", {**NO_LEGACY})
    got = _rules(c, tmp_path)
    assert len(got) == 1 and set(got) < {a.id, b.id}         # one is proposed; the other is what keeps it redundant
    [(rid, cand)] = got.items()
    assert ({a.id, b.id} - {rid}).pop() in cand.why
    c.close()


def test_the_newer_of_two_duplicates_is_the_one_proposed(tmp_path):
    import time
    c = _arepo(tmp_path)
    old = _enforce(c, "no legacy in src", NO_LEGACY)
    time.sleep(0.01)
    new = _enforce(c, "no legacy imports in src", {**NO_LEGACY})
    assert set(_rules(c, tmp_path)) == {new.id} and old.id
    c.close()


def test_command_rules_are_never_candidates(tmp_path):
    c = _arepo(tmp_path)
    _enforce(c, "no legacy in src", NO_LEGACY)
    _enforce(c, "no legacy (shell)", {"type": "command", "run": "true"})
    assert _rules(c, tmp_path) == {}
    c.close()


def test_a_vacuous_rule_is_reported_not_proposed(tmp_path):
    c = _arepo(tmp_path)
    v = _enforce(c, "no legacy in gen", {**NO_LEGACY, "paths": ["gen/**/*.py"]})
    got = _rules(c, tmp_path)
    assert got[v.id].action == "vacuous" and "retiring it is a loss" in got[v.id].why
    c.close()


# --- retiring ---------------------------------------------------------------------------------------------------

def test_retire_needs_no_higher_layer_but_does_need_no_loss(tmp_path):
    c = _arepo(tmp_path, policy="local")
    mine = _local(c, tmp_path, "no legacy in mid", NO_LEGACY_A)
    with pytest.raises(AuthorityError, match="global loss"):
        authority.retire(c, mine.id, "mid", "tidy", "local")
    _enforce(c, "no legacy in src", NO_LEGACY)
    with pytest.raises(AuthorityError, match="neither"):
        authority.retire(c, mine.id, "side", "tidy", "local")
    with pytest.raises(AuthorityError, match="accept_policy"):
        authority.retire(c, mine.id, "mid", "tidy", "human_only")
    got = authority.retire(c, mine.id, "mid", "redundant", "local")
    assert got.rule_state == "overridden"
    h = c.store.rule_history(mine.id)[-1]
    assert h["action"] == "retire" and h["who"] == "mid" and h["detail"]["loss"] is False and h["detail"]["absorbed_by"]
    c.close()


def test_a_root_rule_is_never_retired_by_an_agent(tmp_path):
    c = _arepo(tmp_path, policy="local")
    r = _enforce(c, "no legacy in mid", NO_LEGACY_A)
    _enforce(c, "no legacy in src", NO_LEGACY)
    with pytest.raises(AuthorityError, match="root rule"):
        authority.retire(c, r.id, "top", "tidy", "local")
    c.close()


def test_apply_retires_node_rules_and_leaves_root_rules_to_the_human(tmp_path):
    c = _arepo(tmp_path, policy="local")
    keep = _enforce(c, "no legacy in src", NO_LEGACY)
    dup = _local(c, tmp_path, "no legacy in mid", NO_LEGACY_A)
    root_dup = _enforce(c, "no legacy in mid (root)", {**NO_LEGACY_A})
    cands = reclaim.sweep(c, tmp_path, {"rules"})
    assert {x.rule_id: x.action for x in cands} == {dup.id: "retire", root_dup.id: "human"}   # keep is the widest
    text = reclaim.render(cands)
    assert f"fractal override {root_dup.id}" in text and f"fractal override {dup.id}" not in text
    done = reclaim.apply(c, tmp_path, cands)
    assert any(dup.id in d for d in done)
    assert c.store.get(dup.id).rule_state == "overridden" and c.store.get(root_dup.id).enforced and c.store.get(keep.id).enforced
    c.close()


def test_cli_reclaim_rules_class(tmp_path, capsys):
    c = _arepo(tmp_path, policy="local")
    _enforce(c, "no legacy in src", NO_LEGACY)
    dup = _local(c, tmp_path, "no legacy in mid", NO_LEGACY_A)
    c.close()
    capsys.readouterr()
    assert main(["--root", str(tmp_path), "reclaim", "--class", "rules"]) == 0
    out = capsys.readouterr().out
    assert dup.id in out and "retires it" in out
    assert main(["--root", str(tmp_path), "reclaim", "--class", "rules", "--apply"]) == 0
    assert "retired" in capsys.readouterr().out


# --- the rule gate on code ----------------------------------------------------------------------------------------

NEEDS_DEAD = {"type": "grep", "pattern": r"^def dead_fn\(", "paths": ["lib.py"], "expect": {"min": 1}}


def test_a_code_deletion_that_breaks_an_enforced_rule_is_blocked(tmp_path):
    c = _rrepo(tmp_path)
    r = _enforce(c, "lib keeps dead_fn (an API others load by name)", NEEDS_DEAD, ["lib.py"])
    cands = reclaim.sweep(c, tmp_path, {"code"})
    by = {x.what: x for x in cands}
    assert by["lib.py::dead_fn"].breaks == [r.id] and by["lib.py::Dead"].breaks == []
    reclaim.verify(c, tmp_path, cands, cmd="python3 check.py", workers=2)
    assert by["lib.py::dead_fn"].verdict == "blocked" and "would break" in reclaim.render(cands)
    reclaim.apply(c, tmp_path, cands)
    left = (tmp_path / "lib.py").read_text()
    assert "def dead_fn" in left and "class Dead" not in left
    c.close()


def test_deletions_that_break_a_rule_only_together_are_refused_whole(tmp_path):
    c = _rrepo(tmp_path)
    either = {"type": "grep", "pattern": r"^(def dead_fn|class Dead)\b", "paths": ["lib.py"], "expect": {"min": 1}}
    _enforce(c, "lib keeps one of its legacy entry points", either, ["lib.py"])
    cands = reclaim.sweep(c, tmp_path, {"code"})
    assert all(not x.breaks for x in cands)                   # each alone leaves the other
    reclaim.verify(c, tmp_path, cands, cmd="python3 check.py", workers=2)
    before = (tmp_path / "lib.py").read_text()
    with pytest.raises(SymbolError, match="together would break"):
        reclaim.apply(c, tmp_path, cands)
    assert (tmp_path / "lib.py").read_text() == before
    c.close()


def test_between_a_node_rule_and_a_root_duplicate_the_node_rule_goes(tmp_path):
    import time
    c = _arepo(tmp_path, policy="local")
    node = _local(c, tmp_path, "no legacy in mid", NO_LEGACY_A)
    time.sleep(0.01)
    root = _enforce(c, "no legacy in mid (root)", {**NO_LEGACY_A})       # newer, but the human's ruling
    assert {x.rule_id: x.action for x in reclaim.sweep(c, tmp_path, {"rules"})} == {node.id: "retire"}
    c.close()


def test_a_rule_that_forbids_nothing_is_retirable(tmp_path):
    c = _arepo(tmp_path, policy="local")
    e = _local(c, tmp_path, "mid may mention legacy", {**NO_LEGACY_A, "expect": {"min": 0}})
    got = _rules(c, tmp_path)
    assert got[e.id].action == "retire" and "forbids nothing" in got[e.id].why
    c.close()


def test_a_rule_already_failing_does_not_block_code_reclamation(tmp_path):
    c = _rrepo(tmp_path)
    _enforce(c, "lib defines nope", {"type": "grep", "pattern": "^def nope", "paths": ["lib.py"]}, ["lib.py"])
    assert all(not x.breaks for x in reclaim.sweep(c, tmp_path, {"code"}))
    c.close()


# --- review fixes -----------------------------------------------------------------------------------------------

def test_a_symbol_rule_blocks_deleting_its_symbol_but_not_its_neighbours(tmp_path):
    from fractal_harness.symbols import digest, symbol_of
    c = _rrepo(tmp_path)
    pin = {"type": "symbol", "ref": "lib.py::dead_fn", "sha": digest(symbol_of(tmp_path, "lib.py::dead_fn"))}
    r = _enforce(c, "dead_fn's interface is pinned", pin, ["lib.py"])
    by = {x.what: x for x in reclaim.sweep(c, tmp_path, {"code"})}
    assert by["lib.py::dead_fn"].breaks == [r.id] and by["lib.py::Dead"].breaks == []
    c.close()


def test_the_file_reading_leaves_of_a_rule_with_a_gate_are_still_checked(tmp_path):
    c = _rrepo(tmp_path)
    both = {"type": "all", "probes": [{"type": "grep", "pattern": r"^class Dead", "paths": ["lib.py"]},
                                      {"type": "staged_paths", "forbid": "x"}]}
    r = _enforce(c, "Dead stays, and no x is committed", both, ["lib.py"])
    by = {x.what: x for x in reclaim.sweep(c, tmp_path, {"code"})}
    assert by["lib.py::Dead"].breaks == [r.id] and by["lib.py::dead_fn"].breaks == []
    c.close()


def test_a_rule_set_aside_by_precedence_is_not_proposed(tmp_path):
    from test_authority import NEEDS_LEGACY
    c = _arepo(tmp_path, policy="local")
    (tmp_path / "src" / "mid.py").write_text("import legacy\ndef f():\n    return 2\n")
    beaten = _local(c, tmp_path, "mid imports legacy", NEEDS_LEGACY)
    (tmp_path / "src" / "mid.py").write_text("def f():\n    return 2\n")
    _enforce(c, "no legacy in src", NO_LEGACY)
    assert beaten.id not in _rules(c, tmp_path)
    c.close()


def test_a_failed_file_write_leaves_no_record_of_a_deletion(tmp_path, monkeypatch):
    from pathlib import Path
    c = _rrepo(tmp_path)
    cands = reclaim.sweep(c, tmp_path, {"code"})
    reclaim.verify(c, tmp_path, cands, cmd="python3 check.py", workers=2)
    before = len(c.store.audit())

    def boom(self, *a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(Path, "write_text", boom)
    with pytest.raises(OSError):
        reclaim.apply(c, tmp_path, cands)
    assert len(c.store.audit()) == before
    c.close()

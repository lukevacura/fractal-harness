"""One primitive and one law. The Delta (loss.delta) is the change in what the rule set forbids; every question the
harness asks of a change is a view of it. The law (authority.judge) gates every change an agent makes: no loss,
standing (or a pure ratchet), and a node holds only rules over its own region."""
import json

import pytest

from fractal_harness import authority, loss, root_signal
from fractal_harness.authority import AuthorityError
from fractal_harness.cache import ClaimCache
from fractal_harness.cli import main
from fractal_harness.loss import contradiction, delta

from test_authority import HAS_DEF, NO_LEGACY, NO_LEGACY_A, _propose, _repo


def g(pat, paths, expect="absent", **kw):
    return {"type": "grep", "pattern": pat, "paths": paths, "expect": expect, **kw}


def _enforce(c, text, probe, reads=None):
    e = _propose(c, text, probe, reads)
    c.set_rule(e.id, "enforced")
    return e


def _local(c, tmp_path, text, probe, node="mid", reads=("src/mid.py",)):
    e = _propose(c, text, probe, list(reads))
    authority.accept_as(c, tmp_path, e.id, node, "local")
    return e


# --- the Delta ---------------------------------------------------------------------------------------------------

def test_delta_splits_both_directions():
    wide, narrow, other = g("x", ["src/**"]), g("x", ["src/a/**"]), g("y", ["src/**"])
    d = delta([("w", wide), ("o", other)], [("n", narrow), ("o", other)])
    assert [p.rule for p in d.allows] == ["w"] and not d.lossless and d.adds_nothing
    assert [(p.rule, p.by) for p in d.kept] == [("n", ["w"])]
    d = delta([("n", narrow)], [("n", narrow), ("w", wide)])
    assert d.lossless and [p.rule for p in d.forbids] == ["w"] and not d.adds_nothing


def test_an_unchanged_rule_is_in_neither_direction():
    d = delta([("a", g("x", ["s/**"]))], [("a", g("x", ["s/**"]))])
    assert d.lossless and d.adds_nothing and not (d.kept or d.absorbed)


def test_a_narrow_is_covered_piecewise():
    """No special case: the narrowed rule covers all but what it skips, another rule covers what it skips."""
    before = g("x", ["src/**"])
    after = g("x", ["src/**"], exclude_paths=["src/gen/**"])
    gen = g("x", ["src/gen/**"])
    d = delta([("r", before), ("gen", gen)], [("r", after), ("gen", gen)])
    assert d.lossless and sorted(d.absorbers()) == ["gen", "r"]
    assert not delta([("r", before)], [("r", after)]).lossless
    # the residual must itself be covered: a rule over part of what was skipped is not enough
    part = g("x", ["src/gen/a/**"])
    assert not delta([("r", before), ("p", part)], [("r", after), ("p", part)]).lossless


def test_the_residual_cover_needs_a_superset_with_a_zero_bound():
    before = g("x", ["src/**"])
    gen = g("x", ["src/gen/**"])
    elsewhere = g("x", ["lib/**"], exclude_paths=["src/gen/**"])          # skips src/gen, but covers no src file
    assert not delta([("r", before), ("e", elsewhere), ("gen", gen)], [("e", elsewhere), ("gen", gen)]).lossless
    loose = g("x", ["src/**"], {"max": 3}, exclude_paths=["src/gen/**"])  # over src, but allows three
    assert not delta([("r", before), ("l", loose), ("gen", gen)], [("l", loose), ("gen", gen)]).lossless
    # a residual cover keeps the narrowed atom's own skips: an atom skipping more than the cover is still covered
    own = g("x", ["src/**"], exclude_paths=["src/vendor/**"])
    wide = g("x", ["src/**"], exclude_paths=["src/gen/**"])
    assert delta([("o", own), ("w", wide), ("gen", gen)], [("w", wide), ("gen", gen)]).lossless


def test_an_uncomparable_part_is_unknown_on_its_side():
    cmd = {"type": "command", "run": "true"}
    d = delta([("c", cmd)], [])
    assert not d.lossless and d.unknown_allows and "not provable" in d.loss_why()
    d = delta([], [("c", cmd)])
    assert d.lossless and not d.adds_nothing and d.unknown_forbids


# --- contradiction: one engine, glob patterns ------------------------------------------------------------------

def test_contradiction_is_judged_on_patterns_not_todays_files():
    """No file matches today, yet no future state satisfies both: sound."""
    c = contradiction(g("x", ["gen/**"]), g("x", ["gen/a.py"], "present"))
    assert c is not None and c.sound and "cannot both hold" in c.why


def test_nested_compatible_bounds_are_not_a_contradiction():
    assert contradiction(g("x", ["src/a.py"]), g("x", ["src/**"], "present"), ["src/a.py", "src/b.py"]) is None


def test_only_in_and_paths_rules_are_compared_too():
    only = {"type": "only_in", "pattern": "x", "paths": ["src/**"], "files": ["src/ok.py"]}
    assert contradiction(only, g("x", ["src/bad.py"], "present")).sound
    misnamed_none = {"type": "paths", "paths": ["migrations/*"], "unless_name": r"^\d{4}_"}
    assert contradiction(misnamed_none, {**misnamed_none, "expect": {"min": 1}}).sound


def test_overlap_that_is_not_nested_is_heuristic():
    c = contradiction(g("x", ["src/a/**", "src/b.py"]), g("x", ["src/a/x.py", "lib/**"], "present"),
                      ["src/a/x.py", "src/b.py", "lib/y.py"])
    assert c is not None and not c.sound


# --- the law ---------------------------------------------------------------------------------------------------

def _debt(c, probe_max, target="absent"):
    return {**NO_LEGACY, "expect": {"max": probe_max}, "debt_target": target}


def test_a_pure_ratchet_needs_no_standing_even_on_a_root_rule(tmp_path):
    c = _repo(tmp_path, policy=None)                                # human_only
    (tmp_path / "src" / "old.py").write_text("import legacy\n")
    r = _enforce(c, "no legacy (debt)", _debt(c, 3))
    authority.judge(c, "anyone", {r.id: _debt(c, 1)})               # down toward the target
    authority.judge(c, "anyone", {r.id: _debt(c, 2)})
    with pytest.raises(AuthorityError):                             # up: not a ratchet, and a root rule
        authority.judge(c, "anyone", {r.id: _debt(c, 5)})
    with pytest.raises(AuthorityError):                             # below the target is not a ratchet
        authority.judge(c, "anyone", {r.id: {**_debt(c, 0), "debt_target": {"max": 1}}})
    c.close()


def test_a_ratchet_changes_only_debt_bounds_and_never_below_the_target():
    debt = {**NO_LEGACY, "expect": {"max": 3}, "debt_target": {"max": 1}}
    assert authority._pure_ratchet(debt, {**debt, "expect": {"max": 2}})
    assert not authority._pure_ratchet(debt, {**debt, "expect": {"max": 0}})              # below the ruled target
    assert not authority._pure_ratchet(debt, {**debt, "expect": {"max": 2}, "paths": ["src/a/**"]})   # reshaped
    assert not authority._pure_ratchet(NO_LEGACY, dict(NO_LEGACY))                         # not a debt rule


def test_paying_off_restores_the_target_and_is_a_ratchet(tmp_path):
    c = _repo(tmp_path, policy=None)
    r = _enforce(c, "no legacy (debt)", _debt(c, 2))
    assert authority._pure_ratchet(r.probe, dict(NO_LEGACY))
    c.close()


def test_a_lossless_reshape_of_a_root_rule_by_an_agent_is_refused(tmp_path):
    c = _repo(tmp_path, policy="local")
    _enforce(c, "no legacy in src", NO_LEGACY)
    r = _enforce(c, "no legacy in mid", NO_LEGACY_A)
    with pytest.raises(AuthorityError, match="does not outrank root"):
        authority.judge(c, "mid", {r.id: None}, policy="local")                     # lossless, but the human's ruling
    with pytest.raises(AuthorityError, match="does not outrank root"):
        authority.judge(c, "top", {r.id: {**NO_LEGACY_A, "pattern": "legacy"}}, policy="local")
    c.close()


def test_the_holder_may_drop_its_own_rule_only_losslessly(tmp_path):
    c = _repo(tmp_path, policy="local")
    mine = _local(c, tmp_path, "no legacy in mid", NO_LEGACY_A)
    with pytest.raises(AuthorityError, match="global loss"):
        authority.judge(c, "mid", {mine.id: None}, policy="local")
    _enforce(c, "no legacy in src", NO_LEGACY)
    authority.judge(c, "mid", {mine.id: None}, policy="local")
    authority.judge(c, "top", {mine.id: None}, policy="local")
    with pytest.raises(AuthorityError, match="neither mid nor above"):
        authority.judge(c, "side", {mine.id: None}, policy="local")
    with pytest.raises(AuthorityError, match="accept_policy"):
        authority.judge(c, "mid", {mine.id: None}, policy="human_only")
    c.close()


def test_a_node_rule_stays_inside_its_holders_region(tmp_path):
    c = _repo(tmp_path, policy="local")
    mine = _local(c, tmp_path, "no legacy in mid", NO_LEGACY_A)
    wider = dict(NO_LEGACY)                                         # lossless (forbids more), but beyond mid
    with pytest.raises(AuthorityError, match="scope gate.*does not own"):
        authority.judge(c, "top", {mine.id: wider}, policy="local")                 # top may change it, but mid would hold it
    c.close()


def test_root_is_not_gated_and_gets_the_delta(tmp_path):
    c = _repo(tmp_path, policy=None)
    r = _enforce(c, "no legacy in src", NO_LEGACY)
    with pytest.raises(AuthorityError, match="acting as root is the human's"):
        authority.judge(c, "root", {r.id: None})                    # an agent naming itself root
    d = authority.judge(c, "root", {r.id: None}, human=True)
    assert not d.lossless and d.allows[0].rule == r.id
    c.close()


def test_a_shadowing_accept_names_the_rule_it_would_set_aside(tmp_path):
    c = _repo(tmp_path, policy="local")
    (tmp_path / "src" / "mid.py").write_text("import legacy\ndef f():\n    return 2\n")
    child = _local(c, tmp_path, "mid imports legacy", {**NO_LEGACY_A, "expect": "present"})
    (tmp_path / "src" / "mid.py").write_text("def f():\n    return 2\n")
    p = _propose(c, "no legacy in top's files", {**NO_LEGACY, "paths": ["src/mid.py", "src/top.py"]},
                 ["src/mid.py", "src/top.py"])
    with pytest.raises(AuthorityError, match=f"set aside mid's rule \\[{child.id}\\]"):
        authority.accept_as(c, tmp_path, p.id, "top", "local")
    c.close()


# --- the verb ----------------------------------------------------------------------------------------------------

def _run(tmp_path, *argv):
    return main(["--root", str(tmp_path), *argv])


def test_change_by_an_agent_is_judged_by_the_law(tmp_path, capsys):
    c = _repo(tmp_path, policy="local")
    mine = _local(c, tmp_path, "no legacy in mid", NO_LEGACY_A)
    c.close()
    assert _run(tmp_path, "change", mine.id, "--drop", "--by", "mid", "--reason", "tidy") == 1
    assert "global loss" in capsys.readouterr().err
    c = ClaimCache(tmp_path)
    _enforce(c, "no legacy in src", NO_LEGACY)
    c.close()
    assert _run(tmp_path, "change", mine.id, "--drop", "--by", "mid", "--reason", "redundant") == 0
    assert "dropped (no information loss)" in capsys.readouterr().out
    c = ClaimCache(tmp_path)
    assert c.store.get(mine.id).rule_state == "overridden" and c.store.rule_history(mine.id)[-1]["action"] == "change"
    c.close()


def test_change_at_root_asks_the_human_with_both_directions(tmp_path, monkeypatch, capsys):
    c = _repo(tmp_path, policy=None)
    r = _enforce(c, "no legacy in mid", NO_LEGACY_A)
    c.close()
    asked = []
    monkeypatch.setattr(root_signal, "confirm", lambda root, what, yes=False: asked.append(what))
    assert _run(tmp_path, "change", r.id, "--probe", json.dumps(NO_LEGACY), "--reason", "widen") == 0
    assert "+ newly enforces: no lines matching" in asked[0] and "no information loss" in asked[0]
    asked.clear()
    assert _run(tmp_path, "change", r.id, "--drop", "--reason", "gone") == 0
    assert "- newly allows" in asked[0] and "GLOBAL LOSS" in asked[0]
    assert "recorded as a loss" in capsys.readouterr().out


def test_change_refuses_a_probe_that_fails_or_checks_nothing(tmp_path, monkeypatch):
    c = _repo(tmp_path, policy=None)
    r = _enforce(c, "no legacy in mid", NO_LEGACY_A)
    c.close()
    monkeypatch.setattr(root_signal, "confirm", lambda *a, **k: pytest.fail("must not ask"))
    assert _run(tmp_path, "change", r.id, "--probe", json.dumps({**NO_LEGACY_A, "expect": "present"}), "--reason", "x") == 1
    assert _run(tmp_path, "change", r.id, "--probe", json.dumps({**NO_LEGACY, "paths": ["no/**"]}), "--reason", "x") == 1


def test_an_agent_narrow_that_cuts_future_files_is_a_global_loss(tmp_path):
    """No file is cut today, but the narrowed pattern no longer covers files named src/mid_gen*.py: decided on patterns."""
    from fractal_harness import conflict
    from fractal_harness.conflict import ConflictError
    from test_conflict import _friction, _local as _conflict_local
    c, _ = _conflict_local(tmp_path)
    loc = _propose(c, "no legacy in mid's modules", {**NO_LEGACY, "paths": ["src/mid*.py"]}, ["src/mid.py"])
    authority.accept_as(c, tmp_path, loc.id, "mid", "local")
    cid = _friction(tmp_path, c, loc)
    with pytest.raises(ConflictError, match="global loss"):
        conflict.resolve(c, tmp_path, cid, "narrow", "generated", exclude=["src/mid_gen*.py"])
    assert c.store.get(loc.id).probe["paths"] == ["src/mid*.py"] and "exclude_paths" not in c.store.get(loc.id).probe
    c.close()

"""Global loss (theory: a change loses information only if it lets through states that no remaining rule forbids).

A change that drops, demotes or narrows a rule is ABSORBED when other enforced, in-effect rules still forbid every
state it let through; then there is no loss and the acting layer's authority suffices. Otherwise it is a GLOBAL
LOSS: only a human may make it (arbitration). Absorption is proven only for grep leaves over glob patterns (not the
files that exist now); anything else is "not provable" and counts as a loss.
"""
import pytest

from fractal_harness import authority, conflict, loss
from fractal_harness.authority import AuthorityError
from fractal_harness.cache import ClaimCache
from fractal_harness.conflict import ConflictError

from test_authority import HAS_DEF, NO_LEGACY, _propose, _repo
from test_conflict import _friction, _local


def g(pattern, paths, expect="absent", **kw):
    return {"type": "grep", "pattern": pattern, "paths": paths, "expect": expect, **kw}


# --- glob containment (sound, syntactic) ----------------------------------------------------------------------

@pytest.mark.parametrize("a,b,covers", [
    ("src/**/*.py", "src/**/*.py", True),
    ("src/**/*.py", "src/a/*.py", True),
    ("src/**/*.py", "src/a.py", True),
    ("src/**/*.py", "src/a/**/*.py", True),
    ("src/**", "src/a/b.rs", True),
    ("src/**/*", "src/a/*.rs", True),
    ("**/*.py", "lib/x/*.py", True),
    ("src/*.py", "src/a/b.py", False),
    ("src/**", "lib/x", False),
    ("src/**/*.py", "src/**/*.rs", False),
    ("src/**/*.py", "src/**/*", False),
    ("src/a.py", "src/*.py", False),
    ("src/**/*.py", "srcx/a.py", False),
    ("src/**", "srcx/*.py", False),          # a prefix is a directory, not a string prefix
    ("src/?.py", "src/*.py", False),         # wildcards in b are not literal characters
    ("src/**/*.py", "src/*x/*.py", True),
    ("**/proptest-regressions/**", "crates/**/proptest-regressions/**", True),
    ("**/proptest-regressions/**", "crates/**/proptest-regressions", False),
    ("src/**/test_*", "src/a/test_x*.py", True),
    ("src/**/test_*", "src/a/*test_x.py", False),
    ("src/*/*.py", "src/**/*.py", False),
    ("src/**/*.py", "src/a**b/*.py", True),     # `a**b` crosses directories, which `**` covers
    ("src/*/*.py", "src/a**b/*.py", False),
    ("src/**", "src", False),
    ("src/**/*.py", "src/*.p?", False),
    ("**", "anything/**/at/*.all", True),
])
def test_glob_covers(a, b, covers):
    assert loss.glob_covers(a, b) is covers


# --- the absorption check on probes ---------------------------------------------------------------------------

def test_an_absent_rule_is_absorbed_by_the_same_pattern_over_a_wider_glob():
    lost = g("eval", ["src/core/**/*.py"])
    assert loss.absorbs_leaf(g("eval", ["src/**/*.py"]), lost, "upper")


def test_listing_todays_files_does_not_absorb_a_glob():
    lost = g("eval", ["src/*.py"])
    assert not loss.absorbs_leaf(g("eval", ["src/a.py", "src/b.py"]), lost, "upper")   # a new src/c.py would be lost


def test_a_different_line_exclusion_does_not_absorb():
    lost = g("eval", ["src/**/*.py"])
    assert not loss.absorbs_leaf(g("eval", ["src/**/*.py"], exclude="^#"), lost, "upper")
    assert loss.absorbs_leaf(g("eval", ["src/**/*.py"]), g("eval", ["src/**/*.py"], exclude="^#"), "upper")


def test_an_absorber_that_skips_files_the_lost_rule_covered_does_not_absorb():
    lost = g("eval", ["src/**/*.py"])
    assert not loss.absorbs_leaf(g("eval", ["src/**/*.py", "!src/gen.py"]), lost, "upper")
    assert loss.absorbs_leaf(g("eval", ["src/**/*.py", "!src/gen.py"]), g("eval", ["src/**/*.py", "!src/gen.py"]), "upper")


def test_a_presence_rule_is_absorbed_by_a_narrower_presence_rule_and_not_a_wider_one():
    lost = g(r"^def f\(", ["src/**/*.py"], expect="present")
    assert loss.absorbs_leaf(g(r"^def f\(", ["src/mid.py"], expect="present"), lost, "lower")
    assert not loss.absorbs_leaf(g(r"^def f\(", ["**/*.py"], expect="present"), lost, "lower")
    assert not loss.absorbs_leaf(g(r"^def f\(", ["src/mid.py"], expect="absent"), lost, "lower")


def test_a_looser_bound_does_not_absorb():
    assert not loss.absorbs_leaf(g("x", ["src/**/*.py"], expect={"max": 3}), g("x", ["src/a.py"], expect={"max": 1}), "upper")
    assert loss.absorbs_leaf(g("x", ["src/**/*.py"], expect={"max": 1}), g("x", ["src/a.py"], expect={"max": 3}), "upper")
    assert not loss.absorbs_leaf(g("x", ["src/a.py"], expect={"min": 1}), g("x", ["src/**/*.py"], expect={"min": 2}), "lower")


def test_the_reason_names_the_bound_that_is_lost():
    r = loss.assess_probes(g(r"^def f\(", ["src/mid.py"], expect="present"), None, [])
    assert not r.absorbed and "at least 1" in r.why and "at most" not in r.why


def test_non_grep_rules_are_not_provable():
    r = loss.assess_probes({"type": "command", "run": "true"}, None, [g("x", ["**"])])
    assert not r.absorbed and "not provable" in r.why


def test_narrowing_an_absent_rule_is_absorbed_by_a_rule_over_what_was_cut():
    before = g("eval", ["src/**/*.py"])
    after = g("eval", ["src/**/*.py"], exclude_paths=["src/gen/**/*.py"])
    assert loss.assess_probes(before, after, [g("eval", ["src/gen/**"])]).absorbed
    assert not loss.assess_probes(before, after, [g("eval", ["src/other/**"])]).absorbed


def test_an_all_probe_needs_every_leaf_absorbed():
    before = {"type": "all", "probes": [g("eval", ["src/**/*.py"]), g("exec", ["src/**/*.py"])]}
    assert not loss.assess_probes(before, None, [g("eval", ["**/*.py"])]).absorbed
    assert loss.assess_probes(before, None, [g("eval", ["**/*.py"]), g("exec", ["**/*.py"])]).absorbed


def test_a_leaf_of_an_enforced_all_rule_can_absorb():
    other = {"type": "all", "probes": [g("eval", ["**/*.py"]), g("zzz", ["x.py"], expect="present")]}
    assert loss.assess_probes(g("eval", ["src/*.py"]), None, [other]).absorbed


# --- the gate: an agent's change is allowed only when absorbed -----------------------------------------------

def _root_rule(c, text, probe):
    e = _propose(c, text, probe)
    c.set_rule(e.id, "enforced")
    return e


def test_a_node_override_that_is_a_global_loss_is_refused(tmp_path):
    c = _repo(tmp_path)
    e = _propose(c, "mid has def f", HAS_DEF, ["src/mid.py"])
    authority.accept_as(c, tmp_path, e.id, "mid", "local")
    with pytest.raises(AuthorityError, match="global loss"):
        authority.override(c, e.id, "top", "superseded", "local")
    assert c.store.get(e.id).enforced
    c.close()


def test_a_node_override_that_is_absorbed_is_allowed_and_recorded(tmp_path):
    c = _repo(tmp_path)
    e = _propose(c, "mid has def f", HAS_DEF, ["src/mid.py"])
    authority.accept_as(c, tmp_path, e.id, "mid", "local")
    keeper = _root_rule(c, "f is defined in mid", {**HAS_DEF})
    got = authority.override(c, e.id, "top", "duplicate of the root rule", "local")
    assert got.rule_state == "overridden"
    h = c.store.rule_history(e.id)[-1]
    assert h["detail"]["loss"] is False and h["detail"]["absorbed_by"] == [keeper.id]
    c.close()


def test_a_proposed_rule_does_not_absorb(tmp_path):
    c = _repo(tmp_path)
    e = _propose(c, "mid has def f", HAS_DEF, ["src/mid.py"])
    authority.accept_as(c, tmp_path, e.id, "mid", "local")
    _propose(c, "f is defined in mid", {**HAS_DEF})
    with pytest.raises(AuthorityError, match="global loss"):
        authority.override(c, e.id, "top", "dup", "local")
    c.close()


def test_the_human_may_make_a_global_loss_and_it_is_recorded_as_one(tmp_path):
    c = _repo(tmp_path)
    r = _root_rule(c, "no legacy in src", NO_LEGACY)
    authority.override(c, r.id, "root", "obsolete", "human_only")
    h = c.store.rule_history(r.id)[-1]
    assert h["detail"]["loss"] is True and h["detail"]["absorbed_by"] == []
    c.close()


def test_an_agent_narrow_of_a_presence_rule_loses_nothing(tmp_path):
    c, e = _local(tmp_path)                    # mid: `def f` present in src/mid.py
    cid = _friction(tmp_path, c, e)
    conflict.resolve(c, tmp_path, cid, "narrow", "generated section", exclude=["src/gen/*.py"])
    [h] = [x for x in c.store.rule_history(e.id) if x["action"] == "narrow"]
    assert h["detail"]["loss"] is False and h["detail"]["absorbed_by"] == [e.id]    # its own, stronger probe
    c.close()


def test_an_agent_narrow_that_cuts_nothing_loses_nothing(tmp_path):
    """The rule names the literal file src/mid.py; excluding src/gen/*.py cannot cut it (decided on the patterns,
    not today's files). A narrow that cuts a real file and nothing covers it is a loss: test_law."""
    from test_authority import NO_LEGACY_A
    c, _ = _local(tmp_path)
    loc = _propose(c, "mid has no legacy import", NO_LEGACY_A, ["src/mid.py"])
    authority.accept_as(c, tmp_path, loc.id, "mid", "local")
    cid = _friction(tmp_path, c, loc)
    conflict.resolve(c, tmp_path, cid, "narrow", "generated", exclude=["src/gen/*.py"])
    [h] = [h for h in c.store.rule_history(loc.id) if h["action"] == "narrow"]
    assert h["detail"]["loss"] is False
    c.close()


def test_the_human_prompt_says_whether_information_is_lost(tmp_path, monkeypatch):
    from fractal_harness import root_signal
    from fractal_harness.cli import main
    asked = []
    monkeypatch.setattr(root_signal, "confirm", lambda root, what, yes=False: asked.append(what))
    c = _repo(tmp_path)
    r = _root_rule(c, "no legacy in src", NO_LEGACY)
    dup = _root_rule(c, "no legacy anywhere", {**NO_LEGACY, "paths": ["**/*.py"]})
    c.close()
    assert main(["--root", str(tmp_path), "override", r.id, "--reason", "dup", "--yes"]) == 0
    assert "no information loss" in asked[-1] and dup.id in asked[-1]
    assert main(["--root", str(tmp_path), "override", dup.id, "--reason", "gone", "--yes"]) == 0
    assert "GLOBAL LOSS" in asked[-1]


# --- subtree accept and shadowing --------------------------------------------------------------------------------

def test_a_parent_may_accept_an_addition_over_its_childrens_files(tmp_path):
    c = _repo(tmp_path)
    e = _propose(c, "mid keeps def f", HAS_DEF, ["src/mid.py"])
    r = authority.accept_as(c, tmp_path, e.id, "top", "local")
    assert r["edge"].enforced and r["edge"].authority == "top"
    c.close()


def test_a_sibling_still_may_not(tmp_path):
    c = _repo(tmp_path)
    e = _propose(c, "mid keeps def f", HAS_DEF, ["src/mid.py"])
    with pytest.raises(AuthorityError, match="does not own"):
        authority.accept_as(c, tmp_path, e.id, "side", "local")
    c.close()


def test_a_parent_addition_that_would_set_aside_a_child_rule_is_refused(tmp_path):
    c = _repo(tmp_path)
    (tmp_path / "src" / "mid.py").write_text("import legacy\ndef f():\n    return 2\n")
    child = _propose(c, "mid must import legacy", {**NO_LEGACY, "paths": ["src/mid.py"], "expect": "present"},
                     ["src/mid.py"])
    authority.accept_as(c, tmp_path, child.id, "mid", "local")
    (tmp_path / "src" / "mid.py").write_text("def f():\n    return 2\n")
    parent = _propose(c, "no legacy in mid", {**NO_LEGACY, "paths": ["src/mid.py"]}, ["src/mid.py"])
    with pytest.raises(AuthorityError, match="set aside"):
        authority.accept_as(c, tmp_path, parent.id, "top", "local")
    assert c.store.get(parent.id).rule_state == "proposed"
    c.close()

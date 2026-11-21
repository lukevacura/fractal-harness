"""One door for rule changes (theory: every change to the judge set is classified and recorded; only a higher
layer may let more states through).

Regression tests for three side doors found on 2026-11-21:
  - re-`put` of an enforced rule with the same text and a weaker probe swapped the probe in place and kept the rule
    enforced and the root's;
  - `fractal rm` deleted an enforced root rule with no authority check and no rule_history entry;
  - `fractal reject` (and set_rule in general) moved rules between states without a rule_history entry.
And the identity rule: what a human accepted is the rule's text AND its probe and read paths (its revision); an
enforced rule whose content no longer matches the revision it was accepted at fails `fractal check`.
"""
import json
import re
import sqlite3
from pathlib import Path

import pytest

from fractal_harness import authority, rules
from fractal_harness.cache import CacheError, ClaimCache
from fractal_harness.check import check
from fractal_harness.cli import main

from test_authority import HAS_DEF, NO_LEGACY, NO_LEGACY_A, _propose, _repo

WEAK = {"type": "grep", "pattern": "NEVER_MATCHES_ANYTHING", "paths": ["src/**/*.py"], "expect": "absent"}


def _enforced(tmp_path, policy=None):
    c = _repo(tmp_path, policy=policy)
    e = _propose(c, "no legacy", NO_LEGACY)
    c.close()
    assert main(["--root", str(tmp_path), "accept", e.id, "--yes"]) == 0
    return e.id


# --- rule 1: a rule's content is part of what was accepted ------------------------------------------------

def test_reputting_an_enforced_rule_with_a_different_probe_is_refused(tmp_path):
    eid = _enforced(tmp_path)
    c = ClaimCache(tmp_path)
    with pytest.raises(CacheError, match="enforced"):
        _propose(c, "no legacy", WEAK)
    got = c.store.get(eid)
    assert got.enforced and got.layer == "root" and got.probe == NO_LEGACY
    c.close()


def test_the_weakened_rule_still_catches_the_violation(tmp_path):
    eid = _enforced(tmp_path)
    assert main(["--root", str(tmp_path), "propose", "no legacy", "--probe", json.dumps(WEAK)]) != 0
    (tmp_path / "src" / "other.py").write_text("import legacy\n")
    code, out = check(tmp_path)
    assert code == 1 and eid in out


def test_reputting_an_enforced_rule_with_different_reads_is_refused(tmp_path):
    eid = _enforced(tmp_path)
    c = ClaimCache(tmp_path)
    with pytest.raises(CacheError, match="enforced"):
        _propose(c, "no legacy", NO_LEGACY, reads=["src/top.py"])
    assert c.store.get(eid).reads == []
    c.close()


def test_reasserting_an_enforced_rule_unchanged_is_allowed(tmp_path):
    eid = _enforced(tmp_path)
    c = ClaimCache(tmp_path)
    e = _propose(c, "no legacy", NO_LEGACY)
    assert e.id == eid and e.enforced and e.layer == "root"
    c.close()


def test_a_proposal_may_still_be_revised_in_place(tmp_path):
    c = _repo(tmp_path, policy=None)
    e = _propose(c, "no legacy", NO_LEGACY)
    e2 = _propose(c, "no legacy", {**NO_LEGACY, "paths": ["src/mid.py"]})
    assert e2.id == e.id and e2.rule_state == "proposed" and e2.probe["paths"] == ["src/mid.py"]
    c.close()


@pytest.mark.parametrize("state", ["rejected", "overridden"])
def test_a_ruled_on_rule_cannot_be_revised_in_place(tmp_path, state):
    c = _repo(tmp_path)
    e = _propose(c, "mid has def f", HAS_DEF, ["src/mid.py"])
    if state == "rejected":
        c.set_rule(e.id, "rejected")
    else:
        authority.accept_as(c, tmp_path, e.id, "mid", "local")
        authority.override(c, e.id, "root", "renaming f")
    with pytest.raises(CacheError, match=state):
        _propose(c, "mid has def f", {**HAS_DEF, "pattern": "^def"}, ["src/mid.py"])
    assert c.store.get(e.id).probe == HAS_DEF
    c.close()


def test_every_transition_records_the_revision_it_applies_to(tmp_path):
    eid = _enforced(tmp_path)
    c = ClaimCache(tmp_path)
    e = c.store.get(eid)
    h = c.store.rule_history(eid)[-1]
    assert h["action"] == "accept" and h["detail"]["revision"] == rules.revision(e)
    assert h["detail"]["from"] == "proposed" and h["detail"]["to"] == "enforced"
    c.close()


def test_revision_covers_text_probe_and_reads_but_not_verdict_state(tmp_path):
    c = _repo(tmp_path, policy=None)
    e = _propose(c, "no legacy", NO_LEGACY)
    r = rules.revision(e)
    from dataclasses import replace
    assert rules.revision(replace(e, status="failed", detail="x", rule_state="enforced", authority="root")) == r
    assert rules.revision(replace(e, probe=WEAK)) != r
    assert rules.revision(replace(e, reads=["src/top.py"])) != r
    assert rules.revision(replace(e, post="no legacy!")) != r
    c.close()


def test_check_fails_when_an_enforced_rule_changed_outside_the_door(tmp_path):
    eid = _enforced(tmp_path)
    db = sqlite3.connect(tmp_path / ".fractal" / "edges.db")
    with db:
        db.execute("UPDATE edges SET probe=? WHERE id=?", (json.dumps(WEAK), eid))
    db.close()
    code, out = check(tmp_path)
    assert code == 1 and eid in out and "changed outside" in out


def test_check_ignores_rules_enforced_before_revisions_were_recorded(tmp_path):
    eid = _enforced(tmp_path)
    db = sqlite3.connect(tmp_path / ".fractal" / "edges.db")
    with db:       # a store from before this change: history without revisions
        db.execute("UPDATE rule_history SET detail='{}' WHERE edge_id=?", (eid,))
    db.close()
    code, out = check(tmp_path)
    assert code == 0 and "changed outside" not in out


# --- rule 2: one door; deletion and every state change is recorded ------------------------------------------

@pytest.mark.parametrize("state", ["enforced", "rejected", "overridden"])
def test_rm_refuses_a_rule_that_has_been_ruled_on(tmp_path, state, capsys):
    c = _repo(tmp_path)
    e = _propose(c, "mid has def f", HAS_DEF, ["src/mid.py"])
    if state == "enforced":
        c.close()
        assert main(["--root", str(tmp_path), "accept", e.id, "--yes"]) == 0
        c = ClaimCache(tmp_path)
    elif state == "rejected":
        c.set_rule(e.id, "rejected")
    else:
        authority.accept_as(c, tmp_path, e.id, "mid", "local")
        authority.override(c, e.id, "root", "renaming f")
    c.close()
    capsys.readouterr()
    assert main(["--root", str(tmp_path), "rm", e.id]) != 0
    err = capsys.readouterr().err
    assert state in err
    c = ClaimCache(tmp_path)
    assert c.store.get(e.id) is not None and c.store.get(e.id).rule_state == state
    c.close()


def test_rm_of_an_enforced_rule_points_at_override(tmp_path, capsys):
    eid = _enforced(tmp_path)
    capsys.readouterr()
    assert main(["--root", str(tmp_path), "rm", eid]) != 0
    assert "override" in capsys.readouterr().err


def test_rm_of_a_proposal_is_allowed_and_recorded(tmp_path):
    c = _repo(tmp_path, policy=None)
    e = _propose(c, "no legacy", NO_LEGACY)
    rev = rules.revision(e)
    c.delete(e.id)
    assert c.store.get(e.id) is None
    h = c.store.rule_history(e.id)[-1]
    assert h["action"] == "delete" and h["detail"]["from"] == "proposed" and h["detail"]["revision"] == rev
    assert h["detail"]["probe"] == NO_LEGACY        # what was dropped stays readable
    c.close()


def test_rm_of_a_fact_is_unchanged(tmp_path):
    c = _repo(tmp_path, policy=None)
    e = c.put("mid defines f", ["src/mid.py"], probe=HAS_DEF)
    c.delete(e.id)
    assert c.store.get(e.id) is None and c.store.rule_history(e.id) == []
    c.close()


def test_reject_is_recorded(tmp_path):
    c = _repo(tmp_path, policy=None)
    e = _propose(c, "no legacy", NO_LEGACY)
    c.close()
    assert main(["--root", str(tmp_path), "reject", e.id, "--yes"]) == 0
    c = ClaimCache(tmp_path)
    h = c.store.rule_history(e.id)[-1]
    assert (h["action"], h["who"], h["detail"]["from"], h["detail"]["to"]) == ("reject", "human", "proposed", "rejected")
    c.close()


def test_rejecting_an_enforced_rule_is_recorded_as_a_loss(tmp_path):
    eid = _enforced(tmp_path)
    assert main(["--root", str(tmp_path), "reject", eid, "--yes"]) == 0
    c = ClaimCache(tmp_path)
    h = c.store.rule_history(eid)[-1]
    assert h["action"] == "reject" and h["detail"]["from"] == "enforced" and h["detail"]["loss"] is True
    c.close()


def test_human_accept_writes_one_history_entry(tmp_path):
    eid = _enforced(tmp_path)
    c = ClaimCache(tmp_path)
    assert [h["action"] for h in c.store.rule_history(eid)] == ["accept"]
    c.close()


def test_narrow_records_old_and_new_revision(tmp_path):
    c = _repo(tmp_path)
    e = _propose(c, "no legacy in mid", NO_LEGACY_A, ["src/mid.py"])
    authority.accept_as(c, tmp_path, e.id, "mid", "local")
    before = rules.revision(c.store.get(e.id))
    rules.change_probe(c, e.id, {**NO_LEGACY_A, "exclude": "^#"}, actor="top", action="narrow",
                       reason="other.py is generated")
    after = c.store.get(e.id)
    h = c.store.rule_history(e.id)[-1]
    assert h["action"] == "narrow" and h["detail"]["old_revision"] == before
    assert h["detail"]["revision"] == rules.revision(after) != before and h["detail"]["loss"] is True
    c.close()
    code, out = check(tmp_path)          # the door's own probe change is not tampering
    assert "changed outside" not in out


def test_only_the_door_changes_rule_state_authority_or_probe():
    """Static: outside store.py (the primitives) and rules.py (the door), nothing in the package calls the store's
    rule-mutating primitives. A new call site must go through rules.py so it is classified and recorded."""
    pkg = Path(__file__).resolve().parents[1] / "src" / "fractal_harness"
    prim = re.compile(r"\.(set_rule_state|set_authority|set_probe)\(")
    offenders = [f"{p.name}:{i}" for p in pkg.glob("*.py") if p.name not in ("store.py", "rules.py")
                 for i, line in enumerate(p.read_text().splitlines(), 1) if prim.search(line)]
    assert offenders == []

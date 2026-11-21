"""The human queue: every pending human decision with what it would newly forbid and newly allow, fresh."""
import json

from fractal_harness import authority, conflict, decisions, loss
from fractal_harness.cache import ClaimCache
from fractal_harness.cli import main

from test_authority import HAS_DEF, NO_LEGACY, NO_LEGACY_A, _propose, _repo


def _q(c, tmp_path, **kw):
    return {i.id: i for i in decisions.items(c, tmp_path, **kw)}


def _enforce(c, text, probe, reads=None):
    e = _propose(c, text, probe, reads)
    c.set_rule(e.id, "enforced")
    return e


def test_an_empty_queue_says_so(tmp_path):
    c = _repo(tmp_path)
    assert decisions.items(c, tmp_path) == []
    assert "nothing awaits" in decisions.render([])
    c.close()


def test_a_new_rule_lists_what_it_newly_enforces(tmp_path):
    c = _repo(tmp_path)
    p = _propose(c, "no legacy in src", NO_LEGACY)
    [it] = decisions.items(c, tmp_path)
    assert it.id == p.id and it.verdict == "addition" and it.allows == []
    assert it.forbids == ["no lines matching /^import legacy\\b/ in src/**/*.py"] and it.already == []
    assert f"fractal accept {p.id}" in it.decide
    c.close()


def test_a_rule_already_covered_adds_nothing(tmp_path):
    c = _repo(tmp_path)
    keep = _enforce(c, "no legacy in src", NO_LEGACY)
    p = _propose(c, "no legacy in mid", NO_LEGACY_A)
    it = _q(c, tmp_path)[p.id]
    assert it.verdict == "nothing" and it.forbids == [] and it.already[0][1] == keep.id
    assert any("rejecting it loses nothing" in n for n in it.notes)
    c.close()


def test_a_wider_rule_is_not_covered_by_a_narrower_one(tmp_path):
    c = _repo(tmp_path)
    _enforce(c, "no legacy in mid", NO_LEGACY_A)
    p = _propose(c, "no legacy in src", NO_LEGACY)
    assert _q(c, tmp_path)[p.id].verdict == "addition"
    c.close()


def test_overlap_between_proposals_is_noted(tmp_path):
    c = _repo(tmp_path)
    wide = _propose(c, "no legacy in src", NO_LEGACY)
    narrow = _propose(c, "no legacy in mid", NO_LEGACY_A)
    q = _q(c, tmp_path)
    assert q[narrow.id].verdict == "addition"                       # nothing enforced covers it yet
    assert any(f"once proposal(s) {wide.id}" in n for n in q[narrow.id].notes)
    assert not any("once proposal" in n for n in q[wide.id].notes)
    c.close()


def test_a_contradiction_with_a_root_rule_comes_first(tmp_path):
    c = _repo(tmp_path)
    (tmp_path / "src" / "mid.py").write_text("import legacy\ndef f():\n    return 2\n")
    _enforce(c, "mid imports legacy", {**NO_LEGACY_A, "expect": "present"})
    bad = _propose(c, "no legacy anywhere", NO_LEGACY)
    plain = _propose(c, "mid has def f", HAS_DEF)
    its = decisions.items(c, tmp_path)
    assert its[0].id == bad.id and its[0].verdict == "contradiction"
    assert any("contradicts" in n for n in its[0].notes)
    assert {i.id: i.verdict for i in its}[plain.id] == "addition"
    c.close()


def test_a_root_accept_that_sets_aside_a_node_rule_is_a_global_loss(tmp_path):
    c = _repo(tmp_path)
    (tmp_path / "src" / "mid.py").write_text("import legacy\ndef f():\n    return 2\n")
    child = _propose(c, "mid must import legacy", {**NO_LEGACY_A, "expect": "present"}, ["src/mid.py"])
    authority.accept_as(c, tmp_path, child.id, "mid", "local")
    (tmp_path / "src" / "mid.py").write_text("def f():\n    return 2\n")
    p = _propose(c, "no legacy in src", NO_LEGACY)
    it = _q(c, tmp_path)[p.id]
    assert it.verdict == "loss" and it.allows and child.id in it.allows[0] and "no information loss" not in it.allows[0]
    c.close()


def test_a_violated_proposal_is_flagged_with_the_debt_hint(tmp_path):
    c = _repo(tmp_path)
    p = _propose(c, "no legacy in src", NO_LEGACY)
    (tmp_path / "src" / "old.py").write_text("import legacy\n")         # the verdict is re-run: fresh
    it = _q(c, tmp_path)[p.id]
    assert it.verdict == "violated" and it.status.startswith("failed") and any("--debt" in n for n in it.notes)
    c.close()


def test_a_command_rule_is_unknown_and_its_verdict_is_rerun_only_with_slow(tmp_path):
    c = _repo(tmp_path)
    p = _propose(c, "the suite passes", {"type": "command", "run": "true"})
    it = _q(c, tmp_path)[p.id]
    assert it.verdict == "unknown" and any("slow probe" in n for n in it.notes)
    it = _q(c, tmp_path, slow=True)[p.id]
    assert not any("slow probe" in n for n in it.notes) and it.status.startswith("verified")
    c.close()


def test_an_arbitration_request_shows_what_revising_would_let_through(tmp_path):
    c = _repo(tmp_path)
    r = _enforce(c, "no legacy in src", NO_LEGACY)
    req = conflict.raise_conflict(c, tmp_path, r.id, "the premise is wrong")
    it = _q(c, tmp_path)[req["id"]]
    assert it.kind == "arbitration" and it.verdict == "arbitration"
    assert "if the rule is dropped" in it.allows[0] and "no lines matching" in it.allows[0]
    assert "upheld: nothing changes" in it.notes
    _enforce(c, "no legacy anywhere", {**NO_LEGACY, "paths": ["**/*.py"]})
    it = _q(c, tmp_path)[req["id"]]
    assert "no information loss" in it.allows[0]
    c.close()


def test_a_rule_changed_outside_the_cli_comes_first(tmp_path):
    import sqlite3
    c = _repo(tmp_path)
    r = _enforce(c, "no legacy in src", NO_LEGACY)
    _propose(c, "mid has def f", HAS_DEF)
    c.close()
    db = sqlite3.connect(tmp_path / ".fractal" / "edges.db")
    with db:
        db.execute("UPDATE edges SET probe=? WHERE id=?", (json.dumps({**NO_LEGACY, "pattern": "NEVER"}), r.id))
    db.close()
    c = ClaimCache(tmp_path)
    its = decisions.items(c, tmp_path)
    assert its[0].kind == "changed" and its[0].id == r.id
    c.close()


def test_an_unpinned_policy_edit_is_an_item(tmp_path):
    from test_root_signal import _policy
    c = _repo(tmp_path)
    c.close()
    _policy(tmp_path, accept_policy="human_only", root_signal="flag")
    assert main(["--root", str(tmp_path), "pin", "--yes"]) == 0
    _policy(tmp_path, accept_policy="local", root_signal="flag")
    c = ClaimCache(tmp_path)
    [it] = decisions.items(c, tmp_path)
    assert it.kind == "policy" and it.id == "accept_policy" and "human_only" in it.notes[0]
    c.close()


def test_cli_queue_text_and_json(tmp_path, capsys):
    c = _repo(tmp_path)
    p = _propose(c, "no legacy in src", NO_LEGACY)
    c.close()
    capsys.readouterr()
    assert main(["--root", str(tmp_path), "queue"]) == 0
    out = capsys.readouterr().out
    assert "1 decision" in out and "+ newly enforces: no lines matching" in out and p.id in out
    assert main(["--root", str(tmp_path), "--json", "queue"]) == 0
    [d] = json.loads(capsys.readouterr().out)
    assert d["id"] == p.id and d["label"] == "adds information" and d["forbids"]


# --- grouping keeps meaning ------------------------------------------------------------------------------------

def g(paths, expect):
    return {"type": "grep", "pattern": "x", "paths": paths, "expect": expect}


def test_per_file_lower_bounds_are_grouped_as_each():
    probe = {"type": "all", "probes": [g([f"s/{i}.py"], "present") for i in range(5)]}
    [line] = loss.coverage(probe, [], []).new
    assert line.startswith("at least 1 line matching /x/ in each of s/0.py") and "2 more" in line


def test_nonzero_upper_bounds_over_different_files_are_not_merged():
    probe = {"type": "all", "probes": [g(["a.py"], {"max": 3}), g(["b.py"], {"max": 3})]}
    assert loss.coverage(probe, [], []).new == ["at most 3 lines matching /x/ in a.py",
                                                "at most 3 lines matching /x/ in b.py"]


def test_zero_upper_bounds_are_merged_soundly():
    probe = {"type": "all", "probes": [g(["a.py"], "absent"), g(["b.py"], "absent")]}
    assert loss.coverage(probe, [], []).new == ["no lines matching /x/ in a.py, b.py"]


def test_adds_nothing_is_never_claimed_over_an_unknown_part():
    covered = {"type": "grep", "pattern": "x", "paths": ["a.py"], "expect": "absent"}
    cov = loss.coverage({"type": "all", "probes": [covered, {"type": "command", "run": "true"}]}, [covered], ["k"])
    assert cov.new == [] and cov.unknown and not cov.adds_nothing


def test_a_contradiction_with_another_proposal_is_a_note_not_an_inconsistency(tmp_path):
    c = _repo(tmp_path)
    (tmp_path / "src" / "mid.py").write_text("import legacy\ndef f():\n    return 2\n")
    other = _propose(c, "mid imports legacy", {**NO_LEGACY_A, "expect": "present"})
    p = _propose(c, "no legacy anywhere", NO_LEGACY)
    it = _q(c, tmp_path)[p.id]
    assert it.verdict != "contradiction" and any("contradicts proposed rule" in n and other.id in n for n in it.notes)
    c.close()


def test_partial_overlap_with_a_proposal_is_not_called_redundant(tmp_path):
    c = _repo(tmp_path)
    _propose(c, "no legacy in mid", NO_LEGACY_A)
    both = _propose(c, "no legacy and def f in mid", {"type": "all", "probes": [NO_LEGACY_A, HAS_DEF]})
    notes = _q(c, tmp_path)[both.id].notes
    assert any(n.startswith("overlaps proposal") for n in notes) and not any("once proposal" in n for n in notes)
    c.close()


def test_a_conflict_on_a_human_node_is_in_the_queue(tmp_path):
    from test_conflict import _friction, _local
    c, e = _local(tmp_path, extra='human_nodes = ["top"]\n')
    cid = _friction(tmp_path, c, e)
    it = _q(c, tmp_path)[cid]
    assert it.kind == "arbitration" and "if the rule is dropped" in it.allows[0]
    c.close()


def test_a_rule_with_no_probe_is_not_called_redundant(tmp_path):
    c = _repo(tmp_path)
    e = c.put("a rule nothing checks", [], kind="invariant")
    it = _q(c, tmp_path)[e.id]
    assert it.verdict == "unknown" and any("no probe" in n for n in it.notes)
    c.close()

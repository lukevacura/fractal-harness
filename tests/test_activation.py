"""P2-P4: evidence gaps and mutation-checked tests, join nodes, and the activation report."""
import pytest

from fractal_harness import activation, ledger, probes
from fractal_harness.cli import main
from fractal_harness.nodes import lint, parse_header, repo_lint, spec_key

from test_nodes import _git, _record, _repo, _spec


# --- P4: activations --------------------------------------------------------------------------

def _due(c, root):
    return {r["node"]: {e["kind"] for e in r["events"]} for r in activation.due(c, root)}


def test_nothing_is_due_until_something_happens(tmp_path):
    c = _repo(tmp_path)
    assert _due(c, tmp_path) == {}


def test_a_parents_contract_change_wakes_its_children_only(tmp_path):
    c = _repo(tmp_path)
    (tmp_path / "stats.md").write_text(_spec(parents="gp", guarantee="Exact windowed mean.",
                                             condition="For every window.", exports="lib.rs::update"))
    due = _due(c, tmp_path)
    assert due["child"] == {"contract"} and due["stats"] == {"spec"} and "naive" not in due
    _git(tmp_path, "checkout", "-q", "--", "stats.md")
    (tmp_path / "gp.md").write_text(_spec(guarantee="Grandparent changed."))
    assert _due(c, tmp_path) == {"gp": {"spec"}, "stats": {"contract"}}        # a grandparent is not the child's premise


def test_an_implementation_change_that_keeps_the_contract_wakes_nobody(tmp_path):
    c = _repo(tmp_path)
    (tmp_path / "lib.rs").write_text((tmp_path / "lib.rs").read_text().replace("secret_impl(x)", "other_impl(x)"))
    assert _due(c, tmp_path) == {}


def test_a_failing_probe_on_the_nodes_files_wakes_it_but_a_proposal_waits_for_a_human(tmp_path):
    c = _repo(tmp_path)
    bad = c.put("lib has no secret", ["lib.rs"], kind="invariant",
                probe={"type": "grep", "pattern": "secret_impl", "paths": ["lib.rs"], "expect": "absent"})
    c.put("lib is documented", ["lib.rs"], kind="invariant",
          probe={"type": "grep", "pattern": "///", "paths": ["lib.rs"]})
    c.put("elsewhere", ["other.rs"], kind="invariant", probe={"type": "grep", "pattern": "x", "paths": ["other.rs"]})
    c.check(bad.id)
    r = activation.scan(c, tmp_path)
    assert {row["node"]: {e["kind"] for e in row["events"]} for row in r["nodes"]} == {"stats": {"probe"}}
    assert sorted((p["rule"], p["nodes"]) for p in r["proposals"]) == [
        ("lib has no secret", ["stats"]), ("lib is documented", ["stats"])]      # "elsewhere" touches no node
    assert "2 proposal(s) awaiting a human" in activation.render(r)


def test_a_rejected_rule_raises_no_probe_event(tmp_path):
    c = _repo(tmp_path)
    bad = c.put("lib has no secret", ["lib.rs"], kind="invariant",
                probe={"type": "grep", "pattern": "secret_impl", "paths": ["lib.rs"], "expect": "absent"})
    c.check(bad.id)
    c.set_rule(bad.id, "rejected")
    assert _due(c, tmp_path) == {}


def test_a_failing_grep_is_blamed_on_the_files_with_matching_lines_and_bang_paths_exclude(tmp_path):
    c = _repo(tmp_path)
    (tmp_path / "sync.rs").write_text("pub fn sync() { secret_impl(1) }\n")
    (tmp_path / "wide.md").write_text(_spec(guarantee="g", exports="sync.rs::sync"))
    ledger.register_node(c, tmp_path, "wide", "wide.md")
    probe = {"type": "grep", "pattern": "secret_impl", "paths": ["*.rs", "!sync.rs"], "expect": "absent"}
    e = c.put("no secrets", ["*.rs"], kind="invariant", probe=probe)
    c.check(e.id)
    assert {r["node"] for r in activation.due(c, tmp_path)} == {"stats"}       # lib.rs matches; sync.rs is excluded
    (tmp_path / "lib.rs").write_text((tmp_path / "lib.rs").read_text().replace("secret_impl", "other_impl"))
    c.check(e.id)
    assert activation.due(c, tmp_path) == []
    assert probes.run({**probe, "paths": ["*.rs"]}, tmp_path).passed is False  # without the `!`, sync.rs fails it
    with pytest.raises(probes.ProbeError):
        probes.validate({**probe, "paths": ["!sync.rs"]})


def test_a_survived_mutant_wakes_the_node_until_a_later_run_kills_it(tmp_path):
    c = _repo(tmp_path)
    kw = dict(file="lib.rs", span="1-2", mutant="m")
    ledger.record(c, tmp_path, "stats", result="survived", **kw)
    assert _due(c, tmp_path) == {"stats": {"survivor"}}
    ledger.record(c, tmp_path, "stats", result="killed", tests=["t"], **kw)
    assert _due(c, tmp_path) == {}


def test_a_survivor_with_an_equivalence_argument_needs_a_record_not_an_agent(tmp_path):
    c = _repo(tmp_path)
    ledger.record(c, tmp_path, "stats", file="lib.rs", span="1-2", mutant="m", result="survived",
                  argument="operands commute")
    r = activation.scan(c, tmp_path)
    assert r["nodes"] == [] and [a["node"] for a in r["argued"]] == ["stats"]
    assert "record them with" in activation.render(r)


def test_activations_command(tmp_path, capsys):
    _repo(tmp_path)
    assert main(["--root", str(tmp_path), "activations"]) == 0
    assert "No node is due" in capsys.readouterr().out


# --- P3: join nodes ---------------------------------------------------------------------------

def test_a_join_must_rely_on_every_node_it_joins_by_contract():
    h = parse_header(_spec(joins="a, b", parents="a", after="b", oracle="equality"))
    notes = lint(h)
    assert any("add b to `parents:`" in n for n in notes) and not any("oracle" in n for n in notes)
    assert any("at least two" in n for n in lint(parse_header(_spec(joins="a", parents="a", oracle="equality"))))
    assert any("oracle: equality" in n for n in lint(parse_header(_spec(joins="a, b", parents="a, b"))))
    assert lint(parse_header(_spec(joins="a, b", parents="a, b", oracle="equality"))) == []


def test_joined_nodes_must_be_registered_and_joins_are_part_of_the_spec_key(tmp_path):
    c = _repo(tmp_path)
    h = parse_header(_spec(joins="stats, ghost", parents="stats"))
    notes = repo_lint(tmp_path, h, c.store.all_nodes(), "", "j")
    assert any("ghost" in n and "register" in n for n in notes) and not any("stats," in n for n in notes)
    assert spec_key(_spec(guarantee="g")) == spec_key(_spec(guarantee="g", joins=""))      # old keys do not move
    assert spec_key(_spec(guarantee="g", joins="a, b")) != spec_key(_spec(guarantee="g"))


# --- P2: behavioral rules and mutation-checked tests ------------------------------------------

def test_evidence_gap_names_what_a_rule_lacks():
    grep = {"type": "grep", "pattern": "x", "paths": ["*.py"]}
    assert "no probe" in probes.evidence_gap(None)
    assert "doc-pinned" in probes.evidence_gap({"type": "symbol", "ref": "a.rs::f", "sha": "0"})
    assert probes.evidence_gap(grep) is None                                    # a style rule may be a pattern
    assert "needs a test" in probes.evidence_gap({**grep, "behavioral": True})
    assert probes.evidence_gap({**grep, "behavioral": True, "evidence": "exact"}) is None
    assert probes.evidence_gap({"type": "command", "run": "cargo test", "behavioral": True}) is None
    with pytest.raises(probes.ProbeError):
        probes.validate({**grep, "behavioral": "yes"})


def test_a_test_is_mutation_checked_only_by_valid_killed_verdicts_that_name_it(tmp_path):
    c = _repo(tmp_path)
    ledger.record(c, tmp_path, "stats", file="lib.rs", span="2-3", mutant="m", result="killed",
                  tests=["bit_equality"])
    run = {"type": "command", "run": "cargo test -q --test bit_equality"}
    assert ledger.mutation_checked(c, tmp_path, run) == ["stats"]
    assert ledger.mutation_checked(c, tmp_path, {"type": "command", "run": "cargo test -q --test other"}) == []
    assert ledger.mutation_checked(c, tmp_path, {"type": "all", "probes": [run]}) == ["stats"]
    (tmp_path / "lib.rs").write_text((tmp_path / "lib.rs").read_text().replace("secret_impl(x)", "other_impl(x)"))
    assert ledger.mutation_checked(c, tmp_path, run) == []                      # the mutant's lines changed: not evidence


def test_invariants_listing_shows_the_gap_and_the_mutation_check(tmp_path, capsys):
    c = _repo(tmp_path)
    ledger.record(c, tmp_path, "stats", file="lib.rs", span="1-2", mutant="m", result="killed", tests=["bit_equality"])
    c.set_rule(c.put("engines agree", ["lib.rs"], kind="invariant",
                     probe={"type": "command", "run": "cargo test --test bit_equality"}).id, "enforced")
    c.set_rule(c.put("windows are exact", ["lib.rs"], kind="invariant",
                     probe={"type": "grep", "pattern": "exact", "paths": ["lib.rs"], "behavioral": True}).id, "enforced")
    assert main(["--root", str(tmp_path), "invariants"]) == 0
    out = capsys.readouterr().out
    assert "mutation-checked by stats" in out and "gap: behavioral rule checked only by a pattern" in out
    assert "1 enforced rule(s) have an evidence gap" in out


def test_only_a_killed_verdict_counts_as_mutation_evidence(tmp_path):
    c = _repo(tmp_path)
    ledger.record(c, tmp_path, "stats", file="lib.rs", span="2-3", mutant="m", result="survived", tests=["bit_equality"])
    ledger.record(c, tmp_path, "stats", file="lib.rs", span="2-3", mutant="n", result="equivalent",
                  argument="same output", tests=["bit_equality"])
    assert ledger.mutation_checked(c, tmp_path, {"type": "command", "run": "cargo test bit_equality"}) == []


# --- renamed headings, oversize sections ------------------------------------------------------

def _decision_repo(tmp_path, heading="S4 (proposed)", body="Windows are exact.\n"):
    c = _repo(tmp_path)
    (tmp_path / "INVARIANTS.md").write_text(f"## {heading}\n{body}")
    (tmp_path / "s4.md").write_text(_spec(guarantee="g", implements="S4"))
    return c


def test_a_renamed_heading_is_relinked_by_id_and_the_citing_node_stops_waking(tmp_path):
    from fractal_harness.context import premises_of, put_extracted, relinkable
    c = _decision_repo(tmp_path)
    old = put_extracted(c, tmp_path, section="INVARIANTS.md::S4 (proposed)")
    ledger.register_node(c, tmp_path, "s4", "s4.md")
    (tmp_path / "INVARIANTS.md").write_text("## S4\nWindows are exact.\n")
    c.verify()
    assert relinkable(c, tmp_path) == [(old.id, "INVARIANTS.md::S4")]
    assert _due(c, tmp_path)["s4"] == {"contract"}
    new = put_extracted(c, tmp_path, section="INVARIANTS.md::S4")
    assert c.store.get(old.id) is None and relinkable(c, tmp_path) == []      # the dead claim was replaced
    ledger.register_node(c, tmp_path, "s4", "s4.md")
    assert premises_of(c.store.get_node("s4"))["decisions"] == [new.id]
    assert "s4" not in _due(c, tmp_path)


def test_registration_prefers_the_live_claim_when_a_dead_one_for_the_same_id_is_still_there(tmp_path):
    from fractal_harness.context import put_extracted, section_claims
    c = _decision_repo(tmp_path)
    old = put_extracted(c, tmp_path, section="INVARIANTS.md::S4 (proposed)")
    (tmp_path / "INVARIANTS.md").write_text("## S4\nWindows are exact.\n")
    c.verify()
    new = c.put("## S4\nWindows are exact.", ["INVARIANTS.md"], kind="knowledge", probe={
        "type": "section", "ref": "INVARIANTS.md::S4", "sha": __import__("fractal_harness.symbols").symbols.digest(
            "## S4\nWindows are exact.")})
    found, missing = section_claims(c.store.all(), ["S4"])
    assert [e.id for e in found] == [new.id] and old.id != new.id and missing == []


def test_a_section_over_the_claim_limit_is_cut_for_injection_but_hashed_whole(tmp_path):
    from fractal_harness.context import MAX_CLAIM, put_extracted
    c = _decision_repo(tmp_path, heading="D24", body="Rule text. " * 250)
    notes: list[str] = []
    e = put_extracted(c, tmp_path, section="INVARIANTS.md::D24", notes=notes)
    assert len(e.post) <= MAX_CLAIM and "cut here, the rest is at INVARIANTS.md::D24" in e.post
    assert notes and "over the 2000" in notes[0]
    c.verify([e.id])
    assert c.store.get(e.id).status == "verified"
    (tmp_path / "INVARIANTS.md").write_text((tmp_path / "INVARIANTS.md").read_text() + "A late amendment.\n")
    c.verify([e.id])
    assert c.store.get(e.id).status == "failed"                      # the cut part is still watched


def test_a_section_near_the_limit_warns(tmp_path):
    from fractal_harness.context import put_extracted
    c = _decision_repo(tmp_path, heading="D24", body="x" * 1850)
    notes: list[str] = []
    e = put_extracted(c, tmp_path, section="INVARIANTS.md::D24", notes=notes)
    assert "cut here" not in e.post and notes and "one more amendment" in notes[0]


def test_a_headerless_spec_also_ignores_the_dead_claim_for_a_renamed_heading(tmp_path):
    from fractal_harness.context import _cited, put_extracted
    c = _decision_repo(tmp_path)
    old = put_extracted(c, tmp_path, section="INVARIANTS.md::S4 (proposed)")
    (tmp_path / "INVARIANTS.md").write_text("## S4\nWindows are exact.\n")
    c.verify()
    new = c.put("## S4\nWindows are exact.", ["INVARIANTS.md"], kind="knowledge", probe={
        "type": "section", "ref": "INVARIANTS.md::S4", "sha": __import__("fractal_harness.symbols").symbols.digest(
            "## S4\nWindows are exact.")})
    found, missing, _ = _cited(c.store.all(), "Implements S4.", r"\bS\d+\b")
    assert [e.id for e in found] == [new.id] and old.id not in [e.id for e in found] and missing == []

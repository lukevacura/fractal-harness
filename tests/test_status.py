"""Derived Done, the generated DAG view, and deterministic `repair --all`. No model anywhere."""
import json

from fractal_harness import dag, ledger, status
from fractal_harness.cache import ClaimCache
from fractal_harness.cli import main
from fractal_harness.repair import reverify

from test_config import _cfg, _two_dirs
from test_nodes import LIB, _git, _repo, _spec


def _verified(tmp_path):
    """test_nodes' repo (gp <- stats <- child, naive), every node with a killed verdict on a file of its own
    (stats on lib.rs, which it exports), all committed after the verdicts' files exist."""
    c = _repo(tmp_path)
    for n in ("gp", "naive", "child"):
        (tmp_path / f"{n}.py").write_text("a = 1\nb = 2\nc = 3\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "files")
    for n in ("gp", "naive", "child"):
        ledger.record(c, tmp_path, n, file=f"{n}.py", span="1-2", mutant="m", result="killed", tests=["t"])
    ledger.record(c, tmp_path, "stats", file="lib.rs", span="2-3", mutant="m", result="killed", tests=["t"])
    return c


def _st(c, root, node=None):
    d = status.derive_all(c, root)
    return {n: v["state"] for n, v in d.items()} if node is None else d[node]


def test_done_is_derived_from_recorded_valid_verdicts(tmp_path):
    c = _verified(tmp_path)
    assert _st(c, tmp_path) == {"child": "done", "gp": "done", "naive": "done", "stats": "done"}
    d = _st(c, tmp_path, "stats")
    assert d["evidence"]["valid_verdicts"] == {"killed": 1} and d["evidence"]["parent_contracts"].keys() == {"gp"}
    assert any("parent gp's contract" in w for w in d["would_change"])


def test_unregistered_and_pending(tmp_path):
    c = _repo(tmp_path)                                     # registered, nothing recorded
    assert status.derive(c, tmp_path, "stats")["state"] == "pending"
    assert "no verdicts" in status.derive(c, tmp_path, "stats")["reasons"][0]
    u = status.derive(c, tmp_path, "ghost")
    assert u["state"] == "unregistered" and "not registered" in u["reasons"][0]


def test_required_mutant_missing_is_pending(tmp_path):
    c = _verified(tmp_path)
    (tmp_path / "naive.md").write_text(_spec(guarantee="Reference implementation.", oracle="equality"))
    ledger.register_node(c, tmp_path, "naive", "naive.md")
    d = _st(c, tmp_path, "naive")
    assert d["state"] == "pending" and "required mutant missing" in d["reasons"][0]


def test_spec_change_is_due_with_its_reason(tmp_path):
    c = _verified(tmp_path)
    (tmp_path / "naive.md").write_text(_spec(guarantee="A different promise."))
    d = _st(c, tmp_path, "naive")
    assert d["state"] == "due" and d["reasons"][0].startswith("spec: spec naive.md changed")


def test_edit_under_a_verdict_is_stale_not_due(tmp_path):
    c = _verified(tmp_path)
    (tmp_path / "gp.py").write_text("a = 9\nb = 2\nc = 3\n")
    d = _st(c, tmp_path, "gp")
    assert d["state"] == "stale" and "lines 1-2 of gp.py changed" in " ".join(d["reasons"])
    (tmp_path / "gp.py").write_text("a = 1\nb = 2\nc = 3\nd = 4\n")            # past the verdict's span: still valid
    assert _st(c, tmp_path, "gp")["state"] == "done"


def test_unresolved_survivor_is_due_but_an_argued_one_stays_done(tmp_path):
    c = _verified(tmp_path)
    ledger.record(c, tmp_path, "naive", file="naive.py", span="3-3", mutant="s", result="survived")
    d = _st(c, tmp_path, "naive")
    assert d["state"] == "due" and d["reasons"][0].startswith("survivor:")
    ledger.record(c, tmp_path, "naive", file="naive.py", span="3-3", mutant="s", result="survived",
                  argument="the third line is dead")
    d = _st(c, tmp_path, "naive")
    assert d["state"] == "done" and len(d["evidence"]["argued_survivors"]) == 1


def test_parent_implementation_change_keeps_dependents_done(tmp_path):
    """Early cutoff: stats's implementation moves, its contract digest does not; child stays done."""
    c = _verified(tmp_path)
    (tmp_path / "lib.rs").write_text(LIB.replace("secret_impl(x)", "other_impl(x)"))
    s = _st(c, tmp_path)
    assert s["child"] == "done" and s["gp"] == "done"
    assert s["stats"] == "stale"                                  # its own verdict covers the lines that changed
    (tmp_path / "lib.rs").write_text(LIB.replace("x: f64", "x: f32"))      # a signature is part of the contract
    s = _st(c, tmp_path)
    assert s["child"] == "due" and "parent stats's contract changed" in status.derive(c, tmp_path, "child")["reasons"][0]


def test_premise_in_another_root_flips_done_to_due(tmp_path):
    c, repo, docs = _two_dirs(tmp_path)
    ledger.record(c, repo, "parent", file="lib.rs", span="1-2", mutant="m", result="killed", tests=["t"])
    ledger.record(c, repo, "child", file="lib.rs", span="1-2", mutant="m", result="killed", tests=["t"])
    assert _st(c, repo) == {"child": "done", "parent": "done"}
    (docs / "DECISIONS.md").write_text("## D18 — Bitwise\nReproduce bits, and round half even.\n")
    assert _st(c, repo) == {"child": "due", "parent": "done"}
    d = status.derive(c, repo, "child")
    assert "decision claim" in d["reasons"][0]
    (docs / "DECISIONS.md").write_text("## D18 — Bitwise\nReproduce bits.\n")           # restored: done again
    assert _st(c, repo)["child"] == "done"


def test_legacy_marks_are_compared_not_trusted(tmp_path):
    c = _verified(tmp_path)
    (tmp_path / "naive.md").write_text(_spec(guarantee="A different promise."))
    table = ("| Node | Status |\n|---|---|\n| `gp` | Done |\n| naive | ✅ |\n| [stats](stats.md) | In progress |\n"
             "| unknown | Done |\n")
    marks = status.legacy_marks(table, {"gp", "naive", "stats", "child"})
    assert marks == {"gp": True, "naive": True, "stats": False}
    dis = status.disagreements(marks, status.derive_all(c, tmp_path))
    assert [(x["node"], x["marked"], x["derived"]) for x in dis] == [
        ("naive", "done", "due"), ("stats", "not done", "done")]
    f = tmp_path / "marks.md"
    f.write_text(table)
    assert main(["--root", str(tmp_path), "status", "--marks", str(f), "--json"]) == 0


def test_status_cli(tmp_path, capsys):
    _verified(tmp_path)
    assert main(["--root", str(tmp_path), "status", "stats"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("stats: done") and "would change if" in out and "evidence:" in out
    assert main(["--root", str(tmp_path), "status", "--json"]) == 0
    assert {n["node"] for n in json.loads(capsys.readouterr().out)["nodes"]} == {"child", "gp", "naive", "stats"}


# --- DAG --------------------------------------------------------------------------------------

def test_dag_has_one_line_per_node_with_distinct_edge_kinds(tmp_path):
    c = _verified(tmp_path)
    (tmp_path / "late.md").write_text(_spec(after="child", guarantee="Runs after child. More prose here."))
    ledger.register_node(c, tmp_path, "late", "late.md")
    md = dag.render(dag.build(c, tmp_path), "md")
    lines = [l for l in md.splitlines() if l.startswith("**")]
    assert len(lines) == 5
    by = {l.split("**")[1]: l for l in lines}
    assert "uses: gp" in by["stats"] and "after:" not in by["stats"]
    assert "after: child" in by["late"] and "uses:" not in by["late"]          # sequencing is not a contract edge
    assert "`done`" in by["gp"] and "Grandparent promise." in by["gp"] and "[spec](gp.md)" in by["gp"]
    assert "Runs after child." in by["late"] and "More prose" not in by["late"]   # first sentence only
    assert md.index("**gp**") < md.index("**stats**") < md.index("**child**")      # by layer, then id


def test_dag_is_deterministic_and_writes_a_file(tmp_path, capsys):
    c = _verified(tmp_path)
    a = dag.render(dag.build(c, tmp_path), "md")
    assert a == dag.render(dag.build(c, tmp_path), "md")
    out = tmp_path / "DAG.md"
    assert main(["--root", str(tmp_path), "dag", "--out", str(out)]) == 0
    assert out.read_text() == a
    assert main(["--root", str(tmp_path), "dag", "--format", "json"]) == 0
    j = json.loads(capsys.readouterr().out)
    assert [r["id"] for r in j["nodes"]] == ["gp", "naive", "stats", "child"]
    assert {r["id"]: (r["uses"], r["after"], r["state"]) for r in j["nodes"]}["stats"] == (["gp"], [], "done")


def test_dag_order_does_not_depend_on_registration_order(tmp_path):
    c = _verified(tmp_path)
    before = dag.render(dag.build(c, tmp_path), "json")
    ledger.deregister(c, "naive")
    ledger.register_node(c, tmp_path, "naive", "naive.md")                      # now registered last
    after = json.loads(dag.render(dag.build(c, tmp_path), "json"))
    assert [r["id"] for r in after["nodes"]] == [r["id"] for r in json.loads(before)["nodes"]]
    assert [r["id"] for r in dag.build(c, tmp_path)["nodes"]] == ["gp", "naive", "stats", "child"]


def test_dag_state_changes_flow_through(tmp_path):
    c = _verified(tmp_path)
    (tmp_path / "gp.md").write_text(_spec(guarantee="Changed."))
    md = dag.render(dag.build(c, tmp_path), "md")
    assert "**gp** `due`" in md and "**stats** `due`" in md and "**child** `done`" in md


def test_dag_cross_root_link_is_relative_to_the_output(tmp_path):
    c, repo, docs = _two_dirs(tmp_path)
    d = dag.build(c, repo)
    assert {r["id"]: r["path"] for r in d["nodes"]}["child"] == "../docs/child.md"


def test_cycle_is_reported_not_layered(tmp_path):
    c = _repo(tmp_path)
    (tmp_path / "naive.md").write_text(_spec(guarantee="Reference.", after="child"))
    (tmp_path / "child.md").write_text((tmp_path / "child.md").read_text().replace("see: naive", "after: naive"))
    d = dag.build(c, tmp_path)
    assert d["cycles"] == ["child", "naive"]
    assert "## Cyclic or unresolved" in dag.render(d) and "Cycles through: child, naive" in dag.render(d)


# --- repair --all -----------------------------------------------------------------------------

def _claims(tmp_path):
    _git(tmp_path, "init", "-q")
    (tmp_path / "a.py").write_text("def f():\n    return 1\n")
    c = ClaimCache(tmp_path)
    ids = {
        "flaky": c.put("f exists", ["a.py"], probe={"type": "grep", "pattern": "def f", "paths": ["a.py"]}).id,
        "dead": c.put("g exists", ["a.py"], probe={"type": "grep", "pattern": "def g", "paths": ["a.py"]}).id,
        "rule": c.put("no eval", ["a.py"], kind="invariant",
                      probe={"type": "grep", "pattern": "eval", "paths": ["a.py"], "expect": "absent"}).id,
    }
    c.set_rule(ids["rule"], "enforced")
    return c, ids


def test_repair_all_reverifies_clears_and_reports_without_a_model(tmp_path, monkeypatch):
    c, ids = _claims(tmp_path)
    c.verify()
    assert c.get(ids["dead"]).status == "failed"
    (tmp_path / "a.py").write_text("def f():\n    return eval('1')\n")             # breaks the rule, f still present
    c.update()
    assert c.get(ids["flaky"]).status in ("stale", "verified") and c.get(ids["rule"]).status == "failed"
    (tmp_path / "a.py").write_text("def f():\n    return 2\n\ndef g():\n    return 3\n")  # now g exists, no eval
    import subprocess
    real = subprocess.run

    def guarded(cmd, *a, **k):
        assert cmd[0] != "claude", "model call"
        return real(cmd, *a, **k)
    monkeypatch.setattr(subprocess, "run", guarded)
    probes_before = {e.id: e.probe for e in c.store.all()}
    c.close()
    r = reverify(tmp_path)
    c = ClaimCache(tmp_path)
    assert r["model_used"] is False and r["cleared"] == [ids["dead"]]
    assert r["remaining"] == [] and {e.id: e.probe for e in c.store.all()} == probes_before     # no probe weakened
    assert all(c.get(i).status == "verified" for i in ids.values())


def test_repair_all_reports_what_remains_and_leaves_enforced_rules_alone(tmp_path):
    c, ids = _claims(tmp_path)
    (tmp_path / "a.py").write_text("def h():\n    return eval('1')\n")
    c.verify()
    c.close()
    r = reverify(tmp_path)
    assert {x["id"] for x in r["remaining"]} == {ids["flaky"], ids["dead"]}
    assert all(x["needs"].startswith("claim or probe edit") for x in r["remaining"])
    assert [v["id"] for v in r["violations"]] == [ids["rule"]]
    c = ClaimCache(tmp_path)
    assert c.get(ids["rule"]).rule_state == "enforced" and c.get(ids["rule"]).status == "failed"


def test_repair_all_includes_blocked_claims_without_a_motion_record(tmp_path):
    from fractal_harness.repair import candidates
    c, ids = _claims(tmp_path)
    dep = c.put("uses g", ["a.py"], depends_on=[ids["dead"]], probe={"type": "grep", "pattern": "return", "paths": ["a.py"]})
    c.verify()
    e = c.get(dep.id)
    assert e.status == "stale" and not e.repair
    assert dep.id in {x.id for x, _ in candidates(c, everything=True)}
    assert dep.id not in {x.id for x, _ in candidates(c)}            # demanded-only path is unchanged


def test_dag_edges_are_sorted_whatever_order_the_header_lists_them(tmp_path):
    c = _verified(tmp_path)
    (tmp_path / "multi.md").write_text(_spec(parents="stats, gp", after="naive, child", guarantee="Many."))
    ledger.register_node(c, tmp_path, "multi", "multi.md")
    row = {r["id"]: r for r in dag.build(c, tmp_path)["nodes"]}["multi"]
    assert row["uses"] == ["gp", "stats"] and row["after"] == ["child", "naive"]


def test_repair_all_rechecks_a_failed_claim_whose_sources_did_not_change(tmp_path):
    _git(tmp_path, "init", "-q")
    (tmp_path / "a.py").write_text("x = 1\n")
    c = ClaimCache(tmp_path)
    e = c.put("flag exists", ["a.py"], probe={"type": "command", "run": "test -f flag"})
    c.verify()
    assert c.get(e.id).status == "failed"
    (tmp_path / "flag").write_text("")                  # the probe's input is outside `reads`: no source change
    c.close()
    assert reverify(tmp_path)["cleared"] == [e.id]

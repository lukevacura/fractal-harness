import subprocess
from pathlib import Path

import pytest

from fractal_harness import ledger
from fractal_harness.cache import ClaimCache
from fractal_harness.context import bundle, put_extracted
from fractal_harness.nodes import body, lint, parse_header
from fractal_harness.symbols import SymbolError

LIB = """/// Windowed stats.
pub fn update(x: f64) -> f64 {
    secret_impl(x)
}
"""
DECISIONS = "## D17 — No FMA\nFMA changes rounding.\n\n## D18 — Bitwise ops\nReproduce the ops bit for bit.\nWhy: parity.\n"


def _spec(**f) -> str:
    return "---\n" + "".join(f"{k}: {v}\n" for k, v in f.items()) + "---\n# Body text\nprose\n"


def _git(root: Path, *a: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=root, check=True, capture_output=True)


def _repo(tmp_path: Path) -> ClaimCache:
    _git(tmp_path, "init", "-q")
    (tmp_path / ".git" / "info" / "exclude").write_text(".fractal/\n")
    (tmp_path / "lib.rs").write_text(LIB)
    (tmp_path / "DECISIONS.md").write_text(DECISIONS)
    (tmp_path / "gp.md").write_text(_spec(guarantee="Grandparent promise."))
    (tmp_path / "stats.md").write_text(_spec(parents="gp", guarantee="Exact windowed mean.",
                                             condition="Only for windows with V < 2^63.",
                                             exports="lib.rs::update"))
    (tmp_path / "naive.md").write_text(_spec(guarantee="Reference implementation."))
    (tmp_path / "child.md").write_text(_spec(parents="stats", implements="D18", see="naive"))
    c = ClaimCache(tmp_path)
    put_extracted(c, tmp_path, section="DECISIONS.md::D17")
    put_extracted(c, tmp_path, section="DECISIONS.md::D18")
    for node in ("gp", "stats", "naive", "child"):
        ledger.register_node(c, tmp_path, node, f"{node}.md")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "c1")
    return c


def test_header_parsing_and_lint():
    h = parse_header(_spec(parents="a, b", implements="D1", guarantee="Only when x holds.", exports="f.rs::g"))
    assert h.parents == ["a", "b"] and h.implements == ["D1"] and h.exports == ["f.rs::g"]
    assert any("conditional" in n for n in lint(h))
    assert parse_header("# no header\n") is None
    assert body(_spec(guarantee="g")).startswith("# Body text")
    with pytest.raises(SymbolError):
        parse_header(_spec(parent="typo"))


def test_context_gives_parent_contracts_and_implemented_decisions_only(tmp_path):
    _repo(tmp_path).close()
    out = bundle(tmp_path, "child.md")
    assert "Reproduce the ops bit for bit" in out and "Why: parity" in out          # implemented: in full
    assert "No FMA" not in out                                                       # D17 not declared
    assert "### stats (NO VERIFICATION RECORDED)" in out
    assert "guarantee: Exact windowed mean." in out and "condition: Only for windows with V < 2^63." in out
    assert "pub fn update(x: f64) -> f64 {" in out and "secret_impl" not in out    # signature, not the body
    assert "Grandparent promise" not in out                                          # grandparents aren't premises
    assert "non-binding" in out and "naive: naive.md" in out and "Reference implementation" not in out
    assert "parents: stats" not in out                                               # header not repeated as spec


def test_lint_flags_undeclared_mentions(tmp_path):
    _repo(tmp_path).close()
    (tmp_path / "child.md").write_text(_spec(parents="stats", implements="D18") + "Also relies on D17.\n")
    out = bundle(tmp_path, "child.md")
    assert "## Spec lint" in out and "mentions D17 without declaring it" in out


def test_parent_must_be_registered_first(tmp_path):
    c = _repo(tmp_path)
    (tmp_path / "orphan.md").write_text(_spec(parents="ghost"))
    with pytest.raises(SymbolError, match="ghost"):
        ledger.register_node(c, tmp_path, "orphan", "orphan.md")


def _record(c, root, node):
    ledger.record(c, root, node, file="lib.rs", span="2-3", mutant="m", result="killed", tests=["t"])


def test_child_stales_on_parent_contract_not_on_grandparent_or_parent_prose(tmp_path):
    c = _repo(tmp_path)
    _record(c, tmp_path, "child")
    _record(c, tmp_path, "stats")
    p = lambda n="child": ledger.plan(c, tmp_path, n)
    assert p()["node_reason"] is None
    (tmp_path / "gp.md").write_text(_spec(guarantee="Grandparent changed."))
    assert p()["node_reason"] is None                                   # a grandparent is not child's premise...
    assert p("stats")["node_reason"] == "parent gp's contract changed: its guarantee or condition changed"  # ...but it is stats's
    (tmp_path / "stats.md").write_text((tmp_path / "stats.md").read_text().replace("prose", "other prose"))
    assert p()["node_reason"] is None                                   # parent's own spec changed, contract didn't
    assert p("stats")["prose_changed"] == "stats.md"      # prose is not part of the key: noted, not stale
    assert "spec stats.md changed" not in str(p("stats")["node_reason"])
    (tmp_path / "stats.md").write_text(_spec(parents="gp", guarantee="Exact windowed mean.",
                                             condition="For every window.", exports="lib.rs::update"))
    assert p()["node_reason"] == "parent stats's contract changed: its guarantee or condition changed"   # condition is part of the contract


def test_child_stales_on_parent_export_signature(tmp_path):
    c = _repo(tmp_path)
    _record(c, tmp_path, "child")
    (tmp_path / "lib.rs").write_text(LIB.replace("x: f64", "x: f32"))
    assert ledger.plan(c, tmp_path, "child")["node_reason"] == (
        "parent stats's contract changed: export lib.rs::update changed")
    (tmp_path / "lib.rs").write_text(LIB.replace("secret_impl(x)", "other_impl(x)"))   # body only
    assert ledger.plan(c, tmp_path, "child")["node_reason"] is None


def test_plan_lists_children_and_context_labels_verification(tmp_path):
    c = _repo(tmp_path)
    _record(c, tmp_path, "stats")
    assert "Nodes that assume this one's contract: child" in ledger.render(ledger.plan(c, tmp_path, "stats"))
    c.close()
    assert "### stats (verified)" in bundle(tmp_path, "child.md")
    (tmp_path / "lib.rs").write_text(LIB.replace("secret_impl(x)", "other_impl(x)"))   # touches the mutant's span
    assert "### stats (UNVERIFIED: 1 of 1 verdicts stale)" in bundle(tmp_path, "child.md")


# --- worktrees and header lint ------------------------------------------------------------

def _worktree(tmp_path: Path, branch: str) -> Path:
    wt = tmp_path.parent / f"{tmp_path.name}-{branch}"
    _git(tmp_path, "worktree", "add", "-q", "-b", branch, str(wt))
    return wt


def test_worktree_shares_the_main_store_and_ledger_defaults_to_the_node_branch(tmp_path):
    from fractal_harness.cache import store_root
    from fractal_harness.cli import main
    _repo(tmp_path).close()
    wt = _worktree(tmp_path, "node-child")
    (wt / "lib.rs").write_text(LIB + "// work\n")
    _git(wt, "commit", "-qam", "node work")
    tip = subprocess.run(["git", "rev-parse", "node-child"], cwd=tmp_path, capture_output=True, text=True).stdout.strip()
    assert store_root(wt) == tmp_path.resolve() and not (wt / ".fractal").exists()
    one = '[{"file":"lib.rs","span":"1-3","mutant":"m","result":"killed","tests":["t"]}]'
    (tmp_path / "v.json").write_text(one)
    # from the MAIN checkout: no --commit still lands on the node branch's tip
    assert main(["--root", str(tmp_path), "ledger", "record", "child", "--json", str(tmp_path / "v.json")]) == 0
    # from the worktree: the same store, and --commit applies to --json items without their own
    (tmp_path / "v.json").write_text(one.replace('"m"', '"m2"'))
    first = subprocess.run(["git", "rev-parse", "HEAD~1"], cwd=wt, capture_output=True, text=True).stdout.strip()
    assert main(["--root", str(wt), "ledger", "record", "child", "--json", str(tmp_path / "v.json"),
                 "--commit", first]) == 0
    c = ClaimCache(tmp_path)
    try:
        assert {r["commit"] for r in c.store.mutants("child")} == {tip, first}
    finally:
        c.close()


def test_worktree_commit_is_checked_against_the_canonical_rules(tmp_path):
    from fractal_harness.check import check
    c = _repo(tmp_path)
    c.put("no FMA spelled anywhere", ["lib.rs"], kind="invariant",
          probe={"type": "grep", "pattern": "fused_mul_add", "paths": ["*.rs"], "expect": "absent"})
    for e in c.store.all():
        if e.kind == "invariant":
            c.set_rule(e.id, "enforced")
    c.close()
    wt = _worktree(tmp_path, "node-x")
    (wt / "lib.rs").write_text(LIB + "// uses fused_mul_add\n")
    _git(wt, "add", "-A")
    code, report = check(wt)
    assert code == 1 and "VIOLATION" in report


def test_decision_claims_are_registered_automatically(tmp_path):
    c = ClaimCache(tmp_path)
    _git(tmp_path, "init", "-q")
    (tmp_path / "DECISIONS.md").write_text(DECISIONS)
    (tmp_path / "n.md").write_text(_spec(implements="D17, D99", guarantee="g"))
    r = ledger.register_node(c, tmp_path, "n", "n.md")
    assert r["autoregistered"] == ["D17"] and len(r["premises"]["decisions"]) == 1
    assert any("D99" in n and "fractal put --section DECISIONS.md::D99" in n for n in r["lint"])
    c.close()


STATS_RS = """pub struct WindowStats {
    n: u64,
}

impl WindowStats {
    pub fn push(&mut self, x: f64) {}
    pub fn mean(&self) -> f64 { 0.0 }
    fn private(&self) {}
}
"""


def test_repo_lint_flags_missing_parent_type_without_methods_and_file_export(tmp_path):
    c = ClaimCache(tmp_path)
    _git(tmp_path, "init", "-q")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "stats.rs").write_text(STATS_RS)
    (tmp_path / "src" / "znorm.rs").write_text("pub fn z() {}\n")
    (tmp_path / "src" / "grow.rs").write_text("use crate::stats::WindowStats;\nuse crate::znorm::z;\npub fn grow() {}\n")
    (tmp_path / "stats.md").write_text(_spec(guarantee="g", exports="src/stats.rs::WindowStats"))
    (tmp_path / "znorm.md").write_text(_spec(guarantee="g", exports="src/znorm.rs"))
    (tmp_path / "grow.md").write_text(_spec(parents="stats", guarantee="g", exports="src/grow.rs::grow"))
    _git(tmp_path, "add", "-A")
    ledger.register_node(c, tmp_path, "znorm", "znorm.md")
    notes = ledger.register_node(c, tmp_path, "stats", "stats.md")["lint"]
    assert any("WindowStats" in n and "push" in n and "mean" in n and "private" not in n for n in notes)
    assert any("whole file" in n for n in ledger.register_node(c, tmp_path, "znorm", "znorm.md")["lint"])
    notes = ledger.register_node(c, tmp_path, "grow", "grow.md")["lint"]
    assert not any("stats" in n and "no parent" in n for n in notes)       # declared
    assert any("`znorm`" in n and "add parent `znorm`" in n for n in notes)
    c.close()


def test_a_local_named_like_a_method_is_not_a_second_definition():
    from fractal_harness.symbols import find
    src = """pub struct Anchor {
    tick: u64,
}

impl Anchor {
    /// Ticks since the anchor.
    pub fn offset(&self, tick: u64) -> u64 {
        let offset = tick - self.tick;
        offset
    }
}
"""
    assert find(src, "Anchor::offset")[1].splitlines()[-1].strip().startswith("pub fn offset")
    assert "let" not in find(src, "offset")[1]       # unqualified: the method, not the local


def test_adding_exports_does_not_stale_children_but_removing_one_does(tmp_path):
    c = _repo(tmp_path)
    _record(c, tmp_path, "child")
    (tmp_path / "lib.rs").write_text(LIB + "pub fn extra() {}\n")
    spec = _spec(parents="gp", guarantee="Exact windowed mean.", condition="Only for windows with V < 2^63.",
                 exports="lib.rs::update, lib.rs::extra")
    (tmp_path / "stats.md").write_text(spec)
    assert ledger.plan(c, tmp_path, "child")["node_reason"] is None
    (tmp_path / "stats.md").write_text(spec.replace("lib.rs::update, ", ""))
    assert "export lib.rs::update was removed" in ledger.plan(c, tmp_path, "child")["node_reason"]


def test_a_contract_recorded_as_one_digest_still_tolerates_appended_exports(tmp_path):
    from fractal_harness.nodes import contract_digest
    c = _repo(tmp_path)
    _record(c, tmp_path, "child")
    info = c.store.get_node("child")
    pre = dict(info["premises"], parents={"stats": contract_digest(tmp_path, "stats.md")})   # the old shape
    c.store.put_node("child", info["spec"], info["spec_sha"], pre)
    (tmp_path / "lib.rs").write_text(LIB + "pub fn extra() {}\n")
    (tmp_path / "stats.md").write_text(_spec(parents="gp", guarantee="Exact windowed mean.",
                                             condition="Only for windows with V < 2^63.",
                                             exports="lib.rs::update, lib.rs::extra"))
    assert ledger.plan(c, tmp_path, "child")["node_reason"] is None


def test_same_content_under_a_different_commit_is_not_a_change(tmp_path):
    c = _repo(tmp_path)
    _git(tmp_path, "checkout", "-q", "-b", "side")
    (tmp_path / "other.txt").write_text("x")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "side")
    _record(c, tmp_path, "stats")                       # verified on `side`
    side = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True).stdout.strip()
    _git(tmp_path, "checkout", "-q", "-", )
    _git(tmp_path, "cherry-pick", side)                 # same content, new hash, side is not an ancestor
    p = ledger.plan(c, tmp_path, "stats")
    assert p["stale"] == [] and "Compared with the working tree" in ledger.render(p, root=tmp_path)
    assert "Compared with commit" in ledger.render(ledger.plan(c, tmp_path, "stats", "HEAD"), root=tmp_path)


def test_deregistering_a_node_removes_it_from_the_children(tmp_path):
    c = _repo(tmp_path)
    _record(c, tmp_path, "stats")
    assert ledger.deregister(c, "child") == []
    assert ledger.plan(c, tmp_path, "stats")["children"] == []
    assert ledger.deregister(c, "stats", forget_verdicts=True) == []
    with pytest.raises(SymbolError):
        ledger.plan(c, tmp_path, "stats")
    c.close()


def test_reattribute_corrects_the_commit_and_is_audited(tmp_path):
    c = _repo(tmp_path)
    wrong = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True).stdout.strip()
    _record(c, tmp_path, "stats")
    (tmp_path / "lib.rs").write_text("// moved\n" + LIB)
    _git(tmp_path, "commit", "-qam", "c2")
    right = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True).stdout.strip()
    with pytest.raises(SymbolError):
        ledger.reattribute(c, tmp_path, "stats", wrong[:7], right, "")                  # a reason is required
    with pytest.raises(SymbolError):
        ledger.reattribute(c, tmp_path, "stats", "deadbee", right, "x")                 # nothing there
    assert ledger.reattribute(c, tmp_path, "stats", wrong[:7], right, "recorded from main by mistake") == 1
    assert c.store.mutants("stats")[0]["commit"] == right
    assert "recorded from main by mistake" in c.store.audit("stats")[0]["detail"]
    _record(c, tmp_path, "child")
    tiny = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True).stdout.strip()
    (tmp_path / "short.txt").write_text("a\n")
    _git(tmp_path, "add", "short.txt")
    _git(tmp_path, "commit", "-qm", "c3")
    c.store.reattribute("child", tiny[:7], tiny, "noop")                                # (setup for the span check below)
    c.store.put_mutant("child", "short.txt", 1, 50, "m9", "killed", tiny, ["t"], "")
    with pytest.raises(SymbolError, match="do not fit"):
        ledger.reattribute(c, tmp_path, "child", tiny[:7], tiny, "why")
    c.close()


def _concept(c, decision="S5", name="nearest order"):
    """A single-ownership claim naming a decision: ownership lints apply to IDs like this one only."""
    return c.put(f"{name} is defined only in n.rs", ["n.rs"], kind="invariant",
                 probe={"type": "only_in", "pattern": "struct Nearest", "paths": ["*.rs"], "files": ["n.rs"],
                        "concept": name, "decision": decision})


def test_ownership_lint_warns_on_a_parallel_symbol_for_an_owned_decision(tmp_path):
    c = ClaimCache(tmp_path)
    _concept(c)
    _git(tmp_path, "init", "-q")
    (tmp_path / "DECISIONS.md").write_text("# D\n\n## S5 nearest\nExact nearest.\n")
    (tmp_path / "a.md").write_text(_spec(implements="S5", guarantee="g", exports="n.rs::Nearest"))
    (tmp_path / "b.md").write_text(_spec(implements="S5", guarantee="g", exports="n.rs::Nearest::k, n.rs::Nearest"))
    (tmp_path / "c.md").write_text(_spec(implements="S5", guarantee="g", exports="n.rs::NearestK"))
    _git(tmp_path, "add", "-A")
    ledger.register_node(c, tmp_path, "a", "a.md")
    assert not any("owned" in n for n in ledger.register_node(c, tmp_path, "b", "b.md")["lint"])   # refines it
    notes = ledger.register_node(c, tmp_path, "c", "c.md")["lint"]
    assert any("S5" in n and "NearestK" in n and "Extend" in n for n in notes)


def test_after_edges_are_not_premises_and_do_not_satisfy_a_used_module(tmp_path):
    c = ClaimCache(tmp_path)
    _git(tmp_path, "init", "-q")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "stats.rs").write_text("pub fn mean() {}\n")
    (tmp_path / "src" / "grow.rs").write_text("use crate::stats::mean;\npub fn grow() {}\n")
    (tmp_path / "stats.md").write_text(_spec(guarantee="g", exports="src/stats.rs::mean"))
    (tmp_path / "grow.md").write_text(_spec(after="stats", guarantee="g", exports="src/grow.rs::grow"))
    _git(tmp_path, "add", "-A")
    ledger.register_node(c, tmp_path, "stats", "stats.md")
    r = ledger.register_node(c, tmp_path, "grow", "grow.md")
    assert r["premises"]["parents"] == {}                              # no contract watched
    assert any("only `after:`" in n and "move it to `parents:`" in n for n in r["lint"])
    assert any("both" in n for n in lint(parse_header(_spec(parents="a", after="a"))))


def test_refines_and_distinct_declarations_silence_the_ownership_lint(tmp_path):
    c = ClaimCache(tmp_path)
    _concept(c)
    _git(tmp_path, "init", "-q")
    (tmp_path / "DECISIONS.md").write_text("# D\n\n## S5 nearest\nExact nearest.\n")
    (tmp_path / "a.md").write_text(_spec(implements="S5", guarantee="g", exports="n.rs::Nearest"))
    (tmp_path / "c.md").write_text(_spec(implements="S5", guarantee="g", exports="n.rs::NearestK",
                                         refines="NearestK=Nearest"))
    (tmp_path / "d.md").write_text(_spec(implements="S5", guarantee="g", exports="n.rs::Other", distinct="Wrong"))
    _git(tmp_path, "add", "-A")
    ledger.register_node(c, tmp_path, "a", "a.md")
    assert not any("owned" in n for n in ledger.register_node(c, tmp_path, "c", "c.md")["lint"])
    notes = ledger.register_node(c, tmp_path, "d", "d.md")["lint"]
    assert any("must be" in n for n in notes) and any("owned" in n for n in notes)


def test_spec_lint_wants_design_points_marked_binding_or_advisory():
    from fractal_harness.nodes import spec_lint
    spec = "---\nguarantee: g\n---\n# N\n\n## Design\n- [binding] use NaN sentinel\n- tile size 64\n- [advisory] unroll 4\n\n## Notes\n- anything\n"
    notes = spec_lint(spec)
    assert len(notes) == 1 and "1 design point" in notes[0] and "tile size" in notes[0]
    assert spec_lint(spec.replace("- tile size 64", "- [advisory] tile size 64")) == []


def test_changed_parent_export_suggests_a_narrow_order_but_keeps_everything_stale(tmp_path):
    c = _repo(tmp_path)
    ledger.record(c, tmp_path, "child", file="lib.rs", span="2-3", mutant="uses update", result="killed", tests=["t"])
    ledger.record(c, tmp_path, "child", file="lib.rs", span="1", mutant="comment only", result="killed", tests=["t"])
    (tmp_path / "lib.rs").write_text(LIB.replace("x: f64", "x: f32"))
    p = ledger.plan(c, tmp_path, "child")
    assert p["node_reason"] and len(p["stale"]) == 2                     # still everything
    assert [r["mutant"] for r in p["narrow"]["near"]] == ["uses update"]
    assert [r["mutant"] for r in p["narrow"]["far"]] == ["comment only"]
    assert "Suggested order" in ledger.render(p, diff=False)


def test_refines_target_must_be_exported_by_another_node(tmp_path):
    c = ClaimCache(tmp_path)
    _concept(c)
    _git(tmp_path, "init", "-q")
    (tmp_path / "DECISIONS.md").write_text("# D\n\n## S5 nearest\nExact nearest.\n")
    (tmp_path / "a.md").write_text(_spec(implements="S5", guarantee="g", exports="n.rs::Nearest"))
    (tmp_path / "typo.md").write_text(_spec(implements="S5", guarantee="g", exports="n.rs::NearestK",
                                            refines="NearestK=Nearst"))
    (tmp_path / "ok.md").write_text(_spec(implements="S5", guarantee="g", exports="n.rs::NearestK",
                                          refines="NearestK=n.rs::Nearest"))
    _git(tmp_path, "add", "-A")
    ledger.register_node(c, tmp_path, "a", "a.md")
    notes = ledger.register_node(c, tmp_path, "typo", "typo.md")["lint"]
    assert any("`Nearst` is not exported" in n and "n.rs::Nearest" in n for n in notes)
    assert not any("not exported" in n for n in ledger.register_node(c, tmp_path, "ok", "ok.md")["lint"])


def test_ownership_lint_frictions_moved_owner_and_symbol_forms(tmp_path):
    c = ClaimCache(tmp_path)
    _concept(c)
    _git(tmp_path, "init", "-q")
    (tmp_path / "DECISIONS.md").write_text("# D\n\n## S5 nearest\nExact nearest.\n")
    (tmp_path / "a.md").write_text(_spec(implements="S5", guarantee="g", exports="src/n.rs::Nearest"))
    # the modifier exports the owner's type from where it lives now: still the owner, not a new symbol
    (tmp_path / "moved.md").write_text(_spec(implements="S5", guarantee="g", exports="src/nearest/mod.rs::Nearest"))
    # every spelling of both sides is accepted
    for i, (left, right) in enumerate([("NearestK", "Nearest"), ("src/n.rs::NearestK", "src/n.rs::Nearest"),
                                       ("NearestK::go", "Nearest")]):
        (tmp_path / f"r{i}.md").write_text(_spec(implements="S5", guarantee="g", exports="src/n.rs::NearestK::go",
                                                 refines=f"{left}={right}"))
    (tmp_path / "bad.md").write_text(_spec(implements="S5", guarantee="g", exports="src/n.rs::NearestK", refines="Nope"))
    (tmp_path / "beside.md").write_text(_spec(implements="S5", guarantee="g", exports="src/n.rs::Helper"))
    _git(tmp_path, "add", "-A")
    ledger.register_node(c, tmp_path, "a", "a.md")
    assert not any("owned" in n for n in ledger.register_node(c, tmp_path, "moved", "moved.md")["lint"])
    for i in range(3):
        notes = ledger.register_node(c, tmp_path, f"r{i}", f"r{i}.md")["lint"]
        assert not any("owned" in n or "must be" in n for n in notes), (i, notes)
    note = next(n for n in ledger.register_node(c, tmp_path, "bad", "bad.md")["lint"] if "must be" in n)
    assert "`Name`, `file::Name` or `Name::method`" in note and "src/n.rs::NearestK" in note
    note = next(n for n in ledger.register_node(c, tmp_path, "beside", "beside.md")["lint"] if "owned" in n)
    assert "if this node modifies src/n.rs::Nearest" in note


def test_spec_prose_does_not_stale_but_header_and_design_points_do(tmp_path):
    c = ClaimCache(tmp_path)
    _git(tmp_path, "init", "-q")
    spec = "---\nguarantee: g\nsee: x\n---\n# Node\nSome prose.\n\n## Design\n- [binding] use NaN\n"
    (tmp_path / "n.md").write_text(spec)
    (tmp_path / "f.rs").write_text("fn a() {}\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "c1")
    ledger.register_node(c, tmp_path, "n", "n.md")
    ledger.record(c, tmp_path, "n", file="f.rs", span="1", mutant="m", result="killed", tests=["t"])
    plan = lambda: ledger.plan(c, tmp_path, "n")
    (tmp_path / "n.md").write_text(spec.replace("Some prose.", "Orchestrator note.").replace("see: x", "see: y"))
    p = plan()
    assert p["node_reason"] is None and p["prose_changed"] == "n.md" and not p["stale"]
    assert "prose changed" in ledger.render(p, root=tmp_path)
    (tmp_path / "n.md").write_text(spec.replace("use NaN", "use zero"))
    assert plan()["node_reason"] == "spec n.md changed"
    (tmp_path / "n.md").write_text(spec.replace("guarantee: g", "guarantee: h"))
    assert plan()["node_reason"] == "spec n.md changed"
    (tmp_path / "n.md").write_text(spec)
    assert plan()["node_reason"] is None and plan()["prose_changed"] is None


def test_edit_header_adds_removes_and_keeps_comments():
    from fractal_harness.nodes import edit_header
    text = _spec(parents="a", exports="f.rs::T") .replace("parents: a", "parents: a   # uses a").replace(
        "---\n# Body", "after: b\n---\n# Body")
    out = edit_header(text, {"parents": ["b", "a"], "exports": ["f.rs::T::m"], "public": ["Z"]}, {"after": ["b"]})
    h = parse_header(out)
    assert h.parents == ["a", "b"] and h.exports == ["f.rs::T", "f.rs::T::m"] and h.public == ["Z"] and h.after == []
    assert "# uses a" in out and out.endswith("# Body text\nprose\n")


def test_ledger_node_fix_writes_missing_parent_and_methods(tmp_path):
    c = ClaimCache(tmp_path)
    _git(tmp_path, "init", "-q")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "stats.rs").write_text(STATS_RS)
    (tmp_path / "src" / "grow.rs").write_text("use crate::stats::WindowStats;\npub fn grow() {}\n")
    (tmp_path / "stats.md").write_text(_spec(guarantee="g", exports="src/stats.rs::WindowStats"))
    (tmp_path / "grow.md").write_text(_spec(after="stats", guarantee="g", exports="src/grow.rs::grow"))
    _git(tmp_path, "add", "-A")
    ledger.register_node(c, tmp_path, "stats", "stats.md", fix=True, dry_run=True)
    assert parse_header((tmp_path / "stats.md").read_text()).exports == ["src/stats.rs::WindowStats"]   # dry run wrote nothing
    r = ledger.register_node(c, tmp_path, "stats", "stats.md", fix=True)
    assert "+exports:" in r["diff"] and "WindowStats::push" in r["diff"]
    assert not any("public methods are not exported" in n for n in r["lint"])
    r = ledger.register_node(c, tmp_path, "grow", "grow.md", fix=True)
    h = parse_header((tmp_path / "grow.md").read_text())
    assert h.parents == ["stats"] and h.after == [] and r["premises"]["parents"]
    assert not any("module" in n for n in r["lint"])


def test_inherent_and_trait_methods_of_the_same_type_resolve_to_the_inherent_one():
    from fractal_harness.symbols import find
    src = """pub trait Reducer {
    fn offer(&mut self, x: f64);
}

pub struct Nearest;

impl Nearest {
    /// Inherent.
    pub fn offer(&mut self, x: f64) -> bool {
        true
    }
}

impl Reducer for Nearest {
    fn offer(&mut self, x: f64) {
    }
}
"""
    assert "-> bool" in find(src, "Nearest::offer")[1]                  # was "ambiguous"
    assert find(src, "Reducer::offer")[0] == 1                          # the trait's own declaration, by the trait's name


def test_ownership_lint_ignores_cross_cutting_decisions_no_concept_claim_names(tmp_path):
    c = ClaimCache(tmp_path)
    _git(tmp_path, "init", "-q")
    (tmp_path / "DECISIONS.md").write_text("# D\n\n## D20 shared\nEveryone follows this.\n")
    for i in range(8):
        (tmp_path / f"n{i}.md").write_text(_spec(implements="D20", guarantee="g", exports=f"f{i}.rs::Thing{i}"))
    _git(tmp_path, "add", "-A")
    notes = [n for i in range(8) for n in ledger.register_node(c, tmp_path, f"n{i}", f"n{i}.md")["lint"]]
    assert not any("owned" in n for n in notes)                         # D20 is implemented by eight nodes, legitimately
    _concept(c, decision="D20", name="the thing")                       # name it as a concept and the lint applies
    again = ledger.register_node(c, tmp_path, "n7", "n7.md")["lint"]
    assert any("D20 (the thing) is already owned" in n for n in again)


def test_an_id_in_the_concept_name_counts_and_only_in_takes_a_decision_flag():
    from fractal_harness.concepts import decisions_of
    assert decisions_of({}, "S5 nearest order") == ["S5"]
    assert decisions_of({"decision": ["D1", "S5"]}, "S5 order") == ["D1", "S5"]
    assert decisions_of({"decision": "D3"}, "order") == ["D3"]


def test_planned_exports_let_a_spec_be_registered_and_depended_on_before_its_code_exists(tmp_path):
    import pytest
    from fractal_harness.symbols import SymbolError
    c = ClaimCache(tmp_path)
    _git(tmp_path, "init", "-q")
    (tmp_path / "src").mkdir()
    (tmp_path / "new.md").write_text(_spec(guarantee="Will do X.", exports="src/new.rs::Thing, src/new.rs::Thing::go",
                                           planned="src/new.rs::Thing, src/new.rs::Thing::go"))
    (tmp_path / "child.md").write_text(_spec(parents="new", guarantee="g"))
    (tmp_path / "typo.md").write_text(_spec(guarantee="g", exports="src/new.rs::Thing"))
    (tmp_path / "typo_child.md").write_text(_spec(parents="typo", guarantee="g"))
    (tmp_path / "f.rs").write_text("fn a() {}\n")
    _git(tmp_path, "add", "-A")
    ledger.register_node(c, tmp_path, "new", "new.md")
    r = ledger.register_node(c, tmp_path, "child", "child.md")                     # was: error: no such file
    assert r["premises"]["parents"]["new"]["exports"]["src/new.rs::Thing"]
    from fractal_harness.context import bundle
    assert "(planned: not defined yet)" in bundle(tmp_path, "child.md")
    # an unplanned export that does not exist is still an error, and it says what to do
    ledger.register_node(c, tmp_path, "typo", "typo.md")
    with pytest.raises(SymbolError, match="list it under `planned:`"):
        ledger.register_node(c, tmp_path, "typo_child", "typo_child.md")
    # not stale while it is still planned; building it is a contract event the child is told about
    _git(tmp_path, "commit", "-qm", "c1")
    ledger.record(c, tmp_path, "child", file="f.rs", span="1", mutant="m", result="killed", tests=["t"])
    assert ledger.plan(c, tmp_path, "child")["node_reason"] is None
    (tmp_path / "src" / "new.rs").write_text("pub struct Thing;\nimpl Thing {\n    pub fn go(&self) {}\n}\n")
    assert "is now defined (it was planned" in ledger.plan(c, tmp_path, "child")["node_reason"]
    notes = ledger.register_node(c, tmp_path, "new", "new.md")["lint"]
    assert any("is now defined; remove it from `planned:`" in n for n in notes)
    ledger.register_node(c, tmp_path, "new", "new.md", fix=True)                   # --fix drops them from planned:
    assert parse_header((tmp_path / "new.md").read_text()).planned == []


def test_graph_check_does_not_call_a_planned_export_stale(tmp_path):
    from fractal_harness import graph_check
    c = ClaimCache(tmp_path)
    _git(tmp_path, "init", "-q")
    (tmp_path / "new.md").write_text(_spec(guarantee="g", exports="src/new.rs::Thing", planned="src/new.rs::Thing"))
    (tmp_path / "old.md").write_text(_spec(guarantee="g", exports="src/old.rs::Gone"))
    _git(tmp_path, "add", "-A")
    ledger.register_node(c, tmp_path, "new", "new.md")
    ledger.register_node(c, tmp_path, "old", "old.md")
    assert [m["symbol"] for m in graph_check.check(c, tmp_path)["missing"]] == ["src/old.rs::Gone"]

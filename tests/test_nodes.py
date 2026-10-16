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
    assert p("stats")["node_reason"] == "parent gp's contract changed"  # ...but it is stats's
    (tmp_path / "stats.md").write_text((tmp_path / "stats.md").read_text().replace("prose", "other prose"))
    assert p()["node_reason"] is None                                   # parent's own spec changed, contract didn't
    assert p("stats")["node_reason"] == "spec stats.md changed"   # (spec check precedes parents)
    (tmp_path / "stats.md").write_text(_spec(parents="gp", guarantee="Exact windowed mean.",
                                             condition="For every window.", exports="lib.rs::update"))
    assert p()["node_reason"] == "parent stats's contract changed"       # condition is part of the contract


def test_child_stales_on_parent_export_signature(tmp_path):
    c = _repo(tmp_path)
    _record(c, tmp_path, "child")
    (tmp_path / "lib.rs").write_text(LIB.replace("x: f64", "x: f32"))
    assert ledger.plan(c, tmp_path, "child")["node_reason"] == "parent stats's contract changed"
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

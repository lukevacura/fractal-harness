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

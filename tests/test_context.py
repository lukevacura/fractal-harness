from pathlib import Path

import pytest

from fractal_harness import probes
from fractal_harness.cache import ClaimCache
from fractal_harness.context import bundle, put_extracted
from fractal_harness.symbols import SymbolError, find, section

DECISIONS = """# Decisions

## D16 — Pin the float library
Exactly pinned, never a range.

## D17 — No FMA
Fused multiply-add changes rounding.

### D17 detail
Applies to hot loops too.

## D18 — Unrelated
x
"""

LIB = """/// Computes the profile.
///
/// Panics if `m` is zero.
pub fn stomp(a: &[f64],
             m: usize) -> Vec<f64> {
    let x = 1;
    x
}

pub struct Profile {
    pub p: Vec<f64>,
}

impl Profile {
    pub fn len(&self) -> usize { self.p.len() }
}
"""


def _repo(tmp_path: Path) -> Path:
    (tmp_path / "DECISIONS.md").write_text(DECISIONS)
    (tmp_path / "lib.rs").write_text(LIB)
    c = ClaimCache(tmp_path)
    put_extracted(c, tmp_path, section="DECISIONS.md::D16")
    put_extracted(c, tmp_path, section="DECISIONS.md::D17")
    put_extracted(c, tmp_path, symbol="lib.rs::stomp")
    put_extracted(c, tmp_path, symbol="lib.rs::Profile::len")
    rule = c.put("never use FMA in src", [], kind="invariant",
                 probe={"type": "grep", "pattern": "mul_add", "paths": ["*.rs"], "expect": "absent"})
    c.set_rule(rule.id, "enforced")
    c.close()
    return tmp_path


def test_section_extraction():
    assert section(DECISIONS, "D16") == "## D16 — Pin the float library\nExactly pinned, never a range."
    assert "D17 detail" in section(DECISIONS, "D17") and "D18" not in section(DECISIONS, "D17")
    assert section(DECISIONS, "D17 detail").endswith("hot loops too.")
    with pytest.raises(SymbolError):
        section(DECISIONS, "D99")


def test_symbol_extraction():
    sig = find(LIB, "stomp")[1]
    assert sig.startswith("/// Computes the profile.") and sig.endswith("-> Vec<f64> {") and "let x" not in sig
    assert find(LIB, "Profile")[1] == "pub struct Profile {\n    pub p: Vec<f64>,\n}"
    assert find(LIB, "Profile::len")[1].startswith("pub fn len")
    with pytest.raises(SymbolError):
        find(LIB, "missing")


def test_bundle_contains_exactly_the_cited_items_plus_rules(tmp_path):
    root = _repo(tmp_path)
    (root / "spec.md").write_text("# Node 7\nImplements D17 using `stomp` from lib.rs, returning a Profile::len-sized "
                                  "result. Also cites D99.\n")
    out = bundle(root, "spec.md")
    assert "# Node 7" in out
    assert "Fused multiply-add" in out and "Exactly pinned" not in out          # D17 yes, D16 not cited
    assert "lib.rs::stomp" in out and "Computes the profile" in out and "pub fn len" in out
    assert "D99" in out.split("Cited but not in the claim store")[1].splitlines()[2]
    assert "never use FMA in src" in out and "GUARANTEED" in out


def test_changed_section_is_labeled_and_replaced_on_reput(tmp_path):
    root = _repo(tmp_path)
    (root / "spec.md").write_text("Implements D16.")
    (root / "DECISIONS.md").write_text(DECISIONS.replace("Exactly pinned", "Pinned loosely"))
    out = bundle(root, "spec.md")
    assert "FAILED" in out and "read it again" in out
    c = ClaimCache(root)
    put_extracted(c, root, section="DECISIONS.md::D16")
    sections = [e for e in c.store.all() if (e.probe or {}).get("type") == "section" and "D16" in e.probe["ref"]]
    c.close()
    assert len(sections) == 1 and "Pinned loosely" in sections[0].post
    assert "FAILED" not in bundle(root, "spec.md") and "Pinned loosely" in bundle(root, "spec.md")


def test_extract_probes_validate(tmp_path):
    with pytest.raises(probes.ProbeError):
        probes.validate({"type": "section", "ref": "nofile", "sha": "x"})
    with pytest.raises(probes.ProbeError):
        probes.validate({"type": "symbol", "ref": "a::b"})


def test_subagent_hook_carries_cited_decisions(tmp_path):
    import json
    from fractal_harness import hook
    root = _repo(tmp_path)
    (root / "spec.md").write_text("Implements D17.\n")
    hook.LAUNCH_WAIT = 0
    t = tmp_path / "t.jsonl"
    t.write_text(json.dumps({"message": {"content": [{"type": "tool_use", "id": "a", "name": "Agent",
                 "input": {"subagent_type": "general-purpose", "prompt": "Implement spec.md"}}]}}) + "\n")
    out = hook.subagent_start_hook(json.dumps({"cwd": str(root), "transcript_path": str(t),
                                               "agent_type": "general-purpose"}))
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "Fused multiply-add" in ctx and "Exactly pinned" not in ctx   # D17 cited, D16 not


def test_unchecked_decisions_are_those_nothing_names(tmp_path):
    import subprocess
    from fractal_harness.context import put_extracted, unchecked_decisions
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "DECISIONS.md").write_text(
        "## D1 — tested\nx\n\n## D2 — nothing\ny\n\n## D3 — has a line\nz\nCheck: bench_d3\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("def test_d1_holds(): pass\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    c = ClaimCache(tmp_path)
    for i in ("D1", "D2", "D3"):
        put_extracted(c, tmp_path, section=f"DECISIONS.md::{i}")
    assert unchecked_decisions(c, tmp_path) == ["D2"]

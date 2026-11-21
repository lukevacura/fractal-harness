"""Fixes for issues an agent hit using fractal on a real repo (matrix-profile), one test per reproduction."""
import subprocess
from pathlib import Path

import pytest

from fractal_harness import concepts, ledger, loss, probes
from fractal_harness.cache import ClaimCache
from fractal_harness.nodes import lint, parse_header
from fractal_harness.symbols import SymbolError


def _git(root: Path, *a: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=root, check=True, capture_output=True)


def _spec(**f) -> str:
    return "---\n" + "".join(f"{k}: {v}\n" for k, v in f.items()) + "---\n# Body\n"


# -- a malformed probe is an error, not a pass ------------------------------------------------------------------

def test_an_unknown_probe_key_is_refused_with_the_likely_meaning():
    with pytest.raises(probes.ProbeError, match=r"does not read 'absent'.*\"expect\": \"absent\""):
        probes.validate({"type": "grep", "pattern": "x", "paths": ["*.py"], "absent": True})
    with pytest.raises(probes.ProbeError, match="did you mean 'exclude_paths'"):
        probes.validate({"type": "grep", "pattern": "x", "paths": ["*.py"], "exclude_path": ["a.py"]})
    with pytest.raises(probes.ProbeError, match="does not read"):      # inside an `all` too
        probes.validate({"type": "all", "probes": [{"type": "command", "run": "true", "expect": 0}]})


def test_a_malformed_rule_in_the_store_fails_instead_of_passing(tmp_path):
    (tmp_path / "a.py").write_text("eval(x)\n")
    with pytest.raises(probes.ProbeError):
        probes.run({"type": "grep", "pattern": "eval", "paths": ["*.py"], "absent": True}, tmp_path)


def test_every_key_the_code_writes_is_accepted():
    probes.validate({"type": "grep", "pattern": "x", "paths": ["*.py"], "expect": {"max": 3}, "debt_target": "absent",
                     "exclude": "^#", "exclude_paths": ["a.py"], "skip_tests": True, "behavioral": False,
                     "evidence": "exact", "concept": "c"})
    probes.validate({"type": "command", "run": "true", "expect_exit": 0, "timeout": 5, "evidence": "proxy"})


# -- spec headers ------------------------------------------------------------------------------------------------

def test_status_is_a_header_field_for_people():
    h = parse_header(_spec(status="building 2026-11-12", parents="a", guarantee="g."))
    assert h.parents == ["a"] and h.extra == {}
    assert not any("status" in n for n in lint(h))


def test_a_near_miss_of_a_read_field_is_refused_and_other_fields_are_kept_as_notes():
    with pytest.raises(SymbolError, match="did you mean 'parents'"):
        parse_header(_spec(parent="a", guarantee="g."))
    h = parse_header(_spec(built="2026-11-01", guarantee="g."))
    assert h.extra == {"built": "2026-11-01"}
    assert any("`built:` is not a field fractal reads" in n for n in lint(h))


# -- bulk import of specs into the ledger --------------------------------------------------------------------------

def _specs_repo(tmp_path: Path) -> ClaimCache:
    _git(tmp_path, "init", "-q")
    (tmp_path / ".git" / "info" / "exclude").write_text(".fractal/\n")
    d = tmp_path / "docs" / "specs"
    d.mkdir(parents=True)
    (d / "base.md").write_text(_spec(guarantee="Base promise."))
    (d / "mid.md").write_text(_spec(status="ready", parents="base", guarantee="Mid promise."))
    (d / "top.md").write_text(_spec(parents="mid, base", guarantee="Top promise."))
    (d / "orphan.md").write_text(_spec(parents="ghost", guarantee="Needs a spec-less parent."))
    (d / "below.md").write_text(_spec(parents="orphan", guarantee="Waits on the orphan."))
    (d / "TEMPLATE.md").write_text(_spec(guarantee="x"))
    (d / "_notes.md").write_text("notes\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "c1")
    return ClaimCache(tmp_path)


def test_import_registers_parents_before_children_and_explains_what_it_cannot(tmp_path):
    c = _specs_repo(tmp_path)
    r = ledger.import_specs(c, tmp_path, ["docs/specs/*.md"])
    assert [n for n, _, _ in r["registered"]] == ["base", "mid", "top"]
    assert set(c.store.all_nodes()) == {"base", "mid", "top"}
    assert "ghost" in r["skipped"]["orphan"] and "`after:`" in r["skipped"]["orphan"]
    assert "orphan" in r["skipped"]["below"]
    assert "TEMPLATE" not in r["skipped"] and "_notes" not in r["skipped"]
    c.close()


def test_import_leaves_registered_nodes_alone_and_finds_the_spec_folder(tmp_path):
    c = _specs_repo(tmp_path)
    ledger.register_node(c, tmp_path, "base", "docs/specs/base.md")
    before = c.store.get_node("base")
    (tmp_path / "docs/specs/base.md").write_text(_spec(guarantee="A changed promise."))
    r = ledger.import_specs(c, tmp_path)                 # no globs: the registered specs' folder
    assert "base" not in [n for n, _, _ in r["registered"]] and "mid" in [n for n, _, _ in r["registered"]]
    assert c.store.get_node("base") == before            # its verdicts still read against the spec it had
    c.close()


def test_import_dry_run_changes_nothing(tmp_path):
    c = _specs_repo(tmp_path)
    r = ledger.import_specs(c, tmp_path, dry_run=True)            # nothing registered: docs/specs by convention
    assert [n for n, _, _ in r["registered"]] == ["base", "mid", "top"] and c.store.all_nodes() == {}
    c.close()


# -- command probes in a worktree --------------------------------------------------------------------------------

def test_a_command_on_an_ignored_file_is_not_applicable_in_a_worktree(tmp_path):
    main = tmp_path / "main"
    main.mkdir()
    _git(main, "init", "-q")
    (main / ".gitignore").write_text("scripts/check.py\n.fractal/\n")
    (main / "scripts").mkdir()
    (main / "scripts" / "check.py").write_text("import sys; sys.exit(0)\n")
    (main / "a.txt").write_text("x\n")
    _git(main, "add", "-A")
    _git(main, "commit", "-qm", "c1")
    ClaimCache(main).close()                              # the shared store lives in the main checkout
    wt = tmp_path / "wt"
    _git(main, "worktree", "add", "-q", str(wt))
    p = {"type": "command", "run": "python3 scripts/check.py"}
    assert probes.run(p, main).passed and not probes.run(p, main).vacuous
    r = probes.run(p, wt)
    assert r.passed and r.vacuous and "scripts/check.py is only in the main checkout" in r.detail
    assert not probes.run({"type": "command", "run": "python3 scripts/missing.py"}, wt).passed   # missing everywhere


# -- test code ---------------------------------------------------------------------------------------------------

RUST = """pub fn lib() { zone + 1 }
#[cfg(test)]
use helper;
struct S {
    a: u8,
    #[cfg(test)]
    b: u8,
}
fn more() { zone + 1 }
#[cfg(test)]
mod tests {
    fn t() { let s = "}"; zone + 1 }
}
fn last() { zone + 1 }
#[cfg(test)]
pub(crate) fn join(
    a: u8,
) -> u8 { zone + 1 }
fn after() { zone + 1 }
"""


def test_skip_tests_drops_test_files_and_cfg_test_items(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "lib.rs").write_text(RUST)
    (tmp_path / "src" / "tests.rs").write_text("zone + 1\n")
    (tmp_path / "tests" / "it.rs").write_text("zone + 1\n")
    p = {"type": "grep", "pattern": r"zone \+ 1", "paths": ["**/*.rs"], "expect": {"max": 99}}
    assert probes.run(p, tmp_path).detail.startswith("8 matching")
    assert probes.run({**p, "skip_tests": True}, tmp_path).detail.startswith("4 matching")
    assert probes.test_lines("src/lib.rs", RUST) == {1, 2, 5, 6, 9, 10, 11, 12, 14, 15, 16, 17}


def test_a_cfg_test_field_ends_at_its_comma_or_at_the_closing_brace():
    src = "struct S {\n    #[cfg(test)]\n    b: u8,\n    c: u8,\n}\nenum E {\n    A,\n    #[cfg(test)]\n    B(u8)\n}\nfn f() {}\n"
    assert probes.test_lines("a.rs", src) == {1, 2, 7, 8}


def test_skip_tests_is_compared_soundly():
    g = lambda **k: {"type": "grep", "pattern": "x", "paths": ["src/**"], "expect": "absent", **k}
    assert loss.absorbs_leaf(g(), g(skip_tests=True), "upper")            # counting every line forbids more
    assert not loss.absorbs_leaf(g(skip_tests=True), g(), "upper")        # dropping tests forbids less
    assert loss.absorbs_leaf(g(skip_tests=True), g(skip_tests=True), "upper")
    assert not loss.absorbs_leaf(g(exclude="^#", skip_tests=True), g(exclude="^#"), "upper")
    assert not loss.delta([("r", g())], [("r", g(skip_tests=True))]).lossless


# -- concepts ----------------------------------------------------------------------------------------------------

def test_rejected_rules_are_not_concepts(tmp_path):
    (tmp_path / "n.rs").write_text("struct Nearest;\n")
    c = ClaimCache(tmp_path)
    pr = {"type": "only_in", "pattern": "struct Nearest", "paths": ["*.rs"], "files": ["n.rs"], "concept": "D9 nearest"}
    e = c.put("nearest is defined only in n.rs", ["n.rs"], kind="invariant", probe=pr)
    assert [r["claim"] for r in concepts.registry(c)] == [e.id] and "D9" in concepts.decision_ids(c)
    c.set_rule(e.id, "rejected")
    assert concepts.registry(c) == [] and concepts.decision_ids(c) == {}
    c.close()


# -- task maps in a Rust repo ------------------------------------------------------------------------------------

def test_map_finds_rust_definitions_and_named_nodes(tmp_path):
    from fractal_harness.taskmap import task_map
    _git(tmp_path, "init", "-q")
    (tmp_path / ".git" / "info" / "exclude").write_text(".fractal/\n")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "exclusion.rs").write_text("pub const fn zone(m: usize) -> usize { m }\n")
    (tmp_path / "src" / "walk.rs").write_text("pub(crate) struct Walker;\n")
    (tmp_path / "walk.md").write_text(_spec(guarantee="Walks.", exports="src/walk.rs::Walker"))
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "c1")
    c = ClaimCache(tmp_path)
    ledger.register_node(c, tmp_path, "survivor-walk", "walk.md")
    c.close()
    m = task_map(tmp_path, "widen `zone` and update survivor-walk")
    got = {f: r.why for r in m.regions for f in r.files}
    assert "defines `zone`" in got["src/exclusion.rs"]
    assert "node survivor-walk named in the task" in got["src/walk.rs"]
    assert [f for r in task_map(tmp_path, "finish survivor-walk").regions for f in r.files] == ["src/walk.rs"]


# -- invariants: each node planned once ----------------------------------------------------------------------------

def test_mutation_checked_plans_each_node_once_for_many_probes(tmp_path, monkeypatch):
    c = ClaimCache(tmp_path)
    monkeypatch.setattr(c.store, "verdict_nodes", lambda: ["a", "b"])
    calls = []
    monkeypatch.setattr(ledger, "plan", lambda cache, root, node: calls.append(node) or
                        {"valid": [{"result": "killed", "tests": ["test_x"]}]})
    memo: dict = {}
    for _ in range(3):
        assert ledger.mutation_checked(c, tmp_path, {"type": "command", "run": "pytest -k test_x"}, memo) == ["a", "b"]
    assert calls == ["a", "b"]
    assert ledger.mutation_checked(c, tmp_path, {"type": "grep", "pattern": "x", "paths": ["*"]}, memo) == []
    c.close()

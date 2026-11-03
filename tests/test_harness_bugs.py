"""Regressions for six bugs found running the harness on a real downstream repo."""
import json
import subprocess

import pytest

from fractal_harness import brief, ledger
from fractal_harness.cache import ClaimCache
from fractal_harness.nodes import lint, parse_header
from fractal_harness.symbols import SymbolError, find

from test_nodes import DECISIONS, _git, _repo, _spec


# 1. --fix --dry-run must not register the node, even when there is nothing to fix -------------

def test_dry_run_registers_nothing_when_there_is_nothing_to_fix(tmp_path):
    c = _repo(tmp_path)
    (tmp_path / "n.md").write_text(_spec(guarantee="g"))
    r = ledger.register_node(c, tmp_path, "n", "n.md", fix=True, dry_run=True)
    assert r["dry_run"] and r["diff"] == ""
    assert c.store.get_node("n") is None


def test_dry_run_without_fix_also_changes_nothing(tmp_path):
    c = _repo(tmp_path)
    (tmp_path / "n.md").write_text(_spec(guarantee="g"))
    ledger.register_node(c, tmp_path, "n", "n.md", dry_run=True)
    assert c.store.get_node("n") is None


# 2. a call is not a definition: `match f(x) {` ------------------------------------------------

RS = """pub fn to_ticks(x: f64, scale: f64) -> i64 {
    (x * scale) as i64
}

pub fn quantize(x: f64, scale: f64) -> i64 {
    match to_ticks(x, scale) {
        0 => 1,
        t => t,
    }
}
"""


def test_a_match_scrutinee_call_is_not_a_second_definition():
    line, text = find(RS, "to_ticks")
    assert line == 0 and text.startswith("pub fn to_ticks")


@pytest.mark.parametrize("stmt", ["switch foo(x) {", "case foo(x):", "elif foo(x):", "raise foo(x)", "with foo(x) as y:",
                                  "print foo(x)", "loop foo(x) {", "catch foo(x) {", "return foo(x)"])
def test_control_flow_lines_are_not_definitions(stmt):
    text = f"def foo(x):\n    pass\n\ndef g(x):\n    {stmt}\n"
    assert find(text, "foo")[0] == 0


def test_a_name_that_merely_starts_with_a_keyword_still_defines():
    text = "fn iffy() {}\nfn matcher() {}\n"
    assert find(text, "iffy")[0] == 0 and find(text, "matcher")[0] == 1


# 3. a free function and a method of the same name ---------------------------------------------

LIBRS = """pub fn distance_profile(q: &[f64]) -> Vec<f64> {
    vec![]
}

impl PatternLibrary {
    pub fn distance_profile(&self, q: &[f64]) -> Vec<f64> {
        distance_profile(q)
    }
}
"""


def test_the_bare_name_is_the_free_function_and_the_qualified_name_the_method():
    assert find(LIBRS, "distance_profile")[0] == 0
    line, text = find(LIBRS, "PatternLibrary::distance_profile")
    assert line == 5 and "&self" in text


def test_a_bare_name_with_two_free_definitions_is_still_ambiguous():
    with pytest.raises(SymbolError, match="ambiguous"):
        find("fn a() {}\nfn a(x: i32) {}\n", "a")


def test_two_methods_of_the_same_name_stay_ambiguous_without_a_free_function():
    text = "impl A {\n    fn go(&self) {}\n}\nimpl B {\n    fn go(&self) {}\n}\n"
    with pytest.raises(SymbolError, match="ambiguous"):
        find(text, "go")


# 4. prose in implements:/exports: is not entries ----------------------------------------------

def test_prose_in_implements_keeps_only_decision_ids():
    h = parse_header("---\nimplements: N11, T7 (the grid rules), D25 and R2 plus the survivor filter\n"
                     "guarantee: g\n---\n")
    assert h.implements == ["N11", "T7", "D25", "R2"]
    assert h.ignored["implements"] == ["and", "plus", "the", "survivor", "filter"]
    assert any("implements" in n and "survivor" in n for n in lint(h))


def test_prose_in_exports_keeps_refs_and_whole_files():
    h = parse_header("---\nexports: src/a.rs::f, src/b.rs (whole file) and then docs:x.md\nguarantee: g\n---\n")
    assert h.exports == ["src/a.rs::f", "src/b.rs", "docs:x.md"]
    assert h.ignored["exports"] == ["and", "then"]


def test_other_list_fields_are_only_stripped_of_parenthesised_comments():
    h = parse_header("---\nparents: a, b (the old one)\nafter: c\nguarantee: g\n---\n")
    assert h.parents == ["a", "b"] and h.after == ["c"] and not h.ignored


def test_prose_words_no_longer_pull_unrelated_decisions_into_a_node(tmp_path):
    c = _repo(tmp_path)
    (tmp_path / "DECISIONS.md").write_text(DECISIONS + "\n## D19 — The filter\nprose\n")
    (tmp_path / "n.md").write_text(_spec(implements="D18 and the filter", guarantee="g"))
    _git(tmp_path, "add", "-A")
    r = ledger.register_node(c, tmp_path, "n", "n.md")
    ids = r["premises"]["decisions"]
    assert len(ids) == 1 and "ignored" in " ".join(r["lint"])


# 5. the brief says where the work lives -------------------------------------------------------

def test_brief_names_the_registered_branch_and_the_worktree_on_it(tmp_path):
    c = _repo(tmp_path)
    (tmp_path / "n.md").write_text(_spec(guarantee="g"))
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "x")
    _git(tmp_path, "branch", "ladder-rung")
    wt = tmp_path.parent / (tmp_path.name + "-wt")
    _git(tmp_path, "worktree", "add", "-q", str(wt), "ladder-rung")
    ledger.register_node(c, tmp_path, "n", "n.md", branch="ladder-rung")
    text = brief.build(c, tmp_path, "n")["text"]
    assert "Branch: `ladder-rung` (registered with this node)" in text
    assert str(wt.resolve()) in text or str(wt) in text
    assert text == brief.build(c, tmp_path, "n")["text"]


def test_brief_without_a_registered_branch_says_so(tmp_path):
    c = _repo(tmp_path)
    (tmp_path / "n.md").write_text(_spec(guarantee="g"))
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "x")
    ledger.register_node(c, tmp_path, "n", "n.md")
    text = brief.build(c, tmp_path, "n")["text"]
    assert "default for this node; none registered" in text and "does not exist yet" in text


def test_a_declaration_whose_type_starts_with_a_keyword_still_defines():
    # `format_t` begins with `for`, `newtype_t` with `new`: only a whole keyword excludes a line
    assert find("format_t foo(int x) {\n}\n", "foo")[0] == 0
    assert find("newtype_t bar(int x);\n", "bar")[0] == 0


def test_brief_marks_this_checkout_when_it_is_the_branch_worktree(tmp_path):
    c = _repo(tmp_path)
    (tmp_path / "n.md").write_text(_spec(guarantee="g"))
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "x")
    _git(tmp_path, "branch", "rung")
    wt = tmp_path.parent / (tmp_path.name + "-rung")
    _git(tmp_path, "worktree", "add", "-q", str(wt), "rung")
    ledger.register_node(c, tmp_path, "n", "n.md", branch="rung")
    from_main = brief.build(c, tmp_path, "n")["text"]
    assert "(this checkout)" not in from_main and "this checkout is" in from_main
    from_wt = brief.build(ClaimCache(wt), wt, "n")["text"]
    assert "(this checkout)" in from_wt


# 6. a huge rewritten file must not be held in memory by the diff reader ------------------------

def test_hunks_since_streams_and_matches_the_old_answer(tmp_path):
    _git(tmp_path, "init", "-q")
    (tmp_path / "a.txt").write_text("".join(f"line {i}\n" for i in range(20)))
    (tmp_path / "gone.txt").write_text("x\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "a")
    lines = (tmp_path / "a.txt").read_text().splitlines(True)
    lines[3] = "changed\n"
    del lines[10:12]
    (tmp_path / "a.txt").write_text("".join(lines))
    (tmp_path / "gone.txt").unlink()
    (tmp_path / "new.txt").write_text("n\n")
    _git(tmp_path, "add", "-A")
    h = ledger.hunks_since(tmp_path, "HEAD")
    assert h["a.txt"] == [(4, 1), (11, 2)]
    assert (1, 10**9) in h["gone.txt"] and h["new.txt"] == [(1, 10**9)]


def test_hunks_since_never_holds_the_whole_diff(tmp_path, monkeypatch):
    # the reader must be a generator over git's output, not `_git(...)` (capture the lot, then split it)
    monkeypatch.setattr(ledger, "_git", lambda *a, **k: pytest.fail("hunks_since captured the whole diff"))
    _git(tmp_path, "init", "-q")
    (tmp_path / "a.txt").write_text("1\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "a")
    (tmp_path / "a.txt").write_text("2\n")
    assert ledger.hunks_since(tmp_path, "HEAD") == {"a.txt": [(1, 1)]}


def test_hunks_since_still_raises_on_a_bad_commit(tmp_path):
    _git(tmp_path, "init", "-q")
    (tmp_path / "a.txt").write_text("1\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "a")
    with pytest.raises(SymbolError):
        ledger.hunks_since(tmp_path, "no-such-commit")


# 7. registering on a branch checks the branch's files, not this checkout's ---------------------

def test_a_node_on_a_branch_is_checked_against_the_branchs_files(tmp_path):
    c = _repo(tmp_path)
    (tmp_path / "README").write_text("main\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "main")
    _git(tmp_path, "branch", "ladder")
    wt = tmp_path.parent / (tmp_path.name + "-ladder")
    _git(tmp_path, "worktree", "add", "-q", str(wt), "ladder")
    (wt / "src").mkdir()
    (wt / "src" / "survivor.rs").write_text("pub fn survive(x: u8) -> bool { true }\n")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-qm", "ladder")
    (tmp_path / "surv.md").write_text(_spec(guarantee="g", exports="src/survivor.rs::survive"))
    ledger.register_node(c, tmp_path, "surv", "surv.md", branch="ladder")     # the parent lives on the branch
    (tmp_path / "bound.md").write_text(_spec(parents="surv", guarantee="g"))
    # main has no src/survivor.rs: the parent's export cannot be read there...
    with pytest.raises(SymbolError, match="survivor"):
        ledger.register_node(c, tmp_path, "bound", "bound.md")
    # ...but the child is on the ladder branch, where it is
    r = ledger.register_node(c, tmp_path, "bound", "bound.md", branch="ladder")
    assert r["premises"]["branch"] == "ladder" and "src/survivor.rs::survive" in r["premises"]["parents"]["surv"]["exports"]
    assert not (tmp_path / "src").exists()           # nothing was checked out or left behind


def test_a_parents_exports_come_from_the_branch_when_registering_the_child(tmp_path):
    c = _repo(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "p.rs").write_text("pub fn p() {}\n")
    (tmp_path / "p.md").write_text(_spec(guarantee="g", exports="src/p.rs::p"))
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "main")
    ledger.register_node(c, tmp_path, "p", "p.md")
    _git(tmp_path, "checkout", "-qb", "ladder")
    (tmp_path / "src" / "p.rs").write_text("pub fn p(x: u8) {}\n")      # the parent's export changed on the branch
    (tmp_path / "src" / "k.rs").write_text("pub fn k() {}\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "ladder")
    _git(tmp_path, "checkout", "-q", "-")
    (tmp_path / "k.md").write_text(_spec(parents="p", guarantee="g", exports="src/k.rs::k"))
    r = ledger.register_node(c, tmp_path, "k", "k.md", branch="ladder")
    from fractal_harness.symbols import digest
    assert r["premises"]["parents"]["p"]["exports"]["src/p.rs::p"] == digest("pub fn p(x: u8) {}")


def test_a_missing_branch_is_an_error_and_other_reads_are_unaffected(tmp_path):
    c = _repo(tmp_path)
    (tmp_path / "n.md").write_text(_spec(guarantee="g"))
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "x")
    with pytest.raises(SymbolError, match="does not exist"):
        ledger.register_node(c, tmp_path, "n", "n.md", branch="nope")
    ledger.register_node(c, tmp_path, "n", "n.md")
    from fractal_harness.symbols import at, read_ref
    assert read_ref(tmp_path, "n.md") is not None and at(tmp_path, "n.md") == tmp_path / "n.md"   # view is gone


def test_ignored_prose_is_reported_as_one_phrase():
    h = parse_header("---\nimplements: D18 the survivor filter's rules\nguarantee: g\n---\n")
    note = next(n for n in lint(h) if "implements" in n)
    assert 'the survivor filter\'s rules' in note and "D18" not in note.split("ignored")[1]


# 8. `pub use` re-exports are definitions (a facade module can be exported) ---------------------

SYNC = """#[cfg(loom)]
pub use loom::sync::Arc;
#[cfg(shuttle)]
pub use shuttle::sync::Arc;
#[cfg(not(any(loom, shuttle)))]
pub use alloc::sync::Arc;
#[cfg(loom)] pub use loom::sync::Mutex;
pub use core::sync::atomic::{AtomicBool, AtomicU64 as Counter, Ordering};
use std::fmt::Debug;
pub mod thread {
    pub use loom::thread::*;
}
"""


def test_pub_use_defines_its_name_and_every_cfg_alternative_is_one_interface():
    line, text = find(SYNC, "Arc")
    assert line == 1
    assert text.count("pub use") == 3 and "#[cfg(shuttle)]" in text and "alloc::sync::Arc" in text


def test_same_line_attribute_aliases_and_groups():
    assert find(SYNC, "Mutex") == (6, "#[cfg(loom)] pub use loom::sync::Mutex;")
    for n in ("AtomicBool", "Counter", "Ordering"):
        assert find(SYNC, n) == (7, "pub use core::sync::atomic::{AtomicBool, AtomicU64 as Counter, Ordering};")
    with pytest.raises(SymbolError):
        find(SYNC, "AtomicU64")                       # renamed away: only the alias is visible here


def test_a_private_import_is_not_a_definition_and_a_glob_names_nothing():
    with pytest.raises(SymbolError, match="no definition"):
        find(SYNC, "Debug")
    with pytest.raises(SymbolError, match="no definition"):
        find(SYNC, "loom")            # a path segment, not a name the file makes visible


def test_a_real_definition_beats_a_reexport_of_the_same_name():
    text = "pub use other::Thing;\npub struct Thing {\n    x: u8,\n}\n"
    assert find(text, "Thing")[1].startswith("pub struct Thing")


def test_a_facade_module_of_only_reexports_includes_its_body_but_a_normal_module_does_not():
    assert find(SYNC, "thread")[1] == "pub mod thread {\n    pub use loom::thread::*;\n}"
    assert find("pub mod m {\n    pub fn f() {}\n}\n", "m")[1] == "pub mod m {"


def test_star_exports_the_whole_file():
    assert find(SYNC, "*") == (0, SYNC.rstrip())


def test_a_node_can_export_and_be_depended_on_through_a_facade_module(tmp_path):
    c = _repo(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "sync.rs").write_text(SYNC)
    (tmp_path / "src" / "user.rs").write_text("use crate::sync::Arc;\npub fn go() {}\n")
    (tmp_path / "slow.md").write_text(_spec(guarantee="g", exports="src/sync.rs::Arc, src/sync.rs::thread"))
    (tmp_path / "user.md").write_text(_spec(parents="slow", guarantee="g", exports="src/user.rs::go"))
    _git(tmp_path, "add", "-A")
    ledger.register_node(c, tmp_path, "slow", "slow.md")          # was: export ... Arc is not defined
    r = ledger.register_node(c, tmp_path, "user", "user.md")
    assert set(r["premises"]["parents"]["slow"]["exports"]) == {"src/sync.rs::Arc", "src/sync.rs::thread"}
    # editing one cfg alternative changes the contract a child watches
    (tmp_path / "src" / "sync.rs").write_text(SYNC.replace("alloc::sync::Arc", "std::sync::Arc"))
    from fractal_harness.nodes import contract_change
    assert contract_change(r["premises"]["parents"]["slow"], tmp_path, "slow.md")


def test_a_facade_node_can_export_star(tmp_path):
    c = _repo(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "sync.rs").write_text(SYNC)
    (tmp_path / "slow.md").write_text(_spec(guarantee="g", exports="src/sync.rs::*"))
    _git(tmp_path, "add", "-A")
    r = ledger.register_node(c, tmp_path, "slow", "slow.md")
    assert not any("not defined" in n for n in r["lint"])
    assert ledger.register_node(c, tmp_path, "slow", "slow.md")["declared"]


def test_a_module_with_anything_but_reexports_exposes_only_its_signature():
    text = "pub mod m {\n    pub use a::B;\n    pub fn f() {}\n}\n"
    assert find(text, "m")[1] == "pub mod m {"


def test_self_in_a_use_group_names_the_module_and_underscore_names_nothing():
    text = "pub use a::b::{self, C, D as _};\n"
    assert find(text, "b")[0] == 0 and find(text, "C")[0] == 0
    with pytest.raises(SymbolError):
        find(text, "D")
    with pytest.raises(SymbolError):
        find(text, "self")

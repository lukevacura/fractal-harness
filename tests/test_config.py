"""Phase 0: fractal.toml, named roots, and cross-root premises that make dependents stale."""
import pytest

from fractal_harness import activation, ledger
from fractal_harness.cache import ClaimCache
from fractal_harness.cli import main
from fractal_harness.config import ConfigError, doctor_checks, load, resolve_path
from fractal_harness.context import put_extracted
from fractal_harness.hashing import MISSING, file_hash
from fractal_harness.setup import doctor
from fractal_harness.symbols import SymbolError, section_of, symbol_of

from test_nodes import _git, _spec
from test_activation import _due


def _cfg(repo, text):
    (repo / "fractal.toml").write_text(text)


# --- config ------------------------------------------------------------------------------

def test_no_config_is_defaults_and_plain_paths_resolve_as_before(tmp_path):
    c = load(tmp_path)
    assert (c.roots, c.accept_policy, c.warnings, c.present) == ({}, "human_only", [], False)
    assert resolve_path(tmp_path, "a/b.md") == tmp_path / "a/b.md"
    assert doctor_checks(tmp_path) == []


def test_policy_parsed_and_validated(tmp_path):
    _cfg(tmp_path, '[policy]\naccept_policy = "local_with_audit"\n')
    assert load(tmp_path).accept_policy == "local_with_audit"
    _cfg(tmp_path, '[policy]\naccept_policy = "yolo"\n')
    with pytest.raises(ConfigError, match="accept_policy must be one of"):
        load(tmp_path)


def test_malformed_config_is_a_clear_error_not_a_traceback(tmp_path, capsys):
    _cfg(tmp_path, "[roots\ndocs = ")
    with pytest.raises(ConfigError, match="not valid TOML"):
        load(tmp_path)
    for bad in ('roots = 3', '[roots]\ndocs = 3', '[roots]\nd = "x"', '[roots]\ndocs = ""'):
        _cfg(tmp_path, bad)
        with pytest.raises(ConfigError):
            load(tmp_path)
    assert doctor_checks(tmp_path)[0][0] is False
    _cfg(tmp_path, "[roots\n")
    assert main(["--root", str(tmp_path), "doctor"]) == 1      # reported, no traceback
    assert "not valid TOML" in capsys.readouterr().out


def test_unknown_tables_ignored_unknown_owned_keys_warned(tmp_path):
    _cfg(tmp_path, '[jobs]\nwhatever = 1\n[policy]\naccept_policy = "local"\nbogus = 1\n')
    c = load(tmp_path)
    assert c.accept_policy == "local" and len(c.warnings) == 1 and "bogus" in c.warnings[0]
    assert any("bogus" in m and not ok for ok, m in doctor_checks(tmp_path))


def test_root_resolution_relative_absolute_and_tilde(tmp_path, monkeypatch):
    repo, docs = tmp_path / "code", tmp_path / "docs"
    repo.mkdir(), docs.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    _cfg(repo, f'[roots]\ndocs = "../docs"\nabs = "{docs}"\nhome = "~/docs"\n')
    for r in ("docs", "abs", "home"):
        assert resolve_path(repo, f"{r}:specs/a.md") == docs.resolve() / "specs/a.md"


def test_escape_unknown_root_missing_root_and_absolute_are_rejected(tmp_path):
    repo = tmp_path / "code"
    repo.mkdir()
    (tmp_path / "secret.md").write_text("s")
    _cfg(repo, '[roots]\ndocs = "../docs"\ngone = "../nowhere"\n')
    (tmp_path / "docs").mkdir()
    for ref in ("docs:../secret.md", "docs:a/../../secret.md", "docs:/etc/passwd"):
        with pytest.raises(ConfigError):
            resolve_path(repo, ref)
    with pytest.raises(ConfigError, match="unknown root 'nope'"):
        resolve_path(repo, "nope:a.md")
    with pytest.raises(ConfigError, match="does not exist"):
        resolve_path(repo, "gone:a.md")
    with pytest.raises(ConfigError, match="no such file"):
        load(repo).resolve("docs:missing.md", must_exist=True)
    with pytest.raises(SymbolError, match="escapes"):
        section_of(repo, "docs:../secret.md::A")


def test_symlink_out_of_root_is_an_escape(tmp_path):
    repo, docs = tmp_path / "code", tmp_path / "docs"
    repo.mkdir(), docs.mkdir()
    (tmp_path / "outside.md").write_text("## A\nx\n")
    (docs / "link.md").symlink_to(tmp_path / "outside.md")
    _cfg(repo, '[roots]\ndocs = "../docs"\n')
    with pytest.raises(ConfigError, match="escapes"):
        resolve_path(repo, "docs:link.md")


def test_doctor_reports_roots_that_do_not_resolve(tmp_path):
    repo = tmp_path / "code"
    repo.mkdir()
    (tmp_path / "docs").mkdir()
    _cfg(repo, '[roots]\ndocs = "../docs"\ngone = "../nowhere"\n')
    got = dict((m.split()[1], ok) for ok, m in doctor_checks(repo))
    assert got == {"'docs'": True, "'gone'": False}
    res = doctor(repo)
    assert any(not ok and "'gone'" in m for ok, m in res) and any(ok and "'docs'" in m for ok, m in res)


# --- cross-root digests ------------------------------------------------------------------

def test_file_hash_follows_the_other_roots_content(tmp_path):
    repo, docs = tmp_path / "code", tmp_path / "docs"
    repo.mkdir(), docs.mkdir()
    _cfg(repo, '[roots]\ndocs = "../docs"\n')
    assert file_hash(repo, "docs:a.md") == MISSING
    (docs / "a.md").write_text("one")
    h1 = file_hash(repo, "docs:a.md")
    (docs / "a.md").write_text("two")
    assert h1 != file_hash(repo, "docs:a.md") != MISSING
    assert file_hash(repo, "nope:a.md") == MISSING


def test_section_and_symbol_through_a_root(tmp_path):
    repo, docs = tmp_path / "code", tmp_path / "docs"
    repo.mkdir(), docs.mkdir()
    _cfg(repo, '[roots]\ndocs = "../docs"\n')
    (docs / "D.md").write_text("## D1 — rule\nbody\n")
    (docs / "api.py").write_text("def f(x):\n    return x\n")
    assert section_of(repo, "docs:D.md::D1").startswith("## D1")
    assert symbol_of(repo, "docs:api.py::f").startswith("def f(x)")


def _two_dirs(tmp_path):
    repo, docs = tmp_path / "code", tmp_path / "docs"
    repo.mkdir(), docs.mkdir()
    _git(repo, "init", "-q")
    (repo / ".git" / "info" / "exclude").write_text(".fractal/\n")
    _cfg(repo, '[roots]\ndocs = "../docs"\n')
    (repo / "lib.rs").write_text("pub fn update(x: f64) -> f64 {\n    x\n}\n")
    (docs / "DECISIONS.md").write_text("## D18 — Bitwise\nReproduce bits.\n")
    (docs / "parent.md").write_text(_spec(guarantee="Parent promise.", exports="lib.rs::update"))
    (docs / "child.md").write_text(_spec(parents="parent", implements="D18"))
    c = ClaimCache(repo)
    put_extracted(c, repo, section="docs:DECISIONS.md::D18")
    ledger.register_node(c, repo, "parent", "docs:parent.md")
    ledger.register_node(c, repo, "child", "docs:child.md")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "c1")
    return c, repo, docs


def test_cross_root_nothing_due_until_the_other_repo_changes(tmp_path):
    c, repo, docs = _two_dirs(tmp_path)
    assert _due(c, repo) == {}
    assert c.store.get_node("child")["spec"] == "docs:child.md"


def test_cross_root_decision_change_wakes_dependent(tmp_path):
    c, repo, docs = _two_dirs(tmp_path)
    (docs / "DECISIONS.md").write_text("## D18 — Bitwise\nReproduce bits, and round half even.\n")
    assert _due(c, repo) == {"child": {"contract"}}


def test_cross_root_parent_contract_change_wakes_child(tmp_path):
    c, repo, docs = _two_dirs(tmp_path)
    (docs / "parent.md").write_text(_spec(guarantee="A stronger promise.", exports="lib.rs::update"))
    assert _due(c, repo) == {"child": {"contract"}, "parent": {"spec"}}


def test_cross_root_spec_edit_and_removal_fire_spec_event(tmp_path):
    c, repo, docs = _two_dirs(tmp_path)
    (docs / "child.md").write_text(_spec(parents="parent", implements="D18", guarantee="new"))
    assert _due(c, repo) == {"child": {"spec"}}
    (docs / "child.md").unlink()
    r = activation.due(c, repo)
    assert any("is gone" in e["detail"] for row in r for e in row["events"])


def test_cross_root_prose_outside_the_decision_does_not_wake(tmp_path):
    c, repo, docs = _two_dirs(tmp_path)
    (docs / "DECISIONS.md").write_text("## D18 — Bitwise\nReproduce bits.\n\n## D19 — other\nnew\n")
    assert _due(c, repo) == {}


def test_register_node_with_unknown_root_is_a_clear_error(tmp_path):
    c, repo, docs = _two_dirs(tmp_path)
    with pytest.raises(SymbolError, match="unknown root 'zzz'"):
        ledger.register_node(c, repo, "x", "zzz:a.md")
    with pytest.raises(SymbolError, match="no such spec"):
        ledger.register_node(c, repo, "x", "docs:missing.md")


def test_no_config_behaviour_is_unchanged(tmp_path):
    from test_nodes import _repo
    c = _repo(tmp_path)
    assert not (tmp_path / "fractal.toml").exists()
    assert _due(c, tmp_path) == {}
    (tmp_path / "DECISIONS.md").write_text("## D17 — No FMA\nchanged.\n\n## D18 — Bitwise ops\nReproduce the ops bit for bit.\nWhy: parity.\n")
    assert _due(c, tmp_path) == {}   # D18 untouched; D17 isn't a premise of any node


def test_cross_root_spec_plan_sees_its_oracle_and_unchanged_spec_is_not_stale(tmp_path):
    c, repo, docs = _two_dirs(tmp_path)
    (docs / "eq.md").write_text("---\noracle: equality\n---\n# Node\n")
    ledger.register_node(c, repo, "eq", "docs:eq.md")
    ledger.record(c, repo, "eq", file="lib.rs", span="2", mutant="m", result="killed", tests=["t"])
    p = ledger.plan(c, repo, "eq")
    assert p["node_reason"] is None and p["required"]      # oracle read from the other root


def test_double_colon_is_not_a_root_separator(tmp_path):
    repo = tmp_path / "code"
    repo.mkdir()
    _cfg(repo, '[roots]\ndocs = "."\n')
    assert resolve_path(repo, "docs::x") == repo / "docs::x"


def test_config_edits_are_seen_without_restart(tmp_path):
    repo = tmp_path / "code"
    repo.mkdir()
    _cfg(repo, '[roots]\ndocs = "a"\n')
    assert load(repo).roots == {"docs": "a"}
    _cfg(repo, '[roots]\ndocs = "bb"\n')
    assert load(repo).roots == {"docs": "bb"}


def test_broken_config_is_reported_not_mistaken_for_a_missing_spec(tmp_path, capsys):
    c, repo, docs = _two_dirs(tmp_path)
    _cfg(repo, "[roots\n")
    with pytest.raises(ConfigError, match="not valid TOML"):
        activation.due(c, repo)
    assert main(["--root", str(repo), "activations"]) != 0
    assert "not valid TOML" in capsys.readouterr().err

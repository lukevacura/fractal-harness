"""One function reads a repo-or-root file (symbols.read_ref); the remaining direct reads route through it."""
import pytest

from fractal_harness import activation, impact, ledger, motion, reclaim, regions
from fractal_harness.cache import ClaimCache
from fractal_harness.config import ConfigFileError
from fractal_harness.context import autoregister, decision_docs, unchecked_decisions
from fractal_harness.symbols import is_repo_ref, read_ref

from test_nodes import _git, _spec
from test_reclaim import LIB

SPEC = "---\nguarantee: g\nexports: lib.py::used, lib.py::Gone\n---\n# N\n"


def _two(tmp_path):
    repo, docs = tmp_path / "code", tmp_path / "docs"
    repo.mkdir(), docs.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / ".git" / "info" / "exclude").write_text(".fractal/\n")
    (repo / "fractal.toml").write_text('[roots]\ndocs = "../docs"\n')
    (repo / "lib.py").write_text(LIB)
    (repo / "main.py").write_text("import lib\nprint(lib.used(), lib.after())\n")
    (docs / "n.md").write_text(SPEC)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "c1")
    c = ClaimCache(repo)
    ledger.register_node(c, repo, "n", "docs:n.md")
    return c, repo, docs


def test_read_ref_plain_root_missing_and_broken_config(tmp_path):
    c, repo, docs = _two(tmp_path)
    assert read_ref(repo, "lib.py") == LIB and read_ref(repo, "docs:n.md") == SPEC
    assert read_ref(repo, "docs:nope.md") is None and read_ref(repo, "nope.md", "d") == "d"
    assert read_ref(repo, "zzz:n.md") is None and read_ref(repo, "docs:../code/lib.py") is None   # unknown root, escape
    assert read_ref(repo, "src") is None                                                      # a directory is not a file
    assert is_repo_ref("lib.py") and not is_repo_ref("docs:n.md") and is_repo_ref("a::b")
    (repo / "fractal.toml").write_text("[roots\n")
    with pytest.raises(ConfigFileError):
        read_ref(repo, "docs:n.md")                                                           # never a silent miss


def test_owned_files_with_a_spec_under_a_root_and_an_export_under_a_root_is_not_owned(tmp_path):
    c, repo, docs = _two(tmp_path)
    info = c.store.get_node("n")
    assert activation.owned_files(c, repo, "n", info) == ["lib.py"]
    (docs / "n.md").write_text("---\nguarantee: g\nexports: lib.py::used, docs:api.py::f\n---\n# N\n")
    assert activation.owned_files(c, repo, "n", info) == ["lib.py"]       # docs:api.py is not a file this repo owns


def test_reclaim_sweeps_a_spec_under_a_root(tmp_path):
    c, repo, docs = _two(tmp_path)
    by = {x.what: x for x in reclaim.sweep(c, repo, {"metadata", "store"})}
    assert by["lib.py::Gone"].cls == "metadata" and by["lib.py::Gone"].spec_remove[0] == "docs:n.md"
    (docs / "n.md").unlink()
    store = [x for x in reclaim.sweep(c, repo, {"store"}) if x.cls == "store"]
    assert [(x.what, x.action) for x in store] == [("n", "deregister")]       # a missing spec under a root is seen


def test_impact_with_a_spec_under_a_root_compares_code_not_spec_history(tmp_path):
    c, repo, docs = _two(tmp_path)
    r = impact.changed_exports(repo, "docs:n.md", "HEAD")
    assert r == []                                                              # no old spec in this repo's history
    (repo / "lib.py").write_text(LIB.replace("def used():", "def used(z):"))
    # the spec has no base version in this repo, but its exports (read from the root) are still compared as code
    assert [(x.ref, x.kind) for x in impact.changed_exports(repo, "docs:n.md", "HEAD")] == [("lib.py::used", "changed")]


def test_autoregister_searches_named_roots(tmp_path):
    c, repo, docs = _two(tmp_path)
    (docs / "DECISIONS.md").write_text("## D18 - Bitwise\nReproduce bits.\n")
    assert "docs:DECISIONS.md" in decision_docs(repo) and "DECISIONS.md" in decision_docs(repo)
    done, failed = autoregister(c, repo, ["D18", "D99"])
    assert done == ["D18"] and "D99" in failed and "docs:DECISIONS.md" in failed["D99"]
    assert any(e.probe and e.probe.get("ref", "").startswith("docs:DECISIONS.md") for e in c.store.all())


def test_unchecked_decisions_motion_and_regions_use_the_one_reader(tmp_path):
    c, repo, docs = _two(tmp_path)
    assert unchecked_decisions(c, repo) == []
    assert motion.line_hashes(repo, "lib.py") and motion.line_hashes(repo, "docs:n.md")      # root file snapshots work
    assert motion.line_hashes(repo, "missing.py") is None
    assert regions._lines(repo, "lib.py") == LIB.count("\n") and regions._lines(repo, "missing.py") == 0
    assert regions._lines(repo, "docs:n.md") == SPEC.count("\n")
    no_cfg = tmp_path / "plain"
    no_cfg.mkdir()
    (no_cfg / "a.py").write_text("x\ny\n")
    assert regions._lines(no_cfg, "a.py") == 2 and read_ref(no_cfg, "a.py") == "x\ny\n"        # no config: as before

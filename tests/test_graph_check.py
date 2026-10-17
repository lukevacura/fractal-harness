import subprocess
from pathlib import Path

from fractal_harness import graph_check, ledger
from fractal_harness.cache import ClaimCache


def _git(root: Path, *a: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=root, check=True, capture_output=True)


def _spec(**f) -> str:
    return "---\n" + "".join(f"{k}: {v}\n" for k, v in f.items()) + "---\n# Body\n"


def _repo(tmp_path: Path, child_code: str, **child_header):
    _git(tmp_path, "init", "-q")
    (tmp_path / ".git" / "info" / "exclude").write_text(".fractal/\n")
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "stats.py").write_text("def mean(x):\n    return x\n\ndef Orphan():\n    pass\n\ndef TestedOnly():\n    pass\n")
    (tmp_path / "src" / "grow.py").write_text(child_code)
    (tmp_path / "tests" / "test_x.py").write_text("from src.stats import TestedOnly\n")
    (tmp_path / "stats.md").write_text(_spec(guarantee="g", exports="src/stats.py::mean, src/stats.py::Orphan, "
                                                                  "src/stats.py::TestedOnly"))
    (tmp_path / "grow.md").write_text(_spec(guarantee="g", exports="src/grow.py::grow", **child_header))
    _git(tmp_path, "add", "-A")
    c = ClaimCache(tmp_path)
    for n in ("stats", "grow"):
        ledger.register_node(c, tmp_path, n, f"{n}.md")
    return c


def test_dead_exports_are_reported_and_tested_only_is_distinguished(tmp_path):
    c = _repo(tmp_path, "from src.stats import mean\ndef grow():\n    return mean(1)\n", parents="stats")
    r = graph_check.check(c, tmp_path)
    dead = {d["symbol"]: d["tested_only"] for d in r["dead"] if d["node"] == "stats"}
    assert dead == {"src/stats.py::Orphan": False, "src/stats.py::TestedOnly": True}   # mean is used by grow
    assert r["phantom"] == []


def test_phantom_edge_for_uses_but_not_for_after(tmp_path):
    code = "def grow():\n    return 1\n"
    r = graph_check.check(_repo(tmp_path, code, parents="stats"), tmp_path)
    assert [(d["node"], d["parent"]) for d in r["phantom"]] == [("grow", "stats")]
    assert "Phantom edges" in graph_check.render(r)


def test_after_edges_are_never_phantom(tmp_path):
    r = graph_check.check(_repo(tmp_path, "def grow():\n    return 1\n", after="stats"), tmp_path)
    assert r["phantom"] == []


def test_scans_public_items_no_spec_declares_and_unregistered_specs_and_stale_exports(tmp_path):
    c = _repo(tmp_path, "from src.stats import mean\ndef grow():\n    return mean(1)\n", parents="stats")
    (tmp_path / "src" / "sum.py").write_text("def canonical_sum(x):\n    return x\n\ndef _private():\n    pass\n")
    (tmp_path / "src" / "lib.rs").write_text("pub struct Pair;\npub fn used() {}\n#[cfg(test)]\nmod t { pub fn in_test() {} }\n")
    (tmp_path / "src" / "user.py").write_text("used = 1\n")
    (tmp_path / "sum.md").write_text(_spec(guarantee="g", exports="src/sum.py::canonical_sum"))     # never registered
    (tmp_path / "stats.md").write_text(_spec(guarantee="g", exports="src/stats.py::mean, src/stats.py::Gone"))
    _git(tmp_path, "add", "-A")
    ledger.register_node(c, tmp_path, "stats", "stats.md")
    r = graph_check.check(c, tmp_path)
    assert {d["symbol"] for d in r["undeclared_dead"]} >= {"src/sum.py::canonical_sum", "src/lib.rs::Pair"}
    assert "src/lib.rs::used" not in {d["symbol"] for d in r["undeclared_dead"]}      # user.py names it
    assert "src/lib.rs::in_test" not in {d["symbol"] for d in r["undeclared_dead"]}   # cfg(test) is not public API
    assert "src/sum.py::_private" not in {d["symbol"] for d in r["undeclared_dead"]}
    assert [d["spec"] for d in r["unregistered"]] == ["sum.md"]
    assert [d["symbol"] for d in r["missing"]] == ["src/stats.py::Gone"]
    text = graph_check.render(r)
    assert "not registered" in text and "no longer defines" in text and "no spec exports" in text


def test_public_surface_can_be_marked_per_node_or_repo_wide_and_strict_can_gate(tmp_path, capsys):
    from fractal_harness.cli import main
    c = _repo(tmp_path, "from src.stats import mean\ndef grow():\n    return mean(1)\n", parents="stats")
    (tmp_path / "src" / "testkit").mkdir()
    (tmp_path / "src" / "testkit" / "helpers.py").write_text("def make_fixture():\n    pass\n")
    _git(tmp_path, "add", "-A")
    r = graph_check.check(c, tmp_path)
    assert {d["symbol"] for d in r["dead"] if d["node"] == "stats"} == {"src/stats.py::Orphan", "src/stats.py::TestedOnly"}
    assert "src/testkit/helpers.py::make_fixture" in {d["symbol"] for d in r["undeclared_dead"]}
    (tmp_path / ".fractal-public").write_text("# api\nsrc/testkit/**\nOrphan\nsrc/stats.py::Tested*\ngrow\n")
    r = graph_check.check(c, tmp_path)
    assert r["dead"] == [] and not any("testkit" in d["symbol"] for d in r["undeclared_dead"])
    # a node's own `public:` field marks its surface too
    (tmp_path / ".fractal-public").unlink()
    (tmp_path / "stats.md").write_text(_spec(guarantee="g", public="src/stats.py::Orphan, TestedOnly",
                                             exports="src/stats.py::mean, src/stats.py::Orphan, src/stats.py::TestedOnly"))
    ledger.register_node(c, tmp_path, "stats", "stats.md")
    assert [d for d in graph_check.check(c, tmp_path)["dead"] if d["node"] == "stats"] == []
    # --strict is the commit-probe form: exit 1 while anything is reported
    assert main(["--root", str(tmp_path), "graph-check", "--strict"]) == 1        # testkit still undeclared
    (tmp_path / ".fractal-public").write_text("src/testkit/**\ngrow\n")
    capsys.readouterr()
    assert main(["--root", str(tmp_path), "graph-check", "--strict"]) == 0


def test_python_helpers_used_inside_their_own_module_are_not_orphans_but_rust_pub_items_are(tmp_path):
    c = _repo(tmp_path, "from src.stats import mean\ndef grow():\n    return mean(1)\n", parents="stats")
    (tmp_path / "src" / "util.py").write_text("def helper():\n    return 1\n\ndef api():\n    return helper()\n")
    (tmp_path / "src" / "lib.rs").write_text("pub fn only_here() {}\nfn go() { only_here(); }\n")
    _git(tmp_path, "add", "-A")
    syms = {d["symbol"] for d in graph_check.check(c, tmp_path)["undeclared_dead"]}
    assert "src/util.py::helper" not in syms and "src/util.py::api" in syms        # api has no caller at all
    assert "src/lib.rs::only_here" in syms                                           # `pub` promised API nobody else uses

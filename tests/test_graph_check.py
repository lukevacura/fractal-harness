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

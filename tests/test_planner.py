import json
import subprocess
import sys
from pathlib import Path

import pytest

from fractal_harness.planner import EdgeSpec, Plan, PlanError, git, merge, outside, validate

TEST_CMD = f"{sys.executable} -m pytest -q {{test}}"


def _repo(tmp_path: Path) -> Path:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "a.py").write_text("def a():\n    raise NotImplementedError\n")
    (tmp_path / "pkg" / "b.py").write_text("from pkg.a import a\n\ndef b():\n    raise NotImplementedError\n")
    (tmp_path / "tests" / "test_edge_a.py").write_text("from pkg.a import a\n\ndef test_a():\n    assert a() == 1\n")
    (tmp_path / "tests" / "test_edge_b.py").write_text(
        "import pkg.b as m\n\ndef test_b(monkeypatch):\n    monkeypatch.setattr(m, 'a', lambda: 1)\n    assert m.b() == 2\n")
    (tmp_path / "tests" / "test_integration.py").write_text("from pkg.b import b\n\ndef test_all():\n    assert b() == 2\n")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    git(tmp_path, "add", "-A")
    git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "skeleton")
    return tmp_path


RAW = {"edges": [{"id": "a", "post": "a() returns 1", "writes": ["pkg/a.py"], "test": "tests/test_edge_a.py"},
                 {"id": "b", "post": "b() returns a()+1", "writes": ["pkg/b.py"], "test": "tests/test_edge_b.py",
                  "depends_on": ["a"]}],
       "integration_test": "tests/test_integration.py"}


def test_validate_accepts_good_plan(tmp_path):
    edges, integration = validate(RAW, _repo(tmp_path))
    assert [e.id for e in edges] == ["a", "b"] and integration == "tests/test_integration.py"


@pytest.mark.parametrize("mutate,msg", [
    (lambda r: r["edges"][1]["writes"].append("pkg/a.py"), "overlap"),
    (lambda r: r["edges"][0]["writes"].append("tests/test_edge_a.py"), "test file"),
    (lambda r: r["edges"][0].__setitem__("depends_on", ["b"]), "cycle"),
    (lambda r: r["edges"][0].__setitem__("depends_on", ["zzz"]), "unknown edge"),
    (lambda r: r["edges"][0].__setitem__("test", "tests/missing.py"), "missing"),
])
def test_validate_rejects_bad_plans(tmp_path, mutate, msg):
    raw = json.loads(json.dumps(RAW))
    mutate(raw)
    with pytest.raises(PlanError, match=msg):
        validate(raw, _repo(tmp_path))


def test_outside_write_set():
    assert outside(["pkg/a.py", "tests/x.py", "pkg/sub/c.py"], ["pkg/a.py", "pkg/sub/*"]) == ["tests/x.py"]


def _patch(repo: Path, rel: str, text: str) -> str:
    original = (repo / rel).read_text()
    (repo / rel).write_text(text)
    patch = git(repo, "diff", "--binary") + "\n"
    (repo / rel).write_text(original)
    return patch


def test_merge_composes_passing_edges_and_runs_integration(tmp_path):
    repo = _repo(tmp_path)
    head = git(repo, "rev-parse", "HEAD")
    p = Plan(id="t1", task="t", root_commit=head, skeleton=head, edges=validate(RAW, repo)[0],
             integration_test="tests/test_integration.py", test_cmd=TEST_CMD)
    ra = {"edge": "a", "passed": True, "violations": [], "patch": _patch(repo, "pkg/a.py", "def a():\n    return 1\n")}
    rb = {"edge": "b", "passed": True, "violations": [],
          "patch": _patch(repo, "pkg/b.py", "from pkg.a import a\n\ndef b():\n    return a() + 1\n")}
    m = merge(repo, p, [ra, rb], "t")
    assert m["applied"] == ["a", "b"] and m["edge_tests"] == {"a": True, "b": True} and m["integration"]
    # an edge that broke its write set is not merged, and the composition fails
    rb_bad = {**rb, "violations": ["tests/test_edge_b.py"]}
    m2 = merge(repo, p, [ra, rb_bad], "t2")
    assert m2["applied"] == ["a"] and not m2["integration"]


def test_leftover_stubs_found_outside_comments(tmp_path):
    from fractal_harness.planner import leftover_stubs
    (tmp_path / "cli.py").write_text("def main():\n    # raise NotImplementedError was here\n    raise NotImplementedError\n")
    (tmp_path / "done.py").write_text("def f():\n    return 1\n")
    assert leftover_stubs(tmp_path, ["cli.py", "done.py", "missing.py"]) == ["cli.py:3"]


def test_budget_stops_agent_calls(monkeypatch, tmp_path):
    import fractal_harness.planner as pl
    calls = []

    class Proc:
        stdout = '{"total_cost_usd": 0.6, "num_turns": 1, "result": "ok"}'
        stderr = ""

    monkeypatch.setattr(pl.subprocess, "run", lambda *a, **k: calls.append(1) or Proc())
    pl.BUDGET.set(1.0)
    try:
        pl._agent(tmp_path, "p", "python -m pytest {test}", "m")
        pl._agent(tmp_path, "p", "python -m pytest {test}", "m")   # 1.2 spent: over after this one
        with pytest.raises(pl.BudgetExceeded):
            pl._agent(tmp_path, "p", "python -m pytest {test}", "m")
        assert len(calls) == 2
    finally:
        pl.BUDGET.set(None)

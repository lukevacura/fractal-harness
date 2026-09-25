from pathlib import Path

from fractal_harness.behavior import baseline, import_tests
from fractal_harness.taskmap import render, task_map

from test_behavior import FILES, repo  # noqa: F401  (fixture)


def _prep(repo: Path) -> Path:
    (repo / "pkg" / "orphan.py").write_text("def lonely():\n    return 0\n")
    import_tests(repo)
    baseline(repo)
    return repo


def test_map_finds_regions_contracts_upstream_and_downstream(repo):
    m = task_map(_prep(repo), "Make `quad` handle negative numbers", capacity=1)
    core = next(r for r in m.regions if r.path == "pkg/core.py")
    assert "defines `quad`" in core.why
    assert [e.probe["file"] for e in core.contracts] == ["tests/test_core.py"]
    assert "pkg/util.py" in core.relies_on                     # upstream assumption
    text = render(m)
    assert text.startswith("TASK MAP") and "tests/test_core.py (1 tests)" in text and "relies on: pkg/util.py" in text


def test_editing_an_upstream_file_lists_downstream_contracts(repo):
    m = task_map(_prep(repo), "Change double() in pkg/util.py to use shifts", capacity=1)
    util = next(r for r in m.regions if r.path == "pkg/util.py")
    assert util.contracts == []                                 # no test imports util directly
    assert "pkg/util.py" in m.weak
    assert [e.probe["file"] for e in m.downstream] == ["tests/test_core.py"]


def test_weak_spot_and_empty_map(repo):
    _prep(repo)
    m = task_map(repo, "Give lonely a docstring in pkg/orphan.py", capacity=1)
    assert m.weak == ["pkg/orphan.py"] and "weak spot" in render(m)
    assert task_map(repo, "update the marketing copy").empty

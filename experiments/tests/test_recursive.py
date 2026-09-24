import subprocess
from pathlib import Path

import pytest

from fractal_planner.planner import git
from fractal_planner.recursive import (DecomposeError, Node, Stub, _json_block, _leaves, validate_split,
                                       write_skeleton)

CONVERT = 'def convert(amount: Decimal, rate: Decimal) -> Decimal:\n    """Multiply."""\n    raise NotImplementedError\n'
FORMAT = 'def fmt(x: Decimal) -> str:\n    """Two decimals."""\n    raise NotImplementedError\n'


def _parent() -> Node:
    return Node("root/fx", "fx conversions", ["pkg/fx.py"], [Stub("pkg/fx.py", CONVERT, ["from decimal import Decimal"])],
                depth=1)


def _raw(**over):
    raw = {"leaf": False, "children": [
        {"id": "math", "contract": "c", "writes": ["pkg/fx.py"], "depends_on": [],
         "interface": [{"file": "pkg/fx.py", "stub": CONVERT, "imports": ["from decimal import Decimal"]}]},
        {"id": "fmt", "contract": "f", "writes": ["pkg/fmt.py"], "depends_on": ["math"],
         "interface": [{"file": "pkg/fmt.py", "stub": FORMAT}]}]}
    raw.update(over)
    return raw


def test_valid_split_keeps_endpoints():
    kids = validate_split(_parent(), _raw())
    assert [k.path for k in kids] == ["root/fx/math", "root/fx/fmt"]
    assert kids[1].depends_on == ["root/fx/math"] and kids[0].depth == 2


def test_split_must_provide_parent_interface():
    raw = _raw()
    raw["children"][0]["interface"] = []
    with pytest.raises(DecomposeError, match="convert not provided"):
        validate_split(_parent(), raw)


@pytest.mark.parametrize("mutate,msg", [
    (lambda r: r["children"][1]["writes"].append("pkg/fx.py"), "overlap"),
    (lambda r: r["children"][1]["writes"].append("tests/test_x.py"), "test file"),
    (lambda r: r["children"][1]["writes"].append("other/place.py"), "outside parent"),
    (lambda r: r["children"][0].__setitem__("depends_on", ["fmt"]), "cycle"),
    (lambda r: r["children"].pop(), "at least 2"),
    (lambda r: r["children"][1]["interface"][0].__setitem__("stub", "def broken(:"), "not valid Python"),
    (lambda r: r["children"][1]["interface"][0].__setitem__("file", "pkg/fx.py"), "not in its write set"),
])
def test_invalid_splits_rejected(mutate, msg):
    raw = _raw()
    mutate(raw)
    with pytest.raises(DecomposeError, match=msg):
        validate_split(_parent(), raw)


def test_json_block_extraction():
    assert _json_block('thinking...\n```json\n{"leaf": true}\n```\ndone') == {"leaf": True}
    with pytest.raises(DecomposeError):
        _json_block("no json here")


def test_write_skeleton_creates_and_appends_stubs(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "fx.py").write_text("from __future__ import annotations\n\nRATE = 1\n")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    git(tmp_path, "add", "-A")
    git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "base")
    tree = Node("root", "task", ["**"])
    tree.children = validate_split(Node("root", "task", ["**"]), _raw())
    for k in tree.children:
        k.leaf = True
    sha = write_skeleton(tmp_path, tree, git(tmp_path, "rev-parse", "HEAD"))
    fx = git(tmp_path, "show", f"{sha}:pkg/fx.py")
    fmt = git(tmp_path, "show", f"{sha}:pkg/fmt.py")
    assert fx.startswith("from __future__ import annotations\n\nRATE = 1") and "from decimal import Decimal" in fx
    assert "def convert(" in fx and fmt.startswith("from __future__ import annotations") and "def fmt(" in fmt
    compile(fx, "fx.py", "exec") and compile(fmt, "fmt.py", "exec")
    assert len(_leaves(tree)) == 2


def _wide(n: int) -> dict:
    kids = [{"id": "math", "contract": "c", "writes": ["pkg/fx.py"],
             "interface": [{"file": "pkg/fx.py", "stub": CONVERT}]}]
    kids += [{"id": f"k{i}", "contract": "c", "writes": [f"pkg/k{i}.py"]} for i in range(n - 1)]
    return {"leaf": False, "children": kids}


def test_width_is_the_planners_call_with_optional_cost_limit():
    assert len(validate_split(_parent(), _wide(7))) == 7
    with pytest.raises(DecomposeError, match="cost limit"):
        validate_split(_parent(), _wide(7), max_children=5)


def test_rejected_split_is_retried_with_the_error(monkeypatch, tmp_path):
    import fractal_planner.recursive as r
    replies = iter(['```json\n{"leaf": false, "children": []}\n```',
                    '```json\n' + __import__("json").dumps(_raw()) + '\n```'])
    prompts = []

    def fake_agent(cwd, prompt, *a, **k):
        prompts.append(prompt)
        return {"result": next(replies), "cost_usd": 0.01}

    monkeypatch.setattr(r, "_agent", fake_agent)
    out = r.decompose(tmp_path, tmp_path, _parent(), "task", [], "x {test}", "m", max_depth=3, leaf_lines=150)
    assert not out["leaf"] and len(out["children"]) == 2 and len(out["runs"]) == 2
    assert "REJECTED" in prompts[1] and "at least 2" in prompts[1]


def test_two_rejections_fall_back_to_leaf(monkeypatch, tmp_path):
    import fractal_planner.recursive as r
    monkeypatch.setattr(r, "_agent", lambda *a, **k: {"result": "no json", "cost_usd": 0.01})
    out = r.decompose(tmp_path, tmp_path, _parent(), "task", [], "x {test}", "m", max_depth=3, leaf_lines=150)
    assert out["leaf"] and out["error"] and len(out["runs"]) == 2


def test_signature_mismatches_and_import_errors(tmp_path):
    import sys
    from fractal_planner.recursive import import_errors, signature_mismatches
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    stub = Stub("pkg/fx.py", CONVERT)
    (tmp_path / "pkg" / "fx.py").write_text(
        "from decimal import Decimal\n\ndef convert(amount: Decimal, rate: Decimal) -> Decimal:\n    return amount * rate\n")
    assert signature_mismatches(tmp_path, [stub]) == []
    (tmp_path / "pkg" / "fx.py").write_text("def convert(amount, rate, extra=1):\n    return 1\n")
    assert signature_mismatches(tmp_path, [stub]) == ["pkg/fx.py: convert signature changed"]
    cmd = f"{sys.executable} -m pytest -q {{test}}"
    assert import_errors(tmp_path, cmd, ["pkg/fx.py"]) == []
    (tmp_path / "pkg" / "fx.py").write_text("import nonexistent_module_xyz\n")
    assert import_errors(tmp_path, cmd, ["pkg/fx.py"])[0].startswith("pkg.fx:")


def test_checked_nodes_are_internal_nodes_and_dependency_free_leaves():
    from fractal_planner.recursive import _checked_nodes
    tree = Node("root", "task", ["**"])
    tree.children = validate_split(Node("root", "task", ["**"]), _raw())
    for k in tree.children:
        k.leaf = True
    assert [n.path for n in _checked_nodes(tree)] == ["root", "root/math"]   # fmt depends on math


def test_corroborated_cuts():
    from fractal_planner.recursive import corroborated_cuts
    # a lone failing check (the root's) is not retried: it may be the test's fault
    assert corroborated_cuts(["root"], baseline_ok=True) == []
    # root + a child failing corroborate each other; retry at the deepest node
    assert corroborated_cuts(["root", "root/rules"], baseline_ok=True) == ["root/rules"]
    assert corroborated_cuts(["root/import", "root/import/json", "root/recurring"], True) == ["root/import/json"]
    # a broken original suite corroborates every failing check
    assert corroborated_cuts(["root/recurring"], baseline_ok=False) == ["root/recurring"]

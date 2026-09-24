import json
from pathlib import Path

from fractal_harness.cache import ClaimCache
from fractal_harness.hook import edit_hook

ABSENT = {"type": "grep", "pattern": r"^import legacy\b", "paths": ["src/**/*.py"], "expect": "absent"}


def _repo(tmp_path: Path, enforce: bool = True) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("import os\n")
    (tmp_path / "docs.md").write_text("notes\n")
    c = ClaimCache(tmp_path)
    r = c.put("src never imports legacy", ["src/a.py"], kind="invariant", probe=ABSENT)
    if enforce:
        c.set_rule(r.id, "enforced")
    c.close()
    return tmp_path


def _edit(root: Path, rel: str, tool: str = "Edit") -> tuple[int, str]:
    return edit_hook(json.dumps({"tool_name": tool, "cwd": str(root),
                                 "tool_input": {"file_path": str(root / rel)}}))


def test_violation_blocks_with_offending_line(tmp_path):
    root = _repo(tmp_path)
    (root / "src" / "b.py").write_text("x = 1\nimport legacy\n")      # a new file the glob covers
    code, msg = _edit(root, "src/b.py", "Write")
    assert code == 2
    assert "src never imports legacy" in msg and "src/b.py:2: import legacy" in msg
    assert "human-owned" in msg


def test_clean_edit_and_unrelated_file_pass(tmp_path):
    root = _repo(tmp_path)
    assert _edit(root, "src/a.py") == (0, "")
    (root / "src" / "b.py").write_text("import legacy\n")
    assert _edit(root, "docs.md") == (0, "")            # the rule's probe doesn't scan docs.md


def test_proposed_rules_do_not_block(tmp_path):
    root = _repo(tmp_path, enforce=False)
    (root / "src" / "a.py").write_text("import legacy\n")
    assert _edit(root, "src/a.py") == (0, "")


def test_ignores_other_tools_outside_repo_and_disabled(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    (root / "src" / "a.py").write_text("import legacy\n")
    assert edit_hook(json.dumps({"tool_name": "Read", "cwd": str(root),
                                 "tool_input": {"file_path": str(root / "src/a.py")}})) == (0, "")
    assert edit_hook(json.dumps({"tool_name": "Edit", "cwd": str(root),
                                 "tool_input": {"file_path": "/elsewhere/x.py"}})) == (0, "")
    monkeypatch.setenv("FRACTAL_NO_HOOKS", "1")
    assert _edit(root, "src/a.py") == (0, "")


def test_fixing_the_violation_unblocks(tmp_path):
    root = _repo(tmp_path)
    (root / "src" / "a.py").write_text("import legacy\n")
    assert _edit(root, "src/a.py")[0] == 2
    (root / "src" / "a.py").write_text("import os\n")
    assert _edit(root, "src/a.py") == (0, "")
    c = ClaimCache(root)
    assert c.invariants(("enforced",))[0].status == "verified"
    c.close()


def test_never_raises():
    assert edit_hook("not json")[0] == 0

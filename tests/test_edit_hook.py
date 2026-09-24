import json
from pathlib import Path

from fractal_harness.cache import ClaimCache
from fractal_harness.hook import edit_hook

ABSENT = {"type": "grep", "pattern": r"^import legacy\b", "paths": ["src/**/*.py"], "expect": "absent"}


def _repo(tmp_path: Path, enforce: bool = True) -> Path:
    (tmp_path / "src").mkdir(parents=True)
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


# --- before the edit: checked in memory, never written -------------------------------------

from fractal_harness.hook import pre_edit_hook, proposed_content


def _pre(root: Path, rel: str, tool: str, **inp) -> tuple[int, str]:
    return pre_edit_hook(json.dumps({"tool_name": tool, "cwd": str(root),
                                     "tool_input": {"file_path": str(root / rel), **inp}}))


def test_pre_edit_blocks_violating_write_of_a_new_file(tmp_path):
    root = _repo(tmp_path)
    code, msg = _pre(root, "src/b.py", "Write", content="x = 1\nimport legacy\n")
    assert code == 2 and "NOT applied" in msg and "src/b.py:2: import legacy" in msg
    assert not (root / "src" / "b.py").exists()


def test_pre_edit_blocks_violating_edit_and_allows_clean_one(tmp_path):
    root = _repo(tmp_path)
    assert _pre(root, "src/a.py", "Edit", old_string="import os", new_string="import legacy")[0] == 2
    assert _pre(root, "src/a.py", "Edit", old_string="import os", new_string="import sys") == (0, "")
    assert (root / "src" / "a.py").read_text() == "import os\n"      # nothing was written


def test_pre_edit_multiedit_and_unknown_old_string(tmp_path):
    root = _repo(tmp_path)
    edits = [{"old_string": "import os", "new_string": "import os\nimport json"},
             {"old_string": "import json", "new_string": "import legacy"}]
    assert _pre(root, "src/a.py", "MultiEdit", edits=edits)[0] == 2
    assert _pre(root, "src/a.py", "Edit", old_string="not there", new_string="import legacy") == (0, "")


def test_pre_edit_ignores_proposed_rules_and_other_files(tmp_path):
    root = _repo(tmp_path, enforce=False)
    assert _pre(root, "src/a.py", "Write", content="import legacy\n") == (0, "")
    root2 = _repo(tmp_path / "second")
    assert _pre(root2, "docs.md", "Write", content="import legacy\n") == (0, "")


def test_proposed_content(tmp_path):
    (tmp_path / "f.py").write_text("a a b\n")
    assert proposed_content(tmp_path, "f.py", "Edit", {"old_string": "a", "new_string": "c"}) == "c a b\n"
    assert proposed_content(tmp_path, "f.py", "Edit", {"old_string": "a", "new_string": "c",
                                                        "replace_all": True}) == "c c b\n"
    assert proposed_content(tmp_path, "new.py", "Write", {"content": "x"}) == "x"
    assert proposed_content(tmp_path, "missing.py", "Edit", {"old_string": "a", "new_string": "b"}) is None

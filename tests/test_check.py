import subprocess
from pathlib import Path

from fractal_harness.cache import ClaimCache
from fractal_harness.check import check
from fractal_harness.setup import init


def _repo(tmp_path: Path) -> ClaimCache:
    (tmp_path / "app.py").write_text("import db\nPOOL = 10\n")
    c = ClaimCache(tmp_path)
    c.put("app never imports legacy", ["*.py"], kind="invariant",
          probe={"type": "grep", "pattern": r"^import legacy\b", "paths": ["*.py"], "expect": "absent"})
    c.put("POOL is 10", ["app.py"], probe={"type": "grep", "pattern": r"^POOL = 10$", "paths": ["app.py"]})
    return c


def test_no_store_passes(tmp_path):
    assert check(tmp_path) == (0, "")


def test_clean_repo_passes(tmp_path):
    _repo(tmp_path).close()
    assert check(tmp_path)[0] == 0


def test_violated_invariant_fails(tmp_path):
    _repo(tmp_path).close()
    (tmp_path / "app.py").write_text("import db\nimport legacy\nPOOL = 10\n")
    code, report = check(tmp_path)
    assert code == 1 and "VIOLATION" in report and "never imports legacy" in report


def test_broken_knowledge_claim_warns_unless_strict(tmp_path):
    _repo(tmp_path).close()
    (tmp_path / "app.py").write_text("import db\nPOOL = 12\n")
    code, report = check(tmp_path)
    assert code == 0 and "need repair" in report
    assert check(tmp_path, strict=True)[0] == 1


def test_git_hook_installed_once_and_never_clobbers(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    changes = init(tmp_path, settings=False, git_hook=True)
    hook = tmp_path / ".git" / "hooks" / "pre-commit"
    assert any("git hook" in c for c in changes) and "fractal check" in hook.read_text()
    assert not any("git hook" in c for c in init(tmp_path, settings=False, git_hook=True))
    hook.write_text("#!/bin/sh\necho mine\n")
    assert any("skipped" in c for c in init(tmp_path, settings=False, git_hook=True))
    assert hook.read_text() == "#!/bin/sh\necho mine\n"


def test_edit_to_any_scanned_file_is_seen(tmp_path):
    (tmp_path / "app.py").write_text("import db\n")
    (tmp_path / "other.py").write_text("x = 1\n")
    c = ClaimCache(tmp_path)
    # declared reads name only app.py, but the probe scans every *.py file
    c.put("nothing imports legacy", ["app.py"], kind="invariant",
          probe={"type": "grep", "pattern": r"^import legacy\b", "paths": ["*.py"], "expect": "absent"})
    c.close()
    (tmp_path / "other.py").write_text("import legacy\n")
    code, report = check(tmp_path)
    assert code == 1 and "VIOLATION" in report


def test_fixing_the_code_clears_a_violation(tmp_path):
    _repo(tmp_path).close()
    (tmp_path / "app.py").write_text("import db\nimport legacy\nPOOL = 10\n")
    assert check(tmp_path)[0] == 1
    (tmp_path / "app.py").write_text("import db\nPOOL = 10\n")
    assert check(tmp_path) == (0, "")

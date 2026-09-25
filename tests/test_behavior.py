import json
import subprocess
from pathlib import Path

import pytest

from fractal_harness.behavior import (affected, baseline, closure, dart_imports, detect_runner, gate,
                                      import_tests, python_imports)
from fractal_harness.cache import ClaimCache
from fractal_harness.hook import stop_hook

FILES = {
    "pyproject.toml": "[project]\nname='demo'\n",
    "pkg/__init__.py": "",
    "pkg/util.py": "def double(x):\n    return 2 * x\n",
    "pkg/core.py": "from pkg.util import double\n\ndef quad(x):\n    return double(double(x))\n",
    "pkg/other.py": "def one():\n    return 1\n",
    "tests/test_core.py": "from pkg.core import quad\n\ndef test_quad():\n    assert quad(2) == 8\n",
    "tests/test_other.py": "from pkg import other\n\ndef test_one():\n    assert other.one() == 1\n",
}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    for rel, text in FILES.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base"], cwd=tmp_path, check=True)
    return tmp_path


def test_python_import_graph(repo):
    files = set(FILES)
    assert python_imports(repo, "tests/test_core.py", files) == {"pkg/core.py"}
    assert closure(repo, "tests/test_core.py", files, "python") == {"pkg/core.py", "pkg/util.py"}
    assert "pkg/other.py" in closure(repo, "tests/test_other.py", files, "python")
    assert detect_runner(repo) == "pytest"


def test_dart_import_graph(tmp_path):
    (tmp_path / "pubspec.yaml").write_text("name: q\ndependencies:\n  flutter:\n    sdk: flutter\n")
    files = {"lib/a.dart": "import 'package:q/b.dart';\nimport 'package:flutter/material.dart';\nimport 'dart:io';\n",
             "lib/b.dart": "export 'sub/c.dart';\npart 'b_part.dart';\n",
             "lib/sub/c.dart": "import '../a.dart';\n", "lib/b_part.dart": "",
             "test/a_test.dart": "import 'package:q/a.dart';\n"}
    for rel, text in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)
    assert dart_imports(tmp_path, "lib/a.dart", "q", set(files)) == {"lib/b.dart"}
    assert closure(tmp_path, "test/a_test.dart", set(files), "dart", "q") == {
        "lib/a.dart", "lib/b.dart", "lib/sub/c.dart", "lib/b_part.dart"}
    assert detect_runner(tmp_path) == "flutter"


def test_import_baseline_and_impact(repo):
    r = import_tests(repo)
    assert {k: r[k] for k in ("runner", "tests", "created", "updated", "vacuous")} == {
        "runner": "pytest", "tests": 2, "created": 2, "updated": 0, "vacuous": 0}
    c = ClaimCache(repo)
    core = c.get_by_probe_file("tests/test_core.py")
    assert set(core.reads) >= {"tests/test_core.py", "pkg/core.py", "pkg/util.py"}
    assert core.status == "stale" and "not yet checked" in core.detail          # deferred, nothing ran
    assert [e.probe["file"] for e in affected(c, ["pkg/util.py"])] == ["tests/test_core.py"]
    c.close()
    assert baseline(repo) == {"checked": 2, "passed": 2, "failed": 0, "seconds": pytest.approx(0, abs=60)}
    assert import_tests(repo)["updated"] == 2                                  # re-import keeps verdicts
    c = ClaimCache(repo)
    assert c.get_by_probe_file("tests/test_core.py").status == "verified"
    c.close()


def test_gate_detects_regression_only_in_affected_tests(repo):
    import_tests(repo)
    baseline(repo)
    (repo / "pkg" / "util.py").write_text("def double(x):\n    return 3 * x\n")
    r = gate(repo)
    assert r["checked"] == 1 and [e.probe["file"] for e in r["regressions"]] == ["tests/test_core.py"]
    (repo / "pkg" / "util.py").write_text(FILES["pkg/util.py"])
    assert gate(repo)["regressions"] == []


def test_already_failing_tests_do_not_block(repo):
    (repo / "tests" / "test_other.py").write_text("def test_one():\n    assert False\n")
    import_tests(repo)
    baseline(repo)
    (repo / "pkg" / "other.py").write_text("def one():\n    return 1  # touched\n")
    r = gate(repo)
    assert r["regressions"] == [] and len(r["still_failing"]) == 1


def test_stop_hook_blocks_regressions_with_loop_guard(repo):
    import_tests(repo)
    baseline(repo)
    (repo / "pkg" / "util.py").write_text("def double(x):\n    return 3 * x\n")
    payload = json.dumps({"cwd": str(repo), "session_id": "s1"})
    for _ in range(2):
        code, msg = stop_hook(payload)
        assert code == 2 and "REGRESSION" in msg and "tests/test_core.py" in msg
    code, msg = stop_hook(payload)                 # third time in the same session: report, don't trap
    assert code == 0 and "Not blocking" in msg
    (repo / "pkg" / "util.py").write_text(FILES["pkg/util.py"])
    assert stop_hook(json.dumps({"cwd": str(repo), "session_id": "s2"})) == (0, "")


def test_hooks_never_run_slow_probes(repo):
    from fractal_harness.hook import prompt_hook
    import_tests(repo)
    baseline(repo)
    (repo / "pkg" / "util.py").write_text("def double(x):\n    return 3 * x\n")
    prompt_hook(json.dumps({"prompt": "what does quad do with double", "cwd": str(repo)}))
    c = ClaimCache(repo)
    assert c.get_by_probe_file("tests/test_core.py").status in ("stale", "verified")   # never re-run by a hook
    c.close()


def test_regression_is_judged_against_baseline_not_latest_run(repo):
    import_tests(repo)
    baseline(repo)
    (repo / "pkg" / "util.py").write_text("def double(x):\n    return 3 * x\n")
    assert len(gate(repo)["regressions"]) == 1
    assert len(gate(repo)["regressions"]) == 1        # still a regression on the second run


def test_passing_commit_gate_promotes_baseline(repo):
    from fractal_harness.check import check
    (repo / "tests" / "test_other.py").write_text("def test_one():\n    assert False\n")
    import_tests(repo)
    baseline(repo)                                    # test_other fails at baseline
    (repo / "tests" / "test_other.py").write_text(FILES["tests/test_other.py"] + "# fixed\n")
    assert check(repo)[0] == 0                        # fixed: passes, commit allowed, baseline promoted
    (repo / "pkg" / "other.py").write_text("def one():\n    return 2\n")
    assert [e.probe["file"] for e in gate(repo)["regressions"]] == ["tests/test_other.py"]


def test_source_scanning_tests_depend_on_what_they_read(tmp_path):
    from fractal_harness.behavior import scanned_paths
    files = {"lib/a.dart", "lib/sub/b.dart", "lib/models/q.dart", "test/scan_test.dart"}
    for f in files:
        (tmp_path / f).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / f).write_text("")
    (tmp_path / "test/scan_test.dart").write_text(
        "final src = File('lib/models/q.dart');\nfor (final p in ['', '../']) {}\nDirectory('lib/sub').listSync();\n")
    assert scanned_paths(tmp_path, "test/scan_test.dart", files) == {"lib/models/q.dart", "lib/sub/b.dart"}

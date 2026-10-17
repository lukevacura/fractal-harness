import json
import subprocess
from pathlib import Path

import pytest

from fractal_harness import ledger, mutate
from fractal_harness.cache import ClaimCache
from fractal_harness.cli import main
from fractal_harness.symbols import SymbolError

CALC = "def add(a, b):\n    # sum\n    return a + b\n\n\ndef neg(a):\n    return -a\n"
CHECK = "import sys, calc\nassert calc.add(1, 2) == 3, 'add'\nassert calc.neg(2) == -2, 'neg'\n"


def git(cwd, *a):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=cwd, check=True, capture_output=True)


def _repo(tmp_path: Path):
    git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / ".git" / "info" / "exclude").write_text(".fractal/\n")
    (tmp_path / "calc.py").write_text(CALC)
    (tmp_path / "check.py").write_text(CHECK)
    (tmp_path / "spec.md").write_text("# N\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "c1")
    c = ClaimCache(tmp_path)
    ledger.register_node(c, tmp_path, "n", "spec.md")
    return c


CMD = "python3 check.py {tests}"
BUILD = "python3 -m py_compile calc.py"


def _run(c, root, items, **kw):
    return mutate.run(c, root, "n", items, cmd=CMD, build_cmd=BUILD, workers=2, **kw)


ITEMS = [
    {"file": "calc.py", "span": "3", "tests": ["add"], "find": "+", "replace": "-"},                    # killed
    {"file": "calc.py", "span": "2", "tests": ["add"], "replacement": ""},                               # a comment: survives
    {"file": "calc.py", "span": "7", "tests": ["neg"], "find": "-a", "replace": "a +"},                  # does not build
]


def test_mutants_are_killed_survive_or_are_invalid_and_the_tree_is_untouched(tmp_path):
    c = _repo(tmp_path)
    r = _run(c, tmp_path, ITEMS)
    assert [x["verdict"] for x in r["rows"]] == ["killed", "survived", "invalid"]
    assert (tmp_path / "calc.py").read_text() == CALC                                   # never mutated in place
    assert subprocess.run(["git", "worktree", "list"], cwd=tmp_path, capture_output=True, text=True).stdout.count("\n") == 1
    rows = {m["mutant"]: m["result"] for m in c.store.mutants("n")}
    assert rows == {"+ -> -": "killed", "delete lines": "survived"}                    # invalid ones are not recorded
    assert c.store.mutants("n")[0]["tests"] == ["add"]
    assert ledger.plan(c, tmp_path, "n")["open"][0]["mutant"] == "delete lines"          # a survivor stays unresolved
    text = mutate.render(r)
    assert "1 killed, 1 survived, 1 invalid" in text and "Survivors" in text


def test_second_run_is_answered_from_the_memo(tmp_path):
    c = _repo(tmp_path)
    first = _run(c, tmp_path, ITEMS)
    again = _run(c, tmp_path, ITEMS)
    assert not any(x["cached"] for x in first["rows"]) and all(x["cached"] for x in again["rows"])
    fresh = _run(c, tmp_path, ITEMS, use_memo=False)
    assert not any(x["cached"] for x in fresh["rows"])
    # a different commit has a different tree, so nothing carries over
    (tmp_path / "other.txt").write_text("x\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "c2")
    assert not any(x["cached"] for x in _run(c, tmp_path, ITEMS)["rows"])


def test_nothing_is_recorded_when_the_tests_fail_on_the_unmutated_commit(tmp_path):
    c = _repo(tmp_path)
    (tmp_path / "check.py").write_text(CHECK.replace("== 3", "== 4"))
    git(tmp_path, "commit", "-qam", "break")
    with pytest.raises(SymbolError, match="do not pass on the unmutated commit"):
        _run(c, tmp_path, ITEMS)
    assert c.store.mutants("n") == []


def test_validation_and_no_record():
    for bad in ([{"file": "a", "span": "x", "tests": ["t"], "find": "a", "replace": "b"}],
                [{"file": "a", "span": "1", "tests": ["t"]}],
                [{"file": "a", "span": "1", "find": "a", "replace": "b"}],
                [{"file": "a", "span": "1", "tests": ["t"], "find": "a", "replacement": "b"}]):
        with pytest.raises(SymbolError):
            mutate.parse(bad)
    m = mutate.parse([{"file": "a", "span": "2-3", "tests": "t1, t2", "find": "x", "replace": "y"}])[0]
    assert m.tests == ["t1", "t2"] and mutate.apply("a\nx\nx\nb\n", m) == "a\ny\nx\nb\n"
    with pytest.raises(SymbolError, match="does not occur"):
        mutate.apply("a\nb\nc\n", m)


def test_cli_mutate(tmp_path, capsys):
    c = _repo(tmp_path)
    c.close()
    f = tmp_path / "m.json"
    f.write_text(json.dumps(ITEMS[:2]))
    base = ["--root", str(tmp_path), "mutate", "n", "--json", str(f), "--cmd", CMD, "--build-cmd", BUILD, "--no-record"]
    assert main(base) == 0
    out = capsys.readouterr().out
    assert "KILLED" in out and "SURVIVED" in out and "Nothing recorded" in out
    assert main(["--root", str(tmp_path), "mutate", "n", "--json", str(f)]) == 1
    assert "give the test command" in capsys.readouterr().err


def test_timeouts_are_reported_not_recorded_and_not_memoized(tmp_path):
    c = _repo(tmp_path)
    from fractal_harness.executor import Job, run_all
    sha = ledger.resolve_commit(tmp_path)
    out = run_all(c.store, tmp_path, sha, [Job({}, "sleep 30", timeout=1)], workers=1)[0]
    assert out.status == "timeout" and not out.cached
    assert not run_all(c.store, tmp_path, sha, [Job({}, "sleep 30", timeout=1)], workers=1)[0].cached
    assert subprocess.run(["git", "worktree", "list"], cwd=tmp_path, capture_output=True, text=True).stdout.count("\n") == 1


def test_the_memo_key_includes_the_command_the_edits_and_the_environment(tmp_path):
    from fractal_harness.executor import Job, memo_key
    k = memo_key("tree", Job({"a.py": "x"}, "pytest -q", "", {"E": "1"}))
    assert k == memo_key("tree", Job({"a.py": "x"}, "pytest -q", "", {"E": "1"}))
    for other in (Job({"a.py": "x"}, "pytest -x", "", {"E": "1"}), Job({"a.py": "y"}, "pytest -q", "", {"E": "1"}),
                  Job({"a.py": "x"}, "pytest -q", "make", {"E": "1"}), Job({"a.py": "x"}, "pytest -q", "", {"E": "2"})):
        assert memo_key("tree", other) != k
    assert memo_key("other-tree", Job({"a.py": "x"}, "pytest -q", "", {"E": "1"})) != k
    c = _repo(tmp_path)
    sha = ledger.resolve_commit(tmp_path)
    from fractal_harness.executor import run_all
    assert not run_all(c.store, tmp_path, sha, [Job({}, "true")])[0].cached
    assert run_all(c.store, tmp_path, sha, [Job({}, "true")])[0].cached
    assert run_all(c.store, tmp_path, sha, [Job({}, "false")])[0].status == "failed"       # not answered by the "true" run

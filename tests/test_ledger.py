import subprocess
from pathlib import Path

import pytest

from fractal_harness import ledger
from fractal_harness.cache import ClaimCache
from fractal_harness.context import put_extracted
from fractal_harness.symbols import SymbolError

LIB = "".join(f"line {i}\n" for i in range(1, 21))
TESTS = "fn test_a() {\n  one();\n}\nfn test_b() {\n  two();\n}\n"
DECISIONS = "## D17 — No FMA\nFused multiply-add changes rounding.\n"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=root,
                          capture_output=True, text=True, check=True).stdout.strip()


def _commit(root: Path, msg: str) -> str:
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", msg)
    return _git(root, "rev-parse", "HEAD")


def _repo(tmp_path: Path):
    _git(tmp_path, "init", "-q")
    (tmp_path / "lib.rs").write_text(LIB)
    (tmp_path / "tests.rs").write_text(TESTS)
    (tmp_path / "DECISIONS.md").write_text(DECISIONS)
    (tmp_path / "spec.md").write_text("# Node\nImplements D17.\n")
    c = ClaimCache(tmp_path)
    put_extracted(c, tmp_path, section="DECISIONS.md::D17")
    ledger.register_node(c, tmp_path, "n1", "spec.md")
    _commit(tmp_path, "c1")
    for file, span, mutant, result, tests, arg in (
            ("lib.rs", "3-4", "flip comparison", "killed", ["test_a"], ""),
            ("lib.rs", "10-12", "drop branch", "killed", ["test_b"], ""),
            ("lib.rs", "15", "swap operands", "equivalent", [], "operands commute"),
            ("lib.rs", "18", "remove check", "survived", [], "")):
        ledger.record(c, tmp_path, "n1", file=file, span=span, mutant=mutant, result=result, tests=tests,
                      argument=arg)
    return c


def _plan(c: ClaimCache, root: Path) -> dict:
    p = ledger.plan(c, root, "n1")
    return {"stale": {r["mutant"]: why for r, why in p["stale"]}, "valid": {r["mutant"] for r in p["valid"]}, "p": p}


def test_unchanged_everything_holds(tmp_path):
    c = _repo(tmp_path)
    r = _plan(c, tmp_path)
    assert r["stale"] == {} and len(r["valid"]) == 4
    assert [m["mutant"] for m in r["p"]["open"]] == ["remove check"]


def test_edit_inside_a_span_stales_only_that_mutant(tmp_path):
    c = _repo(tmp_path)
    (tmp_path / "lib.rs").write_text(LIB.replace("line 3\n", "line three\n"))
    _commit(tmp_path, "amend")
    r = _plan(c, tmp_path)
    assert list(r["stale"]) == ["flip comparison"] and "lines 3-4 of lib.rs changed" in r["stale"]["flip comparison"]
    assert {"drop branch", "swap operands", "remove check"} <= r["valid"]
    out = ledger.render(r["p"], root=tmp_path)
    assert "Re-run these mutants" in out and "-line 3" in out and "+line three" in out


def test_insertion_above_does_not_stale_but_inside_does(tmp_path):
    c = _repo(tmp_path)
    (tmp_path / "lib.rs").write_text(LIB.replace("line 5\n", "line 5\nnew\n"))
    assert _plan(c, tmp_path)["stale"] == {}
    (tmp_path / "lib.rs").write_text(LIB.replace("line 10\n", "line 10\nnew\n"))
    assert list(_plan(c, tmp_path)["stale"]) == ["drop branch"]


def test_changed_killing_test_stales_its_mutants_and_shows_the_test_diff(tmp_path):
    c = _repo(tmp_path)
    (tmp_path / "tests.rs").write_text(TESTS.replace("two()", "three()"))
    r = _plan(c, tmp_path)
    assert "killing test test_b is in changed file tests.rs" in r["stale"]["drop branch"]
    assert "flip comparison" in r["stale"]   # test_a lives in the same changed file: conservative
    assert "three()" in ledger.render(r["p"], root=tmp_path)


def test_changed_spec_or_decision_stales_everything(tmp_path):
    c = _repo(tmp_path)
    (tmp_path / "spec.md").write_text("# Node\nImplements D17, differently.\n")
    r = _plan(c, tmp_path)
    assert len(r["stale"]) == 4 and "spec spec.md changed" in r["stale"]["drop branch"]
    assert "Everything is stale" in ledger.render(r["p"], root=tmp_path)


def test_changed_decision_stales_every_citing_node(tmp_path):
    c = _repo(tmp_path)
    (tmp_path / "DECISIONS.md").write_text("## D17 — No FMA\nFMA is fine in cold paths.\n")
    put_extracted(c, tmp_path, section="DECISIONS.md::D17")   # re-assert: the old claim is replaced
    r = _plan(c, tmp_path)
    assert len(r["stale"]) == 4 and "decision claim" in r["stale"]["drop branch"]


def test_unknown_commit_is_stale_and_arguments_are_opt_in(tmp_path):
    c = _repo(tmp_path)
    c.store.db.execute("UPDATE mutants SET commit_sha = ? WHERE mutant = 'drop branch'", ("0" * 40,))
    c.store.db.commit()
    r = _plan(c, tmp_path)
    assert list(r["stale"]) == ["drop branch"] and "unavailable" in r["stale"]["drop branch"]
    assert "operands commute" not in ledger.render(r["p"], root=tmp_path)
    assert "operands commute" in ledger.render(r["p"], with_arguments=True, root=tmp_path)


def test_record_validation(tmp_path):
    c = _repo(tmp_path)
    for bad in (dict(result="killed", tests=[]), dict(result="equivalent", argument=""),
                dict(result="weird"), dict(result="survived", span="x")):
        kw = dict(file="lib.rs", span="1-2", mutant="m", argument="") | bad
        with pytest.raises(SymbolError):
            ledger.record(c, tmp_path, "n1", **kw)


def test_rerecord_replaces_and_rebases(tmp_path):
    c = _repo(tmp_path)
    (tmp_path / "lib.rs").write_text(LIB.replace("line 3\n", "line three\n"))
    new = _commit(tmp_path, "amend")
    ledger.record(c, tmp_path, "n1", file="lib.rs", span="3-4", mutant="flip comparison", result="killed",
                  tests=["test_a"])
    r = _plan(c, tmp_path)
    assert r["stale"] == {} and r["p"]["base"] == new


def test_cli_roundtrip(tmp_path, capsys, monkeypatch):
    from fractal_harness.cli import main
    _git(tmp_path, "init", "-q")
    (tmp_path / "lib.rs").write_text(LIB)
    _commit(tmp_path, "c1")
    f = tmp_path / "v.json"
    f.write_text('[{"file":"lib.rs","span":"3-4","mutant":"m1","result":"killed","tests":"t1,t2"}]')
    assert main(["--root", str(tmp_path), "ledger", "record", "n", "--json", str(f)]) == 0
    assert main(["--root", str(tmp_path), "verify-plan", "n"]) == 0
    assert "1 of 1 recorded verdicts still hold" in capsys.readouterr().out

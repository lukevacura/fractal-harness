import subprocess
from pathlib import Path

from fractal_harness import impact, ledger
from fractal_harness.cache import ClaimCache
from fractal_harness.cli import main

ROWS = "pub fn row_sum(x: &[f64]) -> f64 {\n    x.iter().sum()\n}\n\npub fn keep() {}\n"
SPEC = "---\nguarantee: Sums a row.\nexports: src/rows.rs::row_sum, src/rows.rs::keep\n---\n# Rows\n"


def git(cwd, *a):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=cwd, check=True, capture_output=True)


def _repos(tmp_path: Path):
    m = tmp_path / "main"
    m.mkdir()
    git(m, "init", "-q", "-b", "main")
    (m / ".git" / "info" / "exclude").write_text(".fractal/\n")
    (m / "src").mkdir()
    (m / "src" / "rows.rs").write_text(ROWS)
    (m / "src" / "old_user.rs").write_text("fn a() { row_sum(&[]); }\n")
    (m / "rows.md").write_text(SPEC)
    git(m, "add", "-A")
    git(m, "commit", "-qm", "c1")
    wt = tmp_path / "wt"
    git(m, "worktree", "add", "-q", "-b", "node-batch", str(wt))
    (wt / "src" / "batch.rs").write_text("fn b() { row_sum(&[1.0]); }\n")                 # a caller only batch has
    (wt / "tests").mkdir()
    (wt / "tests" / "test_batch.rs").write_text("fn t() { row_sum(&[]); keep(); }\n")
    c = ClaimCache(m)
    ledger.register_node(c, m, "rows", "rows.md")
    return m, wt, c


def test_changed_export_lists_callers_this_checkout_cannot_see(tmp_path):
    m, wt, c = _repos(tmp_path)
    (m / "src" / "rows.rs").write_text(ROWS.replace("x: &[f64]", "x: &[f64], w: usize"))   # signature change, uncommitted
    r = impact.check(c, m, "rows")
    assert [(x["export"], x["change"]) for x in r["rows"]] == [("src/rows.rs::row_sum", "changed")]
    assert r["rows"][0]["callers"] == {"node-batch": ["src/batch.rs", "tests/test_batch.rs"]}   # old_user.rs is visible here
    text = impact.render(r)
    assert "node-batch: src/batch.rs, tests/test_batch.rs (test)" in text and "word match" in text


def test_removed_export_and_guarantee_change_and_no_change(tmp_path):
    m, wt, c = _repos(tmp_path)
    assert "no exported item changed" in impact.render(impact.check(c, m, "rows"))
    (m / "src" / "rows.rs").write_text("pub fn row_sum(x: &[f64]) -> f64 {\n    x.iter().sum()\n}\n")   # keep() deleted
    r = impact.check(c, m, "rows")
    assert [(x["export"], x["change"]) for x in r["rows"]] == [("src/rows.rs::keep", "removed")]
    assert list(r["rows"][0]["callers"]) == ["node-batch"]
    git(m, "checkout", "-q", "src/rows.rs")
    (m / "rows.md").write_text(SPEC.replace("Sums a row.", "Sums a row, NaN-safe."))
    r = impact.check(c, m, "rows")
    assert {x["export"]: x["change"] for x in r["rows"]} == {"src/rows.rs::row_sum": "guarantee", "src/rows.rs::keep": "guarantee"}


def test_callers_unseen_only_versus_every_mention(tmp_path):
    m, wt, c = _repos(tmp_path)
    from fractal_harness.refindex import RefIndex
    idx = RefIndex.load(m)
    assert impact.callers(idx, "row_sum", "src/rows.rs") == {"node-batch": ["src/batch.rs", "tests/test_batch.rs"]}
    assert impact.callers(idx, "row_sum", "src/rows.rs", unseen_only=False)["node-batch"] == [
        "src/batch.rs", "src/old_user.rs", "tests/test_batch.rs"]


def test_cli_impact_and_verify_plan_mentions_it(tmp_path, capsys):
    m, wt, c = _repos(tmp_path)
    c.close()
    (m / "src" / "rows.rs").write_text(ROWS.replace("x: &[f64]", "x: &[f64], w: usize"))
    assert main(["--root", str(m), "impact", "rows"]) == 0
    assert "node-batch" in capsys.readouterr().out
    c = ClaimCache(m)
    ledger.record(c, m, "rows", file="src/rows.rs", span="2", mutant="m", result="killed", tests=["t"], commit="HEAD")
    c.close()
    assert main(["--root", str(m), "verify-plan", "rows", "--no-diff"]) == 0
    assert "Contract impact of rows" in capsys.readouterr().out

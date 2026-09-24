import json


def _budgets(tmp_path, **limits):
    p = tmp_path / "budgets.json"
    p.write_text(json.dumps({"currency": "USD", **limits}))
    return p


def _usd_only(data_csv):
    data_csv.write_text("\n".join(l for l in data_csv.read_text().splitlines() if ",EUR," not in l) + "\n")
    return data_csv


def test_alerts_report_overspend_and_exit_1(ledger, data_csv, tmp_path):
    r = ledger("alerts", str(_usd_only(data_csv)), "--budgets", str(_budgets(tmp_path, food=100, rent=1000)))
    assert r.returncode == 1, r.stderr
    lines = [l for l in r.stdout.splitlines() if l.strip()]
    assert lines == ["2026-01 food: spent 120.00 of 100.00 (over by 20.00)"]


def test_no_alerts_exit_0(ledger, data_csv, tmp_path):
    r = ledger("alerts", str(_usd_only(data_csv)), "--budgets", str(_budgets(tmp_path, food=500, rent=1000)))
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == ""

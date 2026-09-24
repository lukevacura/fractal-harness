import csv
import io
import json
from decimal import Decimal


def _rows(text: str) -> list[list[str]]:
    return [r for r in csv.reader(io.StringIO(text)) if r]


def _rates(tmp_path):
    p = tmp_path / "rates.json"
    p.write_text(json.dumps({"USD": 1, "EUR": 1.1}))
    return p


def test_export_usd(ledger, data_csv, tmp_path):
    r = ledger("export", str(data_csv), "--rates", str(_rates(tmp_path)), "--currency", "USD")
    assert r.returncode == 0, r.stderr
    rows = _rows(r.stdout)
    assert rows[0] == ["month", "category", "total"]
    got = {(m, c): Decimal(t) for m, c, t in rows[1:]}
    assert got == {("2026-01", "food"): Decimal("131.00"), ("2026-01", "rent"): Decimal("900.00"),
                   ("2026-02", "food"): Decimal("50.00"), ("2026-02", "travel"): Decimal("24.20")}
    assert [(m, c) for m, c, _ in rows[1:]] == sorted((m, c) for m, c, _ in rows[1:])


def test_export_eur_rounded(ledger, data_csv, tmp_path):
    r = ledger("export", str(data_csv), "--rates", str(_rates(tmp_path)), "--currency", "EUR")
    assert r.returncode == 0, r.stderr
    got = {(m, c): t for m, c, t in _rows(r.stdout)[1:]}
    assert Decimal(got[("2026-01", "food")]) == Decimal("119.09")   # 120/1.1 + 10, to 2 dp
    assert Decimal(got[("2026-02", "travel")]) == Decimal("22.00")


def test_export_to_file(ledger, data_csv, tmp_path):
    out = tmp_path / "out.csv"
    r = ledger("export", str(data_csv), "--rates", str(_rates(tmp_path)), "--currency", "USD", "--out", str(out))
    assert r.returncode == 0, r.stderr
    assert _rows(out.read_text())[0] == ["month", "category", "total"]

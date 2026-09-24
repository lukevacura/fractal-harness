import io
from datetime import date
from decimal import Decimal

from ledger.ledgerio import read_ledger, write_ledger
from ledger.models import Transaction


def test_roundtrip(tmp_path):
    t = [Transaction(date(2026, 1, 2), Decimal("-3.5"), "EUR", "food", "Bakery")]
    buf = io.StringIO()
    write_ledger(t, buf)
    assert buf.getvalue() == "date,amount,currency,category,description\n2026-01-02,-3.50,EUR,food,Bakery\n"
    p = tmp_path / "l.csv"
    p.write_text(buf.getvalue())
    assert read_ledger(p)[0].amount == Decimal("-3.50")

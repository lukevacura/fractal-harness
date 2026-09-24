from datetime import date
from decimal import Decimal

import pytest

from ledger.parse import ParseError, parse_csv

CSV = """date,amount,currency,category,description
2026-01-05,-12.50,usd,food,lunch
2026-01-06,2000,USD,salary,
"""


def test_parse_csv():
    txns = parse_csv(CSV.splitlines(keepends=True))
    assert txns[0].day == date(2026, 1, 5) and txns[0].amount == Decimal("-12.50")
    assert txns[0].currency == "USD" and txns[0].month == "2026-01"


def test_bad_currency():
    with pytest.raises(ParseError):
        parse_csv(["date,amount,currency,category\n", "2026-01-05,1,US,x\n"])

from datetime import date
from decimal import Decimal

from ledger.models import Transaction
from ledger.report import spending_by_category


def test_spending_by_category():
    t = [Transaction(date(2026, 1, 1), Decimal("-5"), "USD", "food"),
         Transaction(date(2026, 1, 2), Decimal("-7"), "USD", "food"),
         Transaction(date(2026, 1, 3), Decimal("100"), "USD", "salary")]
    assert spending_by_category(t) == {"food": Decimal("12")}

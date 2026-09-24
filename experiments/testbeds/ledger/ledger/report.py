from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Iterable

from .models import Transaction


def spending_by_category(txns: Iterable[Transaction]) -> dict[str, Decimal]:
    """Total spending (as a positive number) per category. Assumes a single currency."""
    out: dict[str, Decimal] = defaultdict(Decimal)
    for t in txns:
        if t.amount < 0:
            out[t.category] += -t.amount
    return dict(out)

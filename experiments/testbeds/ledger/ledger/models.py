from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal


@dataclass(frozen=True)
class Transaction:
    """One ledger entry. Negative amounts are spending, positive are income."""

    day: date
    amount: Decimal
    currency: str
    category: str
    description: str = ""

    @property
    def month(self) -> str:
        return f"{self.day.year:04d}-{self.day.month:02d}"

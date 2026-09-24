from __future__ import annotations

import csv
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Iterable

from .models import Transaction

FIELDS = ("date", "amount", "currency", "category", "description")


class ParseError(ValueError):
    pass


def parse_row(row: dict[str, str], line: int = 0) -> Transaction:
    try:
        y, m, d = (int(x) for x in row["date"].split("-"))
        amount = Decimal(row["amount"])
    except (KeyError, ValueError, InvalidOperation) as e:
        raise ParseError(f"line {line}: {e}") from e
    currency = (row.get("currency") or "").strip().upper()
    if len(currency) != 3:
        raise ParseError(f"line {line}: bad currency {currency!r}")
    return Transaction(date(y, m, d), amount, currency, (row.get("category") or "uncategorized").strip(),
                       (row.get("description") or "").strip())


def parse_csv(lines: Iterable[str]) -> list[Transaction]:
    reader = csv.DictReader(lines)
    return [parse_row(row, i) for i, row in enumerate(reader, start=2)]

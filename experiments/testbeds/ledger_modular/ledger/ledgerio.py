"""Reading and writing ledger CSV files: the one place that knows the on-disk format."""

from __future__ import annotations

import csv
from decimal import Decimal
from pathlib import Path
from typing import Iterable, TextIO

from .models import Transaction
from .parse import parse_csv

HEADER = ("date", "amount", "currency", "category", "description")


def read_ledger(path: str | Path) -> list[Transaction]:
    """All transactions in a ledger CSV file, in file order. Raises ParseError on bad rows."""
    with open(path, newline="") as f:
        return parse_csv(f)


def format_amount(amount: Decimal) -> str:
    """Amounts are always written with exactly 2 decimals."""
    return f"{amount.quantize(Decimal('0.01'))}"


def write_ledger(txns: Iterable[Transaction], out: TextIO) -> None:
    """Write a ledger CSV (header + one row per transaction, in the given order)."""
    w = csv.writer(out, lineterminator="\n")
    w.writerow(HEADER)
    for t in txns:
        w.writerow([t.day.isoformat(), format_amount(t.amount), t.currency, t.category, t.description])

from __future__ import annotations

from ..ledgerio import read_ledger
from ..report import spending_by_category

NAME = "summary"
HELP = "spending by category"


def add_arguments(parser) -> None:
    parser.add_argument("file")


def run(args) -> int:
    for cat, total in sorted(spending_by_category(read_ledger(args.file)).items()):
        print(f"{cat}\t{total}")
    return 0

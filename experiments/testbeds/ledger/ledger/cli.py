from __future__ import annotations

import argparse
import sys

from .parse import parse_csv
from .report import spending_by_category


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="ledger")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("summary", help="spending by category")
    s.add_argument("file")
    args = p.parse_args(argv)
    with open(args.file) as f:
        txns = parse_csv(f)
    if args.cmd == "summary":
        for cat, total in sorted(spending_by_category(txns).items()):
            print(f"{cat}\t{total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

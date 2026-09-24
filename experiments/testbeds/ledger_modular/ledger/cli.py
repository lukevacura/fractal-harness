from __future__ import annotations

import argparse
import sys

from .commands import discover


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="ledger")
    sub = p.add_subparsers(dest="cmd", required=True)
    commands = {}
    for mod in discover():
        mod.add_arguments(sub.add_parser(mod.NAME, help=mod.HELP))
        commands[mod.NAME] = mod
    args = p.parse_args(argv)
    return commands[args.cmd].run(args)


if __name__ == "__main__":
    sys.exit(main())

"""CLI commands: one module per subcommand, discovered automatically.

To add `ledger <name>`, create `ledger/commands/<name>.py` defining:

    NAME = "<name>"                      # the subcommand
    HELP = "<one line>"
    def add_arguments(parser): ...       # argparse arguments
    def run(args) -> int: ...            # exit code

No other file needs to change: `ledger.cli` imports every module in this package.
"""

from __future__ import annotations

import importlib
import pkgutil
from types import ModuleType


def discover() -> list[ModuleType]:
    """Every command module in this package, sorted by command name."""
    mods = [importlib.import_module(f"{__name__}.{m.name}") for m in pkgutil.iter_modules(__path__)]
    return sorted((m for m in mods if hasattr(m, "NAME")), key=lambda m: m.NAME)

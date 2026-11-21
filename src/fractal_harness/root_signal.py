"""Root signal: how a human (root) decision is confirmed. `[policy] root_signal` in fractal.toml.

Theory: only the root may let more states through, so a root decision must come from somewhere the agent cannot
reach. Every human-only CLI verb (accept without --as, reject, override by root, pin, conflict decisions taken at
root) asks `confirm` before acting. The setting is itself part of the policy `fractal pin` pins (config.py), so an
agent cannot weaken it by editing fractal.toml.

  tty   (default) the decision is shown and confirmed on the controlling terminal (/dev/tty); `--yes` is not
        accepted. An agent's shell has no controlling terminal (checked in Claude Code, 2026-11-21), so it cannot
        confirm.                                                                                      HEURISTIC:
        a same-user process can create a pseudo-terminal (`script`, `expect`, python's `pty`) and answer the
        prompt itself. The PreToolUse hook blocks those wrappers around `fractal` (store_guard.py), which closes
        the obvious paths, not a determined one. Out of an agent's reach for real: physical presence (an OS
        dialog, a hardware key) or a separate account; those are future rungs of this setting.
  flag  the earlier behaviour: stdin is a terminal, or `--yes` is passed. For scripts the human controls. An agent
        can pass `--yes`: nothing but the hook guard stands in its way.
"""

from __future__ import annotations

import sys
from pathlib import Path

SIGNALS = ("tty", "flag")
SHIPPED_DEFAULT = "tty"
DEFAULT = SHIPPED_DEFAULT       # tests set "flag" (conftest) so earlier tests keep their meaning


class RootSignalError(Exception):
    pass


def effective(root: Path) -> str:
    from .config import load
    return load(root).root_signal or DEFAULT


def _open_tty():
    """The controlling terminal, or None when there is none (an agent's shell, a pipeline, CI)."""
    try:
        return open("/dev/tty", "r+")
    except OSError:
        return None


def confirm(root: Path, what: str, yes: bool = False) -> None:
    """Return when a human confirmed `what`; raise RootSignalError otherwise."""
    sig = effective(root)
    if sig == "flag":
        if sys.stdin.isatty() or yes:
            return
        raise RootSignalError(f"`{what}` is a human decision; run it in an interactive terminal "
                              "(or pass --yes from a script you control)")
    tty = _open_tty()
    if tty is None:
        raise RootSignalError(f"`{what}` is a human decision (root_signal = \"tty\"): run it in your own terminal, "
                              "where it asks you to confirm. An agent cannot confirm it"
                              + ("; --yes is not accepted under root_signal = \"tty\"" if yes else "") + ".")
    with tty:
        tty.write(f"fractal: root decision: {what}\nType yes to confirm: ")
        tty.flush()
        answer = tty.readline().strip().lower()
    if answer != "yes":
        raise RootSignalError(f"`{what}` was not confirmed")

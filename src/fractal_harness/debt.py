"""Debt: a rule the code already breaks, kept instead of dropped, with a bound that may only go down.

Theory: onboarding an existing codebase finds rules worth having that the code violates today. Dropping them hides
information (the rule, and how far the code is from it); enforcing them as written blocks every edit. A DEBT rule is
the rule with its upper bound relaxed to the current count: `{"max": k}` plus `debt_target`, the original
expectation. It forbids every state with more violations than today. Lowering the bound (`fractal ratchet`) only
forbids more, so loss.py classifies it as no loss and any layer may make it; raising the bound back up lets states
through and is a loss like any other. When the count reaches the target, the original expectation is restored.

Only upper bounds (absent, max k) on grep and paths leaves can be debt: a violated presence rule (too few matches)
has no count that "goes down" toward compliance. Other leaves are kept as written.
"""

from __future__ import annotations

import copy
from pathlib import Path

from . import probes

DEBT_TYPES = ("grep", "paths")


class DebtError(Exception):
    pass


def _leaves(probe: dict) -> list[dict]:
    if probe.get("type") == "all":
        return [x for p in probe["probes"] for x in _leaves(p)]
    return [probe]


def _expect(leaf: dict):
    return probes.paths_expect(leaf) if leaf.get("type") == "paths" else leaf.get("expect", "present")


def _upper(leaf: dict) -> int | None:
    """The bound of a pure upper-bound leaf (absent, max k), or None."""
    op, n = probes._expectation(_expect(leaf))
    return n if op == "max" or (op == "count" and n == 0) else None


def to_debt(probe: dict, root: Path) -> tuple[dict, list[str]]:
    """The debt form of `probe`: each failing upper-bound leaf gets `max: <current count>`. Raises DebtError when a
    failing leaf cannot be debt (a presence rule, a command, ...)."""
    out = copy.deepcopy(probe)
    notes = []
    for leaf in _leaves(out):
        if probes.run(leaf, root).passed:
            continue
        bound = _upper(leaf) if leaf.get("type") in DEBT_TYPES else None
        if bound is None:
            raise DebtError(f"a failing {leaf.get('type')} leaf ({leaf.get('pattern') or leaf.get('paths')}) with "
                            f"expect {leaf.get('expect')!r} cannot be debt: only absent / max rules on grep "
                            "and paths probes have a count that goes down. Fix the code, or narrow the rule")
        n = probes.count(leaf, root)
        leaf["debt_target"] = _expect(leaf)
        leaf["expect"] = {"max": n}
        notes.append(f"{leaf.get('pattern') or ', '.join(leaf['paths'])}: {n} today, target {_target_n(leaf):g}")
    if not notes:
        raise DebtError("the probe passes now: nothing to carry as debt (propose it without --debt)")
    return out, notes


def _target_n(leaf: dict) -> float:
    return probes._expectation(leaf["debt_target"])[1]


def outstanding(probe: dict | None) -> list[dict]:
    """The debt leaves of a probe."""
    return [x for x in _leaves(probe) if "debt_target" in x] if probe else []


def ratcheted(probe: dict, root: Path) -> tuple[dict, list[str]]:
    """`probe` with each debt bound lowered to today's count (restoring the target once reached). The probe is
    unchanged (and notes empty) when no count went down."""
    out = copy.deepcopy(probe)
    notes = []
    for leaf in outstanding(out):
        bound = probes._expectation(leaf["expect"])[1]
        n = probes.count(leaf, root)
        target = _target_n(leaf)
        what = leaf.get("pattern") or ", ".join(leaf["paths"])
        if n > bound:
            raise DebtError(f"{what}: {n} today, above its bound {bound}: fix the code (the bound only goes down)")
        if n <= target:
            leaf["expect"] = leaf.pop("debt_target")
            notes.append(f"{what}: paid off ({n}); the rule is back to {leaf['expect']!r}")
        elif n < bound:
            leaf["expect"] = {"max": n}
            notes.append(f"{what}: {bound} -> {n} (target {target:g})")
    return out, notes


def summary(probe: dict | None) -> str | None:
    """One line for listings: how much debt the rule carries."""
    parts = [f"{x.get('pattern') or ', '.join(x['paths'])}: at most {probes._expectation(x['expect'])[1]}, target "
             f"{_target_n(x):g}" for x in outstanding(probe)]
    return "; ".join(parts) or None

"""Node specs as DAG vertices: a spec's front matter declares what the node rests on and what it offers.

    ---
    parents: stats, cov            # nodes whose contracts this one assumes (direct dependencies only)
    implements: D18                # decisions this node must reproduce, taken in full
    see: naive                     # peer nodes: non-binding examples of how a similar node was done
    guarantee: Windowed mean and variance, exact for N.
    condition: Only for windows with V < 2^63; stats flags the rest.
    exports: src/stats.rs::Stats::update, src/stats.rs::Stats
    ---

A child may rely on a parent's *contract*: its `guarantee`, its `condition` (a conditional guarantee
carries its condition with it), and the current signatures of its `exports`. It may not rely on
anything else about the parent, and grandparents are not premises. The contract's digest is what
the mutant ledger watches, so a parent's contract changing stales exactly its children.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .symbols import SymbolError, digest, split_ref, symbol_of

LIST_FIELDS = ("parents", "implements", "see", "exports")
STR_FIELDS = ("guarantee", "condition")
CONDITIONAL_WORDS = re.compile(r"\b(only (?:when|if|for)|provided|unless|assuming|as long as|given that)\b", re.I)


@dataclass
class Header:
    parents: list[str] = field(default_factory=list)
    implements: list[str] = field(default_factory=list)
    see: list[str] = field(default_factory=list)
    exports: list[str] = field(default_factory=list)
    guarantee: str = ""
    condition: str = ""


def parse_header(text: str) -> Header | None:
    """The spec's front matter, or None when it has none (a legacy spec)."""
    m = re.match(r"\A---[ \t]*\n(.*?)\n---[ \t]*(?:\n|\Z)", text, re.S)
    if not m:
        return None
    fields: dict[str, str] = {}
    key = None
    for line in m.group(1).splitlines():
        line = re.sub(r"\s+#.*$", "", line.rstrip())
        if not line.strip():
            continue
        if kv := re.match(r"^([a-z]+):\s*(.*)$", line):
            key = kv.group(1)
            fields[key] = kv.group(2).strip()
        elif key and line[0] in " \t":
            fields[key] = f"{fields[key]} {line.strip()}".strip()
    unknown = sorted(set(fields) - set(LIST_FIELDS) - set(STR_FIELDS))
    if unknown:
        raise SymbolError(f"unknown spec header field(s): {', '.join(unknown)}")
    h = Header()
    for k in LIST_FIELDS:
        setattr(h, k, [x for x in re.split(r"[,\s]+", fields.get(k, "")) if x])
    for k in STR_FIELDS:
        setattr(h, k, fields.get(k, ""))
    return h


def body(text: str) -> str:
    return re.sub(r"\A---[ \t]*\n.*?\n---[ \t]*(?:\n|\Z)", "", text, count=1, flags=re.S)


def lint(h: Header) -> list[str]:
    notes = []
    if h.guarantee and CONDITIONAL_WORDS.search(h.guarantee) and not h.condition:
        notes.append("the guarantee reads as conditional but declares no `condition:`; a child would not see it")
    if h.exports and not h.guarantee:
        notes.append("exports are declared without a `guarantee:`")
    return notes


def contract(root: Path, spec_path: str) -> str:
    """The text a child may rely on: guarantee, condition, and the exports' current signatures."""
    f = root / spec_path
    if not f.is_file():
        raise SymbolError(f"no such spec: {spec_path}")
    h = parse_header(f.read_text(errors="replace"))
    if h is None or not h.guarantee:
        raise SymbolError(f"{spec_path} declares no `guarantee:`: nothing for a child to rely on")
    out = [f"guarantee: {h.guarantee}"]
    if h.condition:
        out.append(f"condition: {h.condition}")
    for ref in h.exports:
        split_ref(ref)
        out += [f"interface {ref}:", symbol_of(root, ref)]
    return "\n".join(out)


def contract_digest(root: Path, spec_path: str) -> str:
    return digest(contract(root, spec_path))

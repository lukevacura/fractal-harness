"""`fractal context <spec.md>`: one bounded bundle of everything a node's implementer needs.

  1. the spec itself
  2. the exact text of each decision/invariant the spec cites by ID, from claims written with
     `fractal put --section <file>::<heading>` (the heading starts with the ID)
  3. the interfaces the spec names, from claims written with `fractal put --symbol <file>::<item>`
  4. the enforced rules that govern the spec's paths, stated as guaranteed

Claims whose source changed are labeled, never presented as current. IDs the spec cites with
no claim behind them are listed, so a gap in the claims is visible instead of silent.
"""

from __future__ import annotations

import re
from pathlib import Path

from .cache import ClaimCache
from .store import Edge
from .symbols import SymbolError, digest, section_of, split_ref, symbol_of

DEFAULT_ID_PATTERN = r"\b[A-Z]{1,3}-?\d{1,3}\b"
MAX_RULES = 12


def put_extracted(cache: ClaimCache, root: Path, *, section: str | None = None, symbol: str | None = None,
                  depends_on: list[str] | None = None) -> Edge:
    """Assert a claim carrying a section's or an interface's exact text, probed by its digest.
    A claim for the same ref with older text is replaced, not left behind failing."""
    ref, kind, probe_type = (section, "knowledge", "section") if section else (symbol, "interface", "symbol")
    text = (section_of if section else symbol_of)(root, ref)
    path = split_ref(ref)[0]
    post = text if section else f"{ref}\n{text}"
    if len(post) > 2000:
        raise SymbolError(f"{ref} is {len(post)} characters; claims carry at most 2000")
    old = [e for e in cache.store.all() if (e.probe or {}).get("type") == probe_type and e.probe.get("ref") == ref]
    edge = cache.put(post, [path], kind=kind, probe={"type": probe_type, "ref": ref, "sha": digest(text)},
                     depends_on=depends_on)
    for e in old:
        if e.id != edge.id:
            cache.delete(e.id)
    return edge


def _cited(edges: list[Edge], spec: str, id_pattern: str) -> tuple[list[Edge], list[str], list[Edge]]:
    """(section claims for cited IDs, IDs with no claim, interface claims the spec names)."""
    ids = list(dict.fromkeys(re.findall(id_pattern, spec)))
    sections = [e for e in edges if (e.probe or {}).get("type") == "section"]
    found, missing = [], []
    for i in ids:
        hit = [e for e in sections if re.search(rf"(?<![\w-]){re.escape(i)}(?![\w-])", e.post.splitlines()[0])]
        found += [e for e in hit if e not in found]
        if not hit:
            missing.append(i)
    interfaces = []
    for e in edges:
        if (e.probe or {}).get("type") != "symbol":
            continue
        path, name = split_ref(e.probe["ref"])
        item = name.rpartition("::")[2]
        if e.probe["ref"] in spec or re.search(rf"(?<![\w]){re.escape(name)}(?![\w])", spec) or (
                len(item) >= 4 and re.search(rf"`[^`\n]*(?<![\w]){re.escape(item)}(?![\w])[^`\n]*`", spec)):
            interfaces.append(e)
    return found, missing, interfaces


def _show(e: Edge) -> str:
    note = "" if e.status in ("verified", "trusted") else (
        f"\n[{e.status.upper()}: {e.detail}; the source changed since this was written, so read it again]")
    return f"{e.post}{note}"


def cited_sections(root: Path, spec: str, id_pattern: str = DEFAULT_ID_PATTERN) -> list[str]:
    """The decisions, missing-citation note and interfaces a spec's text calls for (as output lines)."""
    cache = ClaimCache(root)
    try:
        cache.update()
        edges = cache.store.all()
    finally:
        cache.close()
    found, missing, interfaces = _cited(edges, spec, id_pattern)
    out: list[str] = []
    if found:
        out += ["## Cited decisions and invariants", ""] + [t for e in found for t in (_show(e), "")]
    if missing:
        out += ["## Cited but not in the claim store", "",
                ", ".join(missing) + " (read the source docs for these, or add them with "
                "`fractal put --section <file>::<heading>`)", ""]
    if interfaces:
        out += ["## Interfaces", ""] + [t for e in interfaces for t in (f"```\n{_show(e)}\n```", "")]
    return out


def bundle(root: Path, spec_path: str, id_pattern: str = DEFAULT_ID_PATTERN) -> str:
    from .hook import RULES_HEADER, _agent_query, _rule_line, select_context
    f = root / spec_path
    if not f.is_file():
        raise SymbolError(f"no such spec: {spec_path}")
    spec = f.read_text(errors="replace")
    rules, _ = select_context(root, _agent_query(root, spec), log=False)
    out = [f"# Context for {spec_path}", "", "## Spec", "", spec.strip(), ""]
    out += cited_sections(root, spec, id_pattern)
    if rules:
        out += ["## Rules", "", RULES_HEADER, *(_rule_line(e) for e in rules[:MAX_RULES]), ""]
    return "\n".join(out).rstrip() + "\n"

"""Manifests: an owner's context for one region of the region tree.

For a region (a directory or file; "" = the whole repo) the manifest is what an agent that
owns it needs instead of re-reading the code, rules first:

  Rules            enforced invariants that govern the region: placed at the region or an
                   ancestor, or scanning files inside it
  Proposed rules   invariants awaiting a human decision (shown, never binding)
  Big picture      facts placed at an ancestor region (they span more than this region)
  This region      facts placed inside the region
  Neighbours       interface claims placed elsewhere (what this region may call)

Placement comes from the region tree: a claim lives at the smallest region containing
everything it depends on.
"""

from __future__ import annotations

from pathlib import Path

from .cache import ClaimCache, dependencies
from .regions import affects, governs, placement, tracked_files
from .store import GOOD, Edge


def _line(e: Edge) -> str:
    pre = f"{{{e.pre}}} ⟹ " if e.pre else ""
    tag = "" if e.status == "verified" else f" ({e.status})"
    return f"- {pre}{e.post}{tag}  _({', '.join(e.reads) or '-'})_"


def manifest(root: Path, region: str = "") -> str:
    region = region.strip().strip("/")
    cache = ClaimCache(root)
    try:
        cache.update()
        claims = cache.store.all()
        files = tracked_files(cache.root)
    finally:
        cache.close()
    in_region = [f for f in files if governs(region, f)]
    place = {e.id: placement(dependencies(e), files) for e in claims}

    def related(e: Edge) -> bool:            # placed at an ancestor of, or inside, the region
        return governs(place[e.id], region) or governs(region, place[e.id])

    rules = [e for e in claims if e.enforced and e.status in GOOD + ("failed",)
             and (related(e) or any(affects(dependencies(e), f) for f in in_region))]
    proposed = [e for e in claims if e.kind == "invariant" and e.rule_state == "proposed" and related(e)]
    facts = [e for e in claims if e.kind != "invariant" and e.status in GOOD]
    above = [e for e in facts if place[e.id] != region and governs(place[e.id], region)]
    inside = [e for e in facts if governs(region, place[e.id])]
    neighbours = [e for e in facts if e.kind == "interface" and e not in above and e not in inside]

    def section(title: str, edges: list[Edge]) -> list[str]:
        return [f"## {title}", *(_line(e) for e in sorted(edges, key=lambda e: (place[e.id], e.post))), ""] \
            if edges else []

    lines = [f"# Manifest: {region or 'whole repo'}", "",
             "Rules are enforced (edits are checked against them); facts were verified against the current source.", ""]
    lines += section("Rules", rules)
    lines += section("Proposed rules (awaiting a human decision; not binding)", proposed)
    lines += section("Big picture", above)
    lines += section("This region", inside)
    lines += section("Neighbouring interfaces", neighbours)
    return "\n".join(lines).rstrip() + "\n"

"""Manifests: an owner's "easily refreshable context", rendered from verified claims.

A manifest for a region at a zoom level is what an agent that owns that region needs
instead of re-reading the code: the claims describing the region at that level, plus the
coarser claims above it (the big picture) and the interfaces of neighbouring regions.

Levels follow the fractal: L1 describes ~10K-line regions coarsely, L2 ~1K-line regions,
L3 ~100-line regions in full detail. Claims without a level count as L1 when they span
several directories and L2 otherwise.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path

from .cache import ClaimCache
from .store import GOOD, Edge


def _paths(e: Edge) -> list[str]:
    return e.region or e.reads


def _level(e: Edge) -> int:
    if e.level:
        return e.level
    dirs = {str(Path(p).parent) for p in _paths(e)}
    return 1 if len(dirs) > 1 or not dirs else 2


def _overlaps(paths: list[str], region: list[str]) -> bool:
    for p in paths:
        for r in region:
            if (p == r or fnmatch.fnmatch(p, r) or fnmatch.fnmatch(r, p)
                    or p.startswith(r.rstrip("/*") + "/") or r.startswith(p.rstrip("/*") + "/")):
                return True
    return False


def manifest(root: Path, region: list[str] | None = None, level: int | None = None) -> str:
    """Markdown manifest. No region: the whole repo. `level` caps detail (default: all)."""
    cache = ClaimCache(root)
    try:
        cache.update()
        claims = [e for e in cache.store.all() if e.status in GOOD]
    finally:
        cache.close()
    if level:
        claims = [e for e in claims if _level(e) <= level]
    if region:
        above = [e for e in claims if _level(e) == 1]
        inside = [e for e in claims if _level(e) > 1 and _overlaps(_paths(e), region)]
    else:
        above, inside = [], claims
    neighbours = [e for e in claims if region and e not in inside and e not in above and e.kind == "interface"]

    def section(title: str, edges: list[Edge]) -> list[str]:
        if not edges:
            return []
        out = [f"## {title}"]
        for e in sorted(edges, key=lambda e: (e.kind != "workflow", e.kind != "invariant", _paths(e))):
            tag = "" if e.status == "verified" else " (unverified)"
            pre = f"{{{e.pre}}} ⟹ " if e.pre else ""
            where = ", ".join(_paths(e)) or "-"
            out.append(f"- [{e.kind}{tag}] {pre}{e.post}  _({where})_")
        return out + [""]

    title = f"# Manifest: {', '.join(region)}" if region else "# Manifest: whole repo"
    lines = [title, "", "Verified claims about this code (each checked by a probe against the current source).", ""]
    lines += section("Big picture", above)
    lines += section("This region" if region else "Claims", inside)
    lines += section("Neighbouring interfaces", neighbours)
    return "\n".join(lines).rstrip() + "\n"

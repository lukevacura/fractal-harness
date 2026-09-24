"""`fractal check`: a commit/CI gate built from claims. Local, deterministic, no model call.

Re-checks every claim affected by the working tree's changes (`update`). A failing
`invariant` claim is a violation: the code broke a rule, so the check fails. Other broken
claims mean the cache needs repair, which is a warning unless `strict`.
"""

from __future__ import annotations

from pathlib import Path

from .cache import STORE_DIR, ClaimCache


def check(root: Path, strict: bool = False) -> tuple[int, str]:
    """(exit code, report). Exit 0 when the repo has no claim store."""
    if not (root / STORE_DIR / "edges.db").exists():
        return 0, ""
    cache = ClaimCache(root)
    try:
        cache.update()
        edges = cache.store.all()
    finally:
        cache.close()
    violations = [e for e in edges if e.kind == "invariant" and e.status == "failed"]
    needs_repair = [e for e in edges if e.kind != "invariant" and e.status in ("failed", "stale")
                    and e.repair is not None]
    lines = []
    for e in violations:
        lines.append(f"VIOLATION {e.id}: {e.post}\n          probe: {e.detail}")
    if needs_repair:
        kinds: dict[str, int] = {}
        for e in needs_repair:
            kinds[e.repair["kind"]] = kinds.get(e.repair["kind"], 0) + 1
        summary = ", ".join(f"{n} {k}" for k, n in sorted(kinds.items()))
        lines.append(f"fractal: {len(needs_repair)} claim(s) need repair ({summary}); "
                     "run `fractal repair --all` or let them repair on demand")
    if violations:
        lines.append(f"fractal: {len(violations)} invariant(s) violated. Fix the code, or if the rule "
                     "itself changed, update the claim deliberately (`fractal rm` / `fractal put`).")
    code = 1 if violations or (strict and needs_repair) else 0
    return code, "\n".join(lines)

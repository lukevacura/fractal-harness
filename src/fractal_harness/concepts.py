"""The concept registry: what the `only_in` probes claim is owned by exactly one place.

Single-ownership claims ("one ranking expression", "one kind encoding", "one nearest order") are probes,
not prose, so the registry is generated from them: concept -> owner files -> whether the claim still
holds. `fractal concepts` prints it; `--write FILE` saves it as markdown.
"""

from __future__ import annotations

from .cache import ClaimCache
from .probes import only_in_pattern


def _leaves(probe: dict | None) -> list[dict]:
    if not probe:
        return []
    if probe.get("type") == "all":
        return [x for sub in probe["probes"] for x in _leaves(sub)]
    return [probe] if probe.get("type") == "only_in" else []


def registry(cache: ClaimCache) -> list[dict]:
    rows = []
    for e in cache.store.all():
        for leaf in _leaves(e.probe):
            rows.append({"concept": leaf.get("concept") or e.post.splitlines()[0],
                         "owner": leaf["files"], "what": leaf.get("pattern") or
                         f"{leaf['kind']} {leaf.get('name', '*')}",
                         "claim": e.id, "status": e.status, "rule": e.rule_state or "",
                         "detail": "" if e.status in ("verified", "trusted") else e.detail})
    return sorted(rows, key=lambda r: r["concept"].lower())


def render(rows: list[dict]) -> str:
    if not rows:
        return ("No single-ownership claims yet. Add one with "
                "`fractal only-in \"<concept>\" --pattern <regex> --in <file> --paths '<glob>'`.\n")
    out = ["| Concept | Owner | Looks for | Claim | Status |", "|---|---|---|---|---|"]
    for r in rows:
        status = r["status"] + (f" ({r['detail']})" if r["detail"] else "")
        out.append(f"| {r['concept']} | {', '.join(r['owner'])} | `{r['what']}` | {r['claim']}"
                   f"{' (' + r['rule'] + ')' if r['rule'] else ''} | {status} |")
    return "\n".join(out) + "\n"

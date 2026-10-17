"""The concept registry: what the `only_in` probes claim is owned by exactly one place.

Single-ownership claims ("one ranking expression", "one kind encoding", "one nearest order") are probes,
not prose, so the registry is generated from them: concept -> owner files -> whether the claim still
holds. `fractal concepts` prints it; `--write FILE` saves it as markdown.
"""

from __future__ import annotations

import re

from .cache import ClaimCache


def _leaves(probe: dict | None) -> list[dict]:
    if not probe:
        return []
    if probe.get("type") == "all":
        return [x for sub in probe["probes"] for x in _leaves(sub)]
    return [probe] if probe.get("type") == "only_in" else []


ID = r"\b[A-Z]{1,3}-?\d{1,3}\b"


def decisions_of(leaf: dict, concept: str) -> list[str]:
    """The decision IDs a single-ownership claim is about: its `decision` field, plus any ID in the concept's name."""
    declared = leaf.get("decision") or []
    declared = [declared] if isinstance(declared, str) else list(declared)
    return list(dict.fromkeys(declared + re.findall(ID, concept)))


def decision_ids(cache: ClaimCache) -> dict[str, list[str]]:
    """{decision ID: [concept names]} for every decision a concept claim names. Ownership lints only apply to these."""
    out: dict[str, list[str]] = {}
    for e in cache.store.all():
        for leaf in _leaves(e.probe):
            concept = leaf.get("concept") or e.post.splitlines()[0]
            for d in decisions_of(leaf, concept):
                out.setdefault(d, [])
                if concept not in out[d]:
                    out[d].append(concept)
    return out


def registry(cache: ClaimCache) -> list[dict]:
    rows = []
    for e in cache.store.all():
        for leaf in _leaves(e.probe):
            concept = leaf.get("concept") or e.post.splitlines()[0]
            rows.append({"concept": concept, "decisions": decisions_of(leaf, concept),
                         "owner": leaf["files"], "what": leaf.get("pattern") or
                         f"{leaf['kind']} {leaf.get('name', '*')}",
                         "claim": e.id, "status": e.status, "rule": e.rule_state or "",
                         "detail": "" if e.status in ("verified", "trusted") else e.detail})
    return sorted(rows, key=lambda r: r["concept"].lower())


def render(rows: list[dict]) -> str:
    if not rows:
        return ("No single-ownership claims yet. Add one with "
                "`fractal only-in \"<concept>\" --pattern <regex> --in <file> --paths '<glob>'`.\n")
    out = ["| Concept | Decisions | Owner | Looks for | Claim | Status |", "|---|---|---|---|---|---|"]
    for r in rows:
        status = r["status"] + (f" ({r['detail']})" if r["detail"] else "")
        out.append(f"| {r['concept']} | {', '.join(r['decisions']) or '-'} | {', '.join(r['owner'])} | `{r['what']}` | {r['claim']}"
                   f"{' (' + r['rule'] + ')' if r['rule'] else ''} | {status} |")
    return "\n".join(out) + "\n"

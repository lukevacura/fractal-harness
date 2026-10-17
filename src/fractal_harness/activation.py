"""Which nodes need an agent, and why.

A node is a persistent identity; an agent is an activation of it. A node is due an activation when one
of these events has happened since its verdicts were recorded, each decided by a deterministic check:

  spec      its spec's keyed content (header contract, design points) changed, or the spec is gone
  contract  a premise changed: a parent's guarantee, condition or exports, or a decision it implements
  probe     a failing claim depends on files the node owns
  proposal  a proposed invariant is waiting on files the node owns
  survivor  a mutant survived and nothing since has killed it

A parent's *implementation* change that leaves its contract alone raises nothing here: the contract
digest, not the file, is what a dependent watches. Events say that an activation is due, never what
the outcome should be; the verdict stays with the probes and the verifier.
"""

from __future__ import annotations

from pathlib import Path

from .cache import ClaimCache, dependencies
from .context import premises_of
from .ledger import premise_change
from .nodes import parse_header, spec_key
from .regions import affects
from .symbols import SymbolError, digest, split_ref

KINDS = ("spec", "contract", "probe", "proposal", "survivor")


def owned_files(cache: ClaimCache, root: Path, node: str, info: dict) -> list[str]:
    """The code the node owns: the files its exports live in, plus the files its mutants were recorded on."""
    files: set[str] = {r["file"] for r in cache.store.mutants(node)}
    if (spec := root / info["spec"]).is_file() and (h := parse_header(spec.read_text(errors="replace"))):
        files |= {split_ref(r)[0] for r in h.exports if "::" in r}
    return sorted(files)


def events(cache: ClaimCache, root: Path, node: str, info: dict, edges: list) -> list[dict]:
    out: list[dict] = []
    spec = root / info["spec"]
    if not spec.is_file():
        return [{"kind": "spec", "detail": f"spec {info['spec']} is gone"}]
    text = spec.read_text(errors="replace")
    try:
        keyed = info["spec_sha"] in (digest(spec_key(text)), digest(text))
    except SymbolError:
        keyed = False
    if not keyed:
        out.append({"kind": "spec", "detail": f"spec {info['spec']} changed since it was registered"})
    else:
        reason, _ = premise_change(cache, root, premises_of(info))
        if reason:
            out.append({"kind": "contract", "detail": reason})
    files = owned_files(cache, root, node, info)
    for e in edges:
        hit = next((f for f in files if affects(dependencies(e), f)), None)
        if hit is None:
            continue
        if e.status == "failed":
            out.append({"kind": "probe", "detail": f"{e.id} fails on {hit}: {e.post[:80]}"})
        elif e.kind == "invariant" and e.rule_state == "proposed":
            out.append({"kind": "proposal", "detail": f"{e.id} proposes on {hit}: {e.post[:80]}"})
    latest: dict[tuple, dict] = {}
    for r in cache.store.mutants(node):      # oldest first: the last verdict on a mutant wins
        latest[(r["file"], r["start"], r["end"], r["mutant"])] = r
    out += [{"kind": "survivor", "detail": f"{r['file']}:{r['start']}-{r['end']} {r['mutant']}"}
            for r in latest.values() if r["result"] == "survived"]
    return out


def due(cache: ClaimCache, root: Path) -> list[dict]:
    """Every registered node with at least one event, as {node, events: [{kind, detail}]}."""
    cache.update()
    edges = cache.store.all()
    rows = []
    for node, info in sorted(cache.store.all_nodes().items()):
        if ev := events(cache, root, node, info, edges):
            rows.append({"node": node, "events": ev})
    return rows


def render(rows: list[dict]) -> str:
    if not rows:
        return "No node is due an activation.\n"
    out = [f"{len(rows)} node(s) due an activation:", ""]
    for r in rows:
        out.append(f"{r['node']}")
        out += [f"  {e['kind']:9} {e['detail']}" for e in r["events"]]
    return "\n".join(out) + "\n"

"""Which nodes need an agent, and why.

A node is a persistent identity; an agent is an activation of it. A node is due an activation when one
of these events has happened since its verdicts were recorded, each decided by a deterministic check:

  spec      its spec's keyed content (header contract, design points) changed, or the spec is gone
  contract  a premise changed: a parent's guarantee, condition or exports, or a decision it implements
  probe     a failing, non-rejected claim whose probe points at files the node owns (a grep is attributed
            to the files with matching lines; a probe that cannot say falls back to what it depends on)
  survivor  a mutant survived with no equivalence argument and nothing since has killed it

Two things are reported but are not activations. A proposed invariant waits on a human (an agent cannot
accept it), so proposals are listed once, with the nodes they touch. A survivor that carries an equivalence
argument needs a `ledger record --result equivalent`, not an agent.

A parent's *implementation* change that leaves its contract alone raises nothing here: the contract
digest, not the file, is what a dependent watches. Events say that an activation is due, never what
the outcome should be; the verdict stays with the probes and the verifier.
"""

from __future__ import annotations

from pathlib import Path

from . import probes
from .cache import ClaimCache, dependencies
from .context import premises_of
from .ledger import premise_change
from .nodes import parse_header, spec_key
from .regions import affects
from .symbols import SymbolError, at, digest, split_ref

KINDS = ("spec", "contract", "probe", "proposal", "survivor")


def owned_files(cache: ClaimCache, root: Path, node: str, info: dict) -> list[str]:
    """The code the node owns: the files its exports live in, plus the files its mutants were recorded on."""
    files: set[str] = {r["file"] for r in cache.store.mutants(node)}
    if (spec := at(root, info["spec"])).is_file() and (h := parse_header(spec.read_text(errors="replace"))):
        files |= {split_ref(r)[0] for r in h.exports if "::" in r}
    return sorted(files)


def blame(root: Path, e) -> list[str] | None:
    """The files a failing claim's probe points at (those with matching lines), [] when it passes now, or None
    when it cannot say cheaply (a slow probe, or one with no matching lines to point at)."""
    if not e.probe or probes.is_slow(e.probe):
        return None
    try:
        r = probes.run(e.probe, root)
    except (probes.ProbeError, OSError):
        return None
    return [] if r.passed else (sorted({f for f, _ in r.matches}) or None)


def events(cache: ClaimCache, root: Path, node: str, info: dict, failing: dict[str, list[str] | None]) -> list[dict]:
    out: list[dict] = []
    spec = at(root, info["spec"])
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
    for e in failing_edges(cache):
        pointed = failing[e.id]
        mine = [f for f in files if (f in pointed if pointed is not None else affects(dependencies(e), f))]
        if mine:
            out.append({"kind": "probe", "detail": f"{e.id} fails on {mine[0]}: {e.post[:80]}"})
    return out + survivors(cache, node)[0]


def failing_edges(cache: ClaimCache) -> list:
    """Claims that fail and still matter: a rejected rule was refused by the owner, so it no longer counts."""
    return [e for e in cache.store.all() if e.status == "failed" and e.rule_state != "rejected"]


def survivors(cache: ClaimCache, node: str) -> tuple[list[dict], list[dict]]:
    """(unresolved survivor events, survivors that carry an equivalence argument but were never recorded
    equivalent), by the last verdict on each mutant."""
    latest: dict[tuple, dict] = {}
    for r in cache.store.mutants(node):      # oldest first: the last verdict on a mutant wins
        latest[(r["file"], r["start"], r["end"], r["mutant"])] = r
    open_, argued = [], []
    for r in latest.values():
        if r["result"] == "survived":
            (argued if (r["argument"] or "").strip() else open_).append(
                {"kind": "survivor", "detail": f"{r['file']}:{r['start']}-{r['end']} {r['mutant']}"})
    return open_, argued


def scan(cache: ClaimCache, root: Path) -> dict:
    """{nodes: [{node, events}], proposals: [{id, rule, nodes}], argued: [{node, detail}]}."""
    cache.update()
    failing = {e.id: blame(root, e) for e in failing_edges(cache)}
    nodes = sorted(cache.store.all_nodes().items())
    rows, argued = [], []
    owned = {n: owned_files(cache, root, n, info) for n, info in nodes}
    for node, info in nodes:
        if ev := events(cache, root, node, info, failing):
            rows.append({"node": node, "events": ev})
        argued += [{"node": node, **a} for a in survivors(cache, node)[1]]
    proposals = []
    for e in cache.invariants(("proposed",)):
        touched = [n for n, files in owned.items() if any(affects(dependencies(e), f) for f in files)]
        if touched:
            proposals.append({"id": e.id, "rule": e.post[:100], "nodes": touched})
    return {"nodes": rows, "proposals": proposals, "argued": argued}


def due(cache: ClaimCache, root: Path) -> list[dict]:
    """Every registered node with at least one event, as {node, events: [{kind, detail}]}."""
    return scan(cache, root)["nodes"]


def render(r: dict) -> str:
    out = [f"{len(r['nodes'])} node(s) due an activation:", ""] if r["nodes"] else ["No node is due an activation."]
    for row in r["nodes"]:
        out.append(f"{row['node']}")
        out += [f"  {e['kind']:9} {e['detail']}" for e in row["events"]]
    if r["proposals"]:
        out += ["", f"{len(r['proposals'])} proposal(s) awaiting a human (`fractal invariants --state proposed`):"]
        out += [f"  {p['id']}  touches {len(p['nodes'])} node(s): {p['rule']}" for p in r["proposals"]]
    if r["argued"]:
        out += ["", f"{len(r['argued'])} survivor(s) carry an equivalence argument but are recorded `survived`; "
                    "record them with `fractal ledger record --result equivalent --argument ...`:"]
        out += [f"  {a['node']}  {a['detail']}" for a in r["argued"][:10]]
    return "\n".join(out) + "\n"

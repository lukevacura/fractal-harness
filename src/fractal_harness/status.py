"""A node's state, derived from the ledger; never marked.

A hand-set "Done" is an unjudged claim: nothing checks it, so it goes stale silently. Here the state is a
pure function of what is recorded and what is true now (verdicts, the digests they rest on, the activation
events), computed the same way `activations` decides that a node is due:

  unregistered  the node is not in the ledger (no spec registered under that name)
  pending       registered but never verified: no verdicts recorded, or a required mutant is missing
  due           an activation event is pending (spec, contract, probe, or an unresolved survivor: see activation.py)
  stale         no event is pending, but a recorded verdict no longer holds (lines of its span or a killing
                test changed since the commit it was verified at)
  done          none of the above: verdicts recorded, all valid at the current premise digests, every
                surviving mutant carries an equivalence argument

Order of precedence is the order above. SOUND (may gate): the state comes from verdicts and digests only.
Early cutoff is inherited, not added: a parent's implementation change that leaves its contract digest
alone raises no event on its dependents, so they stay `done`; the parent itself goes `stale` only if the
change touched lines its own verdicts cover.

HEURISTIC (warn only): `legacy_marks` reads a hand-maintained markdown table to show where a human claim
disagrees with the derived state. Its parsing is a guess at table layout; it never changes a state.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import activation, ledger
from .cache import ClaimCache
from .context import premises_of
from .nodes import contract_digest
from .symbols import SymbolError

STATES = ("done", "due", "stale", "pending", "unregistered")


def gather(cache: ClaimCache, root: Path, node: str, failing: dict) -> dict:
    """Every fact the state depends on, nothing decided yet."""
    info = cache.store.get_node(node)
    if info is None:
        return {"node": node, "registered": False}
    f: dict = {"node": node, "registered": True, "spec": info["spec"], "premises": premises_of(info),
               "verdicts": len(cache.store.mutants(node)), "events": activation.events(cache, root, node, info, failing),
               "argued": [a["detail"] for a in activation.survivors(cache, node)[1]],
               "stale": [], "valid": [], "required": [], "node_reason": None}
    if f["verdicts"]:
        p = ledger.plan(cache, root, node)
        f.update(stale=[(f"{r['file']}:{r['start']}-{r['end']} {r['mutant']}", why) for r, why in p["stale"]],
                 valid=p["valid"], required=p["required"], node_reason=p["node_reason"], base=p["base"])
    return f


def classify(f: dict) -> tuple[str, list[str]]:
    """(state, reasons) from gathered facts. Pure."""
    if not f["registered"]:
        return "unregistered", [f"{f['node']} is not registered (`fractal ledger node {f['node']} --spec <spec>`)"]
    if not f["verdicts"]:
        return "pending", ["no verdicts recorded (`fractal ledger record`)"]
    if f["events"]:
        return "due", [f"{e['kind']}: {e['detail']}" for e in f["events"]]
    if f["node_reason"] or f["stale"]:
        why = [f["node_reason"]] if f["node_reason"] else []
        return "stale", why + [f"{m}: {r}" for m, r in f["stale"][:5]] + (
            [f"... and {len(f['stale']) - 5} more"] if len(f["stale"]) > 5 else [])
    if f["required"]:
        return "pending", [f"required mutant missing: {f['required'][0]}"]
    return "done", ["every recorded verdict is valid at the current premise digests; no pending activation event"]


def evidence(cache: ClaimCache, root: Path, f: dict) -> dict:
    if not f["registered"]:
        return {}
    by: dict[str, int] = {}
    for r in f["valid"]:
        by[r["result"]] = by.get(r["result"], 0) + 1
    parents = {}
    for p in sorted(f["premises"]["parents"]):
        pinfo = cache.store.get_node(p)
        try:
            parents[p] = contract_digest(root, pinfo["spec"])[:12] if pinfo else "unregistered"
        except SymbolError:
            parents[p] = "unreadable"
    return {"spec": f["spec"], "valid_verdicts": dict(sorted(by.items())), "stale_verdicts": len(f["stale"]),
            "verified_at": (f.get("base") or "")[:10] or None, "decisions": sorted(f["premises"]["decisions"]),
            "parent_contracts": parents, "argued_survivors": f["argued"]}


def would_change(state: str, f: dict) -> list[str]:
    spec = f.get("spec", "<spec>")
    n = f["node"]
    if state == "unregistered":
        return [f"register it: `fractal ledger node {n} --spec <spec>`"]
    if state == "pending":
        return ["record verdicts at its commit: `fractal ledger record` (or `fractal mutate`)"]
    if state in ("due", "stale"):
        return [f"re-verify per `fractal verify-plan {n}`, record the verdicts, then re-register with "
                f"`fractal ledger node {n} --spec {spec}`"]
    out = [f"a change to its spec's contract or design points ({spec})"]
    out += [f"a change to parent {p}'s contract (guarantee, condition or exports; not its implementation)"
            for p in sorted(f["premises"]["parents"])]
    out += [f"a change to decision claim {d}" for d in sorted(f["premises"]["decisions"])]
    return out + ["an edit to lines a verdict covers, or a failing probe on its files, or a new surviving mutant"]


def derive(cache: ClaimCache, root: Path, node: str, failing: dict | None = None) -> dict:
    f = gather(cache, root, node, activation_failing(cache, root) if failing is None else failing)
    state, reasons = classify(f)
    return {"node": node, "state": state, "reasons": reasons, "evidence": evidence(cache, root, f),
            "would_change": would_change(state, f)}


def activation_failing(cache: ClaimCache, root: Path) -> dict:
    return {e.id: activation.blame(root, e) for e in activation.failing_edges(cache)}


def derive_all(cache: ClaimCache, root: Path) -> dict[str, dict]:
    """{node: derived} for every registered node, ordered by name."""
    cache.update()
    failing = activation_failing(cache, root)
    return {n: derive(cache, root, n, failing) for n in sorted(cache.store.all_nodes())}


# --- legacy hand-set marks (heuristic) ------------------------------------------------------

DONE_WORDS = re.compile(r"^(done|complete|completed|finished|✅|✔|✓|\[x\])$", re.I)


def legacy_marks(text: str, nodes: set[str]) -> dict[str, bool]:
    """{node: claimed done} from the rows of a markdown table whose first cell names a registered node.
    HEURISTIC: layout is guessed (a cell equal to done/complete/✅ means claimed done, any other row of
    that node means not), so a disagreement is something to look at, never a verdict."""
    out: dict[str, bool] = {}
    for line in text.splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cells = [re.sub(r"[`*_]|\[([^\]]*)\]\([^)]*\)", r"\1", c).strip() for c in line.strip().strip("|").split("|")]
        if cells and cells[0] in nodes:
            out[cells[0]] = any(DONE_WORDS.match(c) for c in cells[1:])
    return out


def disagreements(marks: dict[str, bool], derived: dict[str, dict]) -> list[dict]:
    out = []
    for n in sorted(marks):
        d = derived[n]["state"]
        if marks[n] != (d == "done"):
            out.append({"node": n, "marked": "done" if marks[n] else "not done", "derived": d,
                        "why": derived[n]["reasons"][0]})
    return out


def render(rows: list[dict], disagree: list[dict] | None = None, verbose: bool = False) -> str:
    out = []
    for d in rows:
        out.append(f"{d['node']}: {d['state']}")
        out += [f"  why: {r}" for r in d["reasons"]]
        if verbose:
            ev = d["evidence"]
            if ev:
                out.append(f"  evidence: spec {ev['spec']}; valid verdicts {ev['valid_verdicts'] or 'none'}"
                           + (f" at {ev['verified_at']}" if ev["verified_at"] else "")
                           + f"; decisions {ev['decisions'] or 'none'}; parent contracts {ev['parent_contracts'] or 'none'}")
                if ev["argued_survivors"]:
                    out.append(f"  note: {len(ev['argued_survivors'])} survivor(s) rest on an equivalence argument")
            out += [f"  would change if: {w}" for w in d["would_change"]]
    if not rows:
        out.append("no registered nodes")
    if disagree:
        out += ["", f"{len(disagree)} legacy mark(s) disagree with the derived state (heuristic read of the marks file):"]
        out += [f"  {x['node']}: marked {x['marked']}, derived {x['derived']} ({x['why']})" for x in disagree]
    return "\n".join(out) + "\n"

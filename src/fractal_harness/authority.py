"""Layered authority for invariants: the human (`root`) is the base case, not the per-rule gate.

Every enforced rule has an *authority*: `root` (a human accepted it) or a node id (an agent accepted it locally,
`fractal accept <id> --as <node>`, only where `accept_policy` allows). Layers form the node DAG: a node's parents
(the nodes whose guarantees it builds on, from its registered premises) are above it; `root` is above everything.

One law (`judge`) gates every change an agent makes to the rule set: accept --as, override --by, retire, conflict
narrow, ratchet, `fractal change --by`. It is stated on the Delta (loss.py), the change in what the rules forbid:
  no loss    nothing that was forbidden is let through, precedence included.                                SOUND
             (an uncomparable part, a command or test leaf, counts as let through)
  standing   to drop or reshape a rule the actor holds it or ranks above it, under accept_policy local; never a
             root rule. A pure ratchet (debt bounds moved toward their ruled target) needs none.
  region     every rule a node holds after the change depends only on files the node speaks for, and on some.  SOUND
             for the tree now (a glob is expanded over today's files; `check` and the hooks do not re-test it)
Anything else is the root's: the human, in their terminal, shown the same Delta.

A local accept also needs a valid probe (it runs, passes now and checks something: SOUND) and must not contradict an
enforced rule of a higher layer (it would be set aside on arrival). Contradiction is loss.contradiction:
  SOUND      two counted parts with the same key whose file PATTERNS nest and whose bounds cannot both hold.
  HEURISTIC  same key, incompatible bounds, overlapping but not nested files (today's tree): warned, never refused.
A mutation audit is advisory: with `local_with_audit` it is run and recorded, never a gate.

Survival is not evidence: nothing here promotes or expires a rule. A local rule stays enforced@node until changed by
the law or by the human (`override` demotes and keeps the history; `override --undo` restores it).

Precedence (`effective`): where an enforced rule of a higher layer SOUNDLY contradicts an enforced rule of a lower
one, the lower is set aside for that conflict; the higher one governs. Heuristic conflicts set nothing aside.
With no non-root rule enforced, `effective` is the identity (the default behaviour is unchanged).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from . import probes
from .cache import CacheError, ClaimCache, dependencies
from .regions import affects, is_glob, tracked_files
from .store import ROOT, Edge

ACCEPT_LOCAL = ("local", "local_with_audit")


class AuthorityError(CacheError):
    pass


# --- the layer order -------------------------------------------------------------------------------

def ancestors(nodes: dict[str, dict], node: str) -> set[str]:
    """Every node above `node` (its parents, transitively). Cycle-safe."""
    from .context import premises_of
    seen: set[str] = set()
    todo = [node]
    while todo:
        n = todo.pop()
        for p in premises_of(nodes.get(n, {"premises": {}}))["parents"]:
            if p not in seen:
                seen.add(p)
                todo.append(p)
    return seen


def outranks(a: str, b: str, nodes: dict[str, dict]) -> bool:
    """Does layer `a` sit strictly above layer `b`? root is above every node; a node is above its descendants."""
    if a == b:
        return False
    return a == ROOT or (b != ROOT and a in ancestors(nodes, b))


# --- scope -----------------------------------------------------------------------------------------

def scope_of(deps: list[str], files: list[str]) -> set[str]:
    """The files a rule's verdict depends on: the tree's files its paths cover, plus literal paths that do not exist
    (a rule about a file that is not there yet still names it)."""
    out = {f for f in files if affects(deps, f)}
    out |= {d.rstrip("/") for d in deps if not is_glob(d) and not any(f == d.rstrip("/") or f.startswith(d.rstrip("/") + "/")
                                                                    for f in files)}
    return out


def region_of(cache: ClaimCache, root: Path, node: str) -> set[str]:
    """The files `node` speaks for: its own and every descendant's (a node is the root of its subtree)."""
    from .activation import owned_files
    info = cache.store.get_node(node)
    if info is None:
        raise AuthorityError(f"{node!r} is not a registered node (see `fractal ledger node`)")
    nodes = cache.store.all_nodes()
    files = set(owned_files(cache, root, node, info))
    for d in descendants(nodes, node):
        if (di := cache.store.get_node(d)) is not None:
            files |= set(owned_files(cache, root, d, di))
    return files


def descendants(nodes: dict[str, dict], node: str) -> set[str]:
    return {n for n in nodes if n != node and node in ancestors(nodes, n)}


# --- conflicts -------------------------------------------------------------------------------------

from .loss import Conflict, _leaves, contradiction  # noqa: E402  one comparison engine: atoms over glob patterns (loss.py)


def shadowed(edges: list[Edge], nodes: dict[str, dict], files: list[str]) -> dict[str, tuple[Edge, Conflict]]:
    """{lower rule id: (the higher enforced rule that soundly contradicts it, the conflict)}."""
    enforced = [e for e in edges if e.enforced]
    out: dict[str, tuple[Edge, Conflict]] = {}
    for lo in enforced:
        for hi in enforced:
            if outranks(hi.layer, lo.layer, nodes) and (c := contradiction(hi.probe, lo.probe, files)) and c.sound:
                out.setdefault(lo.id, (hi, c))
    return out


def effective(cache: ClaimCache, edges: list[Edge]) -> tuple[list[Edge], dict[str, list[Edge]]]:
    """(`edges` with the rules a higher layer soundly contradicts set aside, {governing rule id: rules it set aside}).
    Identity when every enforced rule is the root's (no node data is even read)."""
    if all(e.layer == ROOT for e in edges if e.enforced):
        return edges, {}
    sh = shadowed(edges, cache.store.all_nodes(), tracked_files(cache.root))
    overrides: dict[str, list[Edge]] = {}
    for lo_id, (hi, _) in sh.items():
        overrides.setdefault(hi.id, []).append(next(e for e in edges if e.id == lo_id))
    return [e for e in edges if e.id not in sh], overrides


def overrides_note(set_aside: list[Edge]) -> str:
    return "\n".join(f"    overrides {e.layer}'s rule [{e.id}] \"{e.post[:80]}\" where they conflict (higher layer wins)"
                     for e in set_aside)


# --- the law ---------------------------------------------------------------------------------------------------
#
# One law gates every change an agent makes to the rule set (accept --as, override --by, retire, narrow, ratchet):
#   1. no loss       nothing that was forbidden is let through (loss.change: the Delta, precedence included).  SOUND
#                    (a part that cannot be compared, a command or test leaf, counts as let through)
#   2. standing      to drop or reshape a rule, the actor holds it or ranks above it, under accept_policy local.
#                    A pure ratchet needs none: it only moves debt bounds toward their ruled target (the
#                    information was ruled when the debt rule was accepted; any actor may enforce more of it).
#   3. region        every rule a node holds after the change depends only on files that node speaks for
#                    (region_of), and on at least one (a rule over nothing, or repo-wide, is the root's).   SOUND
#                    for the tree now: a glob is expanded over today's files
# Anything else is the root's: the human, in their terminal, shown the same Delta.

def _shape(probe: dict | None) -> list[dict]:
    return [{k: v for k, v in leaf.items() if k not in ("expect", "debt_target")} for leaf in _leaves(probe)]


def _pure_ratchet(before: dict | None, after: dict | None) -> bool:
    """Only the bounds of debt leaves change, none below its target. Which way they move is clause 1's business."""
    from .debt import outstanding
    if after is None or not outstanding(before) or _shape(before) != _shape(after):
        return False
    for b, a in zip(_leaves(before), _leaves(after)):
        if b == a:
            continue
        if "debt_target" not in b:
            return False
        target = probes._expectation(b["debt_target"])[1]
        if a.get("debt_target", b["debt_target"]) != b["debt_target"] and "debt_target" in a:
            return False
        now = probes._expectation(a["expect"])
        if a.get("expect") == b["debt_target"] and "debt_target" not in a:
            continue                                     # paid off: back to the target
        if "debt_target" not in a or now[0] != "max" or now[1] < target:
            return False                 # raising the bound is not refused here: it is a loss (clause 1 refuses it)
    return True


def judge(cache: ClaimCache, actor: str, edits: dict[str, dict | None], add: list[Edge] = (),
          policy: str = "human_only", what: str = "this change", human: bool = False):
    """Apply the law to a change by `actor`: `edits` {rule id: new probe, or None to stop enforcing it}, `add` new
    rules (with their authority set). Returns the Delta; raises AuthorityError naming the clause that fails.
    The root is not gated, but only a caller holding the human's confirmation may say so (`human=True`): an agent
    naming itself root is refused, never trusted."""
    from dataclasses import replace
    from .loss import change
    d = change(cache, edits, add)
    if actor == ROOT:
        if not human:
            raise AuthorityError("refused: acting as root is the human's, confirmed in their terminal "
                                 "(`--as root` / `--by root` without it is not the human)")
        return d
    rules = {eid: cache.store.get(eid) for eid in edits}
    for eid, e in rules.items():
        if e is None or not e.enforced:
            raise AuthorityError(f"{eid} is not an enforced rule")
    ratchet = not add and edits and all(_pure_ratchet(rules[i].probe, p) for i, p in edits.items())
    if ratchet:     # it enforces what was ruled, but only as far as the code already complies: never breaks the tree
        for i, p in edits.items():
            if not probes.run(p, cache.root).passed:
                raise AuthorityError(f"refused: the ratcheted [{i}] would not hold now: a bound goes below today's "
                                     "count. Fix the code first")
    nodes = cache.store.all_nodes()
    if not ratchet:                                                     # 2. standing
        if policy not in ACCEPT_LOCAL:
            raise AuthorityError(f"refused: an agent changing rules needs accept_policy local (it is {policy!r}); "
                                 "only a human may")
        if actor not in nodes:
            raise AuthorityError(f"{actor!r} is not a registered node")
        for eid, e in rules.items():
            if not (e.layer == actor or outranks(actor, e.layer, nodes)):       # never a root rule: no node outranks root
                if e.layer == ROOT:
                    raise AuthorityError(f"[{eid}] is a root rule: only the human changes it ({actor} does not "
                                         "outrank root); `fractal conflict raise` files a request")
                raise AuthorityError(f"{actor} is neither {e.layer} nor above it (it does not outrank {e.layer}, "
                                     f"which holds [{eid}])")
    if not d.lossless:                                                  # 1. no loss
        shadow = sorted({p.rule for p in d.allows if p.rule not in edits})   # still enforced: set aside by precedence
        how = "".join(f"would set aside {cache.store.get(r).layer}'s rule [{r}] by precedence; " for r in shadow)
        raise AuthorityError(f"refused: {what} {how}is a global loss ({d.loss_why()}). Only a human may let these "
                             f"states through: `fractal conflict raise <rule> --reason ...` files it for arbitration")
    files = tracked_files(cache.root)                                   # 3. region
    held = [*(replace(rules[i], probe=p) for i, p in edits.items() if p is not None), *add]
    for e in held:
        if e.layer == ROOT:
            continue
        scope = scope_of(dependencies(e), files)
        if not scope:
            raise AuthorityError(f"scope gate: [{e.id}] depends on no file (repo-wide or matching nothing): human-only")
        outside = sorted(scope - region_of(cache, cache.root, e.layer))
        if outside:
            raise AuthorityError(f"scope gate: [{e.id}] reaches {len(outside)} file(s) {e.layer} does not own "
                                 f"(e.g. {outside[0]}): a rule wider than the node is human-only")
    return d


# --- accept / override -------------------------------------------------------------------------------

def record_human_accept(cache: ClaimCache, eid: str) -> None:
    """A human enforced `eid` (`fractal accept`): it is the root's. Informational provenance, no penalty."""
    from .rules import human_set
    human_set(cache, eid, "enforced")


def accept_as(cache: ClaimCache, root: Path, eid: str, node: str, policy: str) -> dict:
    """Locally enforce a proposed rule for `node`. Raises AuthorityError (naming the gate) when a gate fails.
    Returns {edge, warnings, audit}."""
    if node == ROOT:
        raise AuthorityError("refused: accepting as root is the human's decision (`fractal accept <id>`, confirmed "
                             "in their terminal), not an agent's `--as root`")
    if policy not in ACCEPT_LOCAL:
        raise AuthorityError(f"refused: accept_policy is {policy!r}; only a human may accept a rule "
                             f"(allow node-local rules with accept_policy = \"local\" in fractal.toml)")
    e = cache.store.get(eid)
    if e is None or e.kind != "invariant":
        raise AuthorityError(f"{eid} is not an invariant")
    if e.rule_state != "proposed":
        raise AuthorityError(f"{eid} is {e.rule_state}, not proposed: an agent can only accept a proposal "
                             "(a rejected or overridden rule is a higher layer's decision)")
    warnings: list[str] = []
    files = tracked_files(cache.root)
    scope = scope_of(dependencies(e), files)
    # the law: a node holds only rules over its own region, and no loss (a rule that sets a lower one aside by
    # precedence lets through what that one forbade)
    from dataclasses import replace
    try:
        judge(cache, node, {}, [replace(e, rule_state="enforced", authority=node)], policy, f"{node} accepting [{eid}]")
    except AuthorityError as x:
        raise AuthorityError(str(x).replace("refused: ", "shadow gate: ", 1)) from x
    # probe: runs, passes, checks something
    if not e.probe:
        raise AuthorityError(f"probe gate: {eid} has no probe; nothing would check it")
    try:
        r = probes.run(e.probe, cache.root)
    except (probes.ProbeError, OSError) as x:
        raise AuthorityError(f"probe gate: the probe does not run: {x}") from x
    if not r.passed:
        raise AuthorityError(f"probe gate: the rule is violated now ({r.detail}); fix the code or ask a human")
    if r.vacuous:
        raise AuthorityError(f"probe gate: the probe checks nothing ({r.detail[len(probes.VACUOUS):]})")
    # conflict: against every enforced rule of a higher layer
    nodes = cache.store.all_nodes()
    for hi in (x for x in cache.store.all() if x.enforced and outranks(x.layer, node, nodes)
               and scope_of(dependencies(x), files) & scope):
        c = contradiction(hi.probe, e.probe, files)
        if c is None:
            continue
        if c.sound:
            raise AuthorityError(f"conflict gate: contradicts {hi.layer}'s rule [{hi.id}] \"{hi.post[:80]}\": {c.why}")
        warnings.append(f"may conflict with {hi.layer}'s rule [{hi.id}]: {c.why} (heuristic)")
    for lo in (x for x in cache.store.all() if x.enforced and outranks(node, x.layer, nodes)
               and scope_of(dependencies(x), files) & scope):
        c = contradiction(e.probe, lo.probe, files)
        if c is not None and not c.sound:
            warnings.append(f"may conflict with {lo.layer}'s rule [{lo.id}]: {c.why} (heuristic)")
    audit = None
    if policy == "local_with_audit":
        from .audit import audit_edge
        a = audit_edge(e, cache.root)
        audit = {"verdict": a.verdict, "findings": a.findings}
    prov = {"by": "agent", "node": node, "ts": time.time(), "evidence": probes.evidence_class(e.probe),
            "policy": policy, **({"audit": audit} if audit else {})}
    from .rules import transition
    transition(cache, eid, "enforced", authority=node, actor=node, action="accept", provenance=prov,
               detail={**prov, "warnings": warnings})
    return {"edge": cache.store.get(eid), "warnings": warnings, "audit": audit}


def retire(cache: ClaimCache, eid: str, by: str, reason: str, policy: str = "human_only") -> Edge:
    """Retire an enforced rule that loses nothing: every state it forbids is still forbidden by other in-effect rules
    (reclamation of the rule set). A node rule may be retired by its own layer or one above it (no higher layer is
    needed: nothing is let through); a root rule is the human's ruling, kept as precedent, so only `fractal override`
    by the human retires it. Demoted to `overridden` with history; `fractal override --undo` restores it."""
    if not reason.strip():
        raise AuthorityError("a reason is required")
    e = cache.store.get(eid)
    if e is None or not e.enforced:
        raise AuthorityError(f"{eid} is not an enforced rule")
    if e.layer == ROOT:
        raise AuthorityError(f"[{eid}] is a root rule: retiring it is the human's decision "
                             f"(`fractal override {eid} --reason ...` in their terminal says whether it loses information)")
    judge(cache, by, {eid: None}, policy=policy, what=f"{by} retiring [{eid}]")
    from .rules import transition
    return transition(cache, eid, "overridden", authority=e.layer, actor=by, action="retire", reason=reason)


def _check_higher(cache: ClaimCache, e: Edge, by: str, policy: str) -> None:
    nodes = cache.store.all_nodes()
    if by != ROOT:
        if policy not in ACCEPT_LOCAL:
            raise AuthorityError(f"refused: --by {by} needs accept_policy local (it is {policy!r}); a human overrides with --by root")
        if by not in nodes:
            raise AuthorityError(f"{by!r} is not a registered node")
    if not outranks(by, e.layer, nodes):
        raise AuthorityError(f"{by} does not outrank {e.layer}, which holds {e.id}: only a higher layer can change it")


def override(cache: ClaimCache, eid: str, by: str, reason: str, policy: str = "human_only", undo: bool = False) -> Edge:
    """Demote an enforced rule to `overridden` (or, with `undo`, restore it): who/when/why go to the history; nothing
    is deleted. Only a layer above the rule's authority may; root may override anything."""
    if not reason.strip():
        raise AuthorityError("a reason is required")
    e = cache.store.get(eid)
    if e is None or e.kind != "invariant":
        raise AuthorityError(f"{eid} is not an invariant")
    want = "overridden" if undo else "enforced"
    if e.rule_state != want:
        raise AuthorityError(f"{eid} is {e.rule_state}; only {'an overridden' if undo else 'an enforced'} rule can be "
                             f"{'restored' if undo else 'overridden'}")
    if undo:
        last = next((h for h in reversed(cache.store.rule_history(eid))
                     if h["detail"].get("to") == "overridden"), None)
        if by != ROOT and last is not None and last["who"] in (ROOT, "human"):
            raise AuthorityError(f"refused: [{eid}] was demoted by the human; only the human restores it")
        if not (by == ROOT and e.layer == ROOT):
            _check_higher(cache, e, by, policy)
    elif by != ROOT:
        judge(cache, by, {eid: None}, policy=policy, what=f"{by} overriding [{e.id}]")
    from .rules import transition
    return transition(cache, eid, "enforced" if undo else "overridden", authority=e.layer, actor=by,
                      action="restore" if undo else "override", reason=reason)

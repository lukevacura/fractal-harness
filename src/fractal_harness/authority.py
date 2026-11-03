"""Layered authority for invariants: the human (`root`) is the base case, not the per-rule gate.

Every enforced rule has an *authority*: `root` (a human accepted it) or a node id (an agent accepted it locally,
`fractal accept <id> --as <node>`, only where `accept_policy` allows). Layers form the node DAG: a node's parents
(the nodes whose guarantees it builds on, from its registered premises) are above it; `root` is above everything.

Gates for a local accept. Each is deterministic and says what it can and cannot prove:
  probe     the probe exists, runs, passes now, and checks something (not vacuous).                       SOUND
  scope     every file the rule depends on is a file the node owns (activation.owned_files). A rule that
            matches nothing, or is repo-wide, or reaches past the node, stays human-only.                  SOUND
            (a glob is expanded over the files in the tree now; the rule can later match more files, and
            `fractal check`/the hooks do not re-test scope)
  conflict  the rule does not contradict an enforced rule held by a higher layer.
              SOUND      both are grep probes with the same pattern whose scopes (as file sets in the tree now)
                         are nested or equal, and no count satisfies both expectations: refused.
              HEURISTIC  same pattern, overlapping but not nested scopes, one demanding none and one some:
                         warned, never refused. Rules of other probe types are not compared: warned.
A mutation audit is advisory: with `local_with_audit` it is run and recorded, never a gate.

Survival is not evidence: nothing here promotes or expires a rule. A local rule stays enforced@node until a higher
layer changes it (`override`, which demotes and keeps the history; `override --undo` restores it).

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
from .regions import affects, is_glob, matches, tracked_files
from .store import ROOT, Edge

ACCEPT_LOCAL = ("local", "local_with_audit")
INF = float("inf")


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
    from .activation import owned_files
    info = cache.store.get_node(node)
    if info is None:
        raise AuthorityError(f"{node!r} is not a registered node (see `fractal ledger node`)")
    return set(owned_files(cache, root, node, info))


# --- conflicts -------------------------------------------------------------------------------------

@dataclass
class Conflict:
    sound: bool
    why: str


def _leaves(probe: dict | None) -> list[dict]:
    if not probe:
        return []
    if probe.get("type") == "all":
        return [x for p in probe["probes"] for x in _leaves(p)]
    return [probe]


def _interval(leaf: dict) -> tuple[float, float]:
    op, n = probes._expectation(leaf.get("expect", "present"))
    return {"count": (n, n), "min": (n, INF), "max": (0, n)}[op]


def _grep_files(leaf: dict, files: list[str]) -> set[str]:
    include = [g for g in leaf["paths"] if not g.startswith("!")]
    skip = [*leaf.get("exclude_paths", []), *(g[1:] for g in leaf["paths"] if g.startswith("!"))]
    return {f for f in files if any(matches(g, f) for g in include) and not any(matches(g, f) for g in skip)}


def contradiction(hi: dict | None, lo: dict | None, files: list[str]) -> Conflict | None:
    """Does a higher layer's probe contradict a lower layer's? None when none is found. See the module doc for
    which answers are sound."""
    unseen = False
    for a in _leaves(hi):
        for b in _leaves(lo):
            if a.get("type") != "grep" or b.get("type") != "grep":
                unseen = True
                continue
            if a["pattern"] != b["pattern"] or a.get("exclude") != b.get("exclude"):
                continue
            fa, fb = _grep_files(a, files), _grep_files(b, files)
            if not fa or not fb or not fa & fb:
                continue
            (alo, ahi), (blo, bhi) = _interval(a), _interval(b)
            pat = f"pattern {a['pattern']!r}"
            if fa == fb and (max(alo, blo) > min(ahi, bhi)):
                return Conflict(True, f"same files, {pat}: expectations {a.get('expect', 'present')} and {b.get('expect', 'present')} cannot both hold")
            if fb < fa and blo > ahi:        # b's files are a subset of a's: count_b <= count_a
                return Conflict(True, f"{pat} is required in files the higher rule limits ({a.get('expect', 'present')})")
            if fa < fb and alo > bhi:
                return Conflict(True, f"{pat} is required in files the lower rule limits ({b.get('expect', 'present')})")
            if max(alo, blo) > min(ahi, bhi) and fa != fb and not (fa < fb or fb < fa):
                return Conflict(False, f"{pat}: overlapping but not nested scopes with expectations "
                                f"{a.get('expect', 'present')} and {b.get('expect', 'present')}; they may conflict")
    if unseen and any(True for _ in _leaves(hi)) and any(True for _ in _leaves(lo)):
        return Conflict(False, "a non-grep probe is involved; conflicts are only compared between grep probes")
    return None


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


# --- accept / override -------------------------------------------------------------------------------

def record_human_accept(cache: ClaimCache, eid: str) -> None:
    """A human enforced `eid` (`fractal accept`): it is the root's. Informational provenance, no penalty."""
    e = cache.store.get(eid)
    prov = {"by": "human", "node": None, "ts": time.time(), "evidence": probes.evidence_class(e.probe)}
    cache.store.set_authority(eid, "enforced", ROOT, prov)
    cache.store.history_add(eid, "accept", "human", "", prov)


def accept_as(cache: ClaimCache, root: Path, eid: str, node: str, policy: str) -> dict:
    """Locally enforce a proposed rule for `node`. Raises AuthorityError (naming the gate) when a gate fails.
    Returns {edge, warnings, audit}."""
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
    # scope: the rule's files must all be the node's
    owned = region_of(cache, root, node)
    files = tracked_files(cache.root)
    scope = scope_of(dependencies(e), files)
    if not scope:
        raise AuthorityError(f"scope gate: {eid} depends on no file (repo-wide or matching nothing): human-only")
    outside = sorted(scope - owned)
    if outside:
        raise AuthorityError(f"scope gate: {eid} reaches {len(outside)} file(s) {node} does not own "
                             f"(e.g. {outside[0]}): a rule wider than the node is human-only")
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
    audit = None
    if policy == "local_with_audit":
        from .audit import audit_edge
        a = audit_edge(e, cache.root)
        audit = {"verdict": a.verdict, "findings": a.findings}
    prov = {"by": "agent", "node": node, "ts": time.time(), "evidence": probes.evidence_class(e.probe),
            "policy": policy, **({"audit": audit} if audit else {})}
    cache.store.set_authority(eid, "enforced", node, prov)
    cache.store.history_add(eid, "accept", node, "", {**prov, "warnings": warnings})
    cache.store.log("rule", eid, state="enforced", authority=node)
    return {"edge": cache.store.get(eid), "warnings": warnings, "audit": audit}


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
    if not (by == ROOT and e.layer == ROOT):      # the human changing their own rule needs no higher layer
        _check_higher(cache, e, by, policy)
    new = "enforced" if undo else "overridden"
    cache.store.set_authority(eid, new, e.layer)
    cache.store.history_add(eid, "restore" if undo else "override", by, reason,
                            {"from": e.rule_state, "to": new, "authority": e.layer})
    cache.store.log("rule", eid, state=new, by=by)
    return cache.store.get(eid)

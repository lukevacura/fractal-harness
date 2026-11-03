"""Contradiction as the signal: a wrong local rule shows up as a `conflict` that the layer above handles.

Detection (derived on demand, never stored; both are HEURISTIC and only raise events, they never block or decide):
  overlap   a local enforced rule heuristically overlaps/contradicts a higher layer's enforced rule (authority.contradiction,
            sound and heuristic findings alike; the "a non-grep probe is involved" non-comparison is not an event)
  friction  the same local rule blocked work (pre-edit hook, `fractal check`, Stop hook; see blocklog.py) at least
            [policy] friction_threshold times within friction_window_days. Counts come from a capped history.
  spec      raised explicitly (`fractal conflict raise <claim|rule>`): the spec/premise is wrong or ambiguous, so the
            probe must not be healed; the parent decides. Raising never edits the claim or its probe.

Who handles it (`handler`): the parent of the rule's authority (the first registered parent, by name), or `root`'s queue
when it has none. Like every activation event it says a decision is due, never what it should be. Decisions are rows in
the `conflicts` table plus a `rule_history` entry (who, why):
  keep       the rule stays as is; the event is quiet until new friction (overlap: until the rule changes)
  narrow     add exclude_paths to the local rule's grep probe(s); refused if it would leave the probe checking nothing
  override   demote the local rule (authority.override), history kept
  escalate   hand it one layer up; a conflict that involves a root-held rule, or that reaches root, becomes an
             arbitration request (`awaiting_human`) with the written reason
Bubbling stops at a decision or at root. A human is required only at root and for nodes listed in `[policy] human_nodes`.

Root (human) rules are paramount: no action here changes, narrows or overrides a root-held rule unless a human does it.
An agent that disagrees with a root rule files an arbitration request; only a human (TTY or --yes) arbitrates.
Identity of an "agent" is not authenticated (same boundary as `accept --as`): the guard is the CLI/TTY check and the
layer arithmetic, not a credential.
"""

from __future__ import annotations

import copy
import hashlib
import time
from pathlib import Path

from . import probes
from .authority import AuthorityError, contradiction, outranks
from .cache import ClaimCache
from .context import premises_of
from .regions import tracked_files
from .store import ROOT

CLOSED = ("kept", "narrowed", "overridden", "upheld", "revised")
ACTIONS = ("keep", "narrow", "override")


class ConflictError(AuthorityError):
    pass


def cid_of(kind: str, rule: str, other: str = "") -> str:
    return "c-" + hashlib.sha1(f"{kind}|{rule}|{other}".encode()).hexdigest()[:10]


def parent_of(nodes: dict[str, dict], authority: str) -> str:
    """The layer above `authority`: its first registered parent (by name), else root."""
    if authority == ROOT or authority not in nodes:
        return ROOT
    for p in sorted(premises_of(nodes[authority])["parents"]):
        if p in nodes:
            return p
    return ROOT


def needs_human(handler: str, human_nodes) -> bool:
    return handler == ROOT or handler in human_nodes


def _cfg(root: Path):
    from .config import load
    return load(root)


# --- detection ---------------------------------------------------------------------------------

def detect(cache: ClaimCache, root: Path, now: float | None = None) -> list[dict]:
    """Every conflict the current state implies, before any decision is applied. HEURISTIC."""
    now = time.time() if now is None else now
    cfg = _cfg(root)
    enforced = [e for e in cache.store.all() if e.kind == "invariant" and e.enforced]
    local = [e for e in enforced if e.layer != ROOT]
    if not local:
        return []
    nodes = cache.store.all_nodes()
    files = tracked_files(cache.root)
    since = now - cfg.friction_window_days * 86400
    out = []
    for lo in local:
        handler = parent_of(nodes, lo.layer)
        for hi in enforced:
            if hi.id != lo.id and outranks(hi.layer, lo.layer, nodes):
                c = contradiction(hi.probe, lo.probe, files)
                if c and c.kind != "unseen":
                    out.append({"id": cid_of("overlap", lo.id, hi.id), "kind": "overlap", "rule": lo.id, "other": hi.id,
                                "handler": handler, "sound": c.sound,
                                "why": f"{lo.layer}'s rule overlaps {hi.layer}'s rule [{hi.id}]: {c.why} "
                                       f"({'sound' if c.sound else 'heuristic'})"})
        n = cache.store.block_count(lo.id, since)
        out.append({"id": cid_of("friction", lo.id), "kind": "friction", "rule": lo.id, "other": None, "handler": handler,
                    "count": n, "why": f"blocked work {n}x in {cfg.friction_window_days:g} day(s) (heuristic)"})
    return out


def pending(cache: ClaimCache, root: Path, now: float | None = None) -> list[dict]:
    """Conflicts awaiting a decision: detections not yet decided, reopened friction, and open explicit ones.
    Each: {id, kind, rule, other, handler, state: open|awaiting_human, why, reason, human}. Friction below the threshold
    is not listed."""
    now = time.time() if now is None else now
    cfg = _cfg(root)
    rows = {r["id"]: r for r in cache.store.conflict_all()}
    out, seen = [], set()
    for d in detect(cache, root, now):
        row = rows.get(d["id"])
        seen.add(d["id"])
        if d["kind"] == "friction":
            if row and row["state"] in CLOSED:      # decided: only friction since the decision counts
                n = cache.store.block_count(d["rule"], max(now - cfg.friction_window_days * 86400, row["updated_at"]))
                d = {**d, "count": n}
            if d["count"] < cfg.friction_threshold:
                if not (row and row["state"] in ("open", "awaiting_human")):
                    continue
        elif row and row["state"] in CLOSED:
            continue
        if row and row["state"] in ("open", "awaiting_human"):
            d = {**d, "handler": row["handler"], "state": row["state"], "reason": row["reason"],
                 "path": row["detail"].get("path", [])}
        else:
            d = {**d, "state": "open", "reason": ""}
        out.append({**d, "human": needs_human(d["handler"], cfg.human_nodes)})
    for r in rows.values():
        if r["id"] in seen or r["state"] not in ("open", "awaiting_human"):
            continue
        if cache.store.get(r["rule_id"]) is None:
            continue
        if r["kind"] != "spec" and r["state"] != "awaiting_human":
            continue                               # its detection is gone (rule changed): nothing left to decide
        out.append({"id": r["id"], "kind": r["kind"], "rule": r["rule_id"], "other": r["other_id"], "handler": r["handler"],
                    "state": r["state"], "reason": r["reason"], "why": r["detail"].get("why", ""),
                    "path": r["detail"].get("path", []),
                    "human": needs_human(r["handler"], cfg.human_nodes)})
    return sorted(out, key=lambda x: x["id"])


def events_for(pending_: list[dict], node: str) -> list[dict]:
    """activation events for `node`: the open conflicts it handles."""
    return [{"kind": "conflict", "detail": f"{c['id']} {c['kind']} on [{c['rule']}]: {c['why']}"
                                          + (" (needs a human)" if c["human"] else "")}
            for c in pending_ if c["state"] == "open" and c["handler"] == node]


def awaiting(cache: ClaimCache) -> list[dict]:
    """Arbitration requests awaiting a human, straight from the decision records (no detection; cheap)."""
    return [r for r in cache.store.conflict_all() if r["state"] == "awaiting_human" and cache.store.get(r["rule_id"])]


def banner(rows: list[dict]) -> list[str]:
    """Prominent lines for arbitration requests (rows from `awaiting`, or pending entries); empty when none."""
    if not rows:
        return []
    return [f"!! {len(rows)} ARBITRATION REQUEST(S) awaiting a human (`fractal conflict show <id>`; "
            "`fractal conflict arbitrate <id> --upheld|--revised --reason ...`):",
            *(f"!!   {r['id']}  rule [{r.get('rule') or r.get('rule_id')}]: {r['reason'][:140]}" for r in rows), ""]


def root_queue(pending_: list[dict]) -> list[dict]:
    """Conflicts only a human can take: awaiting arbitration, or sitting at root."""
    return [c for c in pending_ if c["state"] == "awaiting_human" or c["handler"] == ROOT]


# --- decisions ---------------------------------------------------------------------------------

def _need_reason(reason: str) -> str:
    if not (reason or "").strip():
        raise ConflictError("a written reason is required")
    return reason.strip()


def _find(cache: ClaimCache, root: Path, cid: str) -> dict:
    for c in pending(cache, root):
        if c["id"] == cid:
            return c
    raise ConflictError(f"{cid} is not a conflict awaiting a decision (see `fractal conflict list`)")


def _actor(c: dict, as_node: str | None, human: bool, cfg) -> str:
    if c["state"] == "awaiting_human":
        raise ConflictError(f"{c['id']} is an arbitration request: only a human can resolve it "
                            "(`fractal conflict arbitrate`)")
    must_be_human = needs_human(c["handler"], cfg.human_nodes)
    actor = as_node or (ROOT if must_be_human else c["handler"])
    if actor == ROOT or must_be_human:
        if actor != ROOT:
            raise ConflictError(f"{c['handler']} is flagged for a human (or is root): this takes a human decision")
        if not human:
            raise ConflictError("this is a human decision: run it in an interactive terminal (or pass --yes from a script you control)")
    elif actor != c["handler"]:
        raise ConflictError(f"{c['id']} is handled by {c['handler']}, not {actor}")
    return actor


def _record(cache: ClaimCache, c: dict, state: str, action: str, actor: str, reason: str, handler: str | None = None,
            extra: dict | None = None) -> None:
    detail = {"conflict": c["id"], "kind": c["kind"], "why": c["why"], "from": c["state"], "to": state,
              "handler": handler or c["handler"], **(extra or {})}
    cache.store.conflict_put({"id": c["id"], "kind": c["kind"], "rule_id": c["rule"], "other_id": c.get("other"),
                              "handler": handler or c["handler"], "state": state, "reason": reason,
                              "detail": {"why": c["why"], **({"path": c.get("path", [])} if c.get("path") else {})}})
    cache.store.history_add(c["rule"], f"conflict_{action}", actor, reason, detail)
    cache.store.log("conflict", c["rule"], id=c["id"], action=action, by=actor)


def narrowed_probe(probe: dict | None, exclude: list[str]) -> dict:
    """`probe` with `exclude` added to every grep leaf's exclude_paths. Raises when there is no grep leaf."""
    new, n = copy.deepcopy(probe), 0

    def walk(p: dict) -> None:
        nonlocal n
        if p.get("type") == "all":
            for q in p["probes"]:
                walk(q)
        elif p.get("type") == "grep":
            p["exclude_paths"] = sorted({*p.get("exclude_paths", []), *exclude})
            n += 1
    walk(new or {})
    if not n:
        raise ConflictError("narrow works on grep probes only (no grep probe in this rule)")
    return new


def resolve(cache: ClaimCache, root: Path, cid: str, action: str, reason: str, *, as_node: str | None = None,
            human: bool = False, exclude: list[str] | None = None) -> dict:
    """Apply keep / narrow / override to the conflict's subject rule. Returns the updated pending entry."""
    if action not in ACTIONS:
        raise ConflictError(f"unknown action {action!r} (keep, narrow, override; or `escalate`)")
    reason = _need_reason(reason)
    cfg = _cfg(root)
    c = _find(cache, root, cid)
    actor = _actor(c, as_node, human, cfg)
    e = cache.store.get(c["rule"])
    if e is None:
        raise ConflictError(f"{c['rule']} no longer exists")
    if e.layer == ROOT and e.enforced and actor != ROOT:
        raise ConflictError(f"[{e.id}] is a root (human) rule: an agent cannot keep, narrow or override it; "
                            "if you disagree with it, `fractal conflict escalate` files an arbitration request")
    if action == "keep":
        _record(cache, c, "kept", "keep", actor, reason)
        return {**c, "state": "kept"}
    if e.kind != "invariant" or not e.enforced:
        raise ConflictError(f"{action} applies to an enforced rule; {e.id} is not one (use keep)")
    if action == "override":
        from .authority import override
        override(cache, e.id, actor, reason, cfg.accept_policy)
        _record(cache, c, "overridden", "override", actor, reason)
        return {**c, "state": "overridden"}
    if not exclude:
        raise ConflictError("narrow needs --exclude PATH (a path or glob the rule stops applying to)")
    if actor != ROOT and not outranks(actor, e.layer, cache.store.all_nodes()):
        raise ConflictError(f"{actor} does not outrank {e.layer}, which holds {e.id}")
    new = narrowed_probe(e.probe, exclude)
    try:
        r = probes.run(new, cache.root)
    except (probes.ProbeError, OSError) as x:
        raise ConflictError(f"the narrowed probe does not run: {x}") from x
    if r.vacuous:
        raise ConflictError(f"refused: the narrowed probe would check nothing ({r.detail[len(probes.VACUOUS):]})")
    if not r.passed:
        raise ConflictError(f"refused: the narrowed rule would not hold now ({r.detail}); narrowing must leave a rule that checks something and passes")
    old = e.probe
    cache.store.set_probe(e.id, new)
    cache.check(e.id)
    _record(cache, c, "narrowed", "narrow", actor, reason, extra={"exclude": sorted(exclude), "old_probe": old, "new_probe": new})
    return {**c, "state": "narrowed"}


def escalate(cache: ClaimCache, root: Path, cid: str, reason: str, *, as_node: str | None = None, human: bool = False) -> dict:
    """One layer up. A conflict that involves a root-held rule, or whose next layer is root, becomes an arbitration
    request (`awaiting_human`): an agent never acts on a root rule, it asks."""
    reason = _need_reason(reason)
    cfg = _cfg(root)
    c = _find(cache, root, cid)
    actor = _actor(c, as_node, human, cfg)
    if c["handler"] == ROOT:
        raise ConflictError("already at root: a human resolves it (`fractal conflict resolve`)")
    nodes = cache.store.all_nodes()
    held = [x for x in (cache.store.get(c["rule"]), cache.store.get(c["other"]) if c.get("other") else None) if x]
    path = [*c.get("path", []), c["handler"]]
    nxt = parent_of(nodes, c["handler"])
    if any(x.layer == ROOT and x.enforced for x in held):
        nxt = ROOT                                  # bubbling cannot change a root rule: straight to the human
    state = "awaiting_human" if nxt == ROOT else "open"
    _record(cache, {**c, "path": path}, state, "escalate", actor, reason, handler=nxt)
    return {**c, "handler": nxt, "state": state, "path": path}


def raise_conflict(cache: ClaimCache, root: Path, eid: str, reason: str, by: str = "agent") -> dict:
    """The spec/premise behind a claim or rule is wrong or ambiguous: raise it on the owning node's parent instead of
    healing the claim. Never edits the claim or its probe. A root-held rule goes straight to arbitration."""
    reason = _need_reason(reason)
    e = cache.store.get(eid)
    if e is None:
        raise ConflictError(f"{eid} is not a claim or rule")
    nodes = cache.store.all_nodes()
    cid = cid_of("spec", e.id)
    prev = {c["id"]: c for c in pending(cache, root)}.get(cid)
    if prev:
        raise ConflictError(f"{cid} is already raised on {prev['handler']} ({prev['state']})")
    if e.kind == "invariant" and e.enforced:
        handler = parent_of(nodes, e.layer)
    else:
        handler = parent_of(nodes, owning_node(cache, root, e) or ROOT)
    state = "awaiting_human" if e.kind == "invariant" and e.enforced and e.layer == ROOT else "open"
    if state == "awaiting_human":
        handler = ROOT
    c = {"id": cid, "kind": "spec", "rule": e.id, "other": None, "handler": handler, "state": "open",
         "why": f"the spec/premise behind {'rule' if e.kind == 'invariant' else 'claim'} [{e.id}] is wrong or ambiguous"}
    _record(cache, c, state, "raise", by, reason)
    return {**c, "state": state, "reason": reason}


def owning_node(cache: ClaimCache, root: Path, e) -> str | None:
    """The first registered node (by name) whose owned files the claim depends on."""
    from .activation import owned_files
    from .cache import dependencies
    from .regions import affects
    for n, info in sorted(cache.store.all_nodes().items()):
        if any(affects(dependencies(e), f) for f in owned_files(cache, root, n, info)):
            return n
    return None


def arbitrate(cache: ClaimCache, cid: str, outcome: str, reason: str, *, human: bool) -> dict:
    """A human settles an arbitration request. `upheld`: the root rule stands; `revised`: the human is changing it
    (do that with `fractal override`/`reject`/`accept`; this records the decision and why)."""
    if outcome not in ("upheld", "revised"):
        raise ConflictError("outcome must be upheld or revised")
    if not human:
        raise ConflictError("arbitration is a human decision: run it in an interactive terminal (or pass --yes from a script you control)")
    reason = _need_reason(reason)
    row = cache.store.conflict_get(cid)
    if not row or row["state"] != "awaiting_human":
        raise ConflictError(f"{cid} is not an arbitration request awaiting a human")
    c = {"id": cid, "kind": row["kind"], "rule": row["rule_id"], "other": row["other_id"], "handler": ROOT,
         "state": "awaiting_human", "why": row["detail"].get("why", "")}
    _record(cache, c, outcome, "arbitrate", "human", reason)
    return {**c, "state": outcome}


# --- rendering ---------------------------------------------------------------------------------

def render_list(items: list[dict], cache: ClaimCache) -> str:
    if not items:
        return "no conflicts awaiting a decision\n"
    out = []
    for c in sorted(items, key=lambda x: (x["state"] != "awaiting_human", x["id"])):
        e = cache.store.get(c["rule"])
        head = "ARBITRATION REQUEST (awaiting a human)" if c["state"] == "awaiting_human" else f"open on {c['handler']}"
        out.append(f"{c['id']}  {c['kind']:8} {head}{' (human)' if c['human'] and c['state'] == 'open' else ''}")
        out.append(f"    rule [{c['rule']}] {e.post[:90] if e else '(gone)'}")
        out.append(f"    {c['why']}")
        if c["reason"]:
            out.append(f"    reason: {c['reason']}")
    return "\n".join(out) + "\n"


def render_show(cache: ClaimCache, c: dict | None, row: dict | None) -> str:
    base = c or row
    if not base:
        return ""
    rid = base.get("rule") or base.get("rule_id")
    e = cache.store.get(rid)
    lines = [f"{base['id']}  {base['kind']}", f"  rule: [{rid}] {e.post if e else '(gone)'}",
             f"  held by: {e.layer if e else '?'}", f"  state: {(c or row)['state']}  handler: {base['handler']}"]
    if c:
        lines.append(f"  why: {c['why']}")
    if row:
        lines.append(f"  decision: {row['state']}" + (f" - {row['reason']}" if row["reason"] else ""))
    stats = cache.store.block_stats(rid)
    if stats:
        recent = cache.store.blocks(rid)
        lines.append(f"  blocks: {stats['total']} total, {stats['repeats']} repeat(s) on the same file (heuristic); "
                     f"{len(recent)} kept; by hook: "
                     + ", ".join(f"{h}={sum(1 for b in recent if b['hook'] == h)}" for h in sorted({b['hook'] for b in recent})))
    hist = [h for h in cache.store.rule_history(rid) if h["action"].startswith("conflict_")]
    for h in hist:
        lines.append(f"  history: {h['action'][9:]} by {h['who']}: {h['reason']}")
    return "\n".join(lines) + "\n"

"""The door: the one place an invariant's state, authority or content changes.

Theory (docs: "compression as the arbiter", revised 2026-11-21): every change to the judge set is classified and
recorded, and only a higher layer may let more states through. This module is where that is made true for the CLI.
Everything that changes what a rule enforces -- accept, reject, reopen, local accept, override/restore, conflict
narrow, delete -- goes through `transition`, `change_probe` or `delete_guarded`, and each writes one `rule_history`
entry carrying the rule's `revision` before/after. `tests/test_rule_door.py` checks statically that nothing else in
the package calls the store's rule-mutating primitives.

What a rule is (its identity for authority): its text AND its probe and read paths. `revision` digests exactly
those. The edge id stays a digest of the text, so facts and history that point at a rule keep pointing at it;
what was accepted is pinned by the revision recorded at acceptance.

Guarantees, with their strength:
  put of a rule that has been ruled on (enforced, overridden, rejected) with a different revision is refused.  SOUND
  delete of a rule that has been ruled on is refused; delete of a proposal is recorded with what it said.      SOUND
  every state change writes rule_history {from, to, authority, revision, loss}.                                SOUND
  `loss` is the GLOBAL loss (loss.py): an enforced rule leaving `enforced`, or having its probe changed, lets
  states through; it is a loss unless other in-effect rules provably still forbid all of them (`absorbed_by`).
  Unprovable counts as a loss.                                                                                SOUND as an over-approximation
  `drifted`: an enforced rule whose revision is not the one its last recorded transition pinned was changed
  outside this module (a raw store edit). Detected by `fractal check`. Rules ruled on before revisions were
  recorded are unpinned and not checked. A writer who also forges rule_history is not detected: this catches
  accidental and casual paths, not a determined one (the store guard in the hooks covers more of those).      HEURISTIC
"""

from __future__ import annotations

import time

from .hashing import canonical, sha
from .store import ROOT, Edge

RULED = ("enforced", "overridden", "rejected")
ACTION_OF = {"enforced": "accept", "rejected": "reject", "proposed": "reopen"}


def revision(e: Edge) -> str:
    """Digest of what a rule says and checks: text, probe, read paths. Verdicts and state are not part of it."""
    return sha(canonical({"kind": e.kind, "pre": e.pre, "post": e.post, "probe": e.probe,
                          "reads": sorted(e.reads)}))[:16]


def _error(msg: str) -> Exception:
    from .cache import CacheError
    return CacheError(msg)


def _record(store, eid: str, action: str, actor: str, reason: str, before: Edge, after: Edge | None,
            loss: bool, detail: dict | None = None) -> None:
    d = {"from": before.rule_state, "to": after.rule_state if after else None,
         "authority": after.authority if after else before.authority,
         "revision": revision(after) if after else revision(before), "loss": loss, **(detail or {})}
    store.history_add(eid, action, actor, reason, d)
    store.log("rule", eid, state=d["to"], by=actor, authority=d["authority"], action=action)


def global_loss(cache, before: Edge, after_probe: dict | None, leaves_enforced: bool) -> tuple[bool, list[str], str]:
    """(loss, absorbed_by, why) for a change to `before`: dropping it from enforcement, or replacing its probe."""
    if before.rule_state != "enforced" or not (leaves_enforced or after_probe != before.probe):
        return False, [], "nothing that was enforced is let through"
    from .loss import assess
    a = assess(cache, before, None if leaves_enforced else after_probe)
    return (not a.absorbed), a.by, a.why


def transition(cache, eid: str, to: str, *, authority: str | None, actor: str, action: str, reason: str = "",
               provenance: dict | None = None, detail: dict | None = None) -> Edge:
    """Move an invariant to state `to` held by `authority`, and record it. Callers have already checked that
    `actor` may make this move (authority.py, conflict.py, the CLI's human gate)."""
    store = cache.store
    before = store.get(eid)
    if before is None or before.kind != "invariant":
        raise _error(f"{eid} is not an invariant")
    lost, by, why = global_loss(cache, before, None, leaves_enforced=to != "enforced")
    store.set_authority(eid, to, authority, provenance)
    after = store.get(eid)
    _record(store, eid, action, actor, reason, before, after, loss=lost,
            detail={"absorbed_by": by, **({"loss_why": why} if lost else {}), **(detail or {})})
    return after


def human_set(cache, eid: str, state: str) -> Edge:
    """The human's accept / reject / reopen (`fractal accept|reject`, or a script the human controls)."""
    if state not in ACTION_OF:
        raise _error(f"bad rule state {state!r}")
    e = cache.store.get(eid)
    if e is None:
        raise _error(f"no claim {eid}")
    if e.kind != "invariant":
        raise _error(f"{eid} is a {e.kind} claim, not an invariant")
    from . import probes
    prov = ({"by": "human", "node": None, "ts": time.time(), "evidence": probes.evidence_class(e.probe)}
            if state == "enforced" else None)
    return transition(cache, eid, state, authority=ROOT if state == "enforced" else None, actor="human",
                      action=ACTION_OF[state], provenance=prov)


def change_probe(cache, eid: str, probe: dict, *, actor: str, action: str, reason: str,
                 detail: dict | None = None) -> Edge:
    """Replace an invariant's probe in place (conflict `narrow`). The caller has checked authority."""
    store = cache.store
    before = store.get(eid)
    if before is None or before.kind != "invariant":
        raise _error(f"{eid} is not an invariant")
    lost, by, why = global_loss(cache, before, probe, leaves_enforced=False)
    store.set_probe(eid, probe)
    after = store.get(eid)
    _record(store, eid, action, actor, reason, before, after, loss=lost,
            detail={"old_revision": revision(before), "old_probe": before.probe, "new_probe": probe,
                    "absorbed_by": by, **({"loss_why": why} if lost else {}), **(detail or {})})
    return after


def guard_put(existing: Edge | None, new: Edge) -> None:
    """Refuse to change, in place, what a rule that has been ruled on says or checks."""
    if existing is None or existing.kind != "invariant" or existing.rule_state not in RULED:
        return
    if revision(existing) == revision(new):
        return            # re-asserting the same rule: allowed, it is re-checked
    hint = {"enforced": "propose the new version under its own text; a human accepts it, and the old one is "
                        "retired with `fractal override <id> --reason ...` (kept, with history)",
            "overridden": "an overridden rule is a record; propose the new version under its own text",
            "rejected": "a rejected rule is a human ruling kept as precedent; propose the new version under its own text"}
    raise _error(f"[{existing.id}] is {existing.rule_state}: its probe and read paths are part of what was ruled on "
                 f"and cannot change in place. To change it, {hint[existing.rule_state]}.")


def delete_guarded(store, e: Edge) -> None:
    """Delete an edge. A rule that has been ruled on is refused; a deleted proposal is recorded with what it said."""
    if e.kind == "invariant" and e.rule_state in RULED:
        hint = {"enforced": "retire it with `fractal override <id> --reason ...` (a higher layer; the rule is "
                            "demoted and kept, with history)",
                "overridden": "it is kept as a record; `fractal override <id> --undo` restores it",
                "rejected": "it is a human ruling, kept as precedent"}
        raise _error(f"[{e.id}] is {e.rule_state} and cannot be deleted: {hint[e.rule_state]}")
    store.delete(e.id)
    if e.kind == "invariant":
        _record(store, e.id, "delete", "cli", "", e, None, loss=False,
                detail={"probe": e.probe, "post": e.post})


def pin(store, eid: str) -> Edge:
    """Record the current revision of an enforced rule that has none recorded (ruled on before revisions existed).
    A human step (`fractal pin`); a rule that already has a pinned revision is not touched here (re-accept it)."""
    e = store.get(eid)
    if e is None or not e.enforced:
        raise _error(f"{eid} is not an enforced rule")
    if pinned_revision(store, eid) is not None:
        raise _error(f"{eid} already has a pinned revision; to take a changed rule, a human re-accepts it")
    _record(store, eid, "pin", "human", "", e, e, loss=False)
    return e


def unpinned(store, edges: list[Edge]) -> list[Edge]:
    return [e for e in edges if e.enforced and pinned_revision(store, e.id) is None]


def pinned_revision(store, eid: str) -> str | None:
    """The revision the rule's last recorded transition pinned, or None if none was recorded (an older store)."""
    for h in reversed(store.rule_history(eid)):
        if "revision" in h["detail"]:
            return h["detail"]["revision"]
    return None


def drifted(store, edges: list[Edge]) -> list[tuple[Edge, str]]:
    """Enforced rules whose content is not the revision their last transition pinned: changed outside the door."""
    out = []
    for e in edges:
        if e.enforced:
            pinned = pinned_revision(store, e.id)
            if pinned is not None and pinned != revision(e):
                out.append((e, pinned))
    return out

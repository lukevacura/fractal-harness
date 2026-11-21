"""The human queue: every decision waiting for the root, as the net change in information each would make.

Theory: a human decision is worth the information it changes. Each item carries two sets, computed fresh:
  newly forbids   what accepting it would forbid that no in-effect enforced rule already forbids;
  newly allows    what it would let through that no remaining rule still forbids (the global loss, loss.py).
An item that adds nothing and loses nothing is a quick decision; a contradiction or a global loss needs thought.

Items:
  proposal      accept or reject a proposed rule. Forbids: the parts of its probe not already covered. Allows: the
                rules a root accept would set aside by precedence (authority.effective), each with its loss.
  arbitration   a conflict only the human can settle (awaiting arbitration, held at root, or on a human node).
                Upholding changes nothing; revising (dropping the rule) allows its global loss, shown.
  changed       an enforced rule whose content is not the revision that was accepted (rules.drifted).
  policy        a pinned [policy] key whose fractal.toml value differs (config.policy_drift).

Strength:
  "adds nothing" is SOUND: claimed only when every part of the probe is proven covered (loss.coverage).
  "newly forbids" over-approximates: a part only a command rule covers cannot be proven covered and is listed.
  A command/test leaf makes the item "unknown" for that part instead of either.
  Verdicts are re-run for fast probes every time; slow ones (command, test) with `slow=True`.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from .store import ROOT

RANK = {"changed": 0, "policy": 0, "contradiction": 0, "loss": 1, "arbitration": 1, "addition": 2, "violated": 2,
        "unknown": 3, "nothing": 4}
LABEL = {"changed": "CHANGED OUTSIDE THE CLI", "policy": "POLICY EDIT NOT PINNED", "contradiction": "CONTRADICTION",
         "loss": "GLOBAL LOSS", "arbitration": "ARBITRATION", "addition": "adds information",
         "violated": "adds information, VIOLATED NOW", "unknown": "unknown (not comparable)",
         "nothing": "adds nothing"}


@dataclass
class Item:
    kind: str                      # proposal | arbitration | changed | policy
    id: str
    title: str
    verdict: str                   # a RANK key
    forbids: list[str] = field(default_factory=list)
    already: list[tuple[str, str]] = field(default_factory=list)     # (what, rule that already forbids it)
    allows: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    status: str = ""
    decide: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {**asdict(self), "label": LABEL[self.verdict]}


def _in_effect(cache, edges):
    from .authority import effective
    in_effect, _ = effective(cache, edges)
    return [x for x in in_effect if x.enforced]


def _fresh(cache, e, slow: bool) -> tuple:
    """A fresh verdict for `e`, or the stored one with a note when its probe is slow and `slow` is off."""
    from . import probes
    if e.probe and probes.is_slow(e.probe) and not slow:
        return e, "slow probe: verdict not re-run (`fractal queue --slow` re-runs it)"
    return cache.check(e.id), None


def proposal_item(cache, e, enforced, slow: bool = False, proposals=()) -> Item:
    from .authority import contradiction
    from .debt import summary as debt_summary
    from .loss import coverage, coverage_of, delta, in_effect
    from .regions import tracked_files
    e, slow_note = _fresh(cache, e, slow)
    pool = _in_effect(cache, enforced)
    # one Delta: accepting it as the root's. Its forbids are what it newly enforces; its allows, the rules it would
    # set aside by precedence that nothing else still covers
    as_root = replace(e, rule_state="enforced", authority=ROOT)
    dl = delta([(x.id, x.probe) for x in pool], in_effect(cache, [*enforced, as_root]))
    cov = coverage_of(dl)
    it = Item("proposal", e.id, e.post, "addition", forbids=cov.new, already=cov.covered,
              status=f"{e.status}: {e.detail}", decide=[f"fractal accept {e.id}", f"fractal reject {e.id}"])
    if slow_note:
        it.notes.append(slow_note)
    if cov.unknown:
        it.notes.append(cov.unknown)
    others = [x for x in proposals if x.id != e.id]
    if cov.new and others:              # onboarding: which proposals overlap (accepting one makes the other redundant)
        ov = coverage(e.probe, [*(x.probe for x in pool), *(x.probe for x in others)], [*(x.id for x in pool), *(x.id for x in others)])
        by = sorted({i for _, i in ov.covered} - {x.id for x in pool})
        if by and ov.adds_nothing:
            it.notes.append(f"adds nothing once proposal(s) {', '.join(by)} are accepted: they cover all of it")
        elif by:
            it.notes.append(f"overlaps proposal(s) {', '.join(by)}")
    if d := debt_summary(e.probe):
        it.notes.append(f"debt rule: {d}")
    files = tracked_files(cache.root)
    root_contradicts = []
    for o in [*enforced, *(x for x in proposals if x.id != e.id)]:
        c = contradiction(o.probe, e.probe, files)
        if c is None or c.kind == "unseen":
            continue
        what = (f"{'contradicts' if c.sound else 'may conflict with'} {o.rule_state} rule [{o.id}] "
                f"\"{o.post[:60]}\" (@{o.layer}): {c.why}" + ("" if c.sound else " (heuristic)"))
        if c.sound and o.enforced and o.layer == ROOT:
            root_contradicts.append(what)    # same layer: neither sets the other aside, the rule set is inconsistent
        else:
            it.notes.append(what)
    after = {x.id for x in _in_effect(cache, [*enforced, as_root])}
    for lo in (x for x in pool if x.id not in after):          # set aside by precedence
        lost = [p for p in dl.allows if p.rule == lo.id]
        unknown = [w for r, w in dl.unknown_allows if r == lo.id]
        by = sorted({i for p in dl.absorbed if p.rule == lo.id for i in p.by})
        why = (f"not provable: {unknown[0]}" if unknown else
               f"no other in-effect rule still enforces {lost[0].what}" if lost else
               "no information loss, " + ("absorbed by " + ", ".join(by) if by else "nothing was forbidden"))
        it.allows.append(f"sets aside {lo.layer}'s rule [{lo.id}] \"{lo.post[:60]}\": {why}")
    it.notes = [*root_contradicts, *it.notes]
    if root_contradicts:
        it.verdict = "contradiction"
    elif any("no information loss" not in a for a in it.allows):
        it.verdict = "loss"
    elif e.status == "failed":
        it.verdict = "violated"
        it.notes.append("accepting it blocks edits until the code is fixed; or re-propose it with --debt")
    elif not e.probe:
        it.verdict = "unknown"
        it.notes.append("it has no probe: nothing checks it, so it neither forbids nor adds anything enforceable")
    elif cov.unknown and not cov.new:
        it.verdict = "unknown"
    elif cov.adds_nothing:
        it.verdict = "nothing"
        it.notes.append("everything it checks is already forbidden by enforced rules: rejecting it loses nothing")
    return it


def arbitration_item(cache, c: dict, enforced) -> Item:
    from .rules import global_loss
    rid = c.get("rule") or c.get("rule_id")
    e = cache.store.get(rid)
    it = Item("arbitration", c["id"], f"rule [{rid}] \"{e.post[:70]}\"" if e else f"rule [{rid}]", "arbitration",
              notes=[x for x in (c.get("why"), c.get("reason")) if x],
              decide=[f"fractal conflict show {c['id']}", f"fractal conflict arbitrate {c['id']} --upheld|--revised --reason ..."])
    if e is not None and e.enforced:
        lost, by, why = global_loss(cache, e, None, leaves_enforced=True)
        it.allows.append(("if the rule is dropped: " + why) if lost
                         else f"if the rule is dropped: no information loss, absorbed by {', '.join(by)}")
        it.notes.append("upheld: nothing changes")
    return it


def items(cache, root: Path, slow: bool = False) -> list[Item]:
    from . import conflict
    from .config import load
    from .rules import drifted
    edges = cache.store.all()
    enforced = [e for e in edges if e.kind == "invariant" and e.enforced]
    out: list[Item] = []
    for e, pinned in drifted(cache.store, enforced):
        out.append(Item("changed", e.id, e.post, "changed",
                        notes=[f"its probe or text is not revision {pinned}, which was accepted: the store was edited "
                               "outside the CLI. Restore it, or read it and re-accept it"],
                        decide=[f"fractal invariants --json  # inspect [{e.id}]", f"fractal accept {e.id}"]))
    for k, v in load(root).policy_drift.items():
        out.append(Item("policy", k, f"[policy] {k}", "policy",
                        notes=[f"fractal.toml says {v['file']!r}; the pinned value {v['pinned']!r} is in force"],
                        decide=["fractal pin  # take the edit", "restore fractal.toml  # keep the pinned value"]))
    proposals = sorted((x for x in edges if x.kind == "invariant" and x.rule_state == "proposed"), key=lambda x: x.post)
    for e in proposals:
        out.append(proposal_item(cache, e, enforced, slow, proposals))
    seen = set()
    for c in conflict.pending(cache, root):
        if c["state"] == "awaiting_human" or c["handler"] == ROOT or c.get("human"):
            seen.add(c["id"])
            out.append(arbitration_item(cache, c, enforced))
    for r in conflict.awaiting(cache):
        if r["id"] not in seen:
            out.append(arbitration_item(cache, r, enforced))
    return sorted(out, key=lambda i: (RANK[i.verdict], i.kind, i.title))


def render(its: list[Item]) -> str:
    if not its:
        return "fractal queue: nothing awaits a human decision\n"
    counts: dict[str, int] = {}
    for i in its:
        counts[LABEL[i.verdict]] = counts.get(LABEL[i.verdict], 0) + 1
    lines = [f"fractal queue: {len(its)} decision(s): " + ", ".join(f"{n} {k}" for k, n in counts.items()), ""]
    for n, i in enumerate(its, 1):
        lines.append(f"[{n}] {LABEL[i.verdict].upper() if RANK[i.verdict] <= 1 else LABEL[i.verdict]}  "
                     f"{i.kind} {i.id}  {i.title[:100]}")
        for f in i.forbids:
            lines.append(f"      + newly enforces: {f}")
        for w, by in i.already:
            lines.append(f"      = already enforced by [{by}]: {w}")
        for a in i.allows:
            lines.append(f"      - newly allows: {a}")
        for note in i.notes:
            lines.append(f"      ! {note}")
        if i.status:
            lines.append(f"      verdict now: {i.status[:120]}")
        lines.append(f"      decide: {' | '.join(i.decide)}")
        lines.append("")
    return "\n".join(lines)

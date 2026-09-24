"""`fractal repair`: fix broken claims with the least context that can fix them.

Delta repairs show the repairer only the claim, its probe, and the residual (old anchor vs.
the current region), like decoding a P-frame. Scene cuts, and claims that have had
KEYFRAME_INTERVAL delta repairs in a row, get a keyframe: full re-verification from source,
which stops small patches from drifting a claim away from the truth.

By default only *demanded* claims are repaired: ones the prompt hook wanted to inject but
could not. Claims nobody asks about stay broken for free.

A failing invariant (a rule the code must follow) may mean the code broke the rule, not
that the claim went stale. The repairer reports those as violations and leaves them alone:
spec changes are for humans.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from .cache import ClaimCache
from .record import NO_HOOKS_ENV
from .store import Edge

KEYFRAME_INTERVAL = 3

PROMPT = """\
You repair entries in this repo's claim cache. Each entry below is a claim whose probe
failed or became suspect after the code changed. For each one, decide exactly one outcome
and act on it with the fractal CLI (do not modify any source files):

- STILL TRUE, probe too brittle: re-record the SAME claim text with a fixed probe
  (`fractal put "<exact same claim text>" --read ... --probe '<json>' {delta_flag}`).
- CHANGED: the code changed and the claim should say something different:
  `fractal rm <id>`, then `fractal put "<new claim>" --read ... --probe '<json>' {delta_flag}`.
- NO LONGER TRUE and not worth replacing: `fractal rm <id>`.
- VIOLATION: the claim states a rule the code is supposed to follow (kind "invariant", a
  "never"/"must"/"always" rule, an "absent" probe) and the code now breaks it. Do NOT
  edit or remove the claim; just report it. Invariant entries are only listed here when
  their probe is brittle; if the code truly violates one, it is a VIOLATION.

"Still true" means true AND still an adequate description: if the claim is literally true
but the code around it changed what matters about its subject (e.g. a rewrite replaced the
mechanism the claim describes), treat it as CHANGED and record a claim that describes the
current behavior.

For DELTA entries, the residual shows exactly what changed around the lines the probe used
to match. Decide from the residual; read the file only if the residual is not enough, and
then only the region around the new line numbers. For KEYFRAME entries, re-verify the claim
against the source from scratch; for REWRITE-flagged entries, read what changed in the
rewritten files, since the probed lines themselves may be untouched.

Probe format: {{"type":"grep","pattern":"<regex>","paths":["<glob>"],"expect":"present"|"absent"|{{"count":n}}|{{"min":n}}}}
(line-based; optional "exclude":"<regex>"), or {{"type":"all","probes":[...]}}. Prefer
patterns anchored on identifiers over exact formatting, and "min" over exact "count", so
harmless edits don't break them. The probe must still fail if the claim becomes false.

Finish with one line per entry: `<id>: STILL_TRUE | CHANGED | REMOVED | VIOLATION - <why>`.

Entries:
{entries}
"""


def _entry(e: Edge, kind: str) -> str:
    lines = [f"### {e.id} [{kind.upper()}]{' [INVARIANT]' if e.kind == 'invariant' else ''}", f"Claim: {e.post}"]
    if e.pre:
        lines.append(f"Precondition: {e.pre}")
    lines += [f"Reads: {', '.join(e.reads)}", f"Probe: {json.dumps(e.probe)}", f"Status now: {e.detail}"]
    if e.repair and e.repair.get("kind") == "reassert":
        lines.append("No probe: re-verify from source; add a probe if the claim is mechanically checkable.")
    if kind == "delta":
        for r in (e.repair or {}).get("residuals", []):
            where = f"{r['file']} line {r['old_line']} -> {r['new_line']} (similarity {r['ratio']})"
            lines.append(f"Residual at {where}:\n{r['diff'] or '(unchanged)'}")
    return "\n".join(lines)


def candidates(cache: ClaimCache, ids: list[str] | None = None, everything: bool = False) -> list[tuple[Edge, str]]:
    """(edge, delta|keyframe) for claims needing repair: given ids, all, or demanded since last repair."""
    # Enforced invariants are human-owned: a failure is a violation to report, never to repair.
    broken = {e.id: e for e in cache.store.all()
              if e.repair and e.status in ("failed", "stale") and not e.enforced and e.rule_state != "rejected"}
    if ids:
        chosen = [broken[i] for i in ids if i in broken]
    elif everything:
        chosen = list(broken.values())
    else:
        last = max((ev["ts"] for ev in cache.store.events("repair")), default=0.0)
        demanded = {i for ev in cache.store.events("demand") if ev["ts"] > last for i in ev.get("ids", [])}
        chosen = [broken[i] for i in demanded if i in broken]
    out = []
    for e in chosen:
        # rewrite / scene_cut / reassert always need a keyframe: the residual can't show what changed
        delta = e.repair["kind"] in ("delta", "suspect") and e.delta_count < KEYFRAME_INTERVAL
        out.append((e, "delta" if delta else "keyframe"))
    return out


def repair(root: Path, ids: list[str] | None = None, everything: bool = False,
           model: str = "sonnet", dry_run: bool = False) -> dict:
    root = root.resolve()
    cache = ClaimCache(root)
    try:
        todo = candidates(cache, ids, everything)
        if not todo:
            return {"repaired": 0, "cost_usd": 0.0, "entries": []}
        prompt = PROMPT.replace("{entries}", "\n\n".join(_entry(e, k) for e, k in todo))
        prompt = prompt.replace("{delta_flag}", "[--delta for DELTA entries]")
        if dry_run:
            return {"entries": [{"id": e.id, "kind": k} for e, k in todo], "prompt": prompt}
        before = {e.id for e in cache.store.all()}
    finally:
        cache.close()

    n_keyframes = sum(k == "keyframe" for _, k in todo)
    cmd = ["claude", "-p", prompt, "--output-format", "json", "--model", model,
           "--strict-mcp-config", "--allowedTools", "Read", "Grep", "Glob", "Bash(fractal:*)",
           "--disallowedTools", "Edit", "Write", "NotebookEdit",
           "--no-session-persistence", "--max-turns", str(10 + 3 * len(todo) + 8 * n_keyframes)]
    env = {**os.environ, NO_HOOKS_ENV: "1", "FRACTAL_ROOT": str(root)}
    proc = subprocess.run(cmd, cwd=root, capture_output=True, text=True, env=env, timeout=3600)
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError:
        result = {}

    cache = ClaimCache(root)
    try:
        after = {e.id: e for e in cache.store.all()}
        verdicts = _parse_verdicts(result.get("result", ""))
        entries = []
        for e, kind in todo:
            now = after.get(e.id)
            verdict = verdicts.get(e.id)
            outcome = ("repaired" if now and now.status in ("verified", "trusted")
                       else "replaced" if not now and verdict == "CHANGED"
                       else "removed" if not now
                       else "violation" if verdict == "VIOLATION"
                       else "unresolved")
            entries.append({"id": e.id, "kind": kind, "outcome": outcome, "verdict": verdicts.get(e.id)})
        added = [i for i in after if i not in before]
        cost = result.get("total_cost_usd") or 0.0
        cache.store.log("repair", None, entries=len(todo), keyframes=n_keyframes, added=len(added),
                        cost_usd=cost, outcomes=[x["outcome"] for x in entries])
    finally:
        cache.close()
    return {"entries": entries, "claims_added": len(added), "cost_usd": cost,
            "summary": (result.get("result") or proc.stderr[-300:]).strip()[-800:]}


def _parse_verdicts(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        head, _, rest = line.strip().strip("-* `").partition(":")
        word = rest.strip().split(" ")[0].strip("*`") if rest else ""
        if len(head) == 16 and word in ("STILL_TRUE", "CHANGED", "REMOVED", "VIOLATION"):
            out[head] = word
    return out

"""Goodput analysis of A/B transcripts: split each run's spend into goodput, badput, and investment.

Every model turn's cost (from its own usage, weighted by token price ratios) is split evenly
across the tool calls it issued; turns without tool calls are the answer. Each tool call is
classified:

  goodput     answer turns; claim queries that returned claims; exploration whose input or
              output names a file the final answer cites
  badput      overhead: ToolSearch, claim queries returning nothing
              covered_reread: reading/searching a file covered by an injected verified claim
                         (upper bound on redundant re-checking)
              unused: exploration that surfaced no cited file for the first time
              delegation: subagent spend (not visible turn by turn)
  investment  claims_put / claims_verify (a durable asset; credit it to later tasks)

Usage:
  python experiments/goodput.py <results dir> [--claims-db <store as of the run> --tasks hard]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from ab_onboarding import TASK_SETS  # noqa: E402

# Relative token prices (Sonnet-class): input 1, 5m cache write 1.25, 1h cache write 2,
# cache read 0.1, output 5. Only ratios matter; totals are rescaled to dollars.
W_IN, W_W5, W_W1H, W_READ, W_OUT = 1.0, 1.25, 2.0, 0.1, 5.0
FILE_RE = re.compile(r"[\w./-]*?([\w-]+\.(?:dart|py|yaml|yml|md|json|geojson|sh))\b")


def files_in(text: str) -> set[str]:
    return {m.group(1) for m in FILE_RE.finditer(text or "")}


def turn_weight(u: dict) -> float:
    cc = u.get("cache_creation") or {}
    w1h = cc.get("ephemeral_1h_input_tokens", 0)
    w5 = cc.get("ephemeral_5m_input_tokens", u.get("cache_creation_input_tokens", 0) - w1h)
    return (u.get("input_tokens", 0) * W_IN + w5 * W_W5 + w1h * W_W1H
            + u.get("cache_read_input_tokens", 0) * W_READ + u.get("output_tokens", 0) * W_OUT)


def parse(path: Path) -> dict:
    turns: dict[str, dict] = {}          # message id -> {usage, calls}
    order: list[str] = []
    results: dict[str, str] = {}         # tool_use_id -> result text
    final = {"cost": 0.0, "answer": ""}
    for line in path.read_text().splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "assistant":
            msg = ev["message"]
            t = turns.get(msg["id"])
            if t is None:
                t = turns[msg["id"]] = {"usage": msg.get("usage", {}), "calls": []}
                order.append(msg["id"])
            for b in msg.get("content", []):
                if b.get("type") == "tool_use":
                    t["calls"].append({"id": b["id"], "name": b["name"], "input": b.get("input", {})})
        elif ev.get("type") == "user":
            content = ev.get("message", {}).get("content")
            for b in content if isinstance(content, list) else []:
                if b.get("type") == "tool_result":
                    c = b.get("content")
                    results[b["tool_use_id"]] = c if isinstance(c, str) else json.dumps(c)
        elif ev.get("type") == "result":
            # Sessions resumed by a background task emit several results; cost is cumulative
            # and the last result holds the final answer.
            final["cost"] = max(final["cost"], ev.get("total_cost_usd") or 0)
            final["answer"] = ev.get("result") or final["answer"]
    return {"turns": [turns[i] for i in order], "results": results, **final}


def classify(call: dict, result: str, cited: set[str], covered: set[str],
             seen: set[str], read: set[str]) -> str:
    name, inp = call["name"], call["input"]
    if name == "ToolSearch":
        return "overhead"
    if name.startswith("mcp__fractal-claims__claims_query"):
        return "claim_hit" if '"id"' in result else "overhead"
    if name.startswith("mcp__fractal-claims__"):
        return "investment"
    if name == "Agent":
        return "delegation"
    targets = files_in(json.dumps(inp))
    if targets & covered:
        return "covered_reread"
    if name == "Read":
        new = (targets & cited) - read
        read.update(targets)
        seen.update(targets)
        return "used" if new else "unused"
    # A search contributes only if it is the first to surface a cited file (novelty).
    found = (targets | files_in(result)) & cited
    new = found - seen
    seen.update(found)
    return "used" if new else "unused"


GOOD = {"answer", "claim_hit", "used"}
# covered_reread (reading a file an injected verified claim covers) is an upper bound on
# redundant re-checking: the task may need parts of the file the claim does not state.
BAD = {"overhead", "covered_reread", "unused", "delegation"}


def price_per_weight(paths: list[Path]) -> float:
    """$ per weight unit, calibrated on runs without subagents (their turns are all visible)."""
    ratios = []
    for f in paths:
        run = parse(f)
        if not any(c["name"] == "Agent" for t in run["turns"] for c in t["calls"]):
            w = sum(turn_weight(t["usage"]) for t in run["turns"])
            if w:
                ratios.append(run["cost"] / w)
    ratios.sort()
    return ratios[len(ratios) // 2]


def analyze(path: Path, covered: set[str], k: float) -> dict:
    run = parse(path)
    cited = files_in(run["answer"])
    spend: dict[str, float] = defaultdict(float)
    seen: set[str] = set()
    read: set[str] = set()
    for t in run["turns"]:
        dollars = turn_weight(t["usage"]) * k
        if not t["calls"]:
            spend["answer"] += dollars
            continue
        for call in t["calls"]:
            cat = classify(call, run["results"].get(call["id"], ""), cited, covered, seen, read)
            spend[cat] += dollars / len(t["calls"])
    residual = run["cost"] - sum(spend.values())
    if residual > 0.001:  # subagent turns are not in the transcript; only in the total
        spend["delegation"] += residual
    total = sum(spend.values())
    good = sum(v for k, v in spend.items() if k in GOOD)
    bad = sum(v for k, v in spend.items() if k in BAD)
    return {"cost": total, "good": good, "bad": bad, "invest": spend.get("investment", 0.0),
            "spend": dict(spend), "answer": run["answer"]}


def injected_covered(db: Path | None, prompt: str) -> set[str]:
    """Files covered by verified claims the hook would have injected for this prompt."""
    if db is None:
        return set()
    from fractal_harness.hook import MAX_CLAIMS
    from fractal_harness.store import Store, tokens
    store = Store(db)
    try:
        terms = tokens(prompt)
        hits = store.search(prompt, statuses=["verified"], limit=MAX_CLAIMS,
                            min_matches=2 if len(terms) >= 3 else 1)
        return {Path(r).name for e in hits for r in e.reads if "*" not in r}
    finally:
        store.close()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("results", type=Path)
    p.add_argument("--claims-db", type=Path, help="claim store as it was during the run (for arm H/B/C)")
    p.add_argument("--tasks", choices=list(TASK_SETS), default="hard")
    args = p.parse_args()
    prompts = {t[0]: t[1] for t in TASK_SETS[args.tasks]}

    files = sorted(f for f in args.results.glob("*.jsonl") if not f.name.startswith("record_input_"))
    k = price_per_weight(files)
    # Runs that logged what the hook injected (runner records it) use that; else reconstruct.
    logged = {}
    if (args.results / "results.json").exists():
        for r in json.loads((args.results / "results.json").read_text()):
            if "injected" in r:
                logged[(r["arm"], r["task"], r["rep"])] = {
                    Path(p).name for e in r["injected"] for p in e["reads"] if "*" not in p}
    rows = []
    for f in files:
        arm, rest = f.stem.split("_", 1)
        task, rep = rest.rsplit("_", 1)
        if arm == "A":
            covered = set()
        elif (arm, task, int(rep)) in logged:
            covered = logged[(arm, task, int(rep))]
        else:
            covered = injected_covered(args.claims_db, prompts[task])
        rows.append({"arm": arm, "task": task, "rep": int(rep), **analyze(f, covered, k)})
    (args.results / "goodput.json").write_text(json.dumps(rows, indent=2))

    cats = ["answer", "claim_hit", "used", "overhead", "covered_reread", "unused", "delegation", "investment"]
    print(f"{'task':15} {'arm':3} {'rep':3} {'cost':>6} {'good%':>6} " + " ".join(f"{c[:8]:>8}" for c in cats))
    for r in sorted(rows, key=lambda r: (r["task"], r["arm"], r["rep"])):
        print(f"{r['task']:15} {r['arm']:3} {r['rep']:3} {r['cost']:6.3f} {100*r['good']/r['cost']:5.0f}% "
              + " ".join(f"{r['spend'].get(c, 0):8.3f}" for c in cats))
    print()
    for arm in sorted({r["arm"] for r in rows}):
        rs = [r for r in rows if r["arm"] == arm]
        cost, good, bad, inv = (sum(r[k] for r in rs) for k in ("cost", "good", "bad", "invest"))
        by = defaultdict(float)
        for r in rs:
            for k, v in r["spend"].items():
                by[k] += v
        print(f"{arm}: cost ${cost:.2f}  goodput ${good:.2f} ({100*good/cost:.0f}%)  badput ${bad:.2f} "
              f"({100*bad/cost:.0f}%)  investment ${inv:.2f}   "
              + "  ".join(f"{k}={v:.2f}" for k, v in sorted(by.items(), key=lambda kv: -kv[1])))


if __name__ == "__main__":
    main()

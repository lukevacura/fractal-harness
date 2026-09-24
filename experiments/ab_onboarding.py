"""A/B: does the claim cache reduce exploration cost on a real repo?

Arm A: repo as-is (its own CLAUDE.md), no MCP servers.
Arm B: same repo after `fractal init` + a pre-built claim store, with fractal-claims MCP.
Arm C: arm B plus an appended system prompt that tells the agent to query claims first
       (approximates a hook-enforced nudge; measures benefit when the cache is actually used).
Each task is a read-only question; both arms get the same model, tools, and prompt.

Usage:
  python experiments/ab_onboarding.py --a <clone A> --b <clone B> \
      --mcp-none mcp-none.json --mcp-fractal mcp-fractal.json --out results/ [--reps 2]
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

TASKS = [
    ("paint", "When a walked edge is painted on the map, how does the app decide which GeoJSON source "
              "to update for that edge? Point to the file.",
     [r"sub_edge_", r"inner_edges", r"main\.dart"]),
    ("slow_tests", "How do I run the app's slow tests, and why don't they run by default?",
     [r"--tags slow", r"--run-skipped"]),
    ("gps", "What GPS distance filter does the app use, and where is it set?",
     [r"_gpsDistanceFilterM", r"(=\s*5\b|\b5\s*(m\b|meter|metre))"]),
    ("zero_wp", "Can a Quest ever have zero waypoints? What enforces this and what happens with old saved quests?",
     [r"assert", r"release"]),
    ("generator", "Which quest generator does production use, and what does it fall back to?",
     [r"V4", r"V2"]),
]

# Cross-module questions: the baseline has to explore several files to answer.
HARD_TASKS = [
    ("gps_to_paint", "Trace what happens from a GPS fix arriving to an edge being painted as walked on the map: "
                     "which components are involved, in order, and in which files?",
     [r"snap_?matcher", r"walked_?store", r"setFeatureState"]),
    ("quest_complete", "How does the app detect that a quest is complete, and what happens when it completes?",
     [r"questIsComplete", r"quest_complete_screen|QuestCompleteScreen"]),
    ("edge_ids", "Explain how canonical edge IDs are produced and how they reach the app at runtime.",
     [r"build_canonical_graph", r"canonical_edges\.geojson", r"pubspec|rootBundle"]),
    ("generators", "List every quest generator version, where each one is used (production vs tests), "
                   "and why the older ones are still kept.",
     [r"V1|quest_generator\.dart", r"V3", r"V4", r"harness|baseline|comparison"]),
    ("new_store", "I want to add a new SQLite-backed store whose state widgets react to. What conventions "
                  "should it follow for provider registration and reactivity? Point to an existing example.",
     [r"providers\.dart", r"AsyncNotifier"]),
    ("fog", "How does the fog-of-war reveal work: what does it depend on and which files implement it?",
     [r"fog_mask", r"walked|discover"]),
]
TASK_SETS = {"easy": TASKS, "hard": HARD_TASKS}

# Key facts per hard task, each verified against sidewalk's source (case-insensitive regexes).
# Recall = share of facts an answer states; it measures useful output, not just pass/fail.
FACTS = {
    "gps_to_paint": [r"_onGpsUpdate", r"\bingest\b", r"CanonicalGraph|candidatesWithDistance",
                     r"WalkedStore|walked_store", r"_drawClaimedSegments", r"setFeatureState",
                     r"sub_edge_", r"accuracy"],
    "quest_complete": [r"questIsComplete", r"_maybeRecordQuestCompletion", r"_onWaypointDiscovered",
                       r"recordCompletion|completed_quests", r"xpForQuest|\bXP\b",
                       r"QuestCompleteScreen|quest_complete_screen", r"pending.?celebration"],
    "edge_ids": [r"build_canonical_graph", r"[\"'`]edge_|edge_\{|edge_<|edge_N\b|edge_\d",
                 r"build_inner_graphs", r"sub_edge_", r"canonical_edges\.geojson", r"app/assets|assets/",
                 r"rootBundle|pubspec"],
    "generators": [r"V1|quest_generator\.dart", r"V2", r"V3", r"V4", r"main\.dart",
                   r"harness|baseline|regression", r"rough_loop"],
    "new_store": [r"providers\.dart", r"AsyncNotifierProvider", r"AsyncNotifier\b|extends AsyncNotifier",
                  r"ref\.watch", r"AsyncData|state\s*=", r"openDatabase|sqflite",
                  r"walked_store|subway_station_store|discovery_store"],
    "fog": [r"fog_mask|FogMask", r"walked", r"buffer", r"\b40\s*m|kBufferMeters", r"union|dissolve",
            r"neighbou?rhood|NTA", r"fog-mask|GeoJSON source|runtime source", r"tier|gradient|feather"],
}

SUFFIX = "\n\nAnswer concisely. Do not modify any files."
ALLOWED = ["Read", "Grep", "Glob", "Bash(ls:*)", "Bash(grep:*)", "Bash(find:*)", "Bash(cat:*)",
           "Bash(head:*)", "Bash(sed -n:*)", "Bash(git log:*)", "mcp__fractal-claims"]
DENIED = ["Edit", "Write", "NotebookEdit"]
NUDGE = ("This repo has a verified claim cache. Before reading or searching code, call "
         "mcp__fractal-claims__claims_query with keywords from the question; rely on `verified` "
         "claims instead of re-reading the files they cover.")


def run_one(arm: str, repo: Path, mcp: Path, task: tuple, rep: int, out: Path, model: str,
            nudge: bool = False, settings: Path | None = None) -> dict:
    tid, prompt, keys = task
    injected: list[dict] = []
    if settings:  # what the prompt hook will inject for this run, captured before it runs
        from fractal_harness.hook import select
        injected = [{"id": e.id, "reads": e.reads} for e in select(repo.resolve(), prompt, log=False)]
    raw = out / f"{arm}_{tid}_{rep}.jsonl"
    cmd = ["claude", "-p", prompt + SUFFIX, "--output-format", "stream-json", "--verbose",
           "--model", model, "--strict-mcp-config", "--mcp-config", str(mcp),
           "--allowedTools", *ALLOWED, "--disallowedTools", *DENIED,
           "--no-session-persistence", "--max-turns", "40"]
    if nudge:
        cmd += ["--append-system-prompt", NUDGE]
    if settings:
        cmd += ["--settings", str(settings)]
    t0 = time.time()
    proc = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, timeout=900)
    raw.write_text(proc.stdout)
    tools: dict[str, int] = {}
    files_read: set[str] = set()
    result: dict = {}
    for line in proc.stdout.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "assistant":
            for block in ev.get("message", {}).get("content", []):
                if block.get("type") == "tool_use":
                    tools[block["name"]] = tools.get(block["name"], 0) + 1
                    if block["name"] == "Read":
                        files_read.add(block.get("input", {}).get("file_path", ""))
        elif ev.get("type") == "result":
            result = ev
    answer = result.get("result", "") or ""
    usage = result.get("usage", {})
    facts = FACTS.get(tid, [])
    facts_hit = sum(bool(re.search(f, answer, re.I)) for f in facts)
    return {
        "arm": arm, "task": tid, "rep": rep,
        "ok": proc.returncode == 0 and bool(result),
        "correct": all(re.search(k, answer, re.I) for k in keys),
        "cost_usd": result.get("total_cost_usd"),
        "input_tokens": usage.get("input_tokens", 0),
        "cache_read_tokens": usage.get("cache_read_input_tokens", 0),
        "cache_write_tokens": usage.get("cache_creation_input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "turns": result.get("num_turns"),
        "duration_s": round(time.time() - t0, 1),
        "tool_calls": sum(tools.values()),
        "tools": tools,
        "files_read": len(files_read),
        "claim_calls": sum(v for k, v in tools.items() if k.startswith("mcp__fractal-claims")),
        "facts_hit": facts_hit, "facts_total": len(facts),
        "recall": facts_hit / len(facts) if facts else None,
        "injected": injected,
        "answer": answer,
        "stderr": proc.stderr[-500:],
    }


def record_rep(repo: Path, out: Path, rep: int) -> dict:
    """Learn claims from arm H's transcripts of one rep (stream-json lacks the prompt; prepend it)."""
    from fractal_harness.record import record
    prompts = {t[0]: t[1] for ts in TASK_SETS.values() for t in ts}
    paths = []
    for f in sorted(out.glob(f"H_*_{rep}.jsonl")):
        tid = f.stem[2:].rsplit("_", 1)[0]
        tmp = out / f"record_input_{f.name}"
        tmp.write_text(json.dumps({"type": "user", "message": {"content": prompts[tid]}}) + "\n" + f.read_text())
        paths.append(tmp)
    t0 = time.time()
    r = record(repo, paths)
    return {"after_rep": rep, "duration_s": round(time.time() - t0, 1), **r}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--a", type=Path, required=True)
    p.add_argument("--b", type=Path, required=True)
    p.add_argument("--mcp-none", type=Path, required=True)
    p.add_argument("--mcp-fractal", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--reps", type=int, default=2)
    p.add_argument("--model", default="sonnet")
    p.add_argument("--parallel", type=int, default=4)
    p.add_argument("--tasks", choices=list(TASK_SETS), default="easy")
    p.add_argument("--arms", default="A,B,C", help="subset of A,B,C,H (H = claims pushed by hook + MCP)")
    p.add_argument("--hook-settings", type=Path, help="settings file installing the prompt hook (arm H)")
    p.add_argument("--record", action="store_true",
                   help="after each rep, run `fractal record` on arm H's transcripts (compounding)")
    args = p.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    all_arms = {"A": (args.a, args.mcp_none, False, None), "B": (args.b, args.mcp_fractal, False, None),
                "C": (args.b, args.mcp_fractal, True, None), "H": (args.b, args.mcp_fractal, False, args.hook_settings)}
    arms = args.arms.split(",")
    rows = []
    investments: list[dict] = []
    # Reps run in sequence so later reps can reuse claims recorded by earlier ones (compounding).
    for rep in range(args.reps):
        jobs = [(arm, *all_arms[arm], task, rep) for task in TASK_SETS[args.tasks] for arm in arms]
        with ThreadPoolExecutor(args.parallel) as pool:
            rows += list(pool.map(lambda j: run_one(j[0], j[1], j[2], j[5], j[6], args.out, args.model,
                                                    j[3], j[4]), jobs))
        (args.out / "results.json").write_text(json.dumps(rows, indent=2))
        if args.record and "H" in arms and rep < args.reps - 1:
            investments.append(record_rep(args.b, args.out, rep))
            (args.out / "investment.json").write_text(json.dumps(investments, indent=2))
    (args.out / "results.json").write_text(json.dumps(rows, indent=2))

    def agg(arm: str, key: str, rep: int | None = None) -> float:
        vals = [r[key] or 0 for r in rows if r["arm"] == arm and r["ok"] and rep in (None, r["rep"])]
        return sum(vals) / len(vals) if vals else float("nan")

    print(f"{'metric':22}" + "".join(f"{a:>12}" for a in arms))
    for key in ("cost_usd", "recall", "input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens",
                "turns", "tool_calls", "files_read", "claim_calls", "duration_s"):
        print(f"{key:22}" + "".join(f"{agg(a, key):12.3f}" for a in arms))
    for arm in arms:
        rs = [r for r in rows if r["arm"] == arm]
        print(f"{arm}: {sum(r['correct'] for r in rs)}/{len(rs)} correct, {sum(not r['ok'] for r in rs)} errored")
    print("\nby rep (compounding): mean cost / mean fact recall")
    for rep in range(args.reps):
        print(f"  rep {rep}: " + "  ".join(f"{a}=${agg(a, 'cost_usd', rep):.3f}/{agg(a, 'recall', rep):.2f}"
                                          for a in arms))
    for inv in investments:
        print(f"  record after rep {inv['after_rep']}: {inv['sessions']} sessions -> "
              f"+{inv['claims_added']} claims, ${inv['cost_usd']:.3f}")


if __name__ == "__main__":
    main()

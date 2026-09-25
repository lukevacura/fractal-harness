"""Task-map A/B on Q: does working from the behavioral graph help the agent?

  B  baseline agent (same model, same tools incl. subagents), no fractal hooks
  M  the task map is injected at the prompt (regions, contracts = definition of done,
     upstream assumptions, downstream contracts, weak spots) and the turn-end gate runs as
     a backstop

Each task is a real commit: the agent starts at its parent with a symptom-level description;
the commit's tests (hidden) grade it. Regressions are measured identically in both arms after
the run: the behavioral graph (baselined at the task's start) runs every contract the changes
affect; a regression is one that passed at baseline and fails now.

Usage: python experiments/q_map_ab.py --src ~/Projects/Q/q --out <dir> [--reps 1] [--budget 12]
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import time
from pathlib import Path

from q_invariant_ab import TASKS as UI_TASKS, env, flutter_tests, git, worktree

from fractal_harness.behavior import baseline, gate, import_tests

TASKS = {
    "h6-monitor": {
        "commit": "d4ae56d",
        "text": (
            "AudioService arms a first-frame monitor whenever playback of a song starts. It is only reset on some "
            "paths: when playSong is reached through playNext, playPrevious, replayCurrent or reloadCurrentItem, "
            "or when playSong throws, a stale first-frame callback from the earlier attempt can still fire later "
            "and record wrong telemetry. Make every call to AudioService.playSong leave no stale first-frame "
            "monitor armed, for all callers, including when playSong throws."
        ),
    },
    "h3-deferral": {
        "commit": "dd5cd02",
        "text": (
            "In QueueService, a suppressed track completion is deferred and replayed later by the reconciler. After "
            "a failed back-skip (playPrevious) followed by replayCurrent, that stale deferral can still fire and "
            "cause a spurious advance to the next song. A deferral must only apply to the play transition it was "
            "captured in: once a newer play transition happens, a stale deferral must never fire. User-initiated "
            "play intents (playNext when the user asks for it, playPrevious, replayCurrent) must clear any pending "
            "deferral, advance halt and autoplay plan before acting; reloadCurrentItem recovers the same song, so "
            "it clears only the deferral; completion-driven advances must not clear this state. For tests, expose "
            "`@visibleForTesting String? get suppressedCompletionSongIdForTest` returning the song id of the "
            "pending deferral, or null."
        ),
    },
    "queue-failed": UI_TASKS["queue-failed"],
    "halt-banner": UI_TASKS["halt-banner"],
}

HOOKS_M = {"hooks": {
    "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "fractal hook prompt"}]}],
    "Stop": [{"hooks": [{"type": "command", "command": "fractal hook stop"}]}],
}}
TOOLS = ["Read", "Write", "Edit", "MultiEdit", "Glob", "Grep", "Agent", "Bash(flutter test:*)",
         "Bash(flutter analyze:*)", "Bash(dart format:*)", "Bash(ls:*)"]


def seed_graph(repo: Path, base: str, out: Path) -> Path:
    """Behavioral graph + baseline verdicts at the task's starting point (no model calls)."""
    store = out / f"graph-{base.replace('~', '_')}"
    if store.exists():
        return store
    wt = worktree(repo, f"graph-{base.replace('~', '_')}", base)
    subprocess.run(["flutter", "pub", "get"], cwd=wt, capture_output=True, env=env(), timeout=600)
    import os
    old = os.environ.get("PATH", "")
    os.environ["PATH"] = env()["PATH"]
    try:
        import_tests(wt, "flutter")
        b = baseline(wt, timeout=1800)
    finally:
        os.environ["PATH"] = old
    print(f"graph at {base}: {b}")
    shutil.copytree(wt / ".fractal", store)
    subprocess.run(["git", "worktree", "remove", "--force", str(wt)], cwd=repo)
    return store


def run_one(repo: Path, task: str, arm: str, graph: Path, rep: int, model: str, out: Path) -> dict:
    spec = TASKS[task]
    wt = worktree(repo, f"{arm}-{task}-{rep}", f"{spec['commit']}~1")
    subprocess.run(["flutter", "pub", "get"], cwd=wt, capture_output=True, env=env(), timeout=600)
    tools_allowed = TOOLS + (["Bash(fractal:*)"] if arm == "M" else [])
    cmd = ["claude", "-p", spec["text"], "--output-format", "stream-json", "--verbose", "--model", model,
           "--strict-mcp-config", "--permission-mode", "acceptEdits", "--allowedTools", *tools_allowed,
           "--no-session-persistence", "--max-turns", "100"]
    if arm == "M":
        shutil.copytree(graph, wt / ".fractal")
        settings = out / "hooks-M.json"
        settings.write_text(json.dumps(HOOKS_M))
        cmd += ["--settings", str(settings)]
    raw = out / f"{arm}_{task}_{rep}.jsonl"
    t0 = time.time()
    proc = subprocess.run(cmd, cwd=wt, capture_output=True, text=True, timeout=5400, env=env())
    wall = round(time.time() - t0, 1)
    raw.write_text(proc.stdout)
    tools, result = {}, {}
    for line in proc.stdout.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "assistant" and not ev.get("parent_tool_use_id"):
            for b in ev["message"].get("content", []):
                if b.get("type") == "tool_use":
                    tools[b["name"]] = tools.get(b["name"], 0) + 1
        elif ev.get("type") == "result":
            result = ev

    # Measure regressions identically for both arms: a fresh copy of the baselined graph.
    if (wt / ".fractal").exists():
        out_count = subprocess.run(["sqlite3", str(wt / ".fractal" / "edges.db"),
                                    "select count(*) from events where kind='gate_block'"],
                                   capture_output=True, text=True).stdout.strip()
        blocks = int(out_count or 0)
        shutil.rmtree(wt / ".fractal")
    else:
        blocks = 0
    shutil.copytree(graph, wt / ".fractal")
    import os
    old = os.environ.get("PATH", "")
    os.environ["PATH"] = env()["PATH"]
    try:
        g = gate(wt, timeout=1800)
    finally:
        os.environ["PATH"] = old
    regressions = sorted({e.probe["file"] for e in g["regressions"]})

    hidden = [f for f in git(wt, "show", "--name-only", "--format=", spec["commit"], "--", "test").splitlines() if f]
    subprocess.run(["git", "checkout", spec["commit"], "--", *hidden], cwd=wt, check=True)
    passed, failed = flutter_tests(wt, hidden)
    subprocess.run(["git", "worktree", "remove", "--force", str(wt)], cwd=repo)
    return {"arm": arm, "task": task, "rep": rep, "cost_usd": result.get("total_cost_usd") or 0.0,
            "turns": result.get("num_turns"), "wall_s": wall, "tool_calls": sum(tools.values()), "tools": tools,
            "subagents": tools.get("Agent", 0) + tools.get("Task", 0),
            "hidden": [passed, failed], "hidden_pass": round(passed / (passed + failed), 3),
            "regressions": regressions, "contracts_checked": g["checked"], "gate_blocks": blocks,
            "summary": (result.get("result") or "")[-300:]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--reps", type=int, default=1)
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--budget", type=float, default=12.0)
    ap.add_argument("--tasks", default=",".join(TASKS))
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    repo = args.out / "q"
    if not repo.exists():
        subprocess.run(["git", "clone", "-q", "--local", str(args.src.expanduser()), str(repo)], check=True)
        (repo / ".git" / "info" / "exclude").write_text(".wt/\n.fractal/\n")
    rows_path = args.out / "results.json"
    rows = json.loads(rows_path.read_text()) if rows_path.exists() else []
    spent = sum(r["cost_usd"] for r in rows)
    for rep in range(args.reps):
        for task in args.tasks.split(","):
            graph = seed_graph(repo, f"{TASKS[task]['commit']}~1", args.out)
            for arm in ("B", "M"):
                if spent >= args.budget:
                    print(f"STOPPED: budget ${args.budget:.2f} reached (spent ${spent:.2f})")
                    break
                r = run_one(repo, task, arm, graph, rep, args.model, args.out)
                spent += r["cost_usd"]
                rows.append(r)
                rows_path.write_text(json.dumps(rows, indent=2))
                print(f"{arm} {task} #{rep}: hidden={r['hidden']} regressions={r['regressions']} "
                      f"blocks={r['gate_blocks']} subagents={r['subagents']} calls={r['tool_calls']} "
                      f"turns={r['turns']} ${r['cost_usd']:.2f} {r['wall_s']}s | spent ${spent:.2f}", flush=True)
    print(f"total spent ${spent:.2f}")


if __name__ == "__main__":
    main()

"""Invariant A/B on a real Flutter codebase (Q), SWE-bench style.

Each task is a real commit: the agent starts at the commit's parent with a user-level task
description (implementation hints and codebase conventions removed), and the commit's own
tests, never shown to the agent, grade the result. Same model, same hooks installed; the only
difference between arms is whether the codebase's conventions are enforced:

  F  rules only proposed (never injected, never enforced)
  R  rules enforced: injected as guarantees, checked in memory before every edit, re-checked after

The rules are conventions Q's code already follows (one is the owner's own source-scanning
test), none of which the task text mentions. Measured per run: hidden-test pass rate, rule
violations left in the final code, blocked edits, `flutter analyze` errors in touched files,
tool calls, turns, cost, wall-clock.

Usage: python experiments/q_invariant_ab.py --src ~/Projects/Q/q --out <dir> [--reps 1] [--budget 10]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from fractal_harness import probes
from fractal_harness.cache import ClaimCache
from fractal_harness.record import NO_HOOKS_ENV

FLUTTER_BIN = Path.home() / "Projects" / "flutter" / "bin"

RULES = [
    ("Border radii use QRadius tokens: never BorderRadius.circular(3), (6) or (10) (use QRadius.xsBr / smBr / mdBr).",
     {"type": "grep", "pattern": r"BorderRadius\.circular\((3|6|10)(\.0)?\)", "paths": ["lib/**/*.dart"],
      "expect": "absent", "exclude": r"^\s*//"}),
    ("Network images load only through QCoverImage (lib/widgets/q_cover_image.dart): no NetworkImage, "
     "CachedNetworkImage or Image.network elsewhere (edit_showcases_sheet.dart is grandfathered).",
     {"type": "grep", "pattern": r"\b(Cached)?NetworkImage\(|Image\.network\(", "paths": ["lib/**/*.dart"],
      "expect": "absent", "exclude": r"^\s*//",
      "exclude_paths": ["lib/widgets/q_cover_image.dart", "lib/widgets/edit_showcases_sheet.dart"]}),
    ("Never use print(): log with debugPrint (q_audio_handler.dart is grandfathered).",
     {"type": "grep", "pattern": r"(?<![\w.])print\(", "paths": ["lib/**/*.dart"], "expect": "absent",
      "exclude": r"^\s*//", "exclude_paths": ["lib/services/q_audio_handler.dart"]}),
    ("No new BackdropFilter (too expensive on scrolling surfaces): blur with ImageFiltered; the three existing "
     "uses are grandfathered.",
     {"type": "grep", "pattern": r"BackdropFilter\(", "paths": ["lib/**/*.dart"], "expect": "absent",
      "exclude": r"^\s*//", "exclude_paths": ["lib/skin_store_page.dart", "lib/widgets/banner_header.dart",
                                             "lib/widgets/collection_folder_overlay.dart"]}),
]

TASKS = {
    "queue-failed": {
        "commit": "1adb777",
        "text": (
            "In the queue, songs that have permanently failed to play should be visibly marked. QueueService "
            "already tracks them (`failedSongIds`, a ValueNotifier). Add a `bool isFailed = false` parameter to "
            "QSongTile (lib/widgets/q_song_tile.dart). In the expressive style, a failed tile dims its leading "
            "area (cover and title) with an `Opacity` of exactly 0.45 and shows an `Icons.error_outline` icon in "
            "the trailing position. The failed marker loses precedence to an explicit `trailing` widget, to the "
            "current-song equalizer (`isCurrent`), and to selection state. Tapping a failed tile behaves as today "
            "(it retries). In lib/queue_page.dart, pass `isFailed` at every place the queue renders song rows and "
            "rebuild when `failedSongIds` changes."
        ),
    },
    "halt-banner": {
        "commit": "5181383",
        "text": (
            "When playback auto-advance halts (QueueService.instance.autoAdvanceHalted, a ValueNotifier<bool>, "
            "becomes true because songs keep failing to load), the user currently gets no persistent explanation. "
            "Add a `PlaybackHaltBanner` widget in lib/widgets/playback_halt_banner.dart with a const constructor. "
            "While autoAdvanceHalted is true it shows a banner whose text contains 'Playback paused — songs "
            "couldn't load. Retrying…' and a 'Retry now' action that retries playback; when it becomes false the "
            "banner collapses (animate the size change) and its text is no longer in the tree. Show the banner on "
            "the music player page (lib/music_player_page.dart)."
        ),
    },
}


def git(cwd: Path, *args: str, check: bool = True) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=check).stdout.strip()


def env() -> dict:
    e = {k: v for k, v in os.environ.items() if k not in (NO_HOOKS_ENV, "FRACTAL_ROOT")}
    e["PATH"] = f"{FLUTTER_BIN}:{e.get('PATH', '')}"
    return e


def worktree(repo: Path, name: str, ref: str) -> Path:
    wt = repo / ".wt" / name
    if wt.exists():
        subprocess.run(["git", "worktree", "remove", "--force", str(wt)], cwd=repo)
    wt.parent.mkdir(exist_ok=True)
    subprocess.run(["git", "worktree", "add", "-q", "--detach", str(wt), ref], cwd=repo, check=True)
    return wt


def seed_store(repo: Path, base: str, enforce: bool, out: Path) -> Path:
    store = out / f"store-{base}-{'R' if enforce else 'F'}"
    if store.exists():
        return store
    wt = worktree(repo, f"seed-{base}", base)
    c = ClaimCache(wt)
    for text, probe in RULES:
        e = c.put(text, probe["paths"], kind="invariant", probe=probe)
        assert e.status == "verified", f"rule fails at base {base}: {text}: {e.detail}"
        if enforce:
            c.set_rule(e.id, "enforced")
    c.close()
    shutil.copytree(wt / ".fractal", store)
    subprocess.run(["git", "worktree", "remove", "--force", str(wt)], cwd=repo)
    return store


HOOKS = {"hooks": {
    "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "fractal hook prompt"}]}],
    "PreToolUse": [{"matcher": "Edit|Write|MultiEdit", "hooks": [{"type": "command", "command": "fractal hook pre-edit"}]}],
    "PostToolUse": [{"matcher": "Edit|Write|MultiEdit|NotebookEdit",
                     "hooks": [{"type": "command", "command": "fractal hook edit"}]}],
}}


def flutter_tests(wt: Path, files: list[str]) -> tuple[int, int]:
    """(passed, failed) using flutter's JSON reporter."""
    proc = subprocess.run(["flutter", "test", "--reporter", "json", *files], cwd=wt, capture_output=True,
                          text=True, env=env(), timeout=900)
    passed = failed = 0
    for line in proc.stdout.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "testDone" and not ev.get("hidden"):
            if ev.get("result") == "success" and not ev.get("skipped"):
                passed += 1
            elif ev.get("result") in ("failure", "error"):
                failed += 1
    if passed + failed == 0:   # compile error: count every hidden test as failed
        failed = 1
    return passed, failed


def analyze_errors(wt: Path, files: list[str]) -> int:
    dart = [f for f in files if f.endswith(".dart") and (wt / f).exists()]
    if not dart:
        return 0
    proc = subprocess.run(["flutter", "analyze", "--no-pub", *dart], cwd=wt, capture_output=True, text=True,
                          env=env(), timeout=600)
    return sum(1 for line in proc.stdout.splitlines() if line.strip().startswith("error"))


def run_one(repo: Path, task: str, arm: str, store: Path, rep: int, model: str, out: Path) -> dict:
    spec = TASKS[task]
    base = f"{spec['commit']}~1"
    wt = worktree(repo, f"{arm}-{task}-{rep}", base)
    subprocess.run(["flutter", "pub", "get"], cwd=wt, capture_output=True, env=env(), timeout=600)
    shutil.copytree(store, wt / ".fractal")
    settings = out / "hooks.json"
    settings.write_text(json.dumps(HOOKS))
    raw = out / f"{arm}_{task}_{rep}.jsonl"
    cmd = ["claude", "-p", spec["text"], "--output-format", "stream-json", "--verbose", "--model", model,
           "--strict-mcp-config", "--settings", str(settings), "--permission-mode", "acceptEdits",
           "--allowedTools", "Read", "Write", "Edit", "MultiEdit", "Glob", "Grep", "Bash(flutter test:*)",
           "Bash(flutter analyze:*)", "Bash(dart format:*)", "Bash(ls:*)",
           "--no-session-persistence", "--max-turns", "80"]
    t0 = time.time()
    proc = subprocess.run(cmd, cwd=wt, capture_output=True, text=True, timeout=3600, env=env())
    wall = round(time.time() - t0, 1)
    raw.write_text(proc.stdout)
    tools, result = {}, {}
    for line in proc.stdout.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "assistant":
            for b in ev["message"].get("content", []):
                if b.get("type") == "tool_use":
                    tools[b["name"]] = tools.get(b["name"], 0) + 1
        elif ev.get("type") == "result":
            result = ev

    # git() strips its output, which would eat the leading status column of the first line
    porcelain = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=wt,
                               capture_output=True, text=True).stdout
    touched = [line[3:].strip() for line in porcelain.splitlines() if line[3:] and not line[3:].startswith(".fractal")]
    violations = [text for text, probe in RULES if not probes.run(probe, wt).passed]
    c = ClaimCache(wt)
    stats = c.stats()
    c.close()
    analyze = analyze_errors(wt, [f for f in touched if f.startswith("lib/")])
    hidden = [f for f in git(wt, "show", "--name-only", "--format=", spec["commit"], "--", "test").splitlines() if f]
    subprocess.run(["git", "checkout", spec["commit"], "--", *hidden], cwd=wt, check=True)
    passed, failed = flutter_tests(wt, hidden)
    subprocess.run(["git", "worktree", "remove", "--force", str(wt)], cwd=repo)
    return {"arm": arm, "task": task, "rep": rep, "cost_usd": result.get("total_cost_usd") or 0.0,
            "turns": result.get("num_turns"), "wall_s": wall, "tool_calls": sum(tools.values()), "tools": tools,
            "hidden_pass": round(passed / (passed + failed), 3), "hidden": [passed, failed],
            "final_violations": violations, "edits_blocked": stats["edits_blocked"],
            "blocked_chars": stats["blocked_chars"], "post_edit_violations": stats["post_edit_violations"],
            "analyze_errors": analyze, "touched": touched, "summary": (result.get("result") or "")[-300:]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--reps", type=int, default=1)
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--budget", type=float, default=10.0)
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
            base = f"{TASKS[task]['commit']}~1"
            for arm in ("F", "R"):
                if spent >= args.budget:
                    print(f"STOPPED: budget ${args.budget:.2f} reached (spent ${spent:.2f})")
                    break
                store = seed_store(repo, base, arm == "R", args.out)
                r = run_one(repo, task, arm, store, rep, args.model, args.out)
                spent += r["cost_usd"]
                rows.append(r)
                rows_path.write_text(json.dumps(rows, indent=2))
                print(f"{arm} {task} #{rep}: hidden={r['hidden']} violations={len(r['final_violations'])} "
                      f"blocked={r['edits_blocked']} analyze_err={r['analyze_errors']} calls={r['tool_calls']} "
                      f"turns={r['turns']} ${r['cost_usd']:.2f} {r['wall_s']}s | spent ${spent:.2f}")
                if r["final_violations"]:
                    print("    violated: " + " | ".join(v[:60] for v in r["final_violations"]))
    print(f"total spent ${spent:.2f}")


if __name__ == "__main__":
    main()

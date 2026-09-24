"""History replay: how much do claim updates cost as a real repo evolves?

Clone the repo at an older commit, onboard there (agent-authored claims), then step forward
one commit at a time. After each commit, `update()` classifies every affected claim by how
its code moved (local, no model call). Every K commits, `repair --all` fixes what broke; its
cost and outcomes are recorded. Git is only the source of realistic edits here; the harness
itself never reads git.

Usage:
  python experiments/replay.py --repo ~/Projects/sidewalk --work <dir> --back 20 --repair-every 5
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from fractal_harness.cache import ClaimCache
from fractal_harness.record import NO_HOOKS_ENV
from fractal_harness.repair import repair
from fractal_harness.setup import SKILL, _template

ONBOARD_TOOLS = ["Read", "Grep", "Glob", "Bash(fractal:*)", "Bash(ls:*)", "Bash(git log:*)"]


def git(work: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=work, capture_output=True, text=True, check=True).stdout.strip()


def setup(repo: Path, work: Path, back: int) -> list[str]:
    if work.exists():
        shutil.rmtree(work)
    subprocess.run(["git", "clone", "-q", "--local", str(repo), str(work)], check=True)
    commits = git(work, "rev-list", "--reverse", "--first-parent", f"HEAD~{back}..HEAD").split()
    git(work, "checkout", "-q", f"HEAD~{back}")
    # Keep the store and skill out of the tracked tree so checkouts never conflict.
    with (work / ".git" / "info" / "exclude").open("a") as f:
        f.write("\n.fractal/\n.claude/\n")
    ClaimCache(work).close()
    skill = work / ".claude" / "skills" / SKILL / "SKILL.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text(_template("onboard_skill.md"))
    return commits


def onboard(work: Path, model: str) -> dict:
    t0 = time.time()
    proc = subprocess.run(
        ["claude", "-p", "/fractal-onboard", "--output-format", "json", "--model", model,
         "--strict-mcp-config", "--allowedTools", *ONBOARD_TOOLS,
         "--disallowedTools", "Edit", "Write", "NotebookEdit", "--no-session-persistence", "--max-turns", "120"],
        cwd=work, capture_output=True, text=True, timeout=3600,
        env={**os.environ, NO_HOOKS_ENV: "1", "FRACTAL_ROOT": str(work)})
    try:
        r = json.loads(proc.stdout)
    except json.JSONDecodeError:
        r = {"result": proc.stderr[-500:]}
    c = ClaimCache(work)
    stats = c.stats()
    c.close()
    return {"cost_usd": r.get("total_cost_usd"), "turns": r.get("num_turns"),
            "duration_s": round(time.time() - t0), "claims": stats["edges"], "by_status": stats["by_status"]}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--repo", type=Path, required=True)
    p.add_argument("--work", type=Path, required=True)
    p.add_argument("--back", type=int, default=20)
    p.add_argument("--repair-every", type=int, default=5)
    p.add_argument("--model", default="sonnet")
    p.add_argument("--reuse", action="store_true", help="reuse an onboarded work dir (skip clone+onboard)")
    args = p.parse_args()
    work = args.work.resolve()
    log: dict = {"steps": [], "repairs": []}

    if args.reuse:
        commits = json.loads((work.parent / "replay_commits.json").read_text())
        git(work, "checkout", "-q", git(work, "rev-parse", f"{commits[0]}~1"))
        log["onboard"] = json.loads((work.parent / "replay_onboard.json").read_text())
    else:
        commits = setup(args.repo.resolve(), work, args.back)
        (work.parent / "replay_commits.json").write_text(json.dumps(commits))
        log["onboard"] = onboard(work, args.model)
        (work.parent / "replay_onboard.json").write_text(json.dumps(log["onboard"]))
        # Snapshot the freshly onboarded store so later runs can --reuse it.
        shutil.copy(work / ".fractal" / "edges.db", work.parent / "replay_onboard.db")
    if args.reuse:
        shutil.copy(work.parent / "replay_onboard.db", work / ".fractal" / "edges.db")
        for f in (".fractal/queue.jsonl", ".fractal/recorded.json"):
            (work / f).unlink(missing_ok=True)
        # Re-verify at the base commit so anchors and file snapshots exist before stepping ($0).
        c = ClaimCache(work)
        c.verify()
        c.close()
    print("onboard:", log["onboard"])

    for i, commit in enumerate(commits, 1):
        git(work, "checkout", "-q", commit)
        stat = git(work, "show", "--shortstat", "--format=%s", commit).splitlines()
        c = ClaimCache(work)
        t0 = time.time()
        classes = c.update()
        elapsed = time.time() - t0
        by_status = c.stats()["by_status"]
        c.close()
        step = {"i": i, "commit": commit[:7], "subject": stat[0][:60], "shortstat": stat[-1].strip(),
                "classes": {k: len(v) for k, v in classes.items()}, "update_s": round(elapsed, 2),
                "by_status": by_status}
        log["steps"].append(step)
        print(f"{i:2} {commit[:7]} {step['classes']} ({elapsed:.1f}s)  {stat[0][:50]}")
        if args.repair_every and i % args.repair_every == 0:
            r = repair(work, everything=True, model=args.model)
            c = ClaimCache(work)
            after = c.update()
            by_status = c.stats()["by_status"]
            c.close()
            rep = {"after_commit": i, **{k: v for k, v in r.items() if k != "summary"},
                   "summary": r.get("summary", "")[-400:], "status_after": by_status,
                   "reclassified_after": {k: len(v) for k, v in after.items()}}
            log["repairs"].append(rep)
            outcomes = [e["outcome"] for e in r.get("entries", [])]
            print(f"   repair: {len(outcomes)} entries {({o: outcomes.count(o) for o in set(outcomes)})} "
                  f"+{r.get('claims_added', 0)} claims ${r.get('cost_usd', 0):.3f} -> {by_status}")
        (work.parent / "replay_log.json").write_text(json.dumps(log, indent=2))

    totals: dict[str, int] = {}
    for s in log["steps"]:
        for k, v in s["classes"].items():
            totals[k] = totals.get(k, 0) + v
    cost = sum(r.get("cost_usd") or 0 for r in log["repairs"])
    n_rep = sum(len(r.get("entries", [])) for r in log["repairs"])
    print("\nclaim updates by class over", len(commits), "commits:", totals)
    print(f"repairs: {n_rep} entries, ${cost:.2f} total" + (f", ${cost / n_rep:.3f}/entry" if n_rep else ""))


if __name__ == "__main__":
    main()

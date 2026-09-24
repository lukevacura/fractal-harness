"""Contract-chunked parallel implementation vs. baselines, scored by hidden acceptance tests.

For each task in the testbed's TASKS.md, in a fresh git repo:
  direct      one agent implements the task with no plan (the real baseline)
  per config  plan with <plan model>:<exact edge count>, then implement the skeleton with one
              agent per edge in parallel worktrees (and optionally sequentially, --sequential)
Every result is scored with black-box acceptance tests the agents never see
(testbeds/ledger_acceptance), so planned and unplanned implementations are judged alike.

Usage: python experiments/planner_bench.py --out <dir> [--tasks fx-export,budgets]
                                           [--configs sonnet:2,sonnet:6,haiku:4] [--sequential]
"""

from __future__ import annotations

import argparse
import sys
import json
import re
import shutil
import subprocess
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from fractal_planner.planner import BUDGET, BudgetExceeded, git, plan, run_direct, run_parallel, run_sequential
from fractal_planner.recursive import build

TESTBEDS = Path(__file__).parent / "testbeds"
TESTBED = TESTBEDS / "ledger"
ACCEPTANCE = Path(__file__).parent / "testbeds" / "ledger_acceptance"


def tasks() -> dict[str, str]:
    text = (TESTBED / "TASKS.md").read_text()
    return {m.group(1): m.group(2).strip() for m in re.finditer(r"^## (\S+)\n(.*?)(?=^## |\Z)", text, re.M | re.S)}


def fresh_repo(dest: Path) -> str:
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(TESTBED, dest, ignore=shutil.ignore_patterns("__pycache__", ".venv", ".pytest_cache"))
    subprocess.run(["git", "init", "-q"], cwd=dest, check=True)
    git(dest, "add", "-A")
    git(dest, "-c", "user.name=bench", "-c", "user.email=bench@localhost", "commit", "-q", "-m", "testbed")
    subprocess.run(["uv", "venv", "-q", str(dest / ".venv"), "--python", "3.13"], check=True)
    subprocess.run(["uv", "pip", "install", "-q", "--python", str(dest / ".venv" / "bin" / "python"), "pytest"],
                   check=True)
    (dest / ".git" / "info" / "exclude").write_text(".venv/\n.fractal/\n")
    return f"{dest / '.venv' / 'bin' / 'python'} -m pytest -q {{test}}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--tasks", default=",".join(tasks()))
    ap.add_argument("--model", default="sonnet", help="model for edge/direct/sequential agents")
    ap.add_argument("--configs", default="sonnet:4", help="plan configs: <plan model>:<exact edges>,...")
    ap.add_argument("--sequential", action="store_true", help="also run the sequential-from-skeleton baseline")
    ap.add_argument("--no-direct", action="store_true")
    ap.add_argument("--reps", type=int, default=1, help="repetitions of direct and recursive runs")
    ap.add_argument("--testbed", default="ledger", help="directory under experiments/testbeds")
    ap.add_argument("--manifest", action="store_true",
                    help="onboard the repo first (agent-authored claims); planner and leaves read manifests")
    ap.add_argument("--plan-only", action="store_true", help="recursive: plan, don't fill leaves")
    ap.add_argument("--leaf-max-turns", type=int, default=60)
    ap.add_argument("--check-model", default=None, help="model for checker agents (default: --model)")
    ap.add_argument("--budget", type=float, default=None,
                    help="hard spend limit in USD for agent calls; the run stops cleanly when reached")
    ap.add_argument("--protocol", choices=["tests", "implement"], default="tests",
                    help="leaf protocol: write faked-dependency tests, or implement only (checkers verify)")
    ap.add_argument("--no-manifest-too", action="store_true",
                    help="with --manifest: also plan once (plan only) without the manifest, for comparison")
    ap.add_argument("--recursive", default="", help="recursive configs: <plan model>:<max depth>[:<root groups>],... e.g. sonnet:2:3")
    args = ap.parse_args()
    BUDGET.set(args.budget)
    try:
        run(args)
    except BudgetExceeded as e:
        print(f"STOPPED: {e}. Partial results are in {args.out / 'report.json'}.")
    finally:
        print(f"agent spend this run: ${BUDGET.spent:.2f}" + (f" of ${args.budget:.2f}" if args.budget else ""))


def run(args) -> None:
    global TESTBED
    TESTBED = TESTBEDS / args.testbed
    args.out.mkdir(parents=True, exist_ok=True)
    report_path = args.out / "report.json"
    report = json.loads(report_path.read_text()) if report_path.exists() else {}
    for name in args.tasks.split(","):
        repo = args.out / name
        test_cmd = fresh_repo(repo)
        acceptance = str(ACCEPTANCE / f"test_accept_{name.replace('-', '_')}.py")
        entry = report.setdefault(name, {})
        if args.manifest:
            from replay import onboard
            from fractal_harness.cache import ClaimCache
            from fractal_harness.setup import SKILL, _template
            ClaimCache(repo).close()
            skill = repo / ".claude" / "skills" / SKILL / "SKILL.md"
            skill.parent.mkdir(parents=True, exist_ok=True)
            skill.write_text(_template("onboard_skill.md"))
            with (repo / ".git" / "info" / "exclude").open("a") as f:
                f.write(".claude/\n")
            BUDGET.check()
            ob = onboard(repo, "sonnet")
            BUDGET.add(ob.get("cost_usd") or 0.0)
            entry["onboard"] = ob
            print(f"[{name}] onboard: {ob}")
            if args.no_manifest_too:
                for cfg in filter(None, args.recursive.split(",")):
                    plan_model, depth, *rest = cfg.split(":")
                    b0 = build(repo, tasks()[name], test_cmd, args.model, plan_model, max_depth=int(depth),
                               plan_only=True)
                    p0 = b0["planned"]
                    entry[f"plan-only:no-manifest:{cfg}"] = {k: v for k, v in p0.items() if k != "tree"} | {
                        "tree": p0["tree"].to_dict()}
                    print(f"[{name}] plan-only WITHOUT manifest {cfg}: {p0['plan_s']}s ${p0['cost_usd']:.2f} "
                          f"levels={[(l['depth'], l['nodes'], l['wall_s']) for l in p0['levels']]}")
        for rep in range(args.reps):
          if not args.no_direct:
            d = run_direct(repo, tasks()[name], test_cmd, args.model, acceptance)
            entry[f"direct:{args.model}#{rep}"] = d
            print(f"[{name}] direct {args.model} #{rep}: {d['wall_s']}s ${d['cost_usd']:.2f} "
                  f"full={d['merge']['full_suite']} accept={d['merge']['acceptance']}")
            report_path.write_text(json.dumps(report, indent=2))
          for cfg in filter(None, args.recursive.split(",")):
              plan_model, depth, *rest = cfg.split(":")
              b = build(repo, tasks()[name], test_cmd, args.model, plan_model, max_depth=int(depth),
                        acceptance=acceptance, root_groups=int(rest[0]) if rest else None,
                        use_manifest=args.manifest, leaf_max_turns=args.leaf_max_turns, plan_only=args.plan_only,
                        protocol=args.protocol, check_model=args.check_model)
              planned, res = b["planned"], b["run"]
              if res is None:
                  entry[f"plan-only:{'manifest' if args.manifest else 'code'}:{cfg}#{rep}"] = {
                      k: v for k, v in planned.items() if k != "tree"} | {"tree": planned["tree"].to_dict()}
                  print(f"[{name}] plan-only {'WITH manifest' if args.manifest else ''} {cfg}: {planned['plan_s']}s "
                        f"${planned['cost_usd']:.2f} levels={[(l['depth'], l['nodes'], l['wall_s']) for l in planned['levels']]}")
                  for n in planned["tree"].walk():
                      print(f"    {'  ' * n.depth}{n.path} {'LEAF' if n.leaf else ''} writes={n.writes}")
                  report_path.write_text(json.dumps(report, indent=2))
                  continue
              tree = planned["tree"]
              nodes = list(tree.walk())
              m = res["merge"]
              leaves_ok = sum(l["passed"] for l in res["leaves"])
              if res.get("rounds"):
                  print(f"[{name}]   rounds: " + " -> ".join(
                      f"checks={r['checks']} baseline={r.get('baseline_ok')} retry_cuts={r.get('retry_cuts')} "
                      f"accept={r['acceptance']}" for r in res["rounds"]))
              print(f"[{name}] recursive {cfg}: total {b['total_s']}s ${b['cost_usd']:.2f} | plan {planned['plan_s']}s "
                    f"${planned['cost_usd']:.2f} levels={[(l['depth'], l['nodes'], l['wall_s']) for l in planned['levels']]} "
                    f"retries={planned['retries']} errors={planned['errors']} | leaves {leaves_ok}/{len(res['leaves'])} "
                    f"in {res['leaves_s']}s, all incl. checks {res['fill_s']}s ${res['cost_usd']:.2f} | "
                    f"checks={m['node_checks']} full={m['full_suite']} accept={m['acceptance']}")
              for n in nodes:
                  print(f"    {'  ' * n.depth}{n.path} {'LEAF' if n.leaf else ''} writes={n.writes}")
              entry[f"recursive:{cfg}:{args.model}#{rep}"] = {"plan": {k: v for k, v in planned.items() if k != "tree"},
                                           "tree": tree.to_dict(), "run": res, "total_s": b["total_s"],
                                           "cost_usd": b["cost_usd"]}
              report_path.write_text(json.dumps(report, indent=2))
        for cfg in filter(None, args.configs.split(",")):
            plan_model, n = cfg.split(":")
            p = plan(repo, tasks()[name], test_cmd, plan_model, exact_edges=int(n))
            par = run_parallel(repo, p, args.model, workers=max(4, len(p.edges)), acceptance=acceptance)
            m = par["merge"]
            print(f"[{name}] {cfg}: plan {len(p.edges)} edges {p.duration_s}s ${p.cost_usd:.2f} | parallel "
                  f"{par['wall_s']}s ${par['cost_usd']:.2f} passed={sum(e['passed'] for e in par['edges'])}/"
                  f"{len(par['edges'])} violations={sum(bool(e['violations']) for e in par['edges'])} "
                  f"integration={m['integration']} full={m['full_suite']} accept={m['acceptance']}")
            result = {"plan": p.to_dict(), "parallel": par}
            if args.sequential:
                seq = run_sequential(repo, p, args.model, acceptance)
                result["sequential"] = seq
                print(f"[{name}] {cfg} sequential: {seq['wall_s']}s ${seq['cost_usd']:.2f} "
                      f"accept={seq['merge']['acceptance']}")
            entry[cfg] = result
            report_path.write_text(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()

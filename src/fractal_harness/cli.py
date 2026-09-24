"""`fractal` command line. Works against any repo (--root, $FRACTAL_ROOT, or the enclosing git repo)."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from .cache import CacheError, ClaimCache
from .probes import ProbeError
from .store import Edge


def resolve_root(explicit: str | None, serving: bool = False) -> Path:
    if explicit:
        return Path(explicit).resolve()
    # Claude Code sets CLAUDE_PROJECT_DIR for MCP servers. Only trust it when serving:
    # shells spawned by Claude Code inherit it even when cd'd into another repo.
    for var in ("FRACTAL_ROOT", "CLAUDE_PROJECT_DIR") if serving else ("FRACTAL_ROOT",):
        if os.environ.get(var):
            return Path(os.environ[var]).resolve()
    try:
        out = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, check=True)
        return Path(out.stdout.strip())
    except (subprocess.CalledProcessError, FileNotFoundError):
        return Path.cwd()


def _fmt(e: Edge) -> str:
    pre = f"{{{e.pre}}} ⟹ " if e.pre else ""
    line = f"{e.id}  [{e.status:8}] {e.kind}: {pre}{e.post}"
    if e.detail and e.status not in ("verified",):
        line += f"\n{'':20}{e.detail}"
    return line


def _emit(args: argparse.Namespace, edges: list[Edge]) -> None:
    if args.json:
        print(json.dumps([e.to_dict() for e in edges], indent=2))
    else:
        for e in edges:
            print(_fmt(e))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="fractal", description=__doc__)
    p.add_argument("--root", help="target repo (default: $FRACTAL_ROOT or enclosing git repo)")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    sub = p.add_subparsers(dest="cmd", required=True)

    put = sub.add_parser("put", help="assert a claim and check it")
    put.add_argument("claim", help="the postcondition / claim text")
    put.add_argument("--pre", default="", help="precondition")
    put.add_argument("--kind", default="knowledge",
                     choices=["knowledge", "workflow", "task", "invariant", "interface"],
                     help="invariant: a rule the code must follow; `fractal check` fails when it breaks")
    put.add_argument("--read", action="append", default=[], help="source path it depends on (repeatable)")
    put.add_argument("--write", action="append", default=[], help="path it may modify (repeatable)")
    put.add_argument("--dep", action="append", default=[], help="edge id it depends on (repeatable)")
    put.add_argument("--parent", help="parent edge id (for zoomed sub-edges)")
    put.add_argument("--probe", help='json probe, e.g. \'{"type":"grep","pattern":"x","paths":["src/**/*.py"]}\'')
    put.add_argument("--run", help="shorthand for a command probe that must exit 0")
    put.add_argument("--delta", action="store_true", help="this put is a delta repair (see `repair`)")
    put.add_argument("--level", type=int, choices=[1, 2, 3], help="zoom level: 1 coarse .. 3 fine")
    put.add_argument("--region", action="append", default=[], help="code region (glob) the claim describes")

    nd = sub.add_parser("needed", help="is a step needed? (redundant if its outcome already holds)")
    nd.add_argument("claim", help="the step's outcome (postcondition)")
    nd.add_argument("--pre", default="")
    nd.add_argument("--kind", default="task", choices=["knowledge", "workflow", "task"])
    nd.add_argument("--read", action="append", default=[])
    nd.add_argument("--probe", help="json probe that passes only once the outcome holds")
    nd.add_argument("--run", help="shorthand for a command probe that must exit 0")
    nd.add_argument("--deliberate", action="store_true", help="intentional redundancy; never pruned")

    pr = sub.add_parser("propose", help="propose an invariant (a rule the code must follow); a human accepts it")
    pr.add_argument("rule", help="the rule, stated so a violation is unambiguous")
    pr.add_argument("--read", action="append", default=[], help="source path it depends on (repeatable)")
    pr.add_argument("--probe", help="json probe that fails when the rule is violated")
    pr.add_argument("--run", help="shorthand for a command probe that must exit 0")
    inv = sub.add_parser("invariants", help="list invariants by state: proposed, enforced, rejected")
    inv.add_argument("--state", choices=["proposed", "enforced", "rejected"])
    inv.add_argument("--audit", action="store_true", help="show each rule's probe audit verdict")
    for name, verb in (("accept", "enforce"), ("reject", "reject")):
        a = sub.add_parser(name, help=f"(human) {verb} proposed invariants")
        a.add_argument("ids", nargs="+")
        a.add_argument("--yes", action="store_true", help="allow a non-interactive terminal (scripts)")

    q = sub.add_parser("query", help="find claims (re-verifies stale matches)")
    q.add_argument("text", nargs="?", default="")
    q.add_argument("--path", action="append", default=[], help="only claims reading under this path")
    q.add_argument("--all", action="store_true", help="include stale/failed/pending")
    q.add_argument("--limit", type=int, default=20)

    sub.add_parser("list", help="list every edge")
    show = sub.add_parser("show", help="show one edge as json")
    show.add_argument("id")
    rm = sub.add_parser("rm", help="delete an edge")
    rm.add_argument("id")
    sub.add_parser("refresh", help="mark edges whose sources changed as stale (no probes run)")
    sub.add_parser("update", help="after edits: re-check stale claims and classify how their code "
                                  "moved (clean/moved/suspect/delta/scene_cut); no model call")
    rp = sub.add_parser("repair", help="fix broken claims with minimal context (default: demanded ones)")
    rp.add_argument("ids", nargs="*")
    rp.add_argument("--all", action="store_true", help="every broken claim, not just demanded ones")
    rp.add_argument("--model", default="sonnet")
    rp.add_argument("--dry-run", action="store_true", help="print the repair prompt, don't run it")
    v = sub.add_parser("verify", help="re-run probes (default: all edges)")
    v.add_argument("ids", nargs="*")
    sub.add_parser("stats", help="counts and telemetry summary")
    mf = sub.add_parser("manifest", help="render verified claims for a region/level as an owner's context")
    mf.add_argument("--region", action="append", default=[], help="glob(s); default: whole repo")
    mf.add_argument("--level", type=int, choices=[1, 2, 3], help="maximum detail level")
    au = sub.add_parser("audit", help="mutation-test probes in memory: do they fail when the claim breaks?")
    au.add_argument("ids", nargs="*")
    au.add_argument("--all-verdicts", action="store_true", help="also list claims that passed the audit")
    pl = sub.add_parser("plan", help="(prototype) plan a task as parallel edges: writes a skeleton commit")
    pl.add_argument("task")
    pl.add_argument("--test-cmd", required=True, help='shell template with {test}, e.g. "python -m pytest -q {test}"')
    pl.add_argument("--max-edges", type=int, default=4)
    pl.add_argument("--model", default="sonnet")
    rn = sub.add_parser("run", help="(prototype) implement a plan's edges in parallel worktrees and merge")
    rn.add_argument("plan_id")
    rn.add_argument("--sequential", action="store_true", help="one agent implements every edge (baseline)")
    rn.add_argument("--workers", type=int, default=4)
    rn.add_argument("--model", default="sonnet")
    ck = sub.add_parser("check", help="pre-commit/CI gate: re-check claims; exit 1 if an invariant is violated")
    ck.add_argument("--strict", action="store_true", help="also fail when non-invariant claims need repair")
    sub.add_parser("mcp", help="serve the cache over MCP (stdio)")
    ini = sub.add_parser("init", help="set up the target repo for Claude Code (idempotent)")
    ini.add_argument("--no-settings", action="store_true", help="don't touch .claude/settings.json")
    ini.add_argument("--mcp", action="store_true", help="also register the fractal-claims MCP server")
    ini.add_argument("--git-hook", action="store_true", help="install `fractal check` as a git pre-commit hook")
    sub.add_parser("doctor", help="check the target repo is ready")
    hk = sub.add_parser("hook", help="Claude Code hook entry points (read hook JSON on stdin)")
    hk.add_argument("event", choices=["prompt", "stop", "edit"],
                    help="prompt: UserPromptSubmit claim injection; stop: queue session for `record`; "
                         "edit: PostToolUse re-check of enforced invariants governing the edited file")
    rec = sub.add_parser("record", help="extract claims from queued (or given) session transcripts")
    rec.add_argument("transcripts", nargs="*", type=Path, help="transcript/stream-json files (default: queue)")
    rec.add_argument("--model", default="sonnet")
    rec.add_argument("--max-per-session", type=int, default=4)
    rec.add_argument("--dry-run", action="store_true", help="print the extraction prompt, don't run it")
    rec.add_argument("--force", action="store_true", help="record even hits, light, and duplicate sessions")

    args = p.parse_args(argv)
    root = resolve_root(args.root, serving=args.cmd == "mcp")

    if args.cmd == "mcp":
        from .mcp_server import serve
        serve(root)
        return 0
    if args.cmd == "hook":
        from .hook import edit_hook, prompt_hook, stop_hook
        if args.event == "edit":
            code, msg = edit_hook(sys.stdin.read())
            if msg:
                print(msg, file=sys.stderr)
            return code
        if args.event == "stop":
            stop_hook(sys.stdin.read())
            return 0
        out = prompt_hook(sys.stdin.read())
        if out:
            print(out)
        return 0
    if args.cmd == "record":
        from .record import record
        r = record(root, args.transcripts or None, args.model, args.max_per_session, args.dry_run, args.force)
        if args.dry_run and "prompt" in r:
            print(r["prompt"])
        print(json.dumps({k: v for k, v in r.items() if k != "prompt"}, indent=2),
              file=sys.stderr if args.dry_run else sys.stdout)
        return 0
    if args.cmd == "init":
        from .setup import init
        changes = init(root, settings=not args.no_settings, mcp=args.mcp, git_hook=args.git_hook)
        print(f"fractal init: {root}")
        for c in changes:
            print(f"  {c}")
        print("\nNext: run /fractal-onboard in Claude Code (optionally with a path) to seed claims."
              "\nMatching verified claims are then injected into each prompt automatically;"
              "\nrun `fractal record` now and then to learn claims from finished sessions.")
        return 0
    if args.cmd == "doctor":
        from .setup import doctor
        results = doctor(root)
        for ok, msg in results:
            print(f"{'ok  ' if ok else 'FAIL'} {msg}")
        return 0 if all(ok for ok, _ in results) else 1

    if args.cmd == "check":
        from .check import check
        code, report = check(root, strict=args.strict)
        if report:
            print(report, file=sys.stderr if code else sys.stdout)
        return code
    if args.cmd in ("plan", "run"):
        from . import planner
        if args.cmd == "plan":
            p = planner.plan(root, args.task, args.test_cmd, args.model, args.max_edges)
            print(f"plan {p.id}: {len(p.edges)} edges, skeleton {p.skeleton[:10]}, ${p.cost_usd:.2f}, {p.duration_s}s")
            print(p.summary())
            for d in p.dropped:
                print(f"dropped {d['id']}: {d['reason']}")
            return 0
        p = planner.load(root, args.plan_id)
        r = planner.run_sequential(root, p, args.model) if args.sequential else \
            planner.run_parallel(root, p, args.model, args.workers)
        out = root / ".fractal" / "plans" / f"{p.id}-{r['mode']}.json"
        out.write_text(json.dumps(r, indent=2))
        print(json.dumps({k: v for k, v in r.items() if k != "edges"}, indent=2))
        return 0 if r["merge"]["integration"] else 1
    if args.cmd == "manifest":
        from .manifest import manifest
        print(manifest(root, args.region or None, args.level), end="")
        return 0
    if args.cmd == "audit":
        from .audit import audit
        results = audit(root, args.ids or None)
        if args.json:
            print(json.dumps([a.to_dict() for a in results], indent=2))
        else:
            for a in results:
                if a.verdict in ("weak", "partial") or args.all_verdicts:
                    print(f"{a.id}  [{a.verdict:7}] {a.claim[:100]}")
                    for f in a.findings:
                        print(f"{'':20}- {f}")
            counts = {v: sum(a.verdict == v for a in results) for v in ("ok", "partial", "weak", "n/a")}
            print(" ".join(f"{k}={v}" for k, v in counts.items()))
        return 1 if any(a.verdict == "weak" for a in results) else 0

    if args.cmd in ("accept", "reject") and not (sys.stdin.isatty() or args.yes):
        print(f"error: `fractal {args.cmd}` is a human decision; run it in an interactive terminal "
              "(or pass --yes from a script you control)", file=sys.stderr)
        return 2

    cache = ClaimCache(root)
    try:
        if args.cmd == "propose":
            probe = json.loads(args.probe) if args.probe else None
            if args.run:
                probe = {"type": "command", "run": args.run}
            e = cache.put(args.rule, args.read, kind="invariant", probe=probe)
            _emit(args, [e])
            if not args.json and e.rule_state == "proposed":
                print(f"{'':20}proposed; a human enforces it with: fractal accept {e.id}")
        elif args.cmd in ("accept", "reject"):
            state = "enforced" if args.cmd == "accept" else "rejected"
            for i in args.ids:
                e = cache.set_rule(i, state)
                print(f"{e.id}  {state}: {e.post}" + (f"  (currently {e.status}: {e.detail})"
                                                       if e.status != "verified" else ""))
        elif args.cmd == "invariants":
            rules = cache.invariants((args.state,) if args.state else ("proposed", "enforced", "rejected"))
            verdicts = {}
            if args.audit:
                from .audit import audit_edge
                verdicts = {e.id: audit_edge(e, cache.root).verdict for e in rules}
            if args.json:
                print(json.dumps([{**e.to_dict(), "audit": verdicts.get(e.id)} for e in rules], indent=2))
            else:
                order = {"enforced": 0, "proposed": 1, "rejected": 2}
                for e in sorted(rules, key=lambda e: (order[e.rule_state], e.post)):
                    audit_note = f" audit={verdicts[e.id]}" if e.id in verdicts else ""
                    print(f"{e.id}  [{e.rule_state:8}] [{e.status:8}]{audit_note} {e.post}")
                    if e.status != "verified":
                        print(f"{'':20}{e.detail}")
                print(" ".join(f"{s}={sum(e.rule_state == s for e in rules)}"
                               for s in ("enforced", "proposed", "rejected")))
        elif args.cmd == "put":
            probe = json.loads(args.probe) if args.probe else None
            if args.run:
                probe = {"type": "command", "run": args.run}
            _emit(args, [cache.put(args.claim, args.read, pre=args.pre, kind=args.kind, probe=probe,
                                   writes=args.write, depends_on=args.dep, parent_id=args.parent,
                                   delta=args.delta, level=args.level, region=args.region)])
        elif args.cmd == "needed":
            probe = json.loads(args.probe) if args.probe else None
            if args.run:
                probe = {"type": "command", "run": args.run}
            v = cache.needed(args.claim, args.read, pre=args.pre, kind=args.kind, probe=probe,
                             deliberate=args.deliberate)
            print(json.dumps(v, indent=2) if args.json else
                  f"{'NEEDED   ' if v['needed'] else 'REDUNDANT'} {v['reason']}")
            return 0 if v["needed"] else 3
        elif args.cmd == "query":
            _emit(args, cache.query(args.text, args.path, None if args.all else ("verified", "trusted"),
                                    args.limit))
        elif args.cmd == "list":
            _emit(args, cache.store.all())
        elif args.cmd == "show":
            e = cache.get(args.id)
            if e is None:
                raise CacheError(f"unknown edge: {args.id}")
            print(json.dumps(e.to_dict(), indent=2))
        elif args.cmd == "rm":
            stale = cache.delete(args.id)
            print(f"deleted {args.id}; {len(stale)} dependents now stale")
        elif args.cmd == "refresh":
            stale = cache.refresh()
            _emit(args, [cache.get(i) for i in stale])
            if not args.json:
                print(f"{len(stale)} edges newly stale")
        elif args.cmd == "update":
            out = cache.update()
            if args.json:
                print(json.dumps(out, indent=2))
            else:
                for kind in ("clean", "moved", "fresh", "suspect", "delta", "scene_cut", "blocked"):
                    if out.get(kind):
                        print(f"{kind:10} {len(out[kind]):3}  {' '.join(out[kind][:8])}")
                if not out:
                    print("nothing changed")
        elif args.cmd == "repair":
            from .repair import repair
            r = repair(root, args.ids or None, args.all, args.model, args.dry_run)
            if args.dry_run and "prompt" in r:
                print(r["prompt"])
            else:
                print(json.dumps(r, indent=2))
        elif args.cmd == "verify":
            _emit(args, cache.verify(args.ids or None))
        elif args.cmd == "stats":
            print(json.dumps(cache.stats(), indent=2))
    except (CacheError, ProbeError, json.JSONDecodeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        cache.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

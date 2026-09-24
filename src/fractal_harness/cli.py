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

MOTION_CLASSES = ("clean", "moved", "fresh", "suspect", "delta", "rewrite", "scene_cut", "reassert", "blocked")


def resolve_root(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).resolve()
    if os.environ.get("FRACTAL_ROOT"):
        return Path(os.environ["FRACTAL_ROOT"]).resolve()
    try:
        out = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, check=True)
        return Path(out.stdout.strip())
    except (subprocess.CalledProcessError, FileNotFoundError):
        return Path.cwd()


def _fmt(e: Edge) -> str:
    pre = f"{{{e.pre}}} ⟹ " if e.pre else ""
    rule = f" ({e.rule_state})" if e.rule_state else ""
    line = f"{e.id}  [{e.status:8}] {e.kind}{rule}: {pre}{e.post}"
    if e.detail and e.status != "verified":
        line += f"\n{'':20}{e.detail}"
    return line


def _emit(args: argparse.Namespace, edges: list[Edge]) -> None:
    if args.json:
        print(json.dumps([e.to_dict() for e in edges], indent=2))
    else:
        for e in edges:
            print(_fmt(e))


def _probe_args(parser: argparse.ArgumentParser, probe_help: str) -> None:
    parser.add_argument("--read", action="append", default=[], help="source path it depends on (repeatable)")
    parser.add_argument("--probe", help=probe_help)
    parser.add_argument("--run", help="shorthand for a command probe that must exit 0")


def _probe(args: argparse.Namespace) -> dict | None:
    if args.run:
        return {"type": "command", "run": args.run}
    return json.loads(args.probe) if args.probe else None


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="fractal", description=__doc__)
    p.add_argument("--root", help="target repo (default: $FRACTAL_ROOT or enclosing git repo)")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    sub = p.add_subparsers(dest="cmd", required=True)

    # -- invariants: the rules (human-owned) -------------------------------------------------
    pr = sub.add_parser("propose", help="propose an invariant (a rule the code must follow); a human accepts it")
    pr.add_argument("rule", help="the rule, stated so a violation is unambiguous")
    _probe_args(pr, "json probe that fails when the rule is violated")
    inv = sub.add_parser("invariants", help="list invariants by state: proposed, enforced, rejected")
    inv.add_argument("--state", choices=["proposed", "enforced", "rejected"])
    inv.add_argument("--audit", action="store_true", help="show each rule's probe audit verdict")
    for name, verb in (("accept", "enforce"), ("reject", "reject")):
        a = sub.add_parser(name, help=f"(human) {verb} proposed invariants")
        a.add_argument("ids", nargs="+")
        a.add_argument("--yes", action="store_true", help="allow a non-interactive terminal (scripts)")
    ck = sub.add_parser("check", help="pre-commit/CI gate: re-check claims; exit 1 if an enforced invariant is violated")
    ck.add_argument("--strict", action="store_true", help="also fail when other claims need repair")

    # -- claims: descriptive knowledge (machine-maintained) --------------------------------
    put = sub.add_parser("put", help="assert a claim and check it")
    put.add_argument("claim", help="the claim text")
    put.add_argument("--pre", default="", help="precondition (workflows)")
    put.add_argument("--kind", default="knowledge", choices=["knowledge", "workflow", "interface", "invariant"],
                     help="invariant = a proposed rule (same as `propose`)")
    _probe_args(put, 'json probe, e.g. \'{"type":"grep","pattern":"x","paths":["src/**/*.py"]}\'')
    put.add_argument("--dep", action="append", default=[], help="claim id it depends on (repeatable)")
    put.add_argument("--parent", help="parent claim id (for zoomed sub-claims)")
    put.add_argument("--delta", action="store_true", help="this put is a delta repair (see `repair`)")
    q = sub.add_parser("query", help="find claims (re-verifies stale matches)")
    q.add_argument("text", nargs="?", default="")
    q.add_argument("--path", action="append", default=[], help="only claims depending on this path")
    q.add_argument("--all", action="store_true", help="include stale and failed claims")
    q.add_argument("--limit", type=int, default=20)
    sub.add_parser("list", help="list every claim")
    show = sub.add_parser("show", help="show one claim as json")
    show.add_argument("id")
    rm = sub.add_parser("rm", help="delete a claim")
    rm.add_argument("id")
    mf = sub.add_parser("manifest", help="an owner's context for a region: the rules and facts that govern it")
    mf.add_argument("region", nargs="?", default="", help="directory or file (default: whole repo)")

    # -- keeping claims true -----------------------------------------------------------------
    sub.add_parser("refresh", help="mark claims whose sources changed as stale (no probes run)")
    sub.add_parser("update", help="after edits: re-check stale claims and classify how their code moved; no model")
    v = sub.add_parser("verify", help="re-run probes (default: all claims)")
    v.add_argument("ids", nargs="*")
    rp = sub.add_parser("repair", help="fix broken descriptive claims with minimal context (default: demanded ones)")
    rp.add_argument("ids", nargs="*")
    rp.add_argument("--all", action="store_true", help="every broken claim, not just demanded ones")
    rp.add_argument("--model", default="sonnet")
    rp.add_argument("--dry-run", action="store_true", help="print the repair prompt, don't run it")
    rec = sub.add_parser("record", help="learn claims from queued (or given) session transcripts")
    rec.add_argument("transcripts", nargs="*", type=Path, help="transcript/stream-json files (default: queue)")
    rec.add_argument("--model", default="sonnet")
    rec.add_argument("--max-per-session", type=int, default=4)
    rec.add_argument("--dry-run", action="store_true", help="print the extraction prompt, don't run it")
    rec.add_argument("--force", action="store_true", help="record even hits, light, and duplicate sessions")
    au = sub.add_parser("audit", help="mutation-test probes in memory: do they fail when the claim breaks?")
    au.add_argument("ids", nargs="*")
    au.add_argument("--all-verdicts", action="store_true", help="also list claims that passed the audit")
    sub.add_parser("stats", help="counts and real-use telemetry")

    # -- setup and hooks ---------------------------------------------------------------------
    ini = sub.add_parser("init", help="set up the target repo for Claude Code (idempotent)")
    ini.add_argument("--no-settings", action="store_true", help="don't touch .claude/settings.json")
    ini.add_argument("--git-hook", action="store_true", help="install `fractal check` as a git pre-commit hook")
    sub.add_parser("doctor", help="check the target repo is ready")
    hk = sub.add_parser("hook", help="Claude Code hook entry points (read hook JSON on stdin)")
    hk.add_argument("event", choices=["prompt", "pre-edit", "edit", "stop"],
                    help="prompt: inject rules and facts; pre-edit: block an edit that would violate an "
                         "enforced invariant; edit: re-check after an edit; stop: queue the session for `record`")

    args = p.parse_args(argv)
    root = resolve_root(args.root)

    if args.cmd == "hook":
        from .hook import edit_hook, pre_edit_hook, prompt_hook, stop_hook
        stdin = sys.stdin.read()
        if args.event in ("edit", "pre-edit"):
            code, msg = (pre_edit_hook if args.event == "pre-edit" else edit_hook)(stdin)
            if msg:
                print(msg, file=sys.stderr)
            return code
        if args.event == "stop":
            stop_hook(stdin)
            return 0
        out = prompt_hook(stdin)
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
        changes = init(root, settings=not args.no_settings, git_hook=args.git_hook)
        print(f"fractal init: {root}")
        for c in changes:
            print(f"  {c}")
        print("\nNext: run /fractal-onboard in Claude Code to seed facts and propose rules;"
              "\nreview proposals with `fractal invariants --audit` and enforce them with `fractal accept <id>`.")
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
    if args.cmd == "manifest":
        from .manifest import manifest
        print(manifest(root, args.region), end="")
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
            e = cache.put(args.rule, args.read, kind="invariant", probe=_probe(args))
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
            _emit(args, [cache.put(args.claim, args.read, pre=args.pre, kind=args.kind, probe=_probe(args),
                                   depends_on=args.dep, parent_id=args.parent, delta=args.delta)])
        elif args.cmd == "query":
            _emit(args, cache.query(args.text, args.path, None if args.all else ("verified", "trusted"),
                                    args.limit))
        elif args.cmd == "list":
            _emit(args, cache.store.all())
        elif args.cmd == "show":
            e = cache.get(args.id)
            if e is None:
                raise CacheError(f"unknown claim: {args.id}")
            print(json.dumps(e.to_dict(), indent=2))
        elif args.cmd == "rm":
            stale = cache.delete(args.id)
            print(f"deleted {args.id}; {len(stale)} dependents now stale")
        elif args.cmd == "refresh":
            stale = cache.refresh()
            _emit(args, [cache.get(i) for i in stale])
            if not args.json:
                print(f"{len(stale)} claims newly stale")
        elif args.cmd == "update":
            out = cache.update()
            if args.json:
                print(json.dumps(out, indent=2))
            else:
                for kind in MOTION_CLASSES:
                    if out.get(kind):
                        print(f"{kind:10} {len(out[kind]):3}  {' '.join(out[kind][:8])}")
                if not out:
                    print("nothing changed")
        elif args.cmd == "repair":
            from .repair import repair
            r = repair(root, args.ids or None, args.all, args.model, args.dry_run)
            print(r["prompt"] if args.dry_run and "prompt" in r else json.dumps(r, indent=2))
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

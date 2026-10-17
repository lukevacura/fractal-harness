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
from .symbols import SymbolError
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
    ck.add_argument("--message", metavar="FILE", help="commit-msg hook mode: check only the rules over this commit message file")
    ck.add_argument("--strict", action="store_true", help="also fail when other claims need repair")

    # -- behavior: the codebase as a graph of behavioral contracts ------------------------------
    it = sub.add_parser("import-tests", help="turn every test file into a behavioral claim over the code it imports")
    it.add_argument("--runner", choices=["flutter", "dart", "pytest"], help="default: detected from the repo")
    it.add_argument("--limit", type=int, help="import at most N test files")
    mp = sub.add_parser("map", help="a task's footprint on the behavioral graph: regions, contracts, assumptions")
    mp.add_argument("task")
    bh = sub.add_parser("behavior", help="run behavioral claims: the ones current changes affect, or --all (baseline)")
    bh.add_argument("--all", action="store_true", help="run every behavioral claim (establishes the baseline)")
    bh.add_argument("--timeout", type=int, default=600)

    # -- claims: descriptive knowledge (machine-maintained) --------------------------------
    put = sub.add_parser("put", help="assert a claim and check it")
    put.add_argument("claim", nargs="?", help="the claim text (omit with --section/--symbol)")
    put.add_argument("--section", metavar="FILE::HEADING",
                     help="claim carrying that markdown section's exact text, stale when the section changes")
    put.add_argument("--symbol", metavar="FILE::ITEM",
                     help="interface claim carrying that code item's signature and docs, stale when they change")
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
    lg = sub.add_parser("ledger", help="the mutant ledger: record verification verdicts per node")
    lsub = lg.add_subparsers(dest="ledger_cmd", required=True)
    ln = lsub.add_parser("node", help="register a node's spec; the decisions/interfaces it cites become its premises")
    ln.add_argument("node")
    ln.add_argument("--spec", required=True)
    ln.add_argument("--id-pattern")
    ln.add_argument("--branch", help="the node's branch: verdicts default to its tip (else `node-<name>`, else HEAD)")
    ln.add_argument("--fix", action="store_true", help="write the missing parents/exports the header lint computes "
                    "into the spec (prints the diff; the spec is yours to commit)")
    ln.add_argument("--dry-run", action="store_true", help="with --fix: print the diff, change nothing")
    lx = lsub.add_parser("rm", help="deregister a node that left the DAG (it stops counting as a child)")
    lx.add_argument("node")
    lx.add_argument("--forget-verdicts", action="store_true", help="also delete its recorded verdicts")
    la = lsub.add_parser("reattribute", help="correct the commit a node's verdicts were verified at (audited)")
    la.add_argument("node")
    la.add_argument("--from", dest="old", required=True, help="the wrong commit (a prefix is enough)")
    la.add_argument("--to", required=True, help="the commit the verifiers actually checked")
    la.add_argument("--reason", required=True)
    la.add_argument("--force", action="store_true", help="even if a verdict's file or span is absent at --to")
    lg_log = lsub.add_parser("log", help="the audit trail of corrections to recorded verdicts")
    lg_log.add_argument("node", nargs="?")
    lr = lsub.add_parser("record", help="record a mutant's verdict at a commit")
    lr.add_argument("node")
    lr.add_argument("--json", metavar="FILE", help="a JSON list of verdicts ('-' for stdin) instead of flags: "
                    "[{file, span, mutant, result, tests, argument, commit}]")
    lr.add_argument("--file")
    lr.add_argument("--span", help="N or N-M: 1-based lines in the commit's numbering")
    lr.add_argument("--mutant", help="what was changed")
    lr.add_argument("--result", choices=["killed", "survived", "equivalent"])
    lr.add_argument("--tests", default="", help="comma-separated names of the tests that kill it")
    lr.add_argument("--argument", default="", help="why an equivalent mutant is equivalent")
    lr.add_argument("--commit", help="the commit verified; applies to every verdict that names none "
                    "(default: the node's branch tip, else this checkout's HEAD)")
    vp = sub.add_parser("verify-plan", help="which recorded verdicts are stale for a node, and the diff to re-verify")
    vp.add_argument("node")
    vp.add_argument("--against", metavar="REV", help="judge staleness against this commit instead of the working tree")
    vp.add_argument("--with-arguments", action="store_true", help="include prior equivalence arguments")
    vp.add_argument("--no-diff", action="store_true")
    oi = sub.add_parser("only-in", help="propose a single-ownership rule: a pattern or symbol kind appears only in given files")
    oi.add_argument("concept", help='what is owned once, e.g. "ranking expression"')
    oi.add_argument("--pattern", help="regex for the thing that must exist in one place")
    oi.add_argument("--kind", help="or a symbol kind (struct, enum, trait, class, fn, def, type) ...")
    oi.add_argument("--name", help="... whose name matches this regex (default: any)")
    oi.add_argument("--in", dest="files", action="append", required=True, help="the owner file or glob (repeatable)")
    oi.add_argument("--paths", action="append", required=True, help="where to look (glob, repeatable)")
    oi.add_argument("--exclude", help="skip lines matching this regex")
    cn = sub.add_parser("concepts", help="the registry of single-ownership claims: concept -> owner -> still true?")
    cn.add_argument("--write", metavar="FILE", help="also save it as markdown")
    rf = sub.add_parser("refs", help="which files mention a symbol, on every live worktree (and --branches)")
    rf.add_argument("name")
    rf.add_argument("--branches", action="store_true", help="also local branch tips no worktree has checked out")
    rf.add_argument("--not-in", metavar="FILE", help="leave out the symbol's own file")
    gc = sub.add_parser("graph-check", help="warn about dead exports and phantom parent edges (declared graph vs code)")
    gc.add_argument("node", nargs="?", help="only this node's exports and parents")
    gc.add_argument("--strict", action="store_true", help="exit 1 when anything is reported (a commit probe)")
    cx = sub.add_parser("context", help="one bounded bundle for a spec: its cited decisions, interfaces and rules")
    cx.add_argument("spec", help="path to the spec (markdown)")
    cx.add_argument("--id-pattern", help="regex for the IDs a spec cites (default: letters + digits, e.g. D16)")
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
    ini.add_argument("--no-settings", action="store_true", help="don't touch .claude settings (hooks live in the untracked .claude/settings.local.json)")
    ini.add_argument("--git-hook", action="store_true", help="install `fractal check` as a git pre-commit hook")
    sub.add_parser("doctor", help="check the target repo is ready")
    hk = sub.add_parser("hook", help="Claude Code hook entry points (read hook JSON on stdin)")
    hk.add_argument("event", choices=["prompt", "agent", "pre-edit", "edit", "stop"],
                    help="prompt: inject rules and facts; agent: inject them into a launching subagent's prompt; pre-edit: block an edit that would violate an "
                         "enforced invariant; edit: re-check after an edit; stop: queue the session for `record`")

    args = p.parse_args(argv)
    root = resolve_root(args.root)

    if args.cmd == "hook":
        from .hook import subagent_start_hook, edit_hook, pre_edit_hook, prompt_hook, stop_hook
        stdin = sys.stdin.read()
        if args.event in ("edit", "pre-edit"):
            code, msg = (pre_edit_hook if args.event == "pre-edit" else edit_hook)(stdin)
            if msg:
                print(msg, file=sys.stderr)
            return code
        if args.event == "stop":
            code, msg = stop_hook(stdin)
            if msg:
                print(msg, file=sys.stderr)
            return code
        out = (subagent_start_hook if args.event == "agent" else prompt_hook)(stdin)
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
    if args.cmd == "import-tests":
        from .behavior import import_tests
        print(json.dumps(import_tests(root, args.runner, args.limit), indent=2))
        print("next: `fractal behavior --all` to record a baseline verdict for every behavioral claim", file=sys.stderr)
        return 0
    if args.cmd == "map":
        from .taskmap import render as render_map, task_map
        print(render_map(task_map(root, args.task), budget=20000) or "(no footprint: the task names nothing this "
              "repo's graph knows; explore normally)")
        return 0
    if args.cmd == "behavior":
        from .behavior import baseline, gate
        if args.all:
            print(json.dumps(baseline(root, args.timeout), indent=2))
            return 0
        r = gate(root, timeout=args.timeout)
        for kind in ("violations", "regressions", "still_failing"):
            for e in r[kind]:
                print(f"{kind[:-1] if kind != 'still_failing' else 'failing':10} {e.id} {e.probe['file']}: "
                      f"{r['results'][e.id].detail}")
        print(f"changed files: {r['changed']}, behavioral claims checked: {r['checked']} in {r['seconds']}s")
        return 1 if r["regressions"] or r["violations"] else 0
    if args.cmd == "check":
        from .check import check
        if args.message:
            from .check import check_message
            code, report = check_message(root, args.message)
        else:
            code, report = check(root, strict=args.strict)
        if report:
            print(report, file=sys.stderr if code else sys.stdout)
        return code
    if args.cmd == "context":
        from .context import DEFAULT_ID_PATTERN, bundle
        try:
            print(bundle(root, args.spec, args.id_pattern or DEFAULT_ID_PATTERN), end="")
        except SymbolError as e:
            print(f"fractal: {e}", file=sys.stderr)
            return 1
        return 0
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
        elif args.cmd == "ledger":
            from . import ledger
            if args.ledger_cmd == "node":
                from .context import DEFAULT_ID_PATTERN
                r = ledger.register_node(cache, root, args.node, args.spec, args.id_pattern or DEFAULT_ID_PATTERN,
                                      args.branch, args.fix, args.dry_run)
                if r.get("diff"):
                    print(r["diff"], end="")
                if r.get("dry_run"):
                    print("(dry run: nothing written or registered)")
                    return 0
                pre = r["premises"]
                print(f"{args.node}: spec {r['spec']}, " + (f"{len(pre['parents'])} parent(s), " if r["declared"]
                      else "premises inferred from the text, ") + f"{len(pre['decisions'])} decision claim(s)"
                      + (f"; no claim for: {', '.join(r['uncited'])}" if r["uncited"] else ""))
                if r["autoregistered"]:
                    print(f"  registered claims for: {', '.join(r['autoregistered'])}")
                for note in r["lint"]:
                    print(f"  note: {note}")
            elif args.ledger_cmd == "reattribute":
                n = ledger.reattribute(cache, root, args.node, args.old, args.to, args.reason, args.force)
                print(f"re-attributed {n} verdict(s) of {args.node}: {args.old} -> "
                      f"{ledger.resolve_commit(root, args.to)[:10]} (logged; see `fractal ledger log`)")
            elif args.ledger_cmd == "log":
                import time
                for a in cache.store.audit(args.node):
                    print(f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(a['ts']))}  {a['node']}  {a['action']}: {a['detail']}")
            elif args.ledger_cmd == "rm":
                orphans = ledger.deregister(cache, args.node, args.forget_verdicts)
                print(f"deregistered {args.node}" + (" and forgot its verdicts" if args.forget_verdicts else ""))
                if orphans:
                    print(f"  still declared as a parent by: {', '.join(orphans)} (their verdicts now read as stale)")
            else:
                if args.json:
                    items = json.loads(sys.stdin.read() if args.json == "-" else Path(args.json).read_text())
                else:
                    items = [{"file": args.file, "span": args.span, "mutant": args.mutant, "result": args.result,
                              "tests": args.tests, "argument": args.argument, "commit": args.commit}]
                for it in items:
                    tests = it.get("tests") or []
                    if isinstance(tests, str):
                        tests = [t.strip() for t in tests.split(",") if t.strip()]
                    if not (it.get("file") and it.get("span") and it.get("mutant") and it.get("result")):
                        raise CacheError("a verdict needs file, span, mutant and result")
                    ledger.record(cache, root, args.node, file=it["file"], span=str(it["span"]),
                                  mutant=it["mutant"], result=it["result"], commit=it.get("commit") or args.commit,
                                  tests=tests, argument=it.get("argument") or "")
                print(f"recorded {len(items)} verdict(s) for {args.node} at "
                      f"{ledger.node_commit(cache, root, args.node, args.commit)[:10]}")
        elif args.cmd == "only-in":
            probe = {"type": "only_in", "concept": args.concept, "paths": args.paths, "files": args.files}
            if args.pattern:
                probe["pattern"] = args.pattern
            if args.kind:
                probe["kind"] = args.kind
                if args.name:
                    probe["name"] = args.name
            if args.exclude:
                probe["exclude"] = args.exclude
            from . import probes
            held = probes.run(probe, root)
            if not held.passed:
                raise CacheError(f"the rule does not hold now, so it would be created failing: {held.detail}. "
                                 "Fix the pattern/--in, or the code, first")
            e = cache.put(f"{args.concept} is defined only in {', '.join(args.files)}", args.files, kind="invariant",
                          probe=probe)
            _emit(args, [e])
            if not args.json and e.rule_state == "proposed":
                print(f"{'':20}proposed; a human enforces it with: fractal accept {e.id}")
        elif args.cmd == "concepts":
            from . import concepts
            rows = concepts.registry(cache)
            text = concepts.render(rows)
            if args.write:
                Path(args.write).write_text("# Concepts\n\nGenerated by `fractal concepts`; edit the claims, not this file.\n\n" + text)
            print(json.dumps(rows, indent=2) if args.json else text, end="")
        elif args.cmd == "refs":
            from .refindex import RefIndex, is_test
            idx = RefIndex.load(root, args.branches)
            found = idx.where(args.name, args.not_in)
            if args.json:
                print(json.dumps(found, indent=2))
            else:
                for t in idx.trees:
                    files = found.get(t.label, [])
                    print(f"{t.label}{' (this checkout)' if t.current else ''}: "
                          + (", ".join(f + (" (test)" if is_test(f) else "") for f in files) or "no mentions"))
        elif args.cmd == "graph-check":
            from . import graph_check
            r = graph_check.check(cache, root, args.node)
            print(json.dumps(r, indent=2) if args.json else graph_check.render(r), end="")
            if args.strict and any(r.values()):
                return 1
        elif args.cmd == "verify-plan":
            from . import ledger
            print(ledger.render(ledger.plan(cache, root, args.node, args.against), args.with_arguments,
                                not args.no_diff, root), end="")
        elif args.cmd == "put" and (args.section or args.symbol):
            from .context import put_extracted
            _emit(args, [put_extracted(cache, root, section=args.section, symbol=args.symbol,
                                       depends_on=args.dep)])
        elif args.cmd == "put":
            if not args.claim:
                raise CacheError("put needs a claim text, or --section / --symbol")
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
            from .context import unchecked_decisions
            out = cache.update()
            unchecked = unchecked_decisions(cache, root)
            if args.json:
                print(json.dumps({**out, **({"unchecked_decisions": unchecked} if unchecked else {})}, indent=2))
            else:
                for kind in MOTION_CLASSES:
                    if out.get(kind):
                        print(f"{kind:10} {len(out[kind]):3}  {' '.join(out[kind][:8])}")
                if unchecked:
                    print(f"{'unchecked':10} {len(unchecked):3}  {' '.join(unchecked[:8])}  "
                          "(decided, but no check line, citing claim or test names them)")
                if not out and not unchecked:
                    print("nothing changed")
        elif args.cmd == "repair":
            from .repair import repair
            r = repair(root, args.ids or None, args.all, args.model, args.dry_run)
            print(r["prompt"] if args.dry_run and "prompt" in r else json.dumps(r, indent=2))
        elif args.cmd == "verify":
            _emit(args, cache.verify(args.ids or None))
        elif args.cmd == "stats":
            print(json.dumps(cache.stats(), indent=2))
    except (CacheError, ProbeError, SymbolError, json.JSONDecodeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        cache.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

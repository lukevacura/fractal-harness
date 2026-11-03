"""`fractal` command line. Works against any repo (--root, $FRACTAL_ROOT, or the enclosing git repo)."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from .cache import CacheError, ClaimCache
from .config import ConfigError
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


def _authority_fields(e: Edge, last: dict) -> dict:
    """JSON extras for a node-local or overridden rule (a plain root rule's output is unchanged)."""
    if e.layer == "root" and e.rule_state != "overridden":
        return {}
    out = {"authority": e.layer, "provenance": e.provenance}
    if e.id in last:
        out["last_override"] = last[e.id]
    return out


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
    parser.add_argument("--behavioral", action="store_true",
                        help="the rule constrains what the code does, not how it is written: a pattern alone is a gap")


def _probe(args: argparse.Namespace) -> dict | None:
    probe = {"type": "command", "run": args.run} if args.run else json.loads(args.probe) if args.probe else None
    if getattr(args, "behavioral", False):
        if probe is None:
            raise SystemExit("--behavioral needs a probe (--probe or --run)")
        probe["behavioral"] = True
    return probe


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
    inv.add_argument("--state", choices=["proposed", "enforced", "rejected", "overridden"])
    inv.add_argument("--audit", action="store_true", help="show each rule's probe audit verdict")
    for name, verb in (("accept", "enforce"), ("reject", "reject")):
        a = sub.add_parser(name, help=f"(human) {verb} proposed invariants")
        a.add_argument("ids", nargs="+")
        a.add_argument("--yes", action="store_true", help="allow a non-interactive terminal (scripts)")
        if name == "accept":
            a.add_argument("--as", dest="as_node", metavar="NODE",
                           help="accept as this node (no human): only a rule inside the node's own files, "
                                "and only where accept_policy in fractal.toml allows it")
    ov = sub.add_parser("override", help="demote an enforced rule (kept, with who/why; --undo restores it); higher layer only")
    ov.add_argument("id")
    ov.add_argument("--by", default="root", metavar="NODE|root", help="the layer overriding (default: root, the human)")
    ov.add_argument("--reason", required=True)
    ov.add_argument("--undo", action="store_true", help="restore an overridden rule")
    ov.add_argument("--yes", action="store_true", help="allow a non-interactive terminal (scripts)")
    cf = sub.add_parser("conflict", help="contradiction as the signal: a local rule that clashes with, or keeps blocking "
                                         "work under, a higher layer is decided by the parent's agent (human only at root)")
    cfs = cf.add_subparsers(dest="conflict_cmd", required=True)
    cfl = cfs.add_parser("list", help="conflicts awaiting a decision (heuristic detection)")
    cfl.add_argument("--all", action="store_true", help="also decided ones")
    cfsh = cfs.add_parser("show", help="one conflict: why, block counts, decision history")
    cfsh.add_argument("id")
    cfr = cfs.add_parser("raise", help="the spec/premise behind a claim or rule is wrong or ambiguous: raise it on the parent "
                                       "instead of weakening a probe (never edits the claim)")
    cfr.add_argument("target", help="claim or rule id")
    cfr.add_argument("--reason", required=True)
    cfr.add_argument("--by", default="agent")
    cfv = cfs.add_parser("resolve", help="decide a conflict: keep | narrow | override (the parent's agent; a human at root)")
    cfv.add_argument("id")
    cfv.add_argument("action", choices=["keep", "narrow", "override"])
    cfv.add_argument("--reason", required=True)
    cfv.add_argument("--exclude", action="append", default=[], help="narrow: path/glob the rule stops applying to (repeatable)")
    cfv.add_argument("--as", dest="as_node", metavar="NODE|root", help="acting layer (default: the handler; root = a human)")
    cfv.add_argument("--yes", action="store_true", help="allow a non-interactive terminal for a human decision")
    cfe = cfs.add_parser("escalate", help="one layer up; for a root rule (or at root) an arbitration request for a human")
    cfe.add_argument("id")
    cfe.add_argument("--reason", required=True, help="why (written explanation, required)")
    cfe.add_argument("--as", dest="as_node", metavar="NODE|root")
    cfe.add_argument("--yes", action="store_true")
    cfa = cfs.add_parser("arbitrate", help="(human) settle an arbitration request")
    cfa.add_argument("id")
    cfo = cfa.add_mutually_exclusive_group(required=True)
    cfo.add_argument("--upheld", action="store_true", help="the root rule stands")
    cfo.add_argument("--revised", action="store_true", help="the human is changing the rule (do it with fractal override)")
    cfa.add_argument("--reason", required=True)
    cfa.add_argument("--yes", action="store_true")
    ck = sub.add_parser("check", help="pre-commit/CI gate: re-check claims; exit 1 if an enforced invariant is violated")
    ck.add_argument("--message", metavar="FILE", help="commit-msg hook mode: check only the rules over this commit message file")
    ck.add_argument("--strict", action="store_true", help="also fail when other claims need repair")

    # -- behavior: the codebase as a graph of behavioral contracts ------------------------------
    it = sub.add_parser("import-tests", help="turn every test file into a behavioral claim over the code it imports")
    it.add_argument("--runner", choices=["flutter", "dart", "pytest"], help="default: detected from the repo")
    it.add_argument("--limit", type=int, help="import at most N test files")
    mp = sub.add_parser("map", help="a task's footprint on the behavioral graph: regions, contracts, assumptions")
    mp.add_argument("task")
    ij = sub.add_parser("injection", help="measure what the hooks would inject for prompts (size, shared start); no model, no telemetry")
    ij.add_argument("prompts", nargs="+")
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
    st = sub.add_parser("status", help="a node's state, derived from verdicts and digests (done|due|stale|pending); never marked")
    st.add_argument("node", nargs="?", help="one node (default: all)")
    st.add_argument("--json", action="store_true")
    st.add_argument("--marks", type=Path, help="a hand-maintained markdown table: show where its marks disagree (heuristic)")
    dg = sub.add_parser("dag", help="the node graph, generated from specs and the ledger: state, summary, uses/after edges")
    dg.add_argument("--format", choices=("md", "json"), default="md")
    dg.add_argument("--out", type=Path, help="write to this file instead of stdout")
    sub.add_parser("activations", help="which nodes are due an agent (spec, contract, probe, proposal or survivor events)")
    oi = sub.add_parser("only-in", help="propose a single-ownership rule: a pattern or symbol kind appears only in given files")
    oi.add_argument("concept", help='what is owned once, e.g. "ranking expression"')
    oi.add_argument("--pattern", help="regex for the thing that must exist in one place")
    oi.add_argument("--kind", help="or a symbol kind (struct, enum, trait, class, fn, def, type) ...")
    oi.add_argument("--name", help="... whose name matches this regex (default: any)")
    oi.add_argument("--in", dest="files", action="append", required=True, help="the owner file or glob (repeatable)")
    oi.add_argument("--paths", action="append", required=True, help="where to look (glob, repeatable)")
    oi.add_argument("--exclude", help="skip lines matching this regex")
    oi.add_argument("--decision", action="append", default=[], metavar="ID",
                    help="a decision this concept owns (repeatable); the ownership lint applies to these IDs only")
    cn = sub.add_parser("concepts", help="the registry of single-ownership claims: concept -> owner -> still true?")
    cn.add_argument("--write", metavar="FILE", help="also save it as markdown")
    rc = sub.add_parser("reclaim", help="propose (and with --verify/--apply, carry out) deleting what nothing reaches")
    rc.add_argument("--class", dest="classes", action="append", choices=["metadata", "code", "store"],
                    help="only this class (repeatable; default all)")
    rc.add_argument("--verify", action="store_true", help="run each code deletion in a scratch worktree with --cmd")
    rc.add_argument("--apply", action="store_true", help="write what is reclaimable to the working tree (implies --verify "
                    "for code); nothing is committed")
    rc.add_argument("--cmd", dest="test_cmd", help="test command that must still pass (default: $FRACTAL_MUTATE_CMD)")
    rc.add_argument("--build-cmd", default="")
    rc.add_argument("--env", action="append", default=[], metavar="K=V")
    rc.add_argument("--branches", action="store_true", help="also gate on local branch tips no worktree has checked out")
    rc.add_argument("-j", "--jobs", type=int, default=min(4, os.cpu_count() or 1))
    rc.add_argument("--timeout", type=int, default=600)
    im = sub.add_parser("impact", help="which other live worktrees use an export this node changed")
    im.add_argument("node")
    im.add_argument("--base", metavar="REV", help="compare with this revision (default: merge-base with the default branch)")
    im.add_argument("--branches", action="store_true", help="also local branch tips no worktree has checked out")
    mu = sub.add_parser("mutate", help="run a verifier's mutants in scratch worktrees, memoized; records the verdicts")
    mu.add_argument("node")
    mu.add_argument("--json", dest="mutants", required=True, metavar="FILE",
                    help="the mutants: a JSON list of {file, span, tests, find+replace | replacement} ('-' for stdin)")
    mu.add_argument("--cmd", dest="test_cmd", help="test command; {tests} is replaced by the mutant's test names "
                    "(default: $FRACTAL_MUTATE_CMD)")
    mu.add_argument("--build-cmd", default="", help="run first on each mutant; a failure marks it invalid, not killed")
    mu.add_argument("--env", action="append", default=[], metavar="K=V", help="({job} = the run's number, e.g. a target dir)")
    mu.add_argument("--commit", help="the commit to mutate (default: the node's branch tip, else HEAD)")
    mu.add_argument("-j", "--jobs", type=int, default=min(4, os.cpu_count() or 1))
    mu.add_argument("--timeout", type=int, default=600)
    mu.add_argument("--no-record", action="store_true", help="report only; record nothing in the ledger")
    mu.add_argument("--no-cache", action="store_true", help="ignore the memo")
    br = sub.add_parser("brief", help="the prompt for an agent working on a node: repo root, why due, task, spec and premises, reporting")
    br.add_argument("node")
    br.add_argument("--out", type=Path, help="write to this file instead of stdout")
    br.add_argument("--json", action="store_true")
    rpt = sub.add_parser("report", help="file an agent's scratch report into the docs with provenance, or list filed reports")
    rsub = rpt.add_subparsers(dest="report_cmd", required=True)
    rfi = rsub.add_parser("file", help="copy a scratch report to its destination, with a provenance header")
    rfi.add_argument("scratch")
    rfi.add_argument("--node", required=True)
    rfi.add_argument("--to", metavar="ROOT:PATH", help="destination (default: [brief] report_to); no file suffix = a directory")
    rfi.add_argument("--force", action="store_true", help="replace an existing file")
    rfi.add_argument("--json", action="store_true")
    rli = rsub.add_parser("list", help="reports filed from this checkout's store")
    rli.add_argument("--node")
    rli.add_argument("--json", action="store_true")
    rn = sub.add_parser("run", help="run a command at a commit on the configured backend (memoized); --detach and poll with `run status|log <id>`")
    rn.add_argument("--node", help="run at this node's commit (default: HEAD)")
    rn.add_argument("--backend", help="override [jobs] backend (local, ssh)")
    rn.add_argument("--timeout", type=int, default=3600, metavar="S")
    rn.add_argument("--detach", action="store_true", help="start it in the background and print the run id")
    rn.add_argument("--tail", type=int, help="with `log`: only the last N lines")
    rn.add_argument("words", nargs=argparse.REMAINDER, help="<command...>, or `status <id>` / `log <id>`; options go before the command")
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
    rp.add_argument("--agent", action="store_true", help="with --all: after re-verifying, hand what remains to a model")
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
    hk.add_argument("event", choices=["prompt", "session", "agent", "pre-edit", "edit", "stop"],
                    help="prompt: inject rules and facts; session: give the static instructions once at session start; agent: inject them into a launching subagent's prompt; pre-edit: block an edit that would violate an "
                         "enforced invariant; edit: re-check after an edit; stop: queue the session for `record`")

    args = p.parse_args(argv)
    root = resolve_root(args.root)

    if args.cmd == "hook":
        from .hook import subagent_start_hook, edit_hook, pre_edit_hook, prompt_hook, session_hook, stop_hook
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
        out = {"agent": subagent_start_hook, "session": session_hook}.get(args.event, prompt_hook)(stdin)
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
    if args.cmd == "injection":
        from .measure import stats
        print(json.dumps(stats(root, args.prompts), indent=2))
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
    human_only = (args.cmd == "reject" or (args.cmd == "accept" and not args.as_node)
                  or (args.cmd == "override" and args.by == "root"))
    if human_only and not (sys.stdin.isatty() or args.yes):
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
            from . import authority
            state = "enforced" if args.cmd == "accept" else "rejected"
            for i in args.ids:
                if args.cmd == "accept" and args.as_node:
                    from .config import load as load_config
                    r = authority.accept_as(cache, root, i, args.as_node, load_config(root).accept_policy)
                    e, state = r["edge"], f"enforced@{args.as_node}"
                    for w in r["warnings"]:
                        print(f"warning: {w}", file=sys.stderr)
                    if r["audit"]:
                        print(f"{'':20}audit (advisory): {r['audit']['verdict']} {'; '.join(r['audit']['findings'])}")
                else:
                    e = cache.set_rule(i, state)
                    if args.cmd == "accept":
                        authority.record_human_accept(cache, i)
                print(f"{e.id}  {state}: {e.post}" + (f"  (currently {e.status}: {e.detail})"
                                                       if e.status != "verified" else ""))
        elif args.cmd == "override":
            from . import authority
            from .config import load as load_config
            e = authority.override(cache, args.id, args.by, args.reason, load_config(root).accept_policy, args.undo)
            print(f"{e.id}  {e.rule_state}{'' if args.undo else ' (demoted, not deleted)'}: {e.post}")
        elif args.cmd == "invariants":
            from .ledger import mutation_checked
            from .probes import evidence_class, evidence_gap
            rules = cache.invariants((args.state,) if args.state else ("proposed", "enforced", "rejected", "overridden"))
            last = {h["edge_id"]: h for h in cache.store.rule_history() if h["action"] in ("override", "restore")}
            memo: dict[str, list[str]] = {}

            def checked(e):
                if e.id not in memo:
                    memo[e.id] = mutation_checked(cache, root, e.probe) if evidence_class(e.probe) == "test" else []
                return memo[e.id]
            verdicts = {}
            if args.audit:
                from .audit import audit_edge
                verdicts = {e.id: audit_edge(e, cache.root).verdict for e in rules}
            if args.json:
                print(json.dumps([{**e.to_dict(), **_authority_fields(e, last), "audit": verdicts.get(e.id),
                                   "evidence": evidence_class(e.probe),
                                   "gap": evidence_gap(e.probe), "mutation_checked": checked(e)} for e in rules], indent=2))
            else:
                from .conflict import awaiting, banner
                print("\n".join(banner(awaiting(cache))), end="")
                order = {"enforced": 0, "proposed": 1, "rejected": 2, "overridden": 3}
                for e in sorted(rules, key=lambda e: (order[e.rule_state], e.post)):
                    audit_note = f" audit={verdicts[e.id]}" if e.id in verdicts else ""
                    print(f"{e.id}  [{e.rule_state:8}] [{e.status:8}] [{evidence_class(e.probe) or 'no probe':10}]"
                          f"{audit_note}{f' @{e.layer}' if e.layer != 'root' else ''} {e.post}")
                    if e.rule_state == "overridden" and e.id in last:
                        h = last[e.id]
                        print(f"{'':20}OVERRIDDEN by {h['who']}: {h['reason']}  (held by {e.layer}; `fractal override --undo` restores)")
                    if gap := evidence_gap(e.probe):
                        print(f"{'':20}gap: {gap}")
                    if by := checked(e):
                        print(f"{'':20}mutation-checked by {', '.join(by)}")
                    if e.status != "verified":
                        print(f"{'':20}{e.detail}")
                print(" ".join(f"{s}={sum(e.rule_state == s for e in rules)}"
                               for s in ("enforced", "proposed", "rejected")
                               + (("overridden",) if any(e.rule_state == "overridden" for e in rules) else ())))
                if weak := [e for e in rules if e.rule_state == "enforced" and evidence_gap(e.probe)]:
                    print(f"{len(weak)} enforced rule(s) have an evidence gap (see `gap:` above)")
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
            if args.decision:
                probe["decision"] = args.decision
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
        elif args.cmd == "reclaim":
            from . import reclaim
            cands = reclaim.sweep(cache, root, set(args.classes or ["metadata", "code", "store"]), args.branches)
            info = None
            if (args.verify or args.apply) and any(c.cls == "code" for c in cands):
                cmd = args.test_cmd or os.environ.get("FRACTAL_MUTATE_CMD")
                if not cmd:
                    raise CacheError("give the test command: --cmd 'pytest -q' (or set FRACTAL_MUTATE_CMD)")
                info = reclaim.verify(cache, root, cands, cmd=cmd, build_cmd=args.build_cmd,
                                      env=dict(kv.partition("=")[::2] for kv in args.env), workers=args.jobs,
                                      timeout=args.timeout)
            if args.json:
                print(json.dumps([c.__dict__ for c in cands], indent=2, default=str))
            else:
                print(reclaim.render(cands, info), end="")
            if args.apply:
                for line in reclaim.apply(cache, root, cands):
                    print(f"  {line}")
                print("applied to the working tree; review with `git diff` and commit it yourself")
        elif args.cmd == "impact":
            from . import impact
            r = impact.check(cache, root, args.node, args.base, args.branches)
            print(json.dumps(r, indent=2) if args.json else impact.render(r), end="")
        elif args.cmd == "mutate":
            from . import mutate
            cmd = args.test_cmd or os.environ.get("FRACTAL_MUTATE_CMD")
            if not cmd:
                raise CacheError("give the test command: --cmd 'pytest -q {tests}' (or set FRACTAL_MUTATE_CMD)")
            items = json.loads(sys.stdin.read() if args.mutants == "-" else Path(args.mutants).read_text())
            env = dict(kv.partition("=")[::2] for kv in args.env)
            r = mutate.run(cache, root, args.node, items, cmd=cmd, build_cmd=args.build_cmd, env=env, commit=args.commit,
                           workers=args.jobs, timeout=args.timeout, record=not args.no_record, use_memo=not args.no_cache)
            print(json.dumps(r, indent=2) if args.json else mutate.render(r), end="")
        elif args.cmd == "brief":
            from . import brief
            r = brief.build(cache, root, args.node)
            brief.remember(root, args.node, r["digest"])
            if args.out:
                args.out.write_text(r["text"])
            if args.json:
                print(json.dumps(r, indent=2))
            elif args.out:
                print(f"wrote {args.out} (digest {r['digest']})")
            else:
                print(r["text"], end="")
        elif args.cmd == "report":
            from . import reports
            if args.report_cmd == "file":
                row = reports.file_report(cache, root, args.scratch, args.node, args.to, args.force)
                print(json.dumps(row, indent=2) if args.json else f"filed {row['path']} (node {row['node']}, commit {row['commit'][:10]})")
            else:
                rows = reports.list_reports(root, args.node)
                print(json.dumps(rows, indent=2) if args.json else reports.render(rows), end="")
        elif args.cmd == "run":
            from . import jobs
            w = args.words[1:] if args.words and args.words[0] == "--" else args.words
            if w and w[0] in ("status", "log") and args.words[0] != "--":
                if len(w) != 2:
                    raise CacheError(f"usage: fractal run {w[0]} <id>")
                if w[0] == "log":
                    print(jobs.log_text(root, w[1], args.tail), end="")
                else:
                    st = jobs.status(root, w[1])
                    print(json.dumps(st, indent=2) if args.json else jobs.render(st), end="")
            else:
                if not w:
                    raise CacheError("usage: fractal run [--node N] [--backend B] [--timeout S] [--detach] <command...>")
                import shlex
                run_id = jobs.create(cache, root, shlex.join(w) if len(w) > 1 else w[0], node=args.node,
                                     backend=args.backend, timeout=args.timeout)
                if args.detach:
                    jobs.spawn(root, run_id)
                    print(run_id if not args.json else json.dumps({"id": run_id}))
                    print(f"poll with: fractal run status {run_id}", file=sys.stderr)
                else:
                    res = jobs.execute_run(root, run_id)
                    st = jobs.status(root, run_id)
                    print(json.dumps(st, indent=2) if args.json else jobs.render(st), end="")
                    return 0 if res["status"] == "passed" else 1
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
        elif args.cmd == "status":
            from . import status
            rows = [status.derive(cache, root, args.node)] if args.node else list(status.derive_all(cache, root).values())
            dis = None
            if args.marks:
                derived = {r["node"]: r for r in rows}
                dis = status.disagreements(status.legacy_marks(args.marks.read_text(errors="replace"), set(derived)), derived)
            from .conflict import awaiting, banner
            asked = awaiting(cache)
            if args.json:
                print(json.dumps({"nodes": rows, **({"disagreements": dis} if dis is not None else {}),
                                  **({"arbitrations": asked} if asked else {})}, indent=2, sort_keys=True))
            else:
                print("\n".join(banner(asked)) + ("\n" if asked else ""), end="")
                print(status.render(rows, dis, verbose=bool(args.node)), end="")
        elif args.cmd == "dag":
            from . import dag
            d = dag.build(cache, root, args.out.resolve().parent if args.out else None)
            text = dag.render(d, args.format)
            if args.out:
                args.out.write_text(text)
            else:
                print(text, end="")
        elif args.cmd == "conflict":
            from . import conflict
            human = sys.stdin.isatty() or getattr(args, "yes", False)
            cc = args.conflict_cmd
            if cc == "list":
                items = conflict.pending(cache, root)
                rows = [r for r in cache.store.conflict_all() if args.all or r["state"] in ("open", "awaiting_human")]
                print(json.dumps({"pending": items, "decided": rows if args.all else []}, indent=2) if args.json
                      else conflict.render_list(items, cache), end="")
            elif cc == "show":
                item = next((c for c in conflict.pending(cache, root) if c["id"] == args.id), None)
                row = cache.store.conflict_get(args.id)
                if not item and not row:
                    raise CacheError(f"no conflict {args.id}")
                print(json.dumps({"pending": item, "record": row}, indent=2) if args.json
                      else conflict.render_show(cache, item, row), end="")
            else:
                if cc == "raise":
                    r = conflict.raise_conflict(cache, root, args.target, args.reason, args.by)
                elif cc == "resolve":
                    r = conflict.resolve(cache, root, args.id, args.action, args.reason, as_node=args.as_node,
                                         human=human, exclude=args.exclude)
                elif cc == "escalate":
                    r = conflict.escalate(cache, root, args.id, args.reason, as_node=args.as_node, human=human)
                else:
                    r = conflict.arbitrate(cache, args.id, "upheld" if args.upheld else "revised", args.reason, human=human)
                print(json.dumps(r, indent=2) if args.json else
                      f"{r['id']}  {r['state']}" + (f" (handler {r['handler']})" if r['state'] in ('open', 'awaiting_human') else ""))
        elif args.cmd == "activations":
            from . import activation
            found = activation.scan(cache, root)
            print(json.dumps(found, indent=2) if args.json else activation.render(found), end="")
        elif args.cmd == "verify-plan":
            from . import ledger
            print(ledger.render(ledger.plan(cache, root, args.node, args.against), args.with_arguments,
                                not args.no_diff, root), end="")
            from . import impact
            try:
                r = impact.check(cache, root, args.node)
            except (SymbolError, RuntimeError):
                r = None
            if r and any(x["callers"] for x in r["rows"]):
                print("\n" + impact.render(r), end="")
        elif args.cmd == "put" and (args.section or args.symbol):
            from .context import put_extracted
            notes: list[str] = []
            _emit(args, [put_extracted(cache, root, section=args.section, symbol=args.symbol,
                                       depends_on=args.dep, notes=notes)])
            for note in notes:
                print(f"note: {note}", file=sys.stderr)
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
            from .context import relinkable, unchecked_decisions
            out = cache.update()
            unchecked = unchecked_decisions(cache, root)
            relink = relinkable(cache, root)
            if args.json:
                print(json.dumps({**out, **({"unchecked_decisions": unchecked} if unchecked else {}),
                                  **({"relink": relink} if relink else {})}, indent=2))
            else:
                for kind in MOTION_CLASSES:
                    if out.get(kind):
                        print(f"{kind:10} {len(out[kind]):3}  {' '.join(out[kind][:8])}")
                if unchecked:
                    print(f"{'unchecked':10} {len(unchecked):3}  {' '.join(unchecked[:8])}  "
                          "(decided, but no check line, citing claim or test names them)")
                for cid, ref in relink:
                    print(f"{'relink':10} {cid}  its heading is gone but {ref} exists: `fractal put --section {ref}` "
                          "replaces it, then re-run `fractal ledger node` for the specs that cite it")
                if not out and not unchecked and not relink:
                    print("nothing changed")
        elif args.cmd == "repair":
            from .repair import repair, reverify
            if args.all and not args.dry_run:      # no model unless --agent: re-run every probe, report what is left
                r = reverify(root)
                print(json.dumps(r, indent=2))
                if args.agent and r["remaining"]:
                    r = repair(root, [x["id"] for x in r["remaining"]], False, args.model, False)
                    print(json.dumps(r, indent=2))
            else:
                r = repair(root, args.ids or None, args.all, args.model, args.dry_run)
                print(r["prompt"] if args.dry_run and "prompt" in r else json.dumps(r, indent=2))
        elif args.cmd == "verify":
            _emit(args, cache.verify(args.ids or None))
        elif args.cmd == "stats":
            print(json.dumps(cache.stats(), indent=2))
    except (CacheError, ProbeError, SymbolError, ConfigError, json.JSONDecodeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        cache.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

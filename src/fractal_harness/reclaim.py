"""`fractal reclaim`: shrink the repo to the smallest state that keeps every verdict.

Theory: compression on both halves. The rule set is shrunk without losing information (what it forbids stays
forbidden), then the code is shrunk without losing a verdict (every enforced rule and test still passes). Rules go
first: a smaller contract can leave more code unreachable.

  rules   an enforced rule is REDUNDANT when every state it forbids is still forbidden by the other in-effect rules
          (loss.assess; SOUND, typed probes only: a command rule is never a candidate). Chosen greedily, judged
          together, so two identical rules are not both retired; node rules before root rules, newer before older
          (the older precedent is kept). `--apply` retires redundant node rules at the rule's own layer
          (authority.retire: no information is lost, so no higher layer is needed); a root rule is listed with the
          human's command. A VACUOUS rule (its globs match no file today) is reported, not proposed: an absent rule
          over a glob still forbids files added later, so retiring it is a loss.

Mark, sweep, verify, gate (code, metadata, store):

  mark    `graph-check` finds what nothing reaches: exports the code no longer defines, dead exports, public
          items no spec declares and nothing uses, and nodes pointing at nothing. Anything marked
          public (`public:`, `.fractal-public`) is never a candidate. Tests, rules and probes are never candidates.
  sweep   those become candidates, in classes: metadata (a spec's stale `exports:`), code (delete the item and
          the `impl` blocks that hang off it), store (a node whose spec is gone is deregistered). Recorded
          verdicts are evidence and are never proposed for deletion; those of unregistered nodes are only listed.
  gate    a code candidate that another live worktree mentions is blocked (`impact.callers`): unreachable here
          is not unreachable there. A code candidate whose deletion breaks an enforced rule (a presence or only_in
          rule that needs the item: the code graph does not know about rules) is blocked too; checked in memory
          for each deletion alone and again for all of them together before --apply. Slow rules are not run.
  verify  a code deletion is applied in a scratch worktree and the test command must still pass. Reference
          scanning is a word match and tests can be weak, so passing is required but the only thing the tool
          does is propose: `--apply` writes the verified edits to your working tree and nothing is committed.

Every applied reclamation is written to the ledger's audit log.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import graph_check, impact, ledger
from .hashing import stable_files
from .cache import ClaimCache
from .executor import Job, run_all
from .nodes import edit_header, parse_header
from .refindex import RefIndex, load_trees
from .symbols import SymbolError, at, read_ref, split_ref

RUST_ITEM = r"pub(?:\([^)]*\))?\s+(?:async\s+|unsafe\s+|const\s+|extern\s+\"[^\"]*\"\s+)*(?:struct|enum|trait|fn|type|const|static|union)\s+"


@dataclass
class Candidate:
    cls: str                      # rules | metadata | code | store
    what: str                     # the symbol, export or node
    why: str
    node: str = ""
    path: str = ""
    spans: list[tuple[int, int]] = field(default_factory=list)   # code: 0-based inclusive line ranges to delete
    spec_remove: tuple[str, str] | None = None                   # (spec path, export ref) to drop with it
    gate: dict[str, list[str]] = field(default_factory=dict)     # other trees that mention it
    verdict: str = "not run"      # code: not run | passed | failed | build_failed | timeout
    action: str = ""              # store: deregister | drop_verdicts; rules: retire | human | vacuous
    rule_id: str = ""             # rules: the rule; also the rules a code deletion would break (in `why`)
    breaks: list[str] = field(default_factory=list)              # code: enforced rules the deletion would break


# --- finding an item's lines ----------------------------------------------------------------------

def _block_end(lines: list[str], i: int) -> int:
    depth, seen = 0, False
    for j in range(i, len(lines)):
        code = re.sub(r"//.*$", "", lines[j])
        depth += code.count("{") - code.count("}")
        seen = seen or "{" in code
        if (seen and depth <= 0) or (not seen and depth <= 0 and code.rstrip().endswith(";")):
            return j
    return len(lines) - 1


def item_spans(path: str, text: str, name: str) -> list[tuple[int, int]]:
    """Line ranges (0-based, inclusive) that define a top-level item: its doc comments, attributes or decorators,
    the body, and for Rust the `impl` blocks of that type in the same file. [] when it cannot be found."""
    lines = text.splitlines()
    spans: list[tuple[int, int]] = []
    if path.endswith(".rs"):
        item = re.compile(rf"^{RUST_ITEM}{re.escape(name)}\b")
        impl = re.compile(rf"^impl\b[^{{;]*\b{re.escape(name)}\b")
        found = False
        for i, l in enumerate(lines):
            is_item = bool(item.match(l))
            if is_item or impl.match(l):
                found = found or is_item
                start = i
                while start > 0 and lines[start - 1].lstrip().startswith(("///", "#[", "//!")):
                    start -= 1
                spans.append((start, _block_end(lines, i)))
        return spans if found else []
    item = re.compile(rf"^(?:async\s+)?(?:def|class)\s+{re.escape(name)}\b")
    for i, l in enumerate(lines):
        if item.match(l):
            start = i
            while start > 0 and lines[start - 1].startswith("@"):
                start -= 1
            end = i
            for j in range(i + 1, len(lines)):
                if lines[j].strip() and not lines[j][:1].isspace():
                    break
                if lines[j].strip():
                    end = j
            return [(start, end)]
    return []


def remove_spans(text: str, spans: list[tuple[int, int]]) -> str:
    lines = text.splitlines(keepends=True)
    for a, b in sorted(spans, reverse=True):
        end = b + 1
        if (a == 0 or not lines[a - 1].strip()):
            while end < len(lines) and not lines[end].strip():
                end += 1                                  # do not leave a doubled blank line behind
        del lines[a:end]
    return "".join(lines)


# --- mark and sweep -------------------------------------------------------------------------------

def sweep(cache: ClaimCache, root: Path, classes: set[str], branches: bool = False) -> list[Candidate]:
    with stable_files():        # a read-only scan: globs expanded and files scanned once per sweep (the rule gate)
        return _sweep(cache, root, classes, branches)


def _sweep(cache: ClaimCache, root: Path, classes: set[str], branches: bool) -> list[Candidate]:
    marks = graph_check.check(cache, root)
    texts = next((t.texts for t in load_trees(root) if t.current), {})
    out: list[Candidate] = []
    nodes = cache.store.all_nodes()
    owner_spec = {}
    for n, info in nodes.items():
        if (st := read_ref(root, info["spec"])) is not None and (h := parse_header(st)):
            for r in h.exports:
                owner_spec.setdefault(r.partition("::")[0] + "::" + r.partition("::")[2].split("::")[0], (n, info["spec"]))
    if "rules" in classes:
        out.extend(rule_candidates(cache, root))
    if "metadata" in classes:
        for m in marks["missing"]:
            n = m["node"]
            spec = nodes[n]["spec"]
            refs = [r for r in parse_header(read_ref(root, spec, "")).exports
                    if r.partition("::")[0] + "::" + r.partition("::")[2].split("::")[0] == m["symbol"]]
            for r in refs:
                out.append(Candidate("metadata", r, f"{spec} exports it but the code no longer defines it", n, spec_remove=(spec, r)))
    if "code" in classes:
        idx = RefIndex.load(root, branches)
        seen = set()
        baseline: dict = {}
        for kind, rows in (("dead export of", marks["dead"]), ("public item no spec exports", marks["undeclared_dead"])):
            for d in rows:
                key = d["symbol"]
                if key in seen:
                    continue
                seen.add(key)
                path, name = key.split("::", 1)
                spans = item_spans(path, texts.get(path, ""), name) if path in texts else []
                if not spans:
                    continue
                node, spec = owner_spec.get(key, (d.get("node", ""), None))
                c = Candidate("code", key, f"{kind} {node}".strip() + (", tested only" if d["tested_only"] else ""),
                              node, path, spans)
                if spec:
                    c.spec_remove = (spec, next((r for r in parse_header(read_ref(root, spec, "")).exports
                                                 if r.partition("::")[0] + "::" + r.partition("::")[2].split("::")[0] == key), key))
                c.gate = impact.callers(idx, name, path, unseen_only=False)
                c.breaks = rules_broken(cache, root, {path: remove_spans(texts.get(path, ""), spans)}, baseline)
                out.append(c)
    if "store" in classes:
        for n, info in sorted(nodes.items()):
            if read_ref(root, info["spec"]) is None:
                out.append(Candidate("store", n, f"its spec {info['spec']} no longer exists", n, action="deregister"))
        for n in sorted(cache.store.verdict_nodes() - set(nodes)):    # evidence: listed, never proposed for deletion
            out.append(Candidate("store", n, "holds verdicts but is not registered; they are kept (re-register it with "
                                 "`ledger node`)", n, action="keep"))
    return out


def rule_candidates(cache: ClaimCache, root: Path) -> list[Candidate]:
    """Redundant enforced rules (retirable without loss, judged together) and vacuous ones (reported only)."""
    from . import probes
    from .authority import effective
    from .loss import assess
    # a rule precedence sets aside is not a candidate: its delta is empty now, but it comes back into force if the
    # rule that beats it goes, so retiring it loses that
    in_effect, _ = effective(cache, [e for e in cache.store.all() if e.enforced])
    enforced = [e for e in in_effect if e.enforced]
    order = sorted(enforced, key=lambda e: (e.layer == "root", -(e.created_at or 0), e.id))
    out, gone = [], set()
    for e in order:
        if not e.probe or probes.is_slow(e.probe):
            continue
        a = assess(cache, e, None, without=gone)
        if a.absorbed:
            gone.add(e.id)
            human = e.layer == "root"
            why = (f"redundant: everything it forbids is still forbidden by {', '.join(a.by)}" if a.by else
                   "forbids nothing: its expectation holds whatever the code says")
            out.append(Candidate("rules", e.post, why, e.layer, action="human" if human else "retire", rule_id=e.id))
    for e in enforced:
        if e.id in gone or not e.probe or probes.is_slow(e.probe):
            continue
        r = probes.run(e.probe, root)
        if r.passed and r.vacuous:
            out.append(Candidate("rules", e.post, f"checks nothing today ({r.detail}); not proposed: it still forbids "
                                 "files added later, so retiring it is a loss", e.layer, action="vacuous", rule_id=e.id))
    return out


def rules_broken(cache: ClaimCache, root: Path, overlay: dict[str, str], baseline: dict | None = None) -> list[str]:
    """Ids of enforced, fast rules over the overlaid files that pass now and would fail with the overlay.
    `baseline` memoizes each rule's current verdict across calls (one sweep: the tree does not change)."""
    from . import probes
    from .cache import dependencies
    from .regions import matches
    baseline = {} if baseline is None else baseline
    out = []
    for e in cache.store.all():
        if not e.enforced or not (probe := _checkable(e.probe)):
            continue
        if not any(matches(d, f) for d in dependencies(e) for f in overlay):
            continue
        if e.id not in baseline:
            baseline[e.id] = probes.run(probe, root).passed
        if baseline[e.id] and not probes.run(probe, root, overlay).passed:
            out.append(e.id)
    return out


def _checkable(probe: dict | None) -> dict | None:
    """The part of a probe a deletion can be checked against in memory: its fast leaves that read files (a commit
    gate reads a commit; a command or test is slow and is the verify step's). None when there is none."""
    from . import probes
    from .loss import _leaves
    leaves = [x for x in _leaves(probe) if not probes.is_slow(x) and not probes.gate_leaves(x)]
    return None if not leaves else leaves[0] if len(leaves) == 1 else {"type": "all", "probes": leaves}


# --- verify ---------------------------------------------------------------------------------------

def _dirty(root: Path) -> list[str]:
    p = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=root, capture_output=True, text=True)
    return [l[3:] for l in p.stdout.splitlines() if l.strip()]


def _head_text(root: Path, sha: str, path: str) -> str:
    return subprocess.run(["git", "show", f"{sha}:{path}"], cwd=root, capture_output=True, text=True, check=True).stdout


def verify(cache: ClaimCache, root: Path, cands: list[Candidate], *, cmd: str, build_cmd: str = "",
           env: dict[str, str] | None = None, workers: int = 4, timeout: int = 600) -> dict:
    """Run each unblocked code deletion in a scratch worktree of HEAD, then all the passing ones together."""
    code = [c for c in cands if c.cls == "code"]
    if not code:
        return {"combined": None, "commit": None}
    if dirty := _dirty(root):
        raise SymbolError("verify runs on HEAD, but the working tree has uncommitted changes "
                          f"({', '.join(dirty[:3])}); commit or stash them first")
    sha = ledger.resolve_commit(root)
    env = env or {}
    cmd = cmd if "{tests}" not in cmd else cmd.replace("{tests}", "")
    mk = lambda edits: Job(edits, cmd, build_cmd, env, timeout)
    base = run_all(cache.store, root, sha, [mk({})], workers)[0]
    if base.status != "passed":
        raise SymbolError(f"the tests do not pass on HEAD {sha[:10]} before any deletion: {base.status}\n{base.tail}")

    def edits_for(cs: list[Candidate]) -> dict[str, str]:
        per: dict[str, list] = {}
        for c in cs:
            per.setdefault(c.path, []).extend(c.spans)
        return {p: remove_spans(_head_text(root, sha, p), sp) for p, sp in per.items()}

    todo = [c for c in code if not c.gate and not c.breaks]
    for c in code:
        if c.gate or c.breaks:
            c.verdict = "blocked"
    for c, o in zip(todo, run_all(cache.store, root, sha, [mk(edits_for([c])) for c in todo], workers)):
        c.verdict = o.status
    passing = [c for c in todo if c.verdict == "passed"]
    combined = None
    if len(passing) > 1:
        o = run_all(cache.store, root, sha, [mk(edits_for(passing))], workers)[0]
        combined = o.status
        if o.status != "passed":
            for c in passing:
                c.verdict = "passed alone, fails with the others"
    elif passing:
        combined = "passed"
    return {"combined": combined, "commit": sha}


# --- apply ----------------------------------------------------------------------------------------

def apply(cache: ClaimCache, root: Path, cands: list[Candidate]) -> list[str]:
    """Write the reclaimable candidates to the working tree and the store; returns what was done. Code
    deletions need a passing verdict; nothing is committed."""
    done: list[str] = []
    per: dict[str, list] = {}
    spec_edits: dict[str, dict[str, list[str]]] = {}
    from .authority import retire
    from .config import load as load_config
    policy = load_config(root).accept_policy
    together: dict[str, list] = {}  # checked before anything is written: a refusal leaves nothing half-applied
    for c in cands:
        if c.cls == "code" and c.verdict == "passed":
            together.setdefault(c.path, []).extend(c.spans)
    if together:
        overlay = {p: remove_spans((root / p).read_text(errors="replace"), sp) for p, sp in together.items()}
        if broken := rules_broken(cache, root, overlay):
            raise SymbolError(f"the code deletions together would break enforced rule(s) {', '.join(broken)}; "
                              "nothing was changed (apply them separately with --class)")
    for c in cands:                 # rules first: the contract, then the code against it
        if c.cls == "rules" and c.action == "retire":
            retire(cache, c.rule_id, c.node, f"reclaim: {c.why}", policy)
            done.append(f"retired [{c.rule_id}] ({c.node}): {c.why}")
    records: list = []              # the store is written after the files: it never records a change not made
    for c in cands:
        if c.cls == "code" and c.verdict == "passed":
            per.setdefault(c.path, []).extend(c.spans)
            done.append(f"deleted {c.what}")
            records.append(lambda c=c: cache.store.audit_add(c.node or "(reclaim)", "reclaim", f"deleted {c.what} ({c.why})"))
        if c.cls in ("code", "metadata") and c.spec_remove and (c.cls == "metadata" or c.verdict == "passed"):
            spec_edits.setdefault(c.spec_remove[0], {}).setdefault("exports", []).append(c.spec_remove[1])
            if c.cls == "metadata":
                done.append(f"removed export {c.what} from {c.spec_remove[0]}")
                records.append(lambda c=c: cache.store.audit_add(c.node or "(reclaim)", "reclaim",
                                                                 f"removed stale export {c.what} from {c.spec_remove[0]}"))
        if c.cls == "store" and c.action == "deregister":      # verdicts stay: they are evidence
            records.append(lambda c=c: (ledger.deregister(cache, c.node),
                                        cache.store.audit_add(c.node, "reclaim", f"deregistered, verdicts kept ({c.why})")))
            done.append(f"deregistered {c.node} (its verdicts are kept)")
    for path, spans in per.items():
        f = root / path
        f.write_text(remove_spans(f.read_text(errors="replace"), spans))
    for spec, rem in spec_edits.items():
        f = at(root, spec)
        f.write_text(edit_header(read_ref(root, spec, ""), remove=rem))
    for record in records:
        record()
    return done


def render(cands: list[Candidate], info: dict | None = None) -> str:
    if not cands:
        return "nothing to reclaim\n"
    out = ["# Reclamation plan", ""]
    names = {"rules": "Rules (a redundant one is retired without losing information; a vacuous one is only reported)",
             "metadata": "Stale spec metadata (no code changes)", "code": "Code nothing reaches",
             "store": "Store entries with nothing behind them (verdicts are never deleted)"}
    for cls in ("rules", "metadata", "code", "store"):
        rows = [c for c in cands if c.cls == cls]
        if not rows:
            continue
        out.append(f"## {names[cls]}")
        for c in rows:
            line = f"- {c.what}: {c.why}"
            if cls == "rules":
                line = f"- [{c.rule_id}] @{c.node} {c.what[:90]}: {c.why}" + {
                    "retire": "  [--apply retires it]",
                    "human": f"  [a root rule: `fractal override {c.rule_id} --reason ...` in your terminal]",
                    "vacuous": ""}[c.action]
            if cls == "code" and c.breaks:
                line += f"  [BLOCKED, would break enforced rule(s) {', '.join(c.breaks)}]"
            elif cls == "code":
                where = "; ".join(f"{k}: {', '.join(v[:3])}" for k, v in sorted(c.gate.items()))
                line += f"  [{'BLOCKED, used on ' + where if c.gate else c.verdict}]"
                if c.spec_remove and not c.gate:
                    line += f"  (also drops it from {c.spec_remove[0]} exports: children of {c.node or 'its node'} go stale)"
            out.append(line)
        out.append("")
    if info and info.get("combined"):
        out.append(f"All passing deletions together: {info['combined']}.")
    out.append("Passing is required, not proof: reference scanning is a word match and tests can be weak. Review `git diff` after --apply.")
    return "\n".join(out) + "\n"

"""The mutant ledger: verification verdicts memoized per node, so a re-verification after a small
change re-runs only what the change can have affected.

A verifier records each mutant it tried: `(node, file, span, mutant) -> killed (by which tests) |
survived | equivalent`, at the commit it verified. `fractal verify-plan <node>` then says which of
those verdicts are still good and which are stale, with the diff since the last verified commit:
the exact input for a narrow verifier.

A verdict is stale when
  * the node's spec changed, or a decision/interface the spec cited changed or no longer holds
    (premises are claims, registered by `fractal ledger node`; a changed decision gets a new claim id),
  * a diff since its commit touches lines of its span, or
  * a file defining one of its killing tests changed.
`survived` mutants are never settled: they are listed until a later run records them killed.

The plan carries data and diffs only. It never carries the implementer's reasoning, and prior
equivalence arguments stay out unless asked for: a verifier that re-derives them independently is
the point of having one.
"""

from __future__ import annotations

import re
import subprocess
import difflib
from pathlib import Path

from .cache import ClaimCache
from .context import DEFAULT_ID_PATTERN, _cited, autoregister, premises_of, section_claims
from .nodes import DEGENERATE_MUTANT, contract_change, contract_parts, lint, parse_header, repo_lint, spec_key, spec_lint, edit_header
from .store import GOOD
from .symbols import SymbolError, digest, symbol_of

RESULTS = ("killed", "survived", "equivalent")
MAX_DIFF_CHARS = 20_000


def _git(root: Path, *args: str) -> str:
    p = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)
    if p.returncode:
        raise SymbolError(p.stderr.strip() or f"git {args[0]} failed")
    return p.stdout


def resolve_commit(root: Path, rev: str = "HEAD") -> str:
    return _git(root, "rev-parse", "--verify", f"{rev}^{{commit}}").strip()


# --- recording ----------------------------------------------------------------------------

def node_commit(cache: ClaimCache, root: Path, node: str, commit: str | None = None) -> str:
    """The commit a node's verdicts attach to: the one named, else the tip of the branch registered with
    the node (or `node-<name>`), else this checkout's HEAD (correct inside the node's worktree)."""
    if commit:
        return resolve_commit(root, commit)
    info = cache.store.get_node(node)
    branch = (info or {}).get("premises", {}).get("branch") if isinstance((info or {}).get("premises"), dict) else None
    for b in [x for x in (branch, f"node-{node}") if x]:
        try:
            return resolve_commit(root, f"refs/heads/{b}")
        except SymbolError:
            if b == branch:
                raise SymbolError(f"node {node}'s registered branch {b!r} does not exist")
    return resolve_commit(root, "HEAD")


def register_node(cache: ClaimCache, root: Path, node: str, spec: str,
                  id_pattern: str = DEFAULT_ID_PATTERN, branch: str | None = None, fix: bool = False,
                  dry_run: bool = False) -> dict:
    """Record which spec and which premises the node's verdicts rest on. With a spec header the
    premises are exactly its declared `implements` decisions and `parents` contracts; without one
    (legacy) they are whatever the text cites. Re-run after the spec changes and the node has been
    re-verified. Parents must be registered first, so their contracts can be watched."""
    f = root / spec
    if not f.is_file():
        raise SymbolError(f"no such spec: {spec}")
    text = f.read_text(errors="replace")
    h = parse_header(text)
    diff = ""
    if fix and h is not None:
        fixes: dict = {}
        repo_lint(root, h, cache.store.all_nodes(), text, node, fixes)
        if any(fixes["add"].values()) or any(fixes["remove"].values()):
            new = edit_header(text, fixes["add"], fixes["remove"])
            diff = "".join(difflib.unified_diff(text.splitlines(True), new.splitlines(True), f"a/{spec}", f"b/{spec}"))
            if dry_run:
                return {"spec": spec, "premises": {"decisions": [], "parents": {}}, "uncited": [], "declared": True,
                        "lint": [], "autoregistered": [], "diff": diff, "dry_run": True}
            f.write_text(new)
            text, h = new, parse_header(new)
    edges = cache.store.all()
    registered: list[str] = []
    failed: dict[str, str] = {}
    if h is None:
        found, missing, interfaces = _cited(edges, text, id_pattern)
        premises = {"decisions": sorted({e.id for e in found + interfaces}), "parents": {}}
    else:
        nodes = cache.store.all_nodes()
        unregistered = [n for n in h.parents if n not in nodes]
        if unregistered:
            raise SymbolError(f"parents not registered yet: {', '.join(unregistered)} "
                              "(`fractal ledger node <parent> --spec <its spec>` first)")
        if node in h.parents:
            raise SymbolError(f"{node} cannot be its own parent")
        found, missing = section_claims(edges, h.implements)
        if missing:
            registered, failed = autoregister(cache, root, missing)
            edges = cache.store.all()
            found, missing = section_claims(edges, h.implements)
        premises = {"decisions": sorted(e.id for e in found),
                    "parents": {n: contract_parts(root, nodes[n]["spec"]) for n in h.parents}}
    prev = cache.store.get_node(node)
    keep = premises_of(prev).get("branch") if prev else None
    if branch or keep:
        premises["branch"] = branch or keep
    premises["prose_sha"] = digest(text)         # the whole text, to say when only prose moved
    cache.store.put_node(node, spec, digest(spec_key(text)), premises)
    notes = []
    if h:
        notes = lint(h) + spec_lint(text) + repo_lint(root, h, cache.store.all_nodes(), text, node)
        notes += [f"{i} has no claim ({failed.get(i, 'not found')}); `fractal put --section DECISIONS.md::{i}`"
                  for i in missing]
    return {"spec": spec, "premises": premises, "uncited": [] if h else missing, "declared": h is not None,
            "lint": notes, "autoregistered": registered, "diff": diff}


def record(cache: ClaimCache, root: Path, node: str, *, file: str, span: str, mutant: str, result: str,
           commit: str | None = None, tests: list[str] | None = None, argument: str = "") -> None:
    if result not in RESULTS:
        raise SymbolError(f"result must be one of {RESULTS}")
    m = re.fullmatch(r"(\d+)(?:-(\d+))?", span.strip())
    if not m:
        raise SymbolError(f"span must be N or N-M (1-based lines), got {span!r}")
    start, end = int(m.group(1)), int(m.group(2) or m.group(1))
    if start < 1 or end < start:
        raise SymbolError(f"bad span {span!r}")
    if result == "killed" and not tests:
        raise SymbolError("a killed mutant needs the tests that kill it (--tests)")
    if result == "equivalent" and not argument.strip():
        raise SymbolError("an equivalent mutant needs its argument (--argument)")
    cache.store.put_mutant(node, file, start, end, mutant, result, node_commit(cache, root, node, commit),
                           tests or [], argument.strip())


def reattribute(cache: ClaimCache, root: Path, node: str, old: str, new: str, reason: str,
                force: bool = False) -> int:
    """Correct which commit a node's verdicts were verified at (they were recorded against the wrong one).
    Refuses when a verdict's file or span does not exist at the new commit, unless `force`. The change is
    written to the ledger's audit log with the reason."""
    if not reason.strip():
        raise SymbolError("say why (--reason): the correction is part of the audit trail")
    rows = [r for r in cache.store.mutants(node) if r["commit"].startswith(old)]
    if not rows:
        raise SymbolError(f"{node} has no verdicts recorded at a commit starting {old!r}")
    if len({r["commit"] for r in rows}) > 1:
        raise SymbolError(f"{old!r} matches several commits ({', '.join(sorted({r['commit'][:10] for r in rows}))}); "
                          "give a longer prefix")
    target = resolve_commit(root, new)
    bad = []
    for f in sorted({r["file"] for r in rows}):
        try:
            n = len(_git(root, "show", f"{target}:{f}").splitlines())
        except SymbolError:
            bad += [f"{f}: not in {target[:10]}"]
            continue
        bad += [f"{f}:{r['start']}-{r['end']}: file has {n} lines at {target[:10]}"
                for r in rows if r["file"] == f and r["end"] > n]
    if bad and not force:
        raise SymbolError("these verdicts do not fit the new commit (use --force to override):\n  " + "\n  ".join(bad[:10]))
    return cache.store.reattribute(node, rows[0]["commit"], target, reason.strip() + (" [forced]" if bad else ""))


def deregister(cache: ClaimCache, node: str, forget_verdicts: bool = False) -> list[str]:
    """Remove a node's registration (it stops counting as a child, and a parent's plan stops listing it).
    Its verdicts stay unless `forget_verdicts`. Returns the nodes that still declare it as a parent."""
    if cache.store.get_node(node) is None:
        raise SymbolError(f"no such node: {node}")
    orphans = sorted(n for n, i in cache.store.all_nodes().items() if node in premises_of(i)["parents"])
    cache.store.delete_node(node, forget_verdicts)
    return orphans


# --- diffs --------------------------------------------------------------------------------

def hunks_since(root: Path, commit: str, against: str | None = None) -> dict[str, list[tuple[int, int]]]:
    """{file: [(old_start, old_count)]} for changes from `commit` to the working tree (0 count =
    pure insertion after old_start). A deleted or added file is {file: [(1, 10**9)]}."""
    out: dict[str, list[tuple[int, int]]] = {}
    cur = None
    for line in _git(root, "diff", "-U0", "--no-color", "--no-renames", commit, *([against] if against else []), "--").splitlines():
        if line.startswith("--- "):
            old = line[4:]
            cur = None if old == "/dev/null" else old.removeprefix("a/")
            if cur:
                out.setdefault(cur, [])
        elif line.startswith("+++ "):
            new = line[4:]
            if cur is None and new != "/dev/null":
                out.setdefault(new.removeprefix("b/"), []).append((1, 10**9))
            elif new == "/dev/null" and cur:
                out[cur] = [(1, 10**9)]
        elif (m := re.match(r"@@ -(\d+)(?:,(\d+))? ", line)) and cur:
            out[cur].append((int(m.group(1)), int(m.group(2) if m.group(2) is not None else 1)))
    return out


def _touches(hunks: list[tuple[int, int]], start: int, end: int) -> bool:
    for a, n in hunks:
        if n == 0:
            if start <= a < end:
                return True          # inserted inside the span
        elif a <= end and a + n - 1 >= start:
            return True
    return False


def _word_in(name: str, text: str) -> bool:
    return bool(re.search(rf"(?<![\w]){re.escape(name)}(?![\w])", text))


# --- the plan -----------------------------------------------------------------------------

def plan(cache: ClaimCache, root: Path, node: str, against: str | None = None) -> dict:
    """Classify every recorded mutant of `node`: {valid, stale: [(mutant, reason)], open, base,
    changed, node_reason}."""
    rows = cache.store.mutants(node)
    if not rows:
        raise SymbolError(f"no mutants recorded for node {node!r} (`fractal ledger record`)")
    cache.update()
    node_reason = None
    info = cache.store.get_node(node)
    children: list[str] = []
    changed_export: tuple[str, str] | None = None
    prose_changed = None
    oracle = ""
    if info:
        if (sp := root / info["spec"]).is_file():
            oh = parse_header(sp.read_text(errors="replace"))
            oracle = oh.oracle if oh else ""
        spec = root / info["spec"]
        stext = spec.read_text(errors="replace") if spec.is_file() else None
        try:
            keyed = stext is not None and info["spec_sha"] in (digest(spec_key(stext)), digest(stext))
        except SymbolError:
            keyed = False
        if not keyed:
            node_reason = f"spec {info['spec']} changed"
        else:
            pre = premises_of(info)
            if pre.get("prose_sha") and digest(stext) != pre["prose_sha"]:
                prose_changed = info["spec"]
            for pid in pre["decisions"]:
                e = cache.store.get(pid)
                if e is None:
                    node_reason = f"decision claim {pid} changed or was removed"
                elif e.status not in GOOD:
                    node_reason = f"decision claim {pid} is {e.status}: {e.detail}"
                if node_reason:
                    break
            for name, old in pre["parents"].items():
                if node_reason:
                    break
                pinfo = cache.store.get_node(name)
                if pinfo is None:
                    node_reason = f"parent {name} is no longer registered"
                    continue
                try:
                    why = contract_change(old, root, pinfo["spec"])
                except SymbolError as e:
                    node_reason = f"parent {name}'s contract is unreadable: {e}"
                    continue
                if why:
                    node_reason = f"parent {name}'s contract changed: {why}"
                    if m := re.match(r"export (\S+) (?:changed|was removed)", why):
                        changed_export = (name, m.group(1))
        children = sorted(n for n, i in cache.store.all_nodes().items() if node in premises_of(i)["parents"])
    by_commit: dict[str, dict[str, list[tuple[int, int]]] | str] = {}
    for c in {r["commit"] for r in rows}:
        try:
            by_commit[c] = hunks_since(root, c, against)
        except SymbolError as e:
            by_commit[c] = str(e)
    stale, valid = [], []
    changed_files: set[str] = set()
    test_files: set[str] = set()
    for r in rows:
        h = by_commit[r["commit"]]
        reason = node_reason
        if reason is None and isinstance(h, str):
            reason = f"commit {r['commit'][:10]} unavailable ({h})"
        elif reason is None:
            changed_files |= set(h)
            if r["file"] in h and _touches(h[r["file"]], r["start"], r["end"]):
                reason = f"lines {r['start']}-{r['end']} of {r['file']} changed"
            else:
                for t in r["tests"]:
                    hit = next((f for f in h if _file_defines(root, f, r["commit"], t, h[f])), None)
                    if hit:
                        test_files.add(hit)
                        reason = f"killing test {t} is in changed file {hit}"
                        break
        (stale if reason else valid).append((r, reason))
    required = []
    if oracle.strip().lower() == "equality" and not any(
            r["mutant"] == DEGENERATE_MUTANT and r["result"] == "killed" for r, _ in valid):
        required.append(DEGENERATE_MUTANT)
    base = max(rows, key=lambda r: r["ts"])["commit"]
    return {"node": node, "base": base, "stale": [(r, why) for r, why in stale],
            "valid": [r for r, _ in valid], "node_reason": node_reason,
            "open": [r for r, _ in valid if r["result"] == "survived"], "required": required, "prose_changed": prose_changed,
            "narrow": _narrow(root, rows, changed_export) if changed_export else None,
            "children": children, "against": against, "changed": sorted(changed_files), "test_files": sorted(test_files), "files": sorted({r["file"] for r in rows})}


def _narrow(root: Path, rows: list[dict], changed: tuple[str, str]) -> dict:
    """When one parent export changed, the verdicts stay stale, but say where to start: the mutants whose
    code mentions the changed item come first; the rest reference nothing that changed (by name)."""
    parent, ref = changed
    item = ref.partition("::")[2].rpartition("::")[2]
    near, far = [], []
    for r in rows:
        try:
            lines = _git(root, "show", f"{r['commit']}:{r['file']}").splitlines()[r["start"] - 1:r["end"]]
        except SymbolError:
            lines = []
        (near if item and _word_in(item, "\n".join(lines)) else far).append(r)
    try:
        sig = symbol_of(root, ref)
    except SymbolError:
        sig = ""
    return {"parent": parent, "ref": ref, "item": item, "near": near, "far": far, "signature": sig}


def _file_defines(root: Path, rel: str, commit: str, test: str, hunks: list) -> bool:
    """Does a changed file mention the test (before or after the change)?"""
    f = root / rel
    now = f.read_text(errors="replace") if f.is_file() else ""
    try:
        before = _git(root, "show", f"{commit}:{rel}")
    except SymbolError:
        before = ""
    return _word_in(test, now) or _word_in(test, before)


def _compared_with(root: Path, against: str | None) -> str:
    """Say what the staleness was judged against, so a surprising 'changed' can be traced."""
    if against:
        return f"Compared with commit {resolve_commit(root, against)[:10]} ({against})."
    try:
        head = resolve_commit(root, "HEAD")[:10]
        dirty = [l[3:] for l in _git(root, "status", "--porcelain", "--untracked-files=no").splitlines()]
    except SymbolError:
        return "Compared with this checkout's working tree."
    return (f"Compared with the working tree of {root} (HEAD {head})"
            + (f", which has uncommitted changes in {len(dirty)} file(s): {', '.join(dirty[:5])}" if dirty else "")
            + ". Pass --against REV to compare with a commit instead.")


def render(p: dict, with_arguments: bool = False, diff: bool = True, root: Path | None = None) -> str:
    total = len(p["stale"]) + len(p["valid"])
    out = [f"# Verification plan for {p['node']}", "",
           f"{len(p['valid'])} of {total} recorded verdicts still hold; {len(p['stale'])} need re-running. "
           f"Last verified commit: {p['base'][:10]}."]
    if root is not None:
        out += ["", _compared_with(root, p.get("against"))]
    if p["children"]:
        out += ["", f"Nodes that assume this one's contract: {', '.join(p['children'])}. Changing its guarantee, "
                    "condition or exports stales them."]
    if p.get("prose_changed") and not p["node_reason"]:
        out += ["", f"{p['prose_changed']}'s prose changed since it was registered. Its header and design points did "
                    "not, so the verdicts stand; read the change in case a requirement moved:"]
        if root is not None and diff:
            try:
                text = _git(root, "diff", "--no-color", p["base"], "--", p["prose_changed"])
            except SymbolError:
                text = ""
            if text:
                out += ["", "```diff", text[:MAX_DIFF_CHARS].rstrip(), "```"]
    if p["node_reason"]:
        out += ["", f"Everything is stale: {p['node_reason']}. Re-verify the node in full."]
    if n := p.get("narrow"):
        out += ["", f"## Suggested order: {n['parent']}'s export {n['ref']} changed", ""]
        out += [f"Start with the {len(n['near'])} mutant(s) whose code mentions `{n['item']}`:"]
        out += [f"- {r['file']}:{r['start']}-{r['end']}  {r['mutant']}" for r in n["near"]] or ["- (none)"]
        out += ["", f"The other {len(n['far'])} do not mention it by name. They are still stale (a field or "
                    "behavior of the changed item can reach code that never names it), but re-run them only "
                    "after checking the new signature below against what they depend on:"]
        if n["signature"]:
            out += ["", "```", n["signature"], "```"]
    if p["stale"]:
        out += ["", "## Re-run these mutants"]
        out += [f"- {r['file']}:{r['start']}-{r['end']}  {r['mutant']}  (was {r['result']}) — {why}"
                for r, why in p["stale"]]
    if p["required"]:
        out += ["", "## Required standard mutants (the node declares an equality oracle)"]
        out += [f"- {m}: make the output trivially empty; the equality tests must fail. "
                f"Record it with --mutant '{m}' (killed, with its tests) or the node fails." for m in p["required"]]
    if p["open"]:
        out += ["", "## Survived and still unresolved"]
        out += [f"- {r['file']}:{r['start']}-{r['end']}  {r['mutant']}" for r in p["open"]]
    settled = [r for r in p["valid"] if r["result"] != "survived"]
    if settled:
        out += ["", f"## Settled ({len(settled)}): do not re-run unless the diff below touches them"]
        for r in settled:
            line = f"- {r['file']}:{r['start']}-{r['end']}  {r['mutant']}  ({r['result']}"
            line += f" by {', '.join(r['tests'])})" if r["tests"] else ")"
            if with_arguments and r["argument"]:
                line += f"\n    prior verifier's argument (re-derive it, don't trust it): {r['argument']}"
            out.append(line)
    if diff and root is not None and not p["node_reason"]:
        files = sorted({r["file"] for r, _ in p["stale"]} | set(p["test_files"]) | (set(p["changed"]) & set(p["files"])))
        if files:
            text = _git(root, "diff", "--no-color", p["base"], *([p["against"]] if p.get("against") else []), "--", *files)
            if len(text) > MAX_DIFF_CHARS:
                text = text[:MAX_DIFF_CHARS] + "\n... (diff truncated)"
            out += ["", f"## Diff since {p['base'][:10]}", "", "```diff", text.rstrip(), "```"]
        other = [f for f in p["changed"] if f not in files]
        if other:
            out += ["", "Also changed since then (not part of this node's mutants): " + ", ".join(other[:15])]
    return "\n".join(out) + "\n"

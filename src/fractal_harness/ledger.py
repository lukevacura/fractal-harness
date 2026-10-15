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
from pathlib import Path

from .cache import ClaimCache
from .context import DEFAULT_ID_PATTERN, _cited
from .store import GOOD
from .symbols import SymbolError, digest

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

def register_node(cache: ClaimCache, root: Path, node: str, spec: str,
                  id_pattern: str = DEFAULT_ID_PATTERN) -> dict:
    """Record which spec and which premise claims (cited decisions, named interfaces) the node's
    verdicts rest on. Re-run after the spec changes and the node has been re-verified."""
    f = root / spec
    if not f.is_file():
        raise SymbolError(f"no such spec: {spec}")
    text = f.read_text(errors="replace")
    found, missing, interfaces = _cited(cache.store.all(), text, id_pattern)
    premises = sorted({e.id for e in found + interfaces})
    cache.store.put_node(node, spec, digest(text), premises)
    return {"spec": spec, "premises": premises, "uncited": missing}


def record(cache: ClaimCache, root: Path, node: str, *, file: str, span: str, mutant: str, result: str,
           commit: str = "HEAD", tests: list[str] | None = None, argument: str = "") -> None:
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
    cache.store.put_mutant(node, file, start, end, mutant, result, resolve_commit(root, commit),
                           tests or [], argument.strip())


# --- diffs --------------------------------------------------------------------------------

def hunks_since(root: Path, commit: str) -> dict[str, list[tuple[int, int]]]:
    """{file: [(old_start, old_count)]} for changes from `commit` to the working tree (0 count =
    pure insertion after old_start). A deleted or added file is {file: [(1, 10**9)]}."""
    out: dict[str, list[tuple[int, int]]] = {}
    cur = None
    for line in _git(root, "diff", "-U0", "--no-color", "--no-renames", commit, "--").splitlines():
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

def plan(cache: ClaimCache, root: Path, node: str) -> dict:
    """Classify every recorded mutant of `node`: {valid, stale: [(mutant, reason)], open, base,
    changed, node_reason}."""
    rows = cache.store.mutants(node)
    if not rows:
        raise SymbolError(f"no mutants recorded for node {node!r} (`fractal ledger record`)")
    cache.update()
    node_reason = None
    info = cache.store.get_node(node)
    if info:
        spec = root / info["spec"]
        if not spec.is_file() or digest(spec.read_text(errors="replace")) != info["spec_sha"]:
            node_reason = f"spec {info['spec']} changed"
        else:
            for pid in info["premises"]:
                e = cache.store.get(pid)
                if e is None:
                    node_reason = f"premise {pid} changed or was removed"
                elif e.status not in GOOD:
                    node_reason = f"premise {pid} is {e.status}: {e.detail}"
                if node_reason:
                    break
    by_commit: dict[str, dict[str, list[tuple[int, int]]] | str] = {}
    for c in {r["commit"] for r in rows}:
        try:
            by_commit[c] = hunks_since(root, c)
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
    base = max(rows, key=lambda r: r["ts"])["commit"]
    return {"node": node, "base": base, "stale": [(r, why) for r, why in stale],
            "valid": [r for r, _ in valid], "node_reason": node_reason,
            "open": [r for r, _ in valid if r["result"] == "survived"],
            "changed": sorted(changed_files), "test_files": sorted(test_files), "files": sorted({r["file"] for r in rows})}


def _file_defines(root: Path, rel: str, commit: str, test: str, hunks: list) -> bool:
    """Does a changed file mention the test (before or after the change)?"""
    f = root / rel
    now = f.read_text(errors="replace") if f.is_file() else ""
    try:
        before = _git(root, "show", f"{commit}:{rel}")
    except SymbolError:
        before = ""
    return _word_in(test, now) or _word_in(test, before)


def render(p: dict, with_arguments: bool = False, diff: bool = True, root: Path | None = None) -> str:
    total = len(p["stale"]) + len(p["valid"])
    out = [f"# Verification plan for {p['node']}", "",
           f"{len(p['valid'])} of {total} recorded verdicts still hold; {len(p['stale'])} need re-running. "
           f"Last verified commit: {p['base'][:10]}."]
    if p["node_reason"]:
        out += ["", f"Everything is stale: {p['node_reason']}. Re-verify the node in full."]
    if p["stale"]:
        out += ["", "## Re-run these mutants"]
        out += [f"- {r['file']}:{r['start']}-{r['end']}  {r['mutant']}  (was {r['result']}) — {why}"
                for r, why in p["stale"]]
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
            text = _git(root, "diff", "--no-color", p["base"], "--", *files)
            if len(text) > MAX_DIFF_CHARS:
                text = text[:MAX_DIFF_CHARS] + "\n... (diff truncated)"
            out += ["", f"## Diff since {p['base'][:10]}", "", "```diff", text.rstrip(), "```"]
        other = [f for f in p["changed"] if f not in files]
        if other:
            out += ["", "Also changed since then (not part of this node's mutants): " + ", ".join(other[:15])]
    return "\n".join(out) + "\n"

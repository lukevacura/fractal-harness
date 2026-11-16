"""Clean up worktrees whose work the graph has already swallowed (merged into the base).

A linked worktree is removable when ALL of these hold:
  - its tip is an ancestor of the base (`git merge-base --is-ancestor`)            [sound: a git fact]
    or, only with `squashed=True`, every commit on it has a patch-id twin in base   [HEURISTIC: `git cherry`]
  - it has work of its own already in base: tip != base. A fresh worktree sits exactly at base and would
    look "merged", but it may be an agent that has just started, so it is kept
  - nothing to lose: no uncommitted changes, or every one is already in the base byte for byte (a file equals
    its version at base, a deletion is absent there); anything else is reported and kept
  - not locked, not the main checkout, not the checkout we are running in

Removal is a plain `git worktree remove` (no --force): git itself still refuses if it disagrees. The branch is
kept unless `delete_branches` is set, and a branch some node registered is never deleted, because
`ledger node`/`node_commit` resolve that node's commit from the branch and a missing branch is an error there.
Nothing is committed and no history is rewritten.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .ledger import branch_of


@dataclass
class Tree:
    path: str
    branch: str | None          # None: detached
    head: str
    locked: bool = False
    prunable: bool = False      # the directory is gone; `git worktree prune` clears the entry
    main: bool = False
    current: bool = False
    verdict: str = ""           # remove | keep
    why: str = ""
    basis: str = ""             # ancestor | patch-id (heuristic) | missing | ""
    nodes: list[str] = field(default_factory=list)       # nodes registered on this branch, or named node-<n>
    keep_branch: str = ""       # why the branch survives even with delete_branches ("" = it may go)


def _git(cwd: str | Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)


def listing(root: Path) -> list[Tree]:
    out, cur = [], {}
    for line in _git(root, "worktree", "list", "--porcelain").stdout.splitlines() + [""]:
        if not line:
            if cur.get("worktree"):
                br = cur.get("branch")
                out.append(Tree(cur["worktree"], br.removeprefix("refs/heads/") if br else None, cur.get("HEAD", ""),
                                locked="locked" in cur, prunable="prunable" in cur))
            cur = {}
        else:
            k, _, v = line.partition(" ")
            cur[k] = v
    if out:
        out[0].main = True
    top = _git(root, "rev-parse", "--show-toplevel").stdout.strip() or str(root)
    for t in out:
        t.current = Path(t.path).resolve() == Path(top).resolve()
    return out


def _dirty(path: str) -> list[str]:
    """Tracked changes and untracked (not ignored) files: either would be lost with the directory."""
    return [l for l in _git(path, "status", "--porcelain").stdout.splitlines() if l.strip()]


def _unswallowed(path: str, base: str) -> list[str]:
    """Uncommitted changes in the worktree that the base does NOT already contain, byte for byte: a modified or
    untracked file must equal its version at the base, a deleted one must be absent there. [sound: content hashes]
    Empty means everything uncommitted here is already in the base, so removing the tree loses nothing."""
    out = _git(path, "status", "--porcelain", "-z", "--untracked-files=all").stdout.split("\0")
    left, i = [], 0
    while i < len(out):
        rec = out[i]
        i += 1
        if len(rec) < 4:
            continue
        xy, name = rec[:2], rec[3:]
        if "R" in xy or "C" in xy:
            i += 1                                  # a rename lists its source next: never judged swallowed
            left.append(name)
            continue
        at_base = _git(path, "rev-parse", "--verify", "-q", f"{base}:{name}")
        base_sha = at_base.stdout.strip() if at_base.returncode == 0 else None
        f = Path(path) / name
        if "U" in xy or xy in ("AA", "DD"):
            left.append(name)
        elif "D" in xy and not f.exists():
            if base_sha is not None:
                left.append(name)
        elif not f.is_file():
            left.append(name)
        elif _git(path, "hash-object", "--", name).stdout.strip() != base_sha:   # None (absent at base) never equals
            left.append(name)
    return left


def _merged(root: Path, tip: str, base: str, squashed: bool) -> str:
    if _git(root, "merge-base", "--is-ancestor", tip, base).returncode == 0:
        return "ancestor"
    if squashed:
        cherry = _git(root, "cherry", base, tip).stdout.splitlines()
        if cherry and all(l.startswith("-") for l in cherry):
            return "patch-id"
    return ""


def plan(cache, root: Path, into: str | None = None, squashed: bool = False) -> list[Tree]:
    trees = listing(root)
    if not trees:
        return []
    main = trees[0]
    base = _git(root, "rev-parse", "--verify", f"{into or main.head}^{{commit}}").stdout.strip()
    if not base:
        raise ValueError(f"cannot resolve base {into!r}")
    registered: dict[str, list[str]] = {}
    byname: dict[str, list[str]] = {}
    for n in cache.store.all_nodes():
        if b := branch_of(cache, n):
            registered.setdefault(b, []).append(n)
        byname.setdefault(f"node-{n}", []).append(n)
    for t in trees:
        t.nodes = sorted(set(registered.get(t.branch or "", []) + byname.get(t.branch or "", [])))
        if t.branch and t.branch in registered:
            t.keep_branch = f"node {', '.join(sorted(registered[t.branch]))} registers it"
        if t.main:
            t.verdict, t.why = "keep", "the main checkout"
        elif t.current:
            t.verdict, t.why = "keep", "the checkout you are running in"
        elif t.prunable:
            t.verdict, t.why, t.basis = "remove", "its directory is gone (git worktree prune)", "missing"
        elif t.locked:
            t.verdict, t.why = "keep", "locked"
        elif not t.head:
            t.verdict, t.why = "keep", "no HEAD"
        elif not (basis := _merged(root, t.head, base, squashed)):
            t.verdict, t.why = "keep", "has work not in the base" + ("" if squashed else " (try --squashed for squash/rebase merges)")
        elif t.head == base:
            t.verdict, t.why = "keep", "sits exactly at the base: no work of its own yet (maybe just started)"
        elif (dirty := _dirty(t.path)) and (left := _unswallowed(t.path, base)):
            t.verdict, t.why = "keep", (f"merged, but {len(left)} of {len(dirty)} uncommitted change(s) are not in the base, "
                                        f"e.g. {left[0]}")
        else:
            t.verdict, t.basis = "remove", basis
            t.why = "merged into the base" + (" (patch-id match: HEURISTIC, commits are not ancestors)" if basis == "patch-id" else "")
            if dirty:
                t.why += f"; its {len(dirty)} uncommitted change(s) are already in the base, byte for byte"
    return trees


def apply(root: Path, trees: list[Tree], delete_branches: bool = False) -> list[str]:
    log, pruned = [], False
    for t in trees:
        if t.verdict != "remove":
            continue
        if t.basis == "missing":
            pruned = True
            continue
        r = _git(root, "worktree", "remove", t.path)         # no --force: git refuses a dirty or locked tree itself
        if r.returncode:
            t.verdict, t.why = "keep", f"git refused: {r.stderr.strip().splitlines()[-1] if r.stderr.strip() else 'failed'}"
            log.append(f"kept {t.path}: {t.why}")
            continue
        log.append(f"removed worktree {t.path}")
        if delete_branches and t.branch:
            if t.keep_branch:
                log.append(f"  kept branch {t.branch}: {t.keep_branch}")
            else:
                d = _git(root, "branch", "-d", t.branch)    # -d, not -D: git refuses an unmerged branch
                log.append(f"  deleted branch {t.branch}" if not d.returncode else
                           f"  kept branch {t.branch}: {d.stderr.strip().splitlines()[-1] if d.stderr.strip() else 'not merged'}")
    if pruned:
        _git(root, "worktree", "prune")
        log.append("pruned worktree entries whose directory is gone")
    return log


def render(trees: list[Tree], base_note: str = "") -> str:
    rm = [t for t in trees if t.verdict == "remove"]
    keep = [t for t in trees if t.verdict == "keep" and not t.main]
    out = [f"{len(rm)} of {max(len(trees) - 1, 0)} linked worktree(s) removable" + (f" {base_note}" if base_note else "")]
    for t in rm:
        nodes = f" [nodes: {', '.join(t.nodes)}]" if t.nodes else ""
        out.append(f"  remove {t.path} ({t.branch or 'detached'}): {t.why}{nodes}"
                   + (f"; branch kept ({t.keep_branch})" if t.keep_branch else ""))
    for t in keep:
        out.append(f"  keep   {t.path} ({t.branch or 'detached'}): {t.why}")
    return "\n".join(out) + "\n"

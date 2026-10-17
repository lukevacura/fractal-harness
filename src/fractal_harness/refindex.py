"""The reference index: which files mention a symbol, on every live tree.

One map (name -> files that mention it) per tree, where a tree is a checked-out worktree (its working
files, uncommitted edits included) or, on request, a local branch tip that no worktree has. Everything
that asks "who uses this?" reads it: `graph-check` (this checkout), and the cross-branch questions built
on it ("does another live branch call what I am changing or deleting?").

A mention is a whole identifier in a `.rs`/`.py` file (`\\w+` token), the same word-match rule the lints
use. It is sound for "this name is nowhere" and unsound for "this name is used": a shared generic name
reports a use that is not one. Callers treat a mention as a reason to stop and look, never as proof.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

SOURCE_SUFFIXES = (".rs", ".py")
TOKEN = re.compile(r"\w+")
TEST_PATH = re.compile(r"(^|/)(tests?|test_[^/]*|[^/]*_tests?\.\w+)(/|$)")


def is_test(path: str) -> bool:
    return bool(TEST_PATH.search(path))


def _git(cwd: Path | str, *args: str, input: bytes | None = None) -> bytes:
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, input=input)
    if p.returncode:
        raise RuntimeError(p.stderr.decode(errors="replace").strip() or f"git {args[0]} failed")
    return p.stdout


@dataclass
class Tree:
    label: str                    # the branch name, `detached@<sha>`, or the checkout's label
    texts: dict[str, str]         # repo-relative path -> text, source files only
    current: bool = False         # the checkout the command was run from
    path: str | None = None       # worktree directory, None for a branch tip
    index: "Index" = field(init=False)

    def __post_init__(self) -> None:
        self.index = Index(self.texts)


class Index:
    """name -> the files of one tree that mention it."""

    def __init__(self, texts: dict[str, str]):
        self.texts = texts
        self._files: dict[str, set[str]] = {}
        for path, text in texts.items():
            for tok in set(TOKEN.findall(text)):
                self._files.setdefault(tok, set()).add(path)

    def files(self, name: str, exclude: str | None = None, tests: bool = True) -> list[str]:
        return sorted(f for f in self._files.get(name, ()) if f != exclude and (tests or not is_test(f)))

    def mentions(self, name: str, path: str) -> bool:
        return path in self._files.get(name, ())


def _source(paths: list[str]) -> list[str]:
    return [p for p in paths if p.endswith(SOURCE_SUFFIXES)]


def _worktree_texts(path: str) -> dict[str, str]:
    names = _git(path, "ls-files", "--cached", "--others", "--exclude-standard", "-z").decode().split("\0")
    out = {}
    for rel in _source([n for n in names if n]):
        f = Path(path) / rel
        if f.is_file():
            out[rel] = f.read_text(errors="replace")
    return out


def _rev_texts(root: Path, rev: str) -> dict[str, str]:
    names = _source(_git(root, "ls-tree", "-r", "--name-only", "-z", rev).decode().split("\0"))
    if not names:
        return {}
    raw = _git(root, "cat-file", "--batch", input="".join(f"{rev}:{n}\n" for n in names).encode())
    out, pos = {}, 0
    for n in names:
        end = raw.index(b"\n", pos)
        header = raw[pos:end].split()
        if len(header) == 3 and header[1] == b"blob":
            size = int(header[2])
            out[n] = raw[end + 1:end + 1 + size].decode(errors="replace")
            pos = end + 1 + size + 1
        else:                      # "<rev>:<path> missing"
            pos = end + 1
    return out


def load_trees(root: Path, branches: bool = False) -> list[Tree]:
    """Every live tree: each worktree (the one at `root` marked current), plus, with `branches`, the tip of
    each local branch no worktree has checked out. The checkout at `root` comes first."""
    root = Path(root).resolve()
    entries, cur = [], {}
    for line in _git(root, "worktree", "list", "--porcelain").decode().splitlines() + [""]:
        if not line:
            if cur.get("worktree"):
                entries.append(cur)
            cur = {}
        else:
            k, _, v = line.partition(" ")
            cur[k] = v
    trees, checked_out = [], set()
    for e in entries:
        branch = e.get("branch", "").removeprefix("refs/heads/")
        checked_out.add(branch)
        label = branch or f"detached@{e.get('HEAD', '')[:7]}"
        trees.append(Tree(label, _worktree_texts(e["worktree"]), Path(e["worktree"]).resolve() == root, e["worktree"]))
    if branches:
        for b in _git(root, "for-each-ref", "--format=%(refname:short)", "refs/heads").decode().split():
            if b not in checked_out:
                trees.append(Tree(b, _rev_texts(root, f"refs/heads/{b}")))
    trees.sort(key=lambda t: not t.current)
    return trees


class RefIndex:
    """The indexes of several trees, to ask one question of all of them."""

    def __init__(self, trees: list[Tree]):
        self.trees = trees

    @classmethod
    def load(cls, root: Path, branches: bool = False) -> "RefIndex":
        return cls(load_trees(root, branches))

    @property
    def current(self) -> Tree | None:
        return next((t for t in self.trees if t.current), None)

    def where(self, name: str, exclude: str | None = None, tests: bool = True) -> dict[str, list[str]]:
        """{tree label: files mentioning `name`}, only trees with at least one. `exclude` drops one path
        (the symbol's own definition) from every tree."""
        out = {}
        for t in self.trees:
            if files := t.index.files(name, exclude, tests):
                out[t.label] = files
        return out

    def elsewhere(self, name: str, exclude: str | None = None) -> dict[str, list[str]]:
        """`where`, minus the current checkout: who else uses it?"""
        return {k: v for k, v in self.where(name, exclude).items() if k != (self.current.label if self.current else None)}

"""The region tree: a quadtree's mechanics over the code's own address space.

Regions are directories → files (line-range splits may come later). The tree is
capacity-bounded: a region that holds more lines than one owner can keep in context is split
into its children; a region at or under capacity is one owner's territory.

  placement(deps)   the smallest region containing everything a claim depends on ("loose
                    quadtree": objects live at the smallest node that fully contains them).
                    A claim placed at N governs everything beneath N.
  affects(deps, f)  whether editing file f can change a claim's verdict (f matches one of the
                    paths its verdict depends on). Used to decide which rules an edit re-checks.
  owner(f)          the region that owns f: the first region on the root → f path that fits
                    within capacity.
"""

from __future__ import annotations

import re
import subprocess
from functools import lru_cache
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

DEFAULT_CAPACITY = 1000   # lines one owner holds at full resolution
IGNORED_DIRS = {".git", ".fractal"}


def tracked_files(root: Path) -> list[str]:
    """Repo-relative files: git's tracked + untracked-but-not-ignored, else a filesystem walk."""
    try:
        out = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                             cwd=root, capture_output=True, check=True).stdout
        files = [f for f in out.decode(errors="replace").split("\0") if f]
    except (subprocess.CalledProcessError, FileNotFoundError):
        files = [p.relative_to(root).as_posix() for p in root.rglob("*")
                 if p.is_file() and not IGNORED_DIRS & set(p.relative_to(root).parts)]
    return sorted(f for f in files if not IGNORED_DIRS & set(PurePosixPath(f).parts))


def is_glob(p: str) -> bool:
    return any(c in p for c in "*?[")


@lru_cache(maxsize=1024)
def _glob_regex(pattern: str) -> re.Pattern:
    """Path.glob semantics: `**/` matches zero or more directories; `*` and `?` stay in one segment."""
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?"); i += 3
        elif pattern.startswith("**", i):
            out.append(".*"); i += 2
        elif pattern[i] == "*":
            out.append("[^/]*"); i += 1
        elif pattern[i] == "?":
            out.append("[^/]"); i += 1
        elif pattern[i] == "[":
            j = pattern.find("]", i)
            if j == -1:
                out.append(re.escape(pattern[i])); i += 1
            else:
                out.append(pattern[i:j + 1]); i = j + 1
        else:
            out.append(re.escape(pattern[i])); i += 1
    return re.compile("".join(out) + r"\Z")


def glob_match(pattern: str, rel: str) -> bool:
    return bool(_glob_regex(pattern).match(rel))


def matches(pattern: str, rel: str) -> bool:
    """Does a dependency pattern (file, directory, or glob) cover `rel`?"""
    pattern = pattern.rstrip("/")
    if pattern in ("", "."):
        return True
    if is_glob(pattern):
        return glob_match(pattern, rel)
    return rel == pattern or rel.startswith(pattern + "/")


def affects(deps: list[str], rel: str) -> bool:
    return any(matches(d, rel) for d in deps)


def _literal_prefix(pattern: str) -> str:
    parts = []
    for part in pattern.split("/"):
        if is_glob(part):
            break
        parts.append(part)
    return "/".join(parts)


def _common_dir(paths: list[str]) -> str:
    if not paths:
        return ""
    if len(paths) == 1:
        return paths[0]
    split = [p.split("/") for p in paths]
    common = []
    for parts in zip(*split):
        if len(set(parts)) != 1:
            break
        common.append(parts[0])
    return "/".join(common)


def placement(deps: list[str], files: list[str]) -> str:
    """Smallest region (directory or file path; "" = root) containing every dependency."""
    covered: list[str] = []
    for d in deps:
        if is_glob(d):
            hit = [f for f in files if glob_match(d, f)]
            covered.extend(hit or [_literal_prefix(d)])
        else:
            covered.append(d.rstrip("/"))
    region = _common_dir(sorted(set(covered)))
    return "" if region in (".", "") else region


def ancestors(rel: str) -> list[str]:
    """Regions on the path from the root to `rel`, root ("") first, `rel` itself last."""
    parts = rel.split("/")
    return [""] + ["/".join(parts[:i]) for i in range(1, len(parts) + 1)]


def governs(region: str, rel: str) -> bool:
    return region == "" or rel == region or rel.startswith(region + "/")


@dataclass
class Region:
    path: str
    lines: int = 0
    files: int = 0
    children: dict[str, "Region"] = field(default_factory=dict)

    def walk(self):
        yield self
        for c in self.children.values():
            yield from c.walk()


def _lines(root: Path, rel: str) -> int:
    try:
        with open(root / rel, "rb") as f:
            return sum(chunk.count(b"\n") for chunk in iter(lambda: f.read(1 << 16), b""))
    except OSError:
        return 0


def build(root: Path, files: list[str] | None = None) -> Region:
    """The full directory/file tree with line counts (splitting is decided per query by capacity)."""
    root_region = Region("")
    for rel in files if files is not None else tracked_files(root):
        n = _lines(root, rel)
        node = root_region
        node.lines += n
        node.files += 1
        for path in ancestors(rel)[1:]:
            node = node.children.setdefault(path, Region(path))
            node.lines += n
            node.files += 1
    return root_region


def owner(tree: Region, rel: str, capacity: int = DEFAULT_CAPACITY) -> str:
    """The region owning `rel`: the first region from the root that fits within capacity."""
    node = tree
    for path in ancestors(rel)[1:]:
        if node.lines <= capacity:
            return node.path
        node = node.children.get(path)
        if node is None:
            return path
    return rel


def depth(tree: Region, region: str, capacity: int = DEFAULT_CAPACITY) -> int:
    """Zoom level of a region: how many over-capacity (split) regions lie above it."""
    node, d = tree, 0
    for path in ancestors(region)[1:]:
        if node.lines > capacity:
            d += 1
        node = node.children.get(path)
        if node is None:
            break
    return d

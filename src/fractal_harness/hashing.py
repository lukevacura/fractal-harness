"""Content hashing for source pointers and edge identity."""

from __future__ import annotations

import contextlib
import hashlib
import json
import stat
from pathlib import Path

MISSING = "missing"


def sha(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode()
    return hashlib.sha256(data).hexdigest()


def canonical(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


_FILE_HASHES: dict[tuple, str] = {}


def file_hash(root: Path, rel: str) -> str:
    from .config import ConfigError, ConfigFileError, resolve_path
    try:
        path = resolve_path(root, rel)     # `<named root>:<path>` hashes that file's content, wherever it lives
    except ConfigFileError:
        raise
    except ConfigError:
        return MISSING
    try:
        st = path.stat()
    except OSError:
        return MISSING
    if not stat.S_ISREG(st.st_mode):
        return MISSING
    key = (str(path), st.st_mtime_ns, st.st_size, st.st_ino)   # one command hashes the same file for many claims
    hit = _FILE_HASHES.get(key)
    if hit is None:
        if len(_FILE_HASHES) > 200_000:
            _FILE_HASHES.clear()
        hit = _FILE_HASHES[key] = sha(path.read_bytes())
    return hit


def is_glob(rel: str) -> bool:
    return any(c in rel for c in "*?[")


_SCOPE: dict | None = None


@contextlib.contextmanager
def stable_files():
    """Within this block the tree is assumed not to change (a read-only scan: `status`, `refresh`): globs are
    expanded and hashed once per (root, pattern), not once per claim. Nests; the outermost block owns the memo."""
    global _SCOPE
    outer = _SCOPE is not None
    if not outer:
        _SCOPE = {}
    try:
        yield
    finally:
        if not outer:
            _SCOPE = None


def scoped(key: tuple, compute):
    """`compute()` once per `stable_files()` scope (just `compute()` outside one). Errors are not memoized."""
    if _SCOPE is None:
        return compute()
    if key not in _SCOPE:
        _SCOPE[key] = compute()
    return _SCOPE[key]


def _ignored_dirs(root: Path) -> list[str]:
    """Directories git ignores (`bench/data/`, a venv, `target/`), repo-relative with a trailing slash."""
    import subprocess
    key = ("ignored", str(root))
    if _SCOPE is not None and key in _SCOPE:
        return _SCOPE[key]
    try:
        out = subprocess.run(["git", "ls-files", "-z", "--others", "--ignored", "--exclude-standard", "--directory"],
                             cwd=root, capture_output=True, check=True).stdout
    except (subprocess.CalledProcessError, FileNotFoundError, NotADirectoryError):
        return []
    dirs = [d for d in out.decode(errors="replace").split("\0") if d.endswith("/")]
    if _SCOPE is not None:
        _SCOPE[key] = dirs
    return dirs


def expand_glob(root: Path, pattern: str) -> list[str]:
    """Repo-relative files matching `pattern`. It walks only the glob's literal prefix (`fuzz/` for
    `fuzz/**/*.rs`) and does not descend into directories git ignores (build output, venvs, generated data:
    100k+ files in a real repo) unless the glob itself points into one (`bench/data/**`). A single recursive
    `Path.glob` over such a repo took ~20s per probe; this takes milliseconds."""
    key = ("expand", str(root), pattern)
    if _SCOPE is not None and key in _SCOPE:
        return _SCOPE[key]
    out = _walk_glob(root, pattern)
    if _SCOPE is not None:
        _SCOPE[key] = out
    return out


def _walk_glob(root: Path, pattern: str) -> list[str]:
    import os
    from .regions import _literal_prefix, glob_match
    prefix = _literal_prefix(pattern).strip("/")
    start = root / prefix if prefix else root
    if not start.is_dir():
        return [prefix] if prefix and start.is_file() and glob_match(pattern, prefix) else []
    skip = [d for d in _ignored_dirs(root) if not (prefix + "/").startswith(d)]
    skip_set = {d.rstrip("/") for d in skip}
    out = []
    for dirpath, dirnames, filenames in os.walk(start):
        rel_dir = Path(dirpath).relative_to(root).as_posix()
        rel_dir = "" if rel_dir == "." else rel_dir
        dirnames[:] = [d for d in dirnames
                       if d != ".git" and (f"{rel_dir}/{d}" if rel_dir else d) not in skip_set]
        for f in filenames:
            rel = f"{rel_dir}/{f}" if rel_dir else f
            if glob_match(pattern, rel) and (root / rel).is_file():
                out.append(rel)
    return sorted(out)


def glob_hash(root: Path, pattern: str) -> str:
    """Hash of every matching file's path and content: changes on edit, add, or delete."""
    key = ("hash", str(root), pattern)
    if _SCOPE is not None and key in _SCOPE:
        return _SCOPE[key]
    files = expand_glob(root, pattern)
    out = sha(canonical([[f, file_hash(root, f)] for f in files])) if files else MISSING
    if _SCOPE is not None:
        _SCOPE[key] = out
    return out


def read_hashes(root: Path, reads: list[str]) -> dict[str, str]:
    return {rel: glob_hash(root, rel) if is_glob(rel) else file_hash(root, rel)
            for rel in sorted(set(reads))}


def edge_id(kind: str, pre: str, post: str) -> str:
    """Stable identity: the claim itself, independent of source state."""
    return sha(canonical({"kind": kind, "pre": pre.strip(), "post": post.strip()}))[:16]


def fingerprint(hashes: dict[str, str], probe: dict | None, checker_version: str) -> str:
    """What the verdict depends on. A changed fingerprint means the verdict is stale."""
    return sha(canonical({"reads": hashes, "probe": probe, "checker": checker_version}))

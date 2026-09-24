"""Content hashing for source pointers and edge identity."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

MISSING = "missing"


def sha(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode()
    return hashlib.sha256(data).hexdigest()


def canonical(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def file_hash(root: Path, rel: str) -> str:
    path = root / rel
    if not path.is_file():
        return MISSING
    return sha(path.read_bytes())


def is_glob(rel: str) -> bool:
    return any(c in rel for c in "*?[")


def glob_hash(root: Path, pattern: str) -> str:
    """Hash of every matching file's path and content: changes on edit, add, or delete."""
    files = sorted(p.relative_to(root).as_posix() for p in root.glob(pattern) if p.is_file())
    return sha(canonical([[f, file_hash(root, f)] for f in files])) if files else MISSING


def read_hashes(root: Path, reads: list[str]) -> dict[str, str]:
    return {rel: glob_hash(root, rel) if is_glob(rel) else file_hash(root, rel)
            for rel in sorted(set(reads))}


def edge_id(kind: str, pre: str, post: str) -> str:
    """Stable identity: the claim itself, independent of source state."""
    return sha(canonical({"kind": kind, "pre": pre.strip(), "post": post.strip()}))[:16]


def fingerprint(hashes: dict[str, str], probe: dict | None, checker_version: str) -> str:
    """What the verdict depends on. A changed fingerprint means the verdict is stale."""
    return sha(canonical({"reads": hashes, "probe": probe, "checker": checker_version}))

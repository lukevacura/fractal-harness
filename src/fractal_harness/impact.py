"""`fractal impact <node>`: what a node's contract change does to the other live branches.

A node that changes an exported item's signature (or its guarantee) can break code it cannot see: callers
another worktree added since the two diverged. This finds the node's changed or removed exports (the working
tree against the merge-base with the default branch) and asks the reference index which *other* trees
mention each one, keeping the files this checkout has no mention in: the callers its own tests could not
catch.

It is a warning, never a gate: a mention is a word match (see refindex.py), so it can name a caller that is
not one, and a call through an alias or a macro can go unnamed.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from .cache import ClaimCache
from .nodes import parse_header
from .refindex import RefIndex, is_test
from .symbols import SymbolError, find, split_ref


def _git(root: Path, *args: str) -> str | None:
    p = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)
    return p.stdout if p.returncode == 0 else None


def default_base(root: Path) -> str:
    """The merge-base of HEAD with the repository's default branch, else HEAD itself."""
    for ref in (((_git(root, "symbolic-ref", "--short", "refs/remotes/origin/HEAD") or "").strip()),
                "main", "master"):
        if ref and (_git(root, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}") is not None):
            if base := (_git(root, "merge-base", "HEAD", ref) or "").strip():
                return base
    return (_git(root, "rev-parse", "HEAD") or "").strip()


@dataclass
class Change:
    ref: str            # path::Item as the spec exports it
    kind: str           # changed | removed | guarantee
    detail: str = ""


def _item(ref: str) -> str:
    """The name callers write: `path::Type::method` is called `method`, `path::Type` is `Type`."""
    return split_ref(ref)[1].rpartition("::")[2]


def _signature(text: str | None, ref: str) -> str | None:
    if text is None:
        return None
    try:
        return find(text, split_ref(ref)[1])[1]
    except SymbolError:
        return None


def changed_exports(root: Path, spec: str, base: str) -> list[Change]:
    """The node's exports whose definition differs, or is gone, between `base` and the working tree; and a
    `guarantee` change, which touches every export."""
    now_spec = (root / spec).read_text(errors="replace") if (root / spec).is_file() else ""
    old_spec = _git(root, "show", f"{base}:{spec}") or ""
    now_h, old_h = parse_header(now_spec), parse_header(old_spec) if old_spec else None
    refs = list(dict.fromkeys([*(old_h.exports if old_h else []), *(now_h.exports if now_h else [])]))
    out: list[Change] = []
    for ref in refs:
        if "::" not in ref:
            continue
        path = split_ref(ref)[0]
        old = _signature(_git(root, "show", f"{base}:{path}"), ref)
        f = root / path
        new = _signature(f.read_text(errors="replace") if f.is_file() else None, ref)
        if old is not None and new is None:
            out.append(Change(ref, "removed"))
        elif old is not None and new is not None and old != new:
            out.append(Change(ref, "changed"))
    if now_h and old_h and (now_h.guarantee, now_h.condition) != (old_h.guarantee, old_h.condition):
        out += [Change(r, "guarantee", "the guarantee or condition changed") for r in now_h.exports
                if "::" in r and r not in {c.ref for c in out}]
    return out


def callers(idx: RefIndex, name: str, defining: str | None = None, unseen_only: bool = True) -> dict[str, list[str]]:
    """{other tree: files mentioning `name`}. With `unseen_only`, only files this checkout has no mention
    in (new callers it cannot see); without it, every mention anywhere else (what reclamation must respect)."""
    here = idx.current
    out = {}
    for label, files in idx.elsewhere(name, defining).items():
        if unseen_only and here:
            files = [f for f in files if not here.index.mentions(name, f)]
        if files:
            out[label] = files
    return out


def check(cache: ClaimCache, root: Path, node: str, base: str | None = None, branches: bool = False) -> dict:
    info = cache.store.get_node(node)
    if info is None:
        raise SymbolError(f"no such node: {node} (`fractal ledger node {node} --spec <spec>`)")
    base = base or default_base(root)
    idx = RefIndex.load(root, branches)
    rows = []
    for ch in changed_exports(root, info["spec"], base):
        name = _item(ch.ref)
        rows.append({"export": ch.ref, "change": ch.kind, "detail": ch.detail, "name": name,
                     "callers": callers(idx, name, split_ref(ch.ref)[0])})
    return {"node": node, "base": base, "trees": [t.label for t in idx.trees], "rows": rows}


def render(r: dict) -> str:
    hit = [x for x in r["rows"] if x["callers"]]
    if not r["rows"]:
        return f"{r['node']}: no exported item changed since {r['base'][:10]}\n"
    out = [f"# Contract impact of {r['node']} (exports changed since {r['base'][:10]}; looked at "
           f"{', '.join(r['trees'])})", ""]
    for x in r["rows"]:
        why = x["detail"] or x["change"]
        out.append(f"- {x['export']}: {why}" + ("" if x["callers"] else "  (no other live tree mentions `" + x["name"] + "`)"))
        for label, files in sorted(x["callers"].items()):
            out.append(f"    {label}: " + ", ".join(f + (" (test)" if is_test(f) else "") for f in files))
    if hit:
        out += ["", "These files mention a changed item on another live tree and not here, so this node's own tests "
                    "cannot see them. Tell the owner of that branch, or rebase over the change deliberately. A mention "
                    "is a word match: check that it is a real use."]
    return "\n".join(out) + "\n"

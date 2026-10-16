"""Node specs as DAG vertices: a spec's front matter declares what the node rests on and what it offers.

    ---
    parents: stats, cov            # nodes whose contracts this one assumes (direct dependencies only)
    implements: D18                # decisions this node must reproduce, taken in full
    see: naive                     # peer nodes: non-binding examples of how a similar node was done
    guarantee: Windowed mean and variance, exact for N.
    condition: Only for windows with V < 2^63; stats flags the rest.
    exports: src/stats.rs::Stats::update, src/stats.rs::Stats
    ---

A child may rely on a parent's *contract*: its `guarantee`, its `condition` (a conditional guarantee
carries its condition with it), and the current signatures of its `exports`. It may not rely on
anything else about the parent, and grandparents are not premises. The contract's digest is what
the mutant ledger watches, so a parent's contract changing stales exactly its children.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .symbols import SymbolError, digest, split_ref, symbol_of

LIST_FIELDS = ("parents", "implements", "see", "exports")
STR_FIELDS = ("guarantee", "condition")
CONDITIONAL_WORDS = re.compile(r"\b(only (?:when|if|for)|provided|unless|assuming|as long as|given that)\b", re.I)


@dataclass
class Header:
    parents: list[str] = field(default_factory=list)
    implements: list[str] = field(default_factory=list)
    see: list[str] = field(default_factory=list)
    exports: list[str] = field(default_factory=list)
    guarantee: str = ""
    condition: str = ""


def parse_header(text: str) -> Header | None:
    """The spec's front matter, or None when it has none (a legacy spec)."""
    m = re.match(r"\A---[ \t]*\n(.*?)\n---[ \t]*(?:\n|\Z)", text, re.S)
    if not m:
        return None
    fields: dict[str, str] = {}
    key = None
    for line in m.group(1).splitlines():
        line = re.sub(r"\s+#.*$", "", line.rstrip())
        if not line.strip():
            continue
        if kv := re.match(r"^([a-z]+):\s*(.*)$", line):
            key = kv.group(1)
            fields[key] = kv.group(2).strip()
        elif key and line[0] in " \t":
            fields[key] = f"{fields[key]} {line.strip()}".strip()
    unknown = sorted(set(fields) - set(LIST_FIELDS) - set(STR_FIELDS))
    if unknown:
        raise SymbolError(f"unknown spec header field(s): {', '.join(unknown)}")
    h = Header()
    for k in LIST_FIELDS:
        setattr(h, k, [x for x in re.split(r"[,\s]+", fields.get(k, "")) if x])
    for k in STR_FIELDS:
        setattr(h, k, fields.get(k, ""))
    return h


def body(text: str) -> str:
    return re.sub(r"\A---[ \t]*\n.*?\n---[ \t]*(?:\n|\Z)", "", text, count=1, flags=re.S)


def lint(h: Header) -> list[str]:
    notes = []
    if h.guarantee and CONDITIONAL_WORDS.search(h.guarantee) and not h.condition:
        notes.append("the guarantee reads as conditional but declares no `condition:`; a child would not see it")
    if h.exports and not h.guarantee:
        notes.append("exports are declared without a `guarantee:`")
    return notes


def contract(root: Path, spec_path: str, exports: int | None = None) -> str:
    """The text a child may rely on: guarantee, condition, and the exports' current signatures
    (only the first `exports` of them when given)."""
    f = root / spec_path
    if not f.is_file():
        raise SymbolError(f"no such spec: {spec_path}")
    h = parse_header(f.read_text(errors="replace"))
    if h is None or not h.guarantee:
        raise SymbolError(f"{spec_path} declares no `guarantee:`: nothing for a child to rely on")
    out = [f"guarantee: {h.guarantee}"]
    if h.condition:
        out.append(f"condition: {h.condition}")
    for ref in h.exports[:exports]:
        split_ref(ref)
        out += [f"interface {ref}:", symbol_of(root, ref)]
    return "\n".join(out)


def contract_digest(root: Path, spec_path: str) -> str:
    return digest(contract(root, spec_path))


def contract_parts(root: Path, spec_path: str) -> dict:
    """What a child's verdicts rest on, piece by piece: {claim: digest of guarantee+condition,
    exports: {ref: digest of its signature}}."""
    h = parse_header((root / spec_path).read_text(errors="replace")) if (root / spec_path).is_file() else None
    if h is None or not h.guarantee:
        raise SymbolError(f"{spec_path} declares no `guarantee:`: nothing for a child to rely on")
    for ref in h.exports:
        split_ref(ref)
    return {"claim": digest(f"{h.guarantee}\n{h.condition}"),
            "exports": {ref: digest(symbol_of(root, ref)) for ref in h.exports}}


def contract_change(old, root: Path, spec_path: str) -> str | None:
    """Why a parent's contract no longer matches what was recorded, or None. Adding exports never
    changes it (no child relied on them); changing the guarantee or condition, or removing or
    altering an export that was there, does."""
    if isinstance(old, str):          # recorded as one digest: unchanged, or only exports appended
        n = len(parse_header((root / spec_path).read_text(errors="replace")).exports)
        if any(digest(contract(root, spec_path, k)) == old for k in range(n, -1, -1)):
            return None
        return "its contract changed"
    now = contract_parts(root, spec_path)
    if now["claim"] != old["claim"]:
        return "its guarantee or condition changed"
    for ref, sig in old["exports"].items():
        if ref not in now["exports"]:
            return f"export {ref} was removed"
        if now["exports"][ref] != sig:
            return f"export {ref} changed"
    return None


# --- lints that need the repository -------------------------------------------------------

SOURCE_GLOBS = (".rs", ".py")
TYPE_RE = re.compile(r"\b(struct|enum|class|trait)\b")
MAX_NAMES = 8


def _files(root: Path) -> list[str]:
    import subprocess
    try:
        out = subprocess.run(["git", "ls-files"], cwd=root, capture_output=True, text=True, check=True).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []
    return [l for l in out.splitlines() if l.endswith(SOURCE_GLOBS)]


def _imported_modules(text: str) -> dict[str, list[str]]:
    """{module: candidate file suffixes} for the crate-internal (Rust `crate::m`) and dotted-module
    (Python `a.b`) imports in `text`."""
    mods: dict[str, list[str]] = {}
    for m in re.finditer(r"\bcrate::(\w+)", text):
        n = m.group(1)
        mods[n] = [f"/{n}.rs", f"/{n}/mod.rs"]
    for m in re.finditer(r"^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))", text, re.M):
        dotted = (m.group(1) or m.group(2)).lstrip(".")
        if dotted:
            path = dotted.replace(".", "/")
            mods[dotted] = [f"/{path}.py", f"/{path}/__init__.py"]
    return mods


def _methods(text: str, type_name: str) -> list[str]:
    """Public methods of `type_name` found in `text`: Rust `pub fn` inside its impl blocks, Python
    non-underscore `def` inside its class."""
    lines, names = text.splitlines(), []
    for i, l in enumerate(lines):
        head = re.match(rf"^(\s*)(?:impl\b[^{{]*?\b{re.escape(type_name)}\b[^{{]*\{{|class\s+{re.escape(type_name)}\b.*:)\s*$", l)
        if not head:
            continue
        ind = len(head.group(1))
        for nxt in lines[i + 1:]:
            if nxt.strip() and len(nxt) - len(nxt.lstrip()) <= ind:
                break
            if m := re.match(r"^\s*(?:pub(?:\([^)]*\))?\s+(?:const\s+|async\s+|unsafe\s+)*fn|def)\s+([A-Za-z]\w*)", nxt):
                if "pub" in nxt.split("fn")[0] or nxt.lstrip().startswith("def"):
                    names.append(m.group(1))
    return list(dict.fromkeys(names))


def repo_lint(root: Path, h: Header, nodes: dict[str, dict], own_spec_text: str) -> list[str]:
    """Header problems that are visible in the repository and would starve a node's context bundle,
    each with the fix."""
    notes: list[str] = []
    own_files: set[str] = set()
    exported: set[str] = set(h.exports)
    for ref in h.exports:
        if "::" not in ref:
            notes.append(f"export `{ref}` names a whole file; children cannot resolve it. Export items: "
                         f"`{ref}::<Type>` and `{ref}::<Type>::<method>`")
            continue
        path, name = split_ref(ref)
        own_files.add(path)
        f = root / path
        if not f.is_file() or "::" in name:
            continue
        try:
            sig = symbol_of(root, ref)
        except SymbolError:
            continue
        first = next((l for l in sig.splitlines() if l.strip() and not l.lstrip().startswith(("//", "#", "*", "/*", "@"))), "")
        if TYPE_RE.search(first):
            missing = [m for m in _methods(f.read_text(errors="replace"), name) if f"{ref}::{m}" not in exported]
            if missing:
                shown = ", ".join(missing[:MAX_NAMES]) + (f", +{len(missing) - MAX_NAMES} more" if len(missing) > MAX_NAMES else "")
                notes.append(f"export `{ref}` is a type but its public methods are not exported ({shown}); "
                             f"children would see the definition and not how to call it. Add `{ref}::<method>`")
    # parents: modules the node imports that no parent (or the node itself) exports
    parent_files: set[str] = set()
    for p in h.parents:
        info = nodes.get(p)
        if info and (spec := root / info["spec"]).is_file():
            ph = parse_header(spec.read_text(errors="replace"))
            parent_files |= {r.partition("::")[0] for r in (ph.exports if ph else [])}
    code = own_spec_text + "\n" + "\n".join((root / f).read_text(errors="replace") for f in own_files if (root / f).is_file())
    repo = _files(root)
    owner = {}
    for n, info in nodes.items():
        if (s := root / info["spec"]).is_file() and (ph := parse_header(s.read_text(errors="replace"))):
            for r in ph.exports:
                owner.setdefault(r.partition("::")[0], n)
    for mod, suffixes in sorted(_imported_modules(code).items()):
        files = [f for f in repo if any(("/" + f).endswith(s) for s in suffixes)]
        if not files or any(f in parent_files or f in own_files for f in files):
            continue
        who = next((owner[f] for f in files if f in owner), None)
        notes.append(f"the node uses module `{mod}` ({files[0]}) that no parent exports: "
                     + (f"add parent `{who}`" if who else "no registered node exports it, so declare the node that owns it"))
    return notes

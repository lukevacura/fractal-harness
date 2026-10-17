"""Node specs as DAG vertices: a spec's front matter declares what the node rests on and what it offers.

    ---
    parents: stats, cov            # nodes whose contracts this one assumes (direct dependencies only)
    public: src/api.rs::Reader, src/testkit/**   # intended surface nothing in the repo uses: graph-check skips it
    refines: NearestK=Nearest      # optional: a new export for an owned decision says how it relates
    after: bench                   # nodes that must come first but whose contracts this one does not use
    implements: D18                # decisions this node must reproduce, taken in full
    see: naive                     # peer nodes: non-binding examples of how a similar node was done
    guarantee: Windowed mean and variance, exact for N.
    condition: Only for windows with V < 2^63; stats flags the rest.
    exports: src/stats.rs::Stats::update, src/stats.rs::Stats
    oracle: equality               # optional: tests compare two engines; adds the degenerate-output mutant
    ---

A child may rely on a parent's *contract*: its `guarantee`, its `condition` (a conditional guarantee
carries its condition with it), and the current signatures of its `exports`. It may not rely on
anything else about the parent, and grandparents are not premises. The contract's digest is what
the mutant ledger watches, so a parent's contract changing stales exactly its children.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .symbols import SymbolError, digest, split_ref, symbol_of

LIST_FIELDS = ("parents", "after", "implements", "see", "exports", "public", "refines", "distinct")
STR_FIELDS = ("guarantee", "condition", "oracle")
CONDITIONAL_WORDS = re.compile(r"\b(only (?:when|if|for)|provided|unless|assuming|as long as|given that)\b", re.I)


@dataclass
class Header:
    parents: list[str] = field(default_factory=list)
    after: list[str] = field(default_factory=list)     # sequencing only: not a premise, no contract watched
    implements: list[str] = field(default_factory=list)
    public: list[str] = field(default_factory=list)    # intended API with no consumer in this repo (graph-check skips it)
    refines: list[str] = field(default_factory=list)   # `New=Existing`: New extends Existing's decision, deliberately
    distinct: list[str] = field(default_factory=list)  # `New=Existing`: New is a separate concept despite the shared decision
    see: list[str] = field(default_factory=list)
    exports: list[str] = field(default_factory=list)
    guarantee: str = ""
    condition: str = ""
    oracle: str = ""          # "equality": the tests compare two implementations; verify-plan then requires DEGENERATE_MUTANT


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
    if both := sorted(set(h.parents) & set(h.after)):
        notes.append(f"{', '.join(both)} listed in both `parents:` (uses) and `after:` (sequencing); keep one")
    if h.guarantee and CONDITIONAL_WORDS.search(h.guarantee) and not h.condition:
        notes.append("the guarantee reads as conditional but declares no `condition:`; a child would not see it")
    if h.exports and not h.guarantee:
        notes.append("exports are declared without a `guarantee:`")
    return notes


BINDING_RE = re.compile(r"\[(binding|advisory)\]", re.I)


def design_points(text: str) -> list[str]:
    """List items under a heading containing "design" in the spec body: the points an implementer
    either must reproduce (`[binding]`) or may override with evidence (`[advisory]`)."""
    out, inside = [], False
    for line in body(text).splitlines():
        if m := re.match(r"^#{1,6}\s+(.*)$", line):
            inside = "design" in m.group(1).lower()
        elif inside and re.match(r"^\s*(?:[-*]|\d+[.)])\s+\S", line):
            out.append(line.strip())
    return out


def spec_lint(text: str) -> list[str]:
    """Design points that do not say whether they bind the implementer."""
    unmarked = [p for p in design_points(text) if not BINDING_RE.search(p)]
    if not unmarked:
        return []
    shown = "; ".join(p[:50] for p in unmarked[:3])
    return [f"{len(unmarked)} design point(s) are not marked `[binding]` or `[advisory]` ({shown}"
            f"{'...' if len(unmarked) > 3 else ''}); an implementer cannot tell which they may override"]


def spec_key(text: str) -> str:
    """What a node's verdicts rest on in its spec: the header fields that change what the code must do
    (parents, implements, exports, guarantee, condition, oracle) and the design points. Other prose and the
    lint-only fields (see, after, public, refines, distinct) are not part of it. A spec with no header is
    its own key."""
    h = parse_header(text)
    if h is None:
        return text
    keyed = {k: (sorted(getattr(h, k)) if isinstance(getattr(h, k), list) else getattr(h, k))
             for k in ("parents", "implements", "exports", "guarantee", "condition", "oracle")}
    return json.dumps(keyed, sort_keys=True) + "\n" + "\n".join(" ".join(p.split()) for p in design_points(text))


def edit_header(text: str, add: dict[str, list[str]] | None = None, remove: dict[str, list[str]] | None = None) -> str:
    """The spec with items added to or removed from list fields of its front matter. A touched field is
    rewritten (wrapped) and keeps the trailing comment of its first line; an absent one is appended."""
    lines = text.splitlines(keepends=True)
    if not lines or not re.match(r"---[ \t]*$", lines[0]):
        raise SymbolError("the spec has no front matter to edit")
    end = next((i for i in range(1, len(lines)) if re.match(r"---[ \t]*$", lines[i])), None)
    if end is None:
        raise SymbolError("the spec's front matter is not closed")
    for field_name in dict.fromkeys([*(add or {}), *(remove or {})]):
        start = next((i for i in range(1, end) if re.match(rf"{field_name}:", lines[i])), None)
        items: list[str] = []
        comment, stop = "", None
        if start is not None:
            stop = start + 1
            while stop < end and lines[stop][:1] in (" ", "\t"):
                stop += 1
            if c := re.search(r"\s+(#.*)$", lines[start].rstrip("\n")):
                comment = c.group(1)
            raw = " ".join(re.sub(r"\s+#.*$", "", l.rstrip()) for l in lines[start:stop])
            items = [x for x in re.split(r"[,\s]+", raw.partition(":")[2]) if x]
        items = [x for x in items if x not in (remove or {}).get(field_name, [])]
        items += [x for x in (add or {}).get(field_name, []) if x not in items]
        out, cur = [], f"{field_name}:"
        for k, item in enumerate(items):
            piece = item + ("," if k < len(items) - 1 else "")
            if len(cur) + len(piece) + 1 > 100 and cur.strip() != f"{field_name}:":
                out.append(cur.rstrip() + "\n")
                cur = "  "
            cur += (" " if not cur.endswith(" ") else "") + piece
        out.append(cur + (f"   {comment}" if comment else "") + "\n")
        if not items:
            out = []
        if start is None:
            lines[end:end] = out
            end += len(out)
        else:
            lines[start:stop] = out
            end += len(out) - (stop - start)
    return "".join(lines)


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


DEGENERATE_MUTANT = "every output entry is NONE/empty"


def _symbol_keys(refs: list[str]) -> set[str]:
    """Top-level symbols of export refs: `f.rs::T::m` and `f.rs::T` are both `f.rs::T`."""
    return {r.partition("::")[0] + "::" + r.partition("::")[2].split("::")[0] for r in refs if "::" in r}


def _forms(refs: list[str]) -> dict[str, str]:
    """{every spelling a `refines:`/`distinct:` side may use: top-level key} for export refs. A ref
    `src/n.rs::Nearest::k` is spelled `src/n.rs::Nearest::k`, `src/n.rs::Nearest`, `Nearest::k` or `Nearest`."""
    out: dict[str, str] = {}
    for r in refs:
        if "::" not in r:
            continue
        path, _, rest = r.partition("::")
        key = f"{path}::{rest.split('::')[0]}"
        for form in (r, key, rest, rest.split("::")[0]):
            out.setdefault(form, key)
    return out


def ownership_lint(h: Header, node: str, nodes: dict[str, dict], root: Path,
                   concepts: dict[str, list[str]] | None = None) -> list[str]:
    """A decision already implemented by another node's exported symbol should be extended, not
    shadowed: warn when this node exports a *new* top-level symbol for an ID others already own.
    A symbol counts as already owned by name, wherever it lives now (a moved owner is still the owner).
    Ownership belongs to a concept, not to every decision: only IDs a concept claim names (`concepts`,
    {decision ID: [concept names]}, see concepts.py) are checked. A cross-cutting decision that eight nodes
    implement is not a smell, so without a concept claim the ID is not checked at all."""
    notes: list[str] = []
    mine = _symbol_keys(h.exports)
    mine_forms, other_forms = _forms(h.exports), {}
    for other, info in nodes.items():
        if other != node and (s := root / info["spec"]).is_file() and (oh := parse_header(s.read_text(errors="replace"))):
            other_forms.update(_forms(oh.exports))
    declared: set[str] = set()
    for pair in h.refines + h.distinct:
        left, _, right = pair.partition("=")
        if not right or left not in mine_forms:
            notes.append(f"`{pair}` must be `New=Existing`: New is one of this node's exports, Existing one exported "
                         f"by another node; each as `Name`, `file::Name` or `Name::method` "
                         f"(this node exports: {', '.join(sorted(mine)) or 'nothing'})")
        elif right not in other_forms:
            notes.append(f"`{pair}`: `{right}` is not exported by any other registered node "
                         f"(known: {', '.join(sorted(set(other_forms.values()))[:5]) or 'none'}); check the spelling")
        else:
            declared.add(mine_forms[left])
    mine = {k for k in mine if k not in declared}
    for decision in h.implements:
        if decision not in (concepts or {}):
            continue
        for other, info in sorted(nodes.items()):
            if other == node or not (s := root / info["spec"]).is_file():
                continue
            oh = parse_header(s.read_text(errors="replace"))
            if not oh or decision not in oh.implements:
                continue
            theirs = _symbol_keys(oh.exports)
            owned_names = {k.rpartition("::")[2] for k in theirs}
            new = sorted(k for k in mine - theirs if k.rpartition("::")[2] not in owned_names)
            if theirs and new:
                beside = sorted({t for t in theirs for k in new if t.partition("::")[0] == k.partition("::")[0]})
                notes.append(f"{decision} ({', '.join(concepts[decision])}) is already owned by {', '.join(sorted(theirs))} (node {other}); this node "
                             f"exports new {', '.join(new)}. Extend the existing symbol rather than adding a parallel one"
                             + (f" (if this node modifies {', '.join(beside)}, export that or its methods instead)" if beside else "")
                             + "? If deliberate, declare `refines: New=Existing` or `distinct: New=Existing`.")
    return notes


def repo_lint(root: Path, h: Header, nodes: dict[str, dict], own_spec_text: str, node: str = "",
              fixes: dict | None = None, concepts: dict[str, list[str]] | None = None) -> list[str]:
    """Header problems that are visible in the repository and would starve a node's context bundle,
    each with the fix. When `fixes` is given, the mechanical ones are also collected in it:
    {"add": {field: [items]}, "remove": {field: [items]}}."""
    if fixes is not None:
        fixes.setdefault("add", {})
        fixes.setdefault("remove", {})
    notes: list[str] = ownership_lint(h, node, nodes, root, concepts) if node else []
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
                if fixes is not None:
                    fixes["add"].setdefault("exports", []).extend(f"{ref}::{m}" for m in missing)
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
        if fixes is not None and who and who != node:
            fixes["add"].setdefault("parents", []).append(who)
            if who in h.after:
                fixes["remove"].setdefault("after", []).append(who)
        if who in h.after:
            notes.append(f"the node uses module `{mod}` ({files[0]}) but declares {who} only `after:` (sequencing, "
                         f"not a dependency); move it to `parents:`")
            continue
        notes.append(f"the node uses module `{mod}` ({files[0]}) that no parent exports: "
                     + (f"add parent `{who}`" if who else "no registered node exports it, so declare the node that owns it"))
    return notes

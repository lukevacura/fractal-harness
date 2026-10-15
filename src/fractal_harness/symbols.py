"""Extract a markdown section or a code item's interface from a file, so a claim can carry its
exact text and a probe can notice when it changes.

Both extractors are deliberately language-light: a heading scan for markdown, and a keyword /
brace scan for code (Rust, Python, TS/JS, Go, C-likes). Where they cannot find an item uniquely
they say so rather than guess.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

MAX_SECTION_CHARS = 2000
MAX_BODY_LINES = 40
TYPE_KW = ("struct", "enum", "trait", "interface", "impl", "protocol", "class", "object")
DEF_KW = ("fn", "def", "func", "function", "type", "const", "static", "mod", "val", "var", "let") + TYPE_KW
NOT_DEFS = ("return", "new", "throw", "else", "if", "while", "for", "await", "yield", "assert")
DOC_PREFIXES = ("///", "//!", "//", "#", "*", "/**", "*/", "@")


class SymbolError(ValueError):
    pass


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def split_ref(ref: str) -> tuple[str, str]:
    """'path/to/file::Item' -> (path, Item)."""
    if "::" not in ref:
        raise SymbolError(f"expected <path>::<name>, got {ref!r}")
    path, _, name = ref.partition("::")
    if not path or not name:
        raise SymbolError(f"expected <path>::<name>, got {ref!r}")
    return path, name


def _read(root: Path, path: str) -> str:
    f = root / path
    if not f.is_file():
        raise SymbolError(f"no such file: {path}")
    return f.read_text(errors="replace")


# --- markdown sections --------------------------------------------------------------------

def section(text: str, heading: str) -> str:
    """The section whose heading is `heading` (exactly, or as a leading word: 'D16' finds
    '### D16 — Float pinning'), from its heading line to the next heading of equal or higher level."""
    lines = text.splitlines()
    heads = [(i, len(m.group(1)), m.group(2).strip()) for i, l in enumerate(lines)
             if (m := re.match(r"^(#{1,6})\s+(.*?)\s*#*\s*$", l))]
    want = heading.strip().lstrip("#").strip()
    hits = [h for h in heads if h[2] == want] or [
        h for h in heads if re.match(rf"{re.escape(want)}(?![\w-])", h[2])]
    if not hits:
        raise SymbolError(f"no heading {heading!r}")
    if len(hits) > 1:   # 'D17' also prefixes its own sub-heading 'D17 detail': prefer the shallowest
        top = min(h[1] for h in hits)
        hits = [h for h in hits if h[1] == top]
    if len(hits) > 1:
        raise SymbolError(f"heading {heading!r} is ambiguous: lines " + ", ".join(str(h[0] + 1) for h in hits))
    start, level, _ = hits[0]
    end = next((i for i, lv, _ in heads if i > start and lv <= level), len(lines))
    return "\n".join(lines[start:end]).strip()


def section_of(root: Path, ref: str) -> str:
    path, heading = split_ref(ref)
    return section(_read(root, path), heading)


# --- code items ---------------------------------------------------------------------------

def _is_def(line: str, name: str) -> str | None:
    """The declaring keyword if `line` declares `name`, '' for a C-style typed declaration."""
    s = line.strip()
    if not re.search(rf"\b{re.escape(name)}\b", s) or s.startswith(DOC_PREFIXES) or s.startswith(NOT_DEFS):
        return None
    if m := re.search(rf"\b({'|'.join(DEF_KW)})\s+(?:<[^>]*>\s*)?(?:[\w.]+\.)?{re.escape(name)}\b", s):
        return m.group(1)
    if re.match(rf"^[\w<>\[\]:&*, .]+?\s+\**{re.escape(name)}\s*\(", s) and "=" not in s.split(name)[0]:
        return ""
    return None


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def find(text: str, name: str) -> tuple[int, str]:
    """(0-based line, text) of the interface of `name` ('Item' or 'Qualifier::Item'): preceding doc
    comments, then the signature (plus the body of a struct/enum/trait/class-like item)."""
    qual, _, item = name.rpartition("::")
    lines = text.splitlines()
    hits = [(i, kw) for i, l in enumerate(lines) if (kw := _is_def(l, item)) is not None]
    if qual:
        q = qual.rpartition("::")[2]
        scoped = []
        for i, kw in hits:
            for j in range(i - 1, -1, -1):
                if _indent(lines[j]) < _indent(lines[i]) and lines[j].strip() and re.search(
                        rf"\b({'|'.join(TYPE_KW)})\b.*\b{re.escape(q)}\b", lines[j]):
                    scoped.append((i, kw))
                    break
        hits = scoped
    if len(hits) > 1 and any(kw != "impl" for _, kw in hits):
        hits = [h for h in hits if h[1] != "impl"]     # the type itself, not its impl blocks
    if not hits:
        raise SymbolError(f"no definition of {name!r} found")
    if len(hits) > 1:
        raise SymbolError(f"{name!r} is ambiguous (lines {', '.join(str(i + 1) for i, _ in hits)}); "
                          "qualify it as Type::name")
    i, kw = hits[0]
    start = i
    while start > 0 and lines[start - 1].strip().startswith(DOC_PREFIXES):
        start -= 1
    end, depth = i, 0
    for j in range(i, min(len(lines), i + MAX_BODY_LINES)):
        l = lines[j]
        depth += l.count("(") + l.count("[") - l.count(")") - l.count("]")
        end = j
        s = l.rstrip()
        if depth <= 0 and (s.endswith((";", ":", "=>", "=")) or s.endswith("{") or "{" in s):
            break
    if kw in TYPE_KW and "{" in lines[end] and "}" not in lines[end].split("{", 1)[1]:
        braces = 0
        for j in range(end, min(len(lines), end + MAX_BODY_LINES)):
            braces += lines[j].count("{") - lines[j].count("}")
            end = j
            if braces <= 0:
                break
    elif kw == "def" and end + 1 < len(lines) and lines[end + 1].strip().startswith(('"""', "'''")):
        quote = lines[end + 1].strip()[:3]
        j = end + 1
        if lines[j].strip().count(quote) < 2:
            j = next((k for k in range(j + 1, min(len(lines), j + 12)) if quote in lines[k]), j)
        end = j
    block = lines[start:end + 1]
    indent = min((_indent(l) for l in block if l.strip()), default=0)
    return i, "\n".join(l[indent:].rstrip() for l in block)


def symbol_of(root: Path, ref: str) -> str:
    path, name = split_ref(ref)
    return find(_read(root, path), name)[1]

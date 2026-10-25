"""Signatures of what a source file defines: a neighbour's interface as a few lines of text.

An agent that has to rely on a file it is not changing usually opens it, and pays for its whole body to
learn a handful of signatures. `interface_lines` gives it those signatures instead: of the public names the
file defines, the ones the caller's files actually mention, most-used first, each as one compact line.

Every line is read from the current source at the moment it is asked for, so it cannot be stale. Two
limits to keep in view, both heuristics inherited from the word-match index (refindex.py):

  * "used" is a word match. A name a region's files only define for themselves, or a generic name, can be
    listed as used; and a use through an alias, a re-export or a string can be missed. Callers should treat
    a listed signature as accurate and the list as possibly incomplete.
  * extraction is language-light: it reads the declaring line (and its continuation) that the definition
    patterns match, which covers Python and Dart declarations (functions, methods, getters, fields, types).
    A name declared on more than one line is left out, never guessed.

Nothing here knows about the model or the store: it takes text and returns text.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Iterable

# Definitions, not uses: types; functions/methods with a declared return type (Dart, TS) or
# `def` (Python); fields and top-level variables declared at class/module level (indent <= 2).
DEF_RXS = [
    re.compile(r"^\s*(?:abstract\s+|sealed\s+|final\s+|base\s+|interface\s+)*(?:class|enum|mixin|extension|typedef)\s+(\w+)", re.M),
    re.compile(r"^\s*(?:static\s+|external\s+)?(?:Future|Stream|void|bool|int|double|String|num|dynamic|List|Map|Set|Iterable|[A-Z]\w*)(?:<[^>()]*>)?\??\s+(?:get\s+)?(\w+)\s*(?:\(|=>|\{)", re.M),
    re.compile(r"^\s*(?:async\s+)?def\s+(\w+)", re.M),
    re.compile(r"^ {0,2}(?:static\s+|late\s+)*(?:final|const|var)\s+(?:[\w<>?,\s]+\s+)?(\w+)\s*[=;]", re.M),
]
TOKEN = re.compile(r"[A-Za-z_]\w*")
MIN_NAME = 3
MAX_SIGNATURE_CHARS = 140
MAX_PER_FILE = 5
MAX_SIGNATURE_LINES = 8


def usage(texts: Iterable[str]) -> Counter:
    """How often each identifier-like word appears across `texts`."""
    used: Counter = Counter()
    for text in texts:
        used.update(TOKEN.findall(text))
    return used


def _declarations(text: str) -> dict[str, set[int]]:
    """name -> the start offsets of the lines that declare it."""
    found: dict[str, set[int]] = {}
    for rx in DEF_RXS:
        for m in rx.finditer(text):
            found.setdefault(m.group(1), set()).add(text.rfind("\n", 0, m.start(0)) + 1)
    return found


def defined_names(text: str) -> list[str]:
    """Names `text` defines, once each, in the order they first appear."""
    first: dict[str, int] = {}
    for rx in DEF_RXS:
        for m in rx.finditer(text):
            first[m.group(1)] = min(first.get(m.group(1), m.start(1)), m.start(1))
    return [name for name, _ in sorted(first.items(), key=lambda kv: (kv[1], kv[0]))]


def _cut(declaration: str) -> str:
    """The declaration up to where its body or value begins: the first `{` outside parentheses (a brace inside
    them is a named-parameter list), an expression body `=>`, a Python `:`, or a `;`."""
    python = declaration.lstrip().startswith(("def ", "async def ", "class "))
    depth = 0
    for i, ch in enumerate(declaration):
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        elif depth == 0 and (ch in "{;" or (ch == ":" and python) or declaration.startswith("=>", i)):
            return declaration[:i]
    return declaration


def signature(text: str, name: str) -> str | None:
    """`name`'s declaration as one line, without its body. None when `name` is not declared in `text`, or is
    declared on more than one line (overloads, or the same name in two classes): it is never guessed."""
    starts = _declarations(text).get(name, set())
    if len(starts) != 1:
        return None
    lines = text[next(iter(starts)):].splitlines()[:MAX_SIGNATURE_LINES]
    parts, depth = [], 0
    for line in lines:
        parts.append(line.strip())
        depth += sum(line.count(c) for c in "([") - sum(line.count(c) for c in ")]")
        if depth <= 0 and line.rstrip().endswith((";", ":", "=>", "{")):
            break
    one = re.sub(r"\s+", " ", _cut(" ".join(parts))).strip()
    if not one:
        return None
    return one if len(one) <= MAX_SIGNATURE_CHARS else one[:MAX_SIGNATURE_CHARS - 3].rstrip() + "..."


def interface_lines(text: str, used: Counter, limit: int = MAX_PER_FILE) -> list[str]:
    """Signatures of the public names `text` defines that `used` mentions, most-used first (file order
    breaks ties), at most `limit`. Private names (a leading underscore) and short ones are not interface."""
    names = [n for n in defined_names(text) if used.get(n) and not n.startswith("_") and len(n) >= MIN_NAME]
    names.sort(key=lambda n: -used[n])                     # stable: ties keep file order
    out: list[str] = []
    for name in names:
        if len(out) == limit:
            break
        sig = signature(text, name)
        if sig:
            out.append(sig)
    return out

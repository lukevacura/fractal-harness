"""Anchors and motion estimation: cheap, local classification of how a claim's code changed.

Borrowed from video coding. When a probe passes, the lines it matched (plus a little context)
are stored as the claim's *anchors*, its keyframe. After an edit, each anchor is located in
the current file (motion estimation) and the leftover difference is measured (the residual):

  clean      probe passes, anchors unchanged or only moved            -> free: rebase anchors
  suspect    probe passes, but anchored code changed substantially    -> the probe may no
             longer test the claim; needs a (cheap) delta repair
  delta      probe fails, anchors found with a small residual         -> cheap repair: the
             repairer sees only claim + probe + residual
  scene_cut  probe fails, anchors gone or heavily rewritten           -> full re-verification
  rewrite    probe passes and anchors are intact, but a large share of a file the claim
             reads was rewritten elsewhere (whole-frame scene cut)    -> full re-verification:
             behavior can change far from the lines a probe watches

No model call and no version control: works on uncommitted edits and any VCS.
"""

from __future__ import annotations

import difflib
from pathlib import Path

CONTEXT = 2          # lines of context kept around each matched line
MAX_ANCHORS = 5      # anchors stored per claim
DELTA_MIN_RATIO = 0.6    # a failed probe whose anchors all match at least this well is a delta
SUSPECT_BELOW = 0.5      # a passing probe with an anchor matching worse than this is suspect
MAX_DIFF_LINES = 40
REWRITE_FRACTION = 0.25  # share of a file's lines changed that counts as a whole-file scene cut...
REWRITE_MIN_LINES = 20   # ...and at least this many lines, so small files aren't flagged by tiny edits
MAX_SNAPSHOT_LINES = 20_000


def _lines(root: Path, rel: str) -> list[str] | None:
    try:
        return (root / rel).read_text(errors="replace").splitlines()
    except OSError:
        return None


def line_hashes(root: Path, rel: str) -> list[str] | None:
    """Short per-line hashes of a file (whitespace-trimmed), for measuring how much it changed."""
    import hashlib
    lines = _lines(root, rel)
    if lines is None or len(lines) > MAX_SNAPSHOT_LINES:
        return None
    return [hashlib.blake2b(l.strip().encode(), digest_size=4).hexdigest() for l in lines]


def changed_lines(old: list[str], new: list[str]) -> tuple[int, float]:
    """(lines not preserved, share of the larger version) between two line-hash lists."""
    if not old and not new:
        return 0, 0.0
    sm = difflib.SequenceMatcher(None, old, new, autojunk=False)
    kept = sum(b.size for b in sm.get_matching_blocks())
    total = max(len(old), len(new))
    return total - kept, (total - kept) / total


def is_rewrite(old: list[str], new: list[str]) -> tuple[bool, float]:
    n, frac = changed_lines(old, new)
    return n >= REWRITE_MIN_LINES and frac >= REWRITE_FRACTION, frac


def capture(root: Path, matches: list[tuple[str, int]]) -> list[dict]:
    """Anchors for a probe's matches: [{file, line, block}], first MAX_ANCHORS distinct."""
    anchors, seen = [], set()
    cache: dict[str, list[str] | None] = {}
    for rel, line in matches:
        if (rel, line) in seen:
            continue
        seen.add((rel, line))
        lines = cache.setdefault(rel, _lines(root, rel))
        if lines is None or line >= len(lines):
            continue
        lo, hi = max(0, line - CONTEXT), min(len(lines), line + CONTEXT + 1)
        anchors.append({"file": rel, "line": line, "offset": line - lo, "block": lines[lo:hi]})
        if len(anchors) >= MAX_ANCHORS:
            break
    return anchors


def locate(block: list[str], lines: list[str], hint: int = 0) -> tuple[int, float]:
    """Best (start, similarity) for `block` in `lines`. Exact match first, then a scored scan."""
    n = len(block)
    if not n or not lines:
        return -1, 0.0
    if lines[hint:hint + n] == block:
        return hint, 1.0
    for i in range(0, len(lines) - n + 1):
        if lines[i] == block[0] and lines[i:i + n] == block:
            return i, 1.0
    target = "\n".join(block)
    best, best_ratio = -1, 0.0
    sm = difflib.SequenceMatcher(autojunk=False)
    sm.set_seq2(target)
    for i in range(0, max(1, len(lines) - n + 1)):
        sm.set_seq1("\n".join(lines[i:i + n]))
        if sm.real_quick_ratio() <= best_ratio or sm.quick_ratio() <= best_ratio:
            continue
        r = sm.ratio()
        if r > best_ratio:
            best, best_ratio = i, r
    return best, best_ratio


def compare(root: Path, anchors: list[dict]) -> list[dict]:
    """Motion estimation for each stored anchor against the current source."""
    out = []
    cache: dict[str, list[str] | None] = {}
    for a in anchors:
        lines = cache.setdefault(a["file"], _lines(root, a["file"]))
        start_hint = a["line"] - a.get("offset", 0)
        if lines is None:
            out.append({"file": a["file"], "old_line": a["line"] + 1, "new_line": None, "ratio": 0.0,
                        "diff": "(file removed)"})
            continue
        start, ratio = locate(a["block"], lines, start_hint)
        new_block = lines[start:start + len(a["block"])] if start >= 0 else []
        diff = "" if ratio == 1.0 else "\n".join(list(difflib.unified_diff(
            a["block"], new_block, f"{a['file']} (verified)", f"{a['file']} (now)",
            lineterm="", n=CONTEXT))[:MAX_DIFF_LINES])
        out.append({"file": a["file"], "old_line": a["line"] + 1,
                    "new_line": start + a.get("offset", 0) + 1 if start >= 0 else None,
                    "ratio": round(ratio, 3), "diff": diff})
    return out


def classify(passed: bool, comparisons: list[dict]) -> str:
    """clean | moved | suspect | delta | scene_cut (see module doc)."""
    ratios = [c["ratio"] for c in comparisons]
    if passed:
        if ratios and min(ratios) < SUSPECT_BELOW:
            return "suspect"
        moved = any(c["new_line"] != c["old_line"] for c in comparisons)
        return "moved" if moved else "clean"
    if ratios and min(ratios) >= DELTA_MIN_RATIO:
        return "delta"
    return "scene_cut"

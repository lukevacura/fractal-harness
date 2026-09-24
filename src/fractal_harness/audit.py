"""`fractal audit`: deterministic mutation testing of probes. No model call, no file writes.

A probe is only worth something if it fails when the claim becomes false. For each claim
with a grep probe, the audit mutates the code *in memory* and re-runs the probe:

  removal     delete the lines the probe matched          -> the probe must fail
  injection   (absent probes) add a line that violates it  -> the probe must fail
  values      change each number the claim states, where it appears in matched lines
              -> the probe should fail; if not, the claim states a value its probe never checks
  identifiers rename each identifier the claim mentions (in the probe's files)
              -> the probe should fail; if not, the claim mentions something its probe never checks

Verdicts: ok | partial (claim says more than the probe checks) | weak (the probe survives
removing what it matched, or survives an injected violation) | n/a (command probes, no probe).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from . import probes
from .cache import ClaimCache
from .store import Edge

# Code identifiers in claim text: `backticked`, camelCase (lower->Upper->lower), or snake_case.
IDENT_RE = re.compile(r"`([^`\s]{3,})`|\b([A-Za-z_]*[a-z][A-Z][a-z][A-Za-z0-9_]*|[A-Za-z0-9]+_[A-Za-z0-9_]+)\b")
NUMBER_RE = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)(?![\w.])")


@dataclass
class Audit:
    id: str
    claim: str
    verdict: str
    findings: list[str] = field(default_factory=list)
    coverage: tuple[int, int] = (0, 0)  # (mentioned identifiers the probe depends on, total checked)

    def to_dict(self) -> dict:
        return {"id": self.id, "verdict": self.verdict, "claim": self.claim, "findings": self.findings,
                "identifier_coverage": list(self.coverage)}


def _grep_leaves(probe: dict) -> list[dict]:
    if probe.get("type") == "all":
        return [leaf for sub in probe["probes"] for leaf in _grep_leaves(sub)]
    return [probe] if probe.get("type") == "grep" else []


def _files(root: Path, probe: dict) -> dict[str, str]:
    out = {}
    for leaf in _grep_leaves(probe):
        for glob in leaf["paths"]:
            for p in root.glob(glob):
                if p.is_file():
                    try:
                        out[p.relative_to(root).as_posix()] = p.read_text(errors="replace")
                    except OSError:
                        pass
    return out


def _passes(probe: dict, root: Path, overlay: dict[str, str]) -> bool:
    return probes.run(probe, root, overlay).passed


def _identifiers(claim: str, reads: list[str]) -> list[str]:
    """Identifiers a claim mentions, excluding file/dir names (paths aren't code to check)."""
    path_parts = {Path(part).stem for r in reads for part in r.split("/")}
    path_parts |= {Path(w).stem for w in re.findall(r"[\w./-]+\.\w{1,5}\b", claim)}
    seen: dict[str, None] = {}
    for m in IDENT_RE.finditer(claim):
        tok = (m.group(1) or m.group(2)).strip("`().,;:")
        tok = re.sub(r"\(.*$", "", tok)            # `foo()` -> foo
        tok = tok.rsplit("/", 1)[-1]                # paths: keep the file name
        if re.fullmatch(r"[A-Za-z_]\w*", tok) and len(tok) >= 3 and tok not in path_parts:
            seen.setdefault(tok)
    return list(seen)


EXCLUDED = "<excluded>"  # a line matched the pattern but the probe's exclude filtered it out


def _violating_line(pattern: str, exclude: str | None, word: str | None = None) -> str | None:
    """A line that matches `pattern` (and not `exclude`), built by un-regexing it; None if unsure,
    EXCLUDED if every candidate that matched was filtered out by `exclude`."""
    cands = []
    if word:
        cands += [f"var injected = {word};", f"{word}()", word]
    s = pattern
    s = re.sub(r"\\[bB]|\^|\$", "", s)
    s = re.sub(r"\((?:\?:)?([^()|]*)\|[^()]*\)", r"\1", s)      # (A|B) -> A
    s = re.sub(r"\\s[*+?]?|\s[*+]", " ", s)
    s = re.sub(r"\.\*\??|\.\+\??", " x ", s)
    s = re.sub(r"\\w[*+]?", "x", s).replace("\\d+", "1").replace("\\d", "1")
    s = re.sub(r"\\(.)", r"\1", s).replace("(", "").replace(")", "") if "|" not in s else s
    cands += [s.strip(), s.strip() + ";"]
    blocked = False
    for c in cands:
        try:
            if re.search(pattern, c):
                if exclude and re.search(exclude, c):
                    blocked = True
                    continue
                return c
        except re.error:
            return None
    return EXCLUDED if blocked else None


def _removal(e: Edge, root: Path, files: dict[str, str]) -> str | None:
    """None if removing every matched line makes the probe fail (good), else a finding."""
    result = probes.run(e.probe, root)
    if not result.matches:
        return None
    by_file: dict[str, set[int]] = {}
    for rel, line in result.matches:
        by_file.setdefault(rel, set()).add(line)
    overlay = {rel: "\n".join(l for i, l in enumerate(files[rel].splitlines()) if i not in lines)
               for rel, lines in by_file.items() if rel in files}
    if _passes(e.probe, root, overlay):
        return "probe still passes with every matched line deleted: it does not depend on the code it matched"
    return None


def _inject(leaf: dict, full: dict, root: Path, files: dict[str, str], word: str | None = None) -> bool | None:
    """For an `absent` grep: does the probe fail once a violating line is injected?
    None when no violating line can be synthesized (not auditable this way)."""
    line = _violating_line(leaf["pattern"], leaf.get("exclude"), word)
    if line == EXCLUDED:
        return False  # the exclude swallows even plain code lines: the probe can never fail
    target = next((f for f in sorted(files) if any(Path(f).match(g) for g in leaf["paths"])), None)
    if line is None or target is None:
        return None
    return not _passes(full, root, {target: files[target] + "\n" + line + "\n"})


def audit_edge(e: Edge, root: Path) -> Audit:
    if not e.probe or not _grep_leaves(e.probe):
        return Audit(e.id, e.post, "n/a", ["no grep probe to mutate"])
    files = _files(root, e.probe)
    if not probes.run(e.probe, root).passed:
        return Audit(e.id, e.post, "n/a", ["probe does not pass now; repair before auditing"])

    weak, partial = [], []
    if (f := _removal(e, root, files)):
        weak.append(f)
    absent = [leaf for leaf in _grep_leaves(e.probe) if leaf.get("expect") == "absent"]
    for leaf in absent:
        if _inject(leaf, e.probe, root, files) is False:
            weak.append(f"absent-probe still passes after injecting a line matching /{leaf['pattern']}/")

    matched_text = "\n".join(
        files[rel].splitlines()[line] for rel, line in probes.run(e.probe, root).matches
        if rel in files and line < len(files[rel].splitlines()))
    for num in dict.fromkeys(NUMBER_RE.findall(e.post)):
        if not re.search(rf"(?<![\w.]){re.escape(num)}(?![\w.])", matched_text):
            continue
        bumped = str(float(num) + 1) if "." in num else str(int(num) + 1)
        overlay = {}
        for rel, line in probes.run(e.probe, root).matches:
            lines = overlay.get(rel, files.get(rel, "")).splitlines()
            if line < len(lines):
                lines[line] = re.sub(rf"(?<![\w.]){re.escape(num)}(?![\w.])", bumped, lines[line])
                overlay[rel] = "\n".join(lines)
        if overlay and _passes(e.probe, root, overlay):
            partial.append(f"value {num} is stated in the claim but the probe doesn't check it")

    idents = _identifiers(e.post, e.reads)
    checked = 0
    for ident in idents:
        # Forbidden names (an absent rule mentions them): injecting one must fail the probe.
        forbidden = [leaf for leaf in absent if re.search(leaf["pattern"], ident)]
        if forbidden:
            ok = all(_inject(leaf, e.probe, root, files, ident) is not False for leaf in forbidden)
        else:
            pat = re.compile(rf"\b{re.escape(ident)}\b")
            overlay = {rel: pat.sub(ident + "_MUTATED", t) for rel, t in files.items() if pat.search(t)}
            ok = not overlay or not _passes(e.probe, root, overlay)
            if not overlay:
                continue  # not in the probed files at all: context, not a checkable detail
        if ok:
            checked += 1
        else:
            partial.append(f"`{ident}` is mentioned in the claim but the probe doesn't depend on it")

    verdict = "weak" if weak else "partial" if partial else "ok"
    a = Audit(e.id, e.post, verdict, weak + partial)
    a.coverage = (checked, checked + sum(f.startswith("`") for f in partial))
    return a


def audit(root: Path, ids: list[str] | None = None) -> list[Audit]:
    cache = ClaimCache(root)
    try:
        cache.update()
        edges = [cache.get(i) for i in ids] if ids else cache.store.all()
        results = [audit_edge(e, cache.root) for e in edges if e is not None]
        cache.store.log("audit", None, **{v: sum(a.verdict == v for a in results)
                                          for v in ("ok", "partial", "weak", "n/a")})
        return results
    finally:
        cache.close()

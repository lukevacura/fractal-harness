"""Global loss: does a change let through states that no remaining rule forbids?

Theory (2026-11-21): information is what the rule set rules out. A change that drops, demotes or narrows a rule
loses information only for the states it lets through that no other in-effect rule still forbids. If other rules
still forbid all of them, the change is ABSORBED: nothing is lost, and the acting layer's authority suffices. If
not, it is a GLOBAL LOSS, and only the root (a human, arbitration) may make it. A root-held rule is a ruling kept as
precedent, so changing one always needs the human even when absorbed; the human is told it loses no information.

What is proven, and how (SOUND; anything else is "not provable" and counts as a loss):
  - each probe leaf is split into ATOMS (a leaf of an enforced `all` probe holds on its own, so it can absorb).
    command, test, section and symbol probes have none: they are not provable;
  - COUNTED atoms: a grep (matching lines), an only_in (outside its files: none; inside: at least `min`), a paths
    probe (files; or files not named /unless_name/; or files sharing a /unique/ key). Each count only grows as the
    file set grows. Atoms compare when their key is equal (same probe family, same pattern / name regex); line
    exclusions equal (an absorber with no exclusion also covers an upper bound). An UPPER bound (absent, max k) is
    absorbed by an atom over a superset of the files with a bound at least as tight; a LOWER bound (present, min n)
    by one over a subset of the files with a bound at least as high (count over fewer files <= count over more);
  - GATE atoms (commit time): staged `forbid` and message `forbid`/`require` are absorbed by the same regex; a
    staged `only` allow-list by a narrower one (every absorber glob inside a lost one); `frozen` by wider globs;
  - file sets are compared as GLOB PATTERNS, never as the files in the tree now, so a file added later cannot fall
    through: `glob_covers` is a syntactic containment test that answers False when unsure. Skips: the absorber
    may skip only what the lost rule already skipped (for an upper bound), or must skip at least that (lower bound:
    compared syntactically, so only identical skips are accepted);
  - narrowing an absent grep or paths rule (exclude_paths / `!` paths added) loses only the cut-out files: an absent
    rule over them absorbs it. Any other probe change must have every atom of the old probe absorbed, and the NEW
    probe counts as an absorber: tightening a bound or widening the files loses nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache

from .regions import glob_match, is_glob

INF = float("inf")


@dataclass
class Absorption:
    absorbed: bool
    by: list[str] = field(default_factory=list)     # ids of the rules that absorb it (empty when not absorbed)
    why: str = ""


# --- glob containment -------------------------------------------------------------------------------------------

def glob_covers(a: str, b: str) -> bool:
    """True only if every path matching glob `b` matches glob `a` (syntactic, segment by segment; False when unsure).
    `**` as a whole segment is any number of directories (Path.glob); a segment that merely contains `**` crosses `/`
    and is only compared for equality. Never claimed (unless equal):
      a leading or trailing `/` (`/src/**`, `src/**/`): a probe's walk matches nothing for it, the pattern does;
      a bracket class in `b` (`[^a]`, `[+-0]`): it can match `/`, so a segment of `b` can span several."""
    if a == b:
        return True
    if a.startswith("/") or b.startswith("/") or a.endswith("/") or b.endswith("/") or "[" in b:
        return False
    if not is_glob(b):
        return glob_match(a, b)
    return _covers(tuple(a.split("/")), tuple(b.split("/")))


@lru_cache(maxsize=4096)
def _covers(A: tuple[str, ...], B: tuple[str, ...]) -> bool:
    if not A:
        return not B
    if A[0] == "**":
        if len(A) == 1:
            return bool(B)          # a trailing `**` (`p/**`) matches one or more segments below, never `p` itself
        return _covers(A[1:], B) or (bool(B) and _covers(A, B[1:]))
    if not B or B[0] == "**":
        return False
    return _seg_covers(A[0], B[0]) and _covers(A[1:], B[1:])


def _seg_covers(a: str, b: str) -> bool:
    """Every name matching segment `b` matches segment `a`."""
    if a == b:
        return True
    if "**" in a or "**" in b:
        return False
    if not is_glob(b):
        return glob_match(a, b)
    if a == "*":
        return True
    if a.startswith("*") and not is_glob(a[1:]):            # "*.py": b's literal tail must end with ".py"
        tail = b[max(b.rfind(c) for c in "*?]") + 1:]
        return tail.endswith(a[1:])
    if a.endswith("*") and not is_glob(a[:-1]):             # "test_*": b's literal head must start with "test_"
        head = b[:min((b.find(c) for c in "*?[" if c in b), default=len(b))]
        return head.startswith(a[:-1])
    return False


def as_region(globs) -> list[str]:
    """Patterns a probe reads with `regions.matches` (skips, only_in files, commit gates) as glob patterns: there a
    literal path is that file OR everything under it (`src/vendor` skips `src/vendor/x.py`), so it is both."""
    out: list[str] = []
    for g in globs:
        g = g.rstrip("/")
        out += [g] if is_glob(g) else [g, f"{g}/**"]
    return out


def _split(leaf: dict) -> tuple[list[str], list[str]]:
    """(includes, skips) as glob patterns. An include is read by the walk (`hashing.expand_glob`): a literal is one
    file. A skip is read by `matches`: a literal is a file or a directory (as_region)."""
    include = [p for p in leaf["paths"] if not p.startswith("!")]
    skip = as_region([*leaf.get("exclude_paths", []), *(p[1:] for p in leaf["paths"] if p.startswith("!"))])
    return include, skip


def _bounds(leaf: dict) -> tuple[float, float]:
    from .probes import _expectation
    op, n = _expectation(leaf.get("expect", "present"))
    return {"count": (n, n), "min": (n, INF), "max": (0, n)}[op]


# --- atoms ------------------------------------------------------------------------------------------------------

COUNTED = "counted"


def _lines_key(leaf: dict):
    """Which lines of the selected files a grep/only_in counts, as one comparable value: the `exclude` regex, and
    with `skip_tests` the pair (regex, True). None is every line; equal values are the same lines; anything else
    is not compared (`_absorbs_atom` and `contradiction` require None or equality)."""
    return (leaf.get("exclude"), True) if leaf.get("skip_tests") else leaf.get("exclude")


def _atoms(leaf: dict) -> list[dict] | None:
    """The comparable parts of one probe leaf, or None when it has none (not provable)."""
    t = leaf.get("type")
    inc, skip = _split(leaf) if "paths" in leaf else ([], [])
    if t == "grep":
        lo, hi = _bounds(leaf)
        return [{"form": COUNTED, "key": ("grep", leaf["pattern"]), "exclude": _lines_key(leaf),
                 "inc": inc, "skip": skip, "lo": lo, "hi": hi}]
    if t == "only_in":
        from .probes import only_in_pattern
        key = ("grep", only_in_pattern(leaf))
        files = as_region(leaf["files"])             # read with `matches`: a literal file entry may be a directory
        out = [{"form": COUNTED, "key": key, "exclude": _lines_key(leaf), "inc": inc,
                "skip": [*skip, *files], "lo": 0, "hi": 0}]
        need = leaf.get("min", 1)
        # inside: the probe counts lines in (paths - skips) ∩ files. As something that forbids (absorbing others),
        # the atom says "at least min over all of files": true, files is a superset. As something lost, only a
        # subset of the counted files may stand for it ("lower_inc"): the entries known to lie inside the paths
        sub = [f.rstrip("/") for f in leaf["files"] if any(glob_covers(i, f.rstrip("/")) for i in inc)]
        out.append({"form": COUNTED, "key": key, "exclude": _lines_key(leaf), "inc": files, "skip": [],
                    "lo": need, "hi": INF, "lower_inc": sub, "lower_skip": skip})
        return out
    if t == "paths":
        from .probes import _expectation, paths_expect
        mode = ("unless_name", leaf["unless_name"]) if "unless_name" in leaf else (
            ("unique", leaf["unique"]) if "unique" in leaf else ("files", None))
        op, n = _expectation(paths_expect(leaf))
        lo, hi = {"count": (n, n), "min": (n, INF), "max": (0, n)}[op]
        return [{"form": COUNTED, "key": ("paths", *mode), "exclude": None, "inc": inc, "skip": skip, "lo": lo, "hi": hi}]
    if t == "staged_paths":
        out = []
        if "forbid" in leaf:
            out.append({"form": "regex", "key": ("staged_forbid", leaf["forbid"])})
        if "only" in leaf:
            out.append({"form": "allow", "key": ("staged_only",), "globs": as_region(leaf["only"])})
        if "frozen" in leaf:
            out.append({"form": "cover", "key": ("staged_frozen",), "globs": as_region(leaf["frozen"])})
        return out
    if t == "commit_message":
        return [{"form": "regex", "key": (f"message_{k}", leaf[k])} for k in ("forbid", "require") if k in leaf]
    return None


def _within(inc_i, skip_i, inc_o, skip_o) -> bool:
    """Every file (inc_i - skip_i) is in (inc_o - skip_o), compared as patterns."""
    from .regions import is_glob, matches
    # an outer skip leaves the inner set alone when the inner set skips it too, or is literal paths it does not match
    return (all(any(glob_covers(o, i) for o in inc_o) for i in inc_i)
            and all(s in skip_i or all(not is_glob(i) and not matches(s, i) for i in inc_i) for s in skip_o))


def _absorbs_atom(a: dict, lost: dict, side: str) -> bool:
    if a["form"] != lost["form"] or a["key"] != lost["key"]:
        return False
    if a["form"] == "regex":
        return True
    if a["form"] == "allow":      # absorber allows only paths the lost list allowed
        return all(any(glob_covers(l, g) for l in lost["globs"]) for g in a["globs"])
    if a["form"] == "cover":      # absorber freezes every path the lost list froze
        return all(any(glob_covers(g, l) for g in a["globs"]) for l in lost["globs"])
    if side == "upper":
        if lost["hi"] == INF:
            return True
        same_lines = a["exclude"] in (None, lost["exclude"])
        return same_lines and a["hi"] <= lost["hi"] and _within(lost["inc"], lost["skip"], a["inc"], a["skip"])
    if lost["lo"] == 0:
        return True
    inc, skip = lost.get("lower_inc", lost["inc"]), lost.get("lower_skip", lost["skip"])
    return (a["exclude"] == lost["exclude"] and a["lo"] >= lost["lo"]
            and _within(a["inc"], a["skip"], inc, skip))


def absorbs_leaf(a: dict, lost: dict, side: str) -> bool:
    """Does probe leaf `a` still forbid what leaf `lost` forbade on `side` ("upper" or "lower" bound)?"""
    la, ll = _atoms(a) or [], _atoms(lost)
    if not ll:
        return False
    return all(any(_absorbs_atom(x, y, side) for x in la) for y in ll if _bounded(y, side))


def _bounded(atom: dict, side: str) -> bool:
    if atom["form"] != COUNTED:
        return side == "upper"           # a gate atom forbids; it is compared once
    return atom["hi"] != INF if side == "upper" else atom["lo"] != 0


def _leaves(probe: dict | None) -> list[dict]:
    if not probe:
        return []
    if probe.get("type") == "all":
        return [x for p in probe["probes"] for x in _leaves(p)]
    return [probe]


def _shown(globs: list[str]) -> list[str]:
    """`x, x/**` (a literal read as a file or directory, as_region) is shown as `x`."""
    return [g for g in globs if not (g.endswith("/**") and g[:-3] in globs)]


def _describe(atom: dict, side: str, where: str | None = None) -> str:
    """The constraint an atom enforces on `side`, in words: "no lines matching /eval/ in src/**"."""
    if atom["form"] != COUNTED:
        k = atom["key"]
        globs = ", ".join(atom.get("globs", []))
        return {"staged_forbid": lambda: f"no commit of a path matching /{k[1]}/",
                "staged_only": lambda: f"no commit of a path outside {globs}",
                "staged_frozen": lambda: f"no commit that changes an existing file under {globs}",
                "message_forbid": lambda: f"no commit message matching /{k[1]}/",
                "message_require": lambda: f"no commit message lacking /{k[1]}/"}[k[0]]()
    if where is None:
        where = ", ".join(_shown(atom["inc"]))
    if atom["skip"]:
        where += f" (except {', '.join(_shown(atom['skip']))})"
    k = atom["key"]
    n = atom["hi"] if side == "upper" else atom["lo"]
    amount = "no" if side == "upper" and n == 0 else f"at {'most' if side == 'upper' else 'least'} {n:g}"
    one = n == 1
    if k[0] == "grep":
        thing, prep = f"line{'' if one else 's'} matching /{k[1]}/", "in"
    else:
        thing = {"files": f"file{'' if one else 's'}", "unless_name": f"file{'' if one else 's'} not named /{k[2]}/",
                 "unique": f"file{'' if one else 's'} sharing a /{k[2]}/ key"}[k[1]]
        prep = "under"
    return f"{amount} {thing} {prep} {where}"


def _grouped(entries: list[tuple[dict, str, object]]) -> list[tuple[str, object]]:
    """Describe (atom, side, tag) entries, merging those that differ only in their files (one line for a rule that
    repeats a check over many files). A merged line says "each" for a lower bound (it holds per file group)."""
    groups: dict[tuple, list] = {}
    for atom, side, tag in entries:
        if atom["form"] != COUNTED or (side == "upper" and atom["hi"] != 0):
            # gates, and nonzero upper bounds: "at most 3 in A" and "at most 3 in B" is not "at most 3 in A, B"
            groups.setdefault(("one", id(atom), side, tag), [atom, side, tag, list(atom.get("inc", []))])
            continue
        key = (atom["key"], atom.get("exclude"), atom["lo"], atom["hi"], tuple(atom["skip"]), side, tag)
        groups.setdefault(key, [atom, side, tag, []])[3].extend(atom["inc"])
    out = []
    for atom, side, tag, inc in groups.values():
        if atom["form"] != COUNTED or inc == atom["inc"]:
            out.append((_describe(atom, side), tag))
            continue
        inc = _shown(inc)
        shown = ", ".join(inc[:3]) + (f" and {len(inc) - 3} more" if len(inc) > 3 else "")
        out.append((_describe(atom, side, ("each of " if side == "lower" else "") + shown), tag))  # lower: per file
    return out


# --- the delta: the one primitive ---------------------------------------------------------------------------------
#
# Everything above is the comparison of two atoms. Everything the harness asks about a change to the rule set is a
# question about one Delta between the rules in effect before and after it:
#   global loss     something is newly allowed (or unknown on that side)      absorbed: nothing is
#   adds nothing    nothing is newly forbidden (and nothing unknown on that side)
#   redundant       dropping the rule changes nothing                          shadowing: precedence newly allows
#   ratchet         newly forbids only what the rule's own debt target forbids
# The queue shows the Delta; authority.judge gates on it; reclaim proposes changes whose Delta is empty.

@dataclass
class Part:
    rule: str                       # the rule the part belongs to
    atom: dict
    side: str                       # "upper" | "lower"
    by: list[str] = field(default_factory=list)     # the rules that cover it on the other side (empty: none)

    @property
    def what(self) -> str:
        return _describe(self.atom, self.side)


@dataclass
class Delta:
    forbids: list[Part] = field(default_factory=list)     # in after, covered by nothing before: newly forbidden
    allows: list[Part] = field(default_factory=list)      # in before, covered by nothing after: newly allowed
    kept: list[Part] = field(default_factory=list)        # parts of added rules that were already forbidden
    absorbed: list[Part] = field(default_factory=list)    # parts of removed rules still forbidden after
    unknown_forbids: list[tuple[str, str]] = field(default_factory=list)   # (rule, why): an added part not comparable
    unknown_allows: list[tuple[str, str]] = field(default_factory=list)    # (rule, why): a removed part not comparable

    @property
    def lossless(self) -> bool:
        """SOUND: nothing that was forbidden is let through."""
        return not self.allows and not self.unknown_allows

    @property
    def adds_nothing(self) -> bool:
        """SOUND: nothing is forbidden that was not already."""
        return not self.forbids and not self.unknown_forbids

    def loss_why(self) -> str:
        if self.unknown_allows:
            return f"not provable: {self.unknown_allows[0][1]}"
        if self.allows:
            return f"no other in-effect rule still enforces {self.allows[0].what}"
        return "nothing is let through"

    def absorbers(self) -> list[str]:
        out: list[str] = []
        for p in self.absorbed:
            out += [i for i in p.by if i not in out and not i.startswith("\0")]
        return out


def _canon(probe) -> str:
    import json
    return json.dumps(probe, sort_keys=True, default=str)


def _pool(rules) -> list[tuple[str, dict]]:
    return [(i, x) for i, p in rules for leaf in _leaves(p) for x in (_atoms(leaf) or [])]


def _not_comparable(leaf: dict) -> str:
    return f"a {leaf.get('type')!r} probe is not compared (grep, only_in, paths and gate probes are)"


def cover(atom: dict, side: str, pool: list[tuple[str, dict]], depth: int = 3) -> list[str] | None:
    """The rules in `pool` that together forbid everything `atom` forbids on `side`, or None. One atom covers it
    directly (`_absorbs_atom`), or, for a zero upper bound, an atom over a superset of its files covers all but what
    that atom skips, and that residual is covered in turn (this is what a narrow loses). SOUND: the residual (the
    other atom's skips, with `atom`'s own skips kept) contains every file of `atom` the other atom leaves out."""
    hit = next((i for i, a in pool if _absorbs_atom(a, atom, side)), None)
    if hit is not None:
        return [hit]
    if depth and atom["form"] == COUNTED and side == "upper" and atom["hi"] == 0:
        for i, y in pool:
            if (y["form"] == COUNTED and y["key"] == atom["key"] and y["hi"] == 0
                    and y["exclude"] in (None, atom["exclude"])
                    and all(any(glob_covers(o, x) for o in y["inc"]) for x in atom["inc"])):
                extra = [g for g in y["skip"] if g not in atom["skip"]]
                if extra and (rest := cover({**atom, "inc": extra}, "upper", pool, depth - 1)) is not None:
                    return [i, *(r for r in rest if r != i)]
    return None


def _split_rule(rid: str, probe, pool, uncovered: list, covered: list, unknown: list) -> None:
    for leaf in _leaves(probe):
        atoms = _atoms(leaf)
        if atoms is None:
            unknown.append((rid, _not_comparable(leaf)))
            continue
        for atom in atoms:
            for side in ("upper", "lower"):
                if _bounded(atom, side):
                    by = cover(atom, side, pool)
                    (uncovered if by is None else covered).append(Part(rid, atom, side, by or []))


def delta(before: list[tuple[str, dict]], after: list[tuple[str, dict]]) -> Delta:
    """The change in what is forbidden from the rules `before` to the rules `after` (each a list of (id, probe) in
    effect). A rule present in both, unchanged, contributes nothing but covers parts on both sides."""
    bkeys = {(i, _canon(p)) for i, p in before}
    akeys = {(i, _canon(p)) for i, p in after}
    removed = [(i, p) for i, p in before if (i, _canon(p)) not in akeys]
    added = [(i, p) for i, p in after if (i, _canon(p)) not in bkeys]
    pool_before, pool_after = _pool(before), _pool(after)
    d = Delta()
    for i, p in removed:
        _split_rule(i, p, pool_after, d.allows, d.absorbed, d.unknown_allows)
    for i, p in added:
        _split_rule(i, p, pool_before, d.forbids, d.kept, d.unknown_forbids)
    return d


def in_effect(cache, edges) -> list[tuple[str, dict]]:
    """(id, probe) of the enforced rules among `edges` that precedence does not set aside."""
    from .authority import effective
    kept, _ = effective(cache, [e for e in edges if e.enforced])
    return [(e.id, e.probe) for e in kept if e.enforced]


def change(cache, edits: dict[str, dict | None], add: list = (), without: set[str] = frozenset()) -> Delta:
    """The Delta of a change to the enforced rules: `edits` {rule id: new probe, or None to stop enforcing it}, `add`
    new rules (Edges, taken as enforced), `without` rules taken as already gone (several changes judged together)."""
    from dataclasses import replace
    now = [e for e in cache.store.all() if e.enforced and e.id not in without]
    then = []
    for e in now:
        if e.id in edits:
            if edits[e.id] is not None:
                then.append(replace(e, probe=edits[e.id]))
        else:
            then.append(e)
    then += [replace(e, rule_state="enforced") for e in add]
    return delta(in_effect(cache, now), in_effect(cache, then))


# --- the questions, as views of the Delta ------------------------------------------------------------------------

def assess_probes(before: dict | None, after: dict | None, others: list[dict], ids: list[str] | None = None) -> Absorption:
    """Is replacing probe `before` by `after` (None: dropped) absorbed by the probes in `others` (which, for a probe
    change, should include `after` itself)?"""
    ids = ids or [str(i) for i in range(len(others))]
    rest = list(zip(ids, others))
    new = [("\0after", after)] if after is not None else []
    return _absorption(delta([("\0lost", before), *rest], [*rest, *new]))


def _absorption(d: Delta) -> Absorption:
    if not d.lossless:
        return Absorption(False, [], d.loss_why())
    by = d.absorbers()
    return Absorption(True, by, "absorbed by " + ", ".join(by) if by else "nothing was forbidden")


def assess(cache, e, after_probe: dict | None, without: set[str] = frozenset()) -> Absorption:
    """Is changing rule `e` to `after_probe` (None: it stops being enforced) lossless, judged on the rules in effect
    before and after (precedence included)? `without`: rules taken as already retired."""
    return _absorption(change(cache, {e.id: after_probe}, without=without))


@dataclass
class Coverage:
    new: list[str] = field(default_factory=list)              # what the rule forbids that no other rule is proven to
    covered: list[tuple[str, str]] = field(default_factory=list)   # (what, id of a rule that already forbids it)
    unknown: str | None = None                                # set when a leaf cannot be compared (command, test, ...)

    @property
    def adds_nothing(self) -> bool:
        """SOUND: every part of the rule is proven already forbidden by another rule."""
        return not self.new and self.unknown is None


def coverage_of(d: Delta) -> Coverage:
    return Coverage([w for w, _ in _grouped([(p.atom, p.side, None) for p in d.forbids])],
                    _grouped([(p.atom, p.side, p.by[0]) for p in d.kept]),
                    d.unknown_forbids[0][1].replace("is not compared", "cannot be compared, so what it adds is unknown")
                    if d.unknown_forbids else None)


def coverage(probe: dict | None, others: list[dict], ids: list[str]) -> Coverage:
    """Split what `probe` enforces into the parts other probes already enforce (proven) and the rest."""
    rest = list(zip(ids, others))
    return coverage_of(delta(rest, [*rest, ("\0new", probe)]))


# --- contradiction: the remaining allowed set is empty ---------------------------------------------------------

@dataclass
class Conflict:
    sound: bool
    why: str
    kind: str = "grep"      # "unseen": a probe that cannot be compared was involved, nothing was actually compared


def _files_of(atom: dict, files: list[str]) -> set[str]:
    from .regions import matches
    return {f for f in files if any(matches(g, f) for g in atom["inc"]) and not any(matches(g, f) for g in atom["skip"])}


def contradiction(hi: dict | None, lo: dict | None, files: list[str] | None = None) -> Conflict | None:
    """Do two rules' probes contradict (no state satisfies both)? None when nothing is found.
    SOUND: two counted parts with the same key and line exclusion whose file PATTERNS are nested, the inner one
    requiring more than the outer one allows (a count over fewer files is at most the count over more).
    HEURISTIC (needs `files`, the tree now): same key, incompatible bounds, overlapping but not nested files.
    A probe that cannot be compared is reported as "unseen" (heuristic), never as a contradiction."""
    unseen, heuristic = False, None
    for la in _leaves(hi):
        for lb in _leaves(lo):
            A, B = _atoms(la), _atoms(lb)
            if A is None or B is None:
                unseen = True
                continue
            for a in A:
                for b in B:
                    if a["form"] != COUNTED or b["form"] != COUNTED or a["key"] != b["key"] or a["exclude"] != b["exclude"]:
                        continue
                    if b["lo"] > a["hi"] and _within(b["inc"], b["skip"], a["inc"], a["skip"]):
                        return Conflict(True, f"{_describe(b, 'lower')} is required where the higher rule allows "
                                              f"{_describe(a, 'upper')}: they cannot both hold")
                    if a["lo"] > b["hi"] and _within(a["inc"], a["skip"], b["inc"], b["skip"]):
                        return Conflict(True, f"{_describe(a, 'lower')} is required where the lower rule allows "
                                              f"{_describe(b, 'upper')}: they cannot both hold")
                    nested = (_within(a["inc"], a["skip"], b["inc"], b["skip"])
                              or _within(b["inc"], b["skip"], a["inc"], a["skip"]))   # then the checks above decide
                    if (heuristic is None and files is not None and not nested
                            and max(a["lo"], b["lo"]) > min(a["hi"], b["hi"])
                            and _files_of(a, files) & _files_of(b, files)):
                        heuristic = Conflict(False, f"overlapping but not nested files: {_describe(a, 'upper')} / "
                                                    f"{_describe(b, 'lower')}; they may conflict")
    if heuristic:
        return heuristic
    if unseen and _leaves(hi) and _leaves(lo):
        return Conflict(False, "a probe that cannot be compared is involved; only typed probes are compared", "unseen")
    return None

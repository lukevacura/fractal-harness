"""One meaning of "files". The proofs (loss.py) reason over patterns; probes select files by walking. Each case here
was a counterexample to a SOUND claim because the two disagreed; each test checks the claim against the probes
themselves on a tree that tries to break it."""
import subprocess

import pytest

from fractal_harness import loss, probes
from fractal_harness.loss import contradiction, delta, glob_covers


def g(pat, paths, expect="absent", **kw):
    return {"type": "grep", "pattern": pat, "paths": paths, "expect": expect, **kw}


def _tree(tmp_path, files: dict[str, str]):
    tmp_path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    for rel, text in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)
    return tmp_path


def _passes(probe, root):
    return probes.run(probe, root).passed


def _claim_holds(before, after, root):
    """If delta says lossless, no state of `root` passes every after rule and fails a before rule."""
    d = delta(before, after)
    if d.lossless:
        assert not (all(_passes(p, root) for _, p in after) and not all(_passes(p, root) for _, p in before)), \
            "lossless was claimed, but this tree passes after and fails before"
    return d


# --- a literal path in a skip, only_in files or a gate is a file OR a directory --------------------------------

def test_a_literal_directory_skip_is_not_one_file(tmp_path):
    root = _tree(tmp_path, {"src/vendor/v.py": "eval\n", "src/a.py": "x\n"})
    L = g("eval", ["src/**"])
    Z = g("eval", ["src/*"])                                       # covers src/vendor as a file, not its contents
    for narrowed in (g("eval", ["src/**"], exclude_paths=["src/vendor"]), g("eval", ["src/**", "!src/vendor"]),
                     g("eval", ["src/**"], exclude_paths=["src/vendor/"])):
        assert not _claim_holds([("L", L), ("Z", Z)], [("L", narrowed), ("Z", Z)], root).lossless


def test_only_in_files_may_name_a_directory(tmp_path):
    root = _tree(tmp_path, {"src/rank/r.py": "eval\n"})
    only = {"type": "only_in", "pattern": "eval", "paths": ["src/**"], "files": ["src/rank"]}
    L, Z = g("eval", ["src/**"]), g("eval", ["src/*"])
    assert not _claim_holds([("L", L), ("o", only), ("Z", Z)], [("o", only), ("Z", Z)], root).lossless
    hi = g("eval", ["src/rank"])                                   # a literal include: the FILE src/rank
    assert _passes(hi, root) and _passes(only, root)
    assert contradiction(hi, only) is None or not contradiction(hi, only).sound


def test_only_in_with_literal_files_is_still_absorbed_by_its_grep_pair():
    only = {"type": "only_in", "pattern": "U", "paths": ["core/**/*.rs"], "files": ["core/sync.rs"]}
    pair = [g("U", ["core/**/*.rs", "!core/sync.rs"]), g("U", ["core/sync.rs"], "present")]
    assert loss.assess_probes(only, None, pair).absorbed


@pytest.mark.parametrize("before,after", [
    ({"type": "staged_paths", "frozen": ["db/migrations"]}, {"type": "staged_paths", "frozen": ["db/*"]}),
    ({"type": "staged_paths", "only": ["src/*"]}, {"type": "staged_paths", "only": ["src/lib"]}),
])
def test_commit_gates_read_a_literal_as_a_directory(before, after):
    assert not loss.assess_probes(before, after, [after]).absorbed


def test_the_display_shows_a_literal_once():
    [line] = loss.coverage(g("x", ["src/**"], exclude_paths=["src/vendor"]), [], []).new
    assert line == "no lines matching /x/ in src/** (except src/vendor)"


# --- patterns the walk reads differently ---------------------------------------------------------------------

@pytest.mark.parametrize("pattern", ["/src/**", "src/**/", "/src/a.py"])
def test_a_leading_or_trailing_slash_is_refused_and_never_covered(pattern):
    with pytest.raises(probes.ProbeError, match="repo-relative"):
        probes.validate(g("x", [pattern]))
    assert not glob_covers(pattern, "src/a.py") and not glob_covers("src/**", pattern)


def test_a_bracket_that_can_match_a_slash_is_never_covered(tmp_path):
    root = _tree(tmp_path, {"src/x/y.py": "eval\n"})
    for inner in ("src/x[^a]y.py", "src/a[+-0]b.py"):
        assert not glob_covers("src/*", inner)
        assert not _claim_holds([("i", g("eval", [inner])), ("o", g("eval", ["src/*"]))],
                                [("o", g("eval", ["src/*"]))], root).lossless
    assert glob_covers("src/[ab].py", "src/[ab].py")                # equal patterns are fine


# --- the paths probe: a tracked file is in the repo, whatever .gitignore says ---------------------------------

def test_a_tracked_file_that_gitignore_matches_still_counts(tmp_path):
    root = _tree(tmp_path, {".gitignore": "*.log\n", "debug.log": "x\n", "other.log": "y\n"})
    subprocess.run(["git", "add", "-f", "debug.log"], cwd=root, check=True)
    r = probes.run({"type": "paths", "paths": ["*.log"], "expect": "absent"}, root)
    assert not r.passed and "debug.log" in r.detail and "other.log" not in r.detail


# --- a pending edit is selected as the walk would select it ---------------------------------------------------

def test_an_in_memory_file_is_selected_by_the_walks_rules(tmp_path):
    root = _tree(tmp_path, {"src/a.py": "x\n"})
    probe = g("eval", ["src/vendor"])                              # a literal include is the file src/vendor
    assert probes.run(probe, root, {"src/vendor/x.py": "eval\n"}).passed
    assert not probes.run(g("eval", ["src/**"]), root, {"src/vendor/x.py": "eval\n"}).passed


# --- one universe: ignored and symlinked directories are not the repo's files --------------------------------

def _ignored_tree(tmp_path):
    root = _tree(tmp_path, {".gitignore": "build/\n", "build/x.py": "eval\nSAFE\n", "src/a.py": "x\n"})
    return root


def test_no_probe_sees_an_ignored_directory_so_the_proofs_hold(tmp_path):
    root = _ignored_tree(tmp_path)
    inner, outer = g("eval", ["build/**/*.py"]), g("eval", ["**/*.py"])
    assert _passes(inner, root) and _passes(outer, root)            # neither sees build/
    assert probes.run(inner, root).vacuous                           # and the inner one says it checks nothing
    _claim_holds([("i", inner), ("o", outer)], [("o", outer)], root)
    lo_wide, lo_narrow = g("SAFE", ["**/*.py"], "present"), g("SAFE", ["build/**/*.py"], "present")
    _claim_holds([("w", lo_wide)], [("n", lo_narrow)], root)
    c = contradiction(outer, g("eval", ["build/**/*.py"], "present"))
    assert c is not None and c.sound and not _passes(g("eval", ["build/**/*.py"], "present"), root)


def test_a_tracked_file_inside_an_ignored_directory_is_seen(tmp_path):
    root = _ignored_tree(tmp_path)
    subprocess.run(["git", "add", "-f", "build/x.py"], cwd=root, check=True)
    assert not _passes(g("eval", ["**/*.py"]), root) and not _passes(g("eval", ["build/*.py"]), root)


def test_a_symlinked_directory_is_not_walked_by_any_pattern(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x.py").write_text("eval\n")
    root = _tree(tmp_path / "repo", {"src/a.py": "x\n"})
    (root / "vendor").symlink_to(outside, target_is_directory=True)
    assert _passes(g("eval", ["**/*.py"]), root) and _passes(g("eval", ["vendor/*.py"]), root)
    assert _passes(g("eval", ["vendor/x.py"]), root)
    assert probes.run(g("eval", ["src/*.py"]), root, {"vendor/y.py": "eval\n"}).passed


def test_an_ignored_file_named_literally_is_not_seen(tmp_path):
    root = _tree(tmp_path, {".gitignore": "*.log\n", "debug.log": "eval\n"})
    assert probes.run(g("eval", ["debug.log"]), root).vacuous


def test_a_pending_edit_inside_an_ignored_directory_is_not_seen(tmp_path):
    root = _ignored_tree(tmp_path)
    assert probes.run(g("eval", ["**/*.py"]), root, {"build/new.py": "eval\n"}).passed
    assert not probes.run(g("eval", ["**/*.py"]), root, {"src/new.py": "eval\n"}).passed

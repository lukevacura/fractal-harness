"""Scale fixes found profiling a 300-file Rust repo with 100k+ generated files (status/check took minutes)."""
import os
import subprocess
from pathlib import Path

from fractal_harness import hashing, ledger, motion
from fractal_harness.cache import ClaimCache
from fractal_harness.hashing import expand_glob, glob_hash, stable_files


def _git(root, *a):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=root,
                          capture_output=True, text=True, check=True).stdout


def _tree(tmp_path):
    _git(tmp_path, "init", "-q")
    (tmp_path / ".gitignore").write_text("target/\nbench/data/\n")
    for f in ("src/a.rs", "src/sub/b.rs", "target/debug/x.rs", "bench/data/d.rs", "bench/run.rs"):
        p = tmp_path / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("fn x() {}\n")


def test_glob_does_not_descend_into_ignored_dirs(tmp_path):
    _tree(tmp_path)
    assert expand_glob(tmp_path, "**/*.rs") == ["bench/run.rs", "src/a.rs", "src/sub/b.rs"]
    assert expand_glob(tmp_path, "bench/**/*.rs") == ["bench/run.rs"]


def test_glob_pointing_into_an_ignored_dir_sees_nothing(tmp_path):
    """One universe: every probe sees the repo's files only, so the proofs' pattern reasoning holds (loss.py). A rule
    over an ignored directory checks nothing, and the probe says so (vacuous)."""
    _tree(tmp_path)
    assert expand_glob(tmp_path, "bench/data/**/*.rs") == []
    assert expand_glob(tmp_path, "target/debug/*.rs") == []
    assert expand_glob(tmp_path, "target/debug/x.rs") == []


def test_glob_matches_pathlib_in_a_plain_directory(tmp_path):
    for f in ("a/b/c.txt", "a/d.txt", "e.txt", "a/b/c.md"):
        (tmp_path / f).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / f).write_text("x")
    for pat in ("**/*.txt", "a/*.txt", "a/**/*", "*.txt", "a/b/c.txt", "nothing/**/*"):
        want = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.glob(pat) if p.is_file())
        assert expand_glob(tmp_path, pat) == want, pat


def test_glob_hash_changes_on_edit_add_delete_and_ignores_ignored(tmp_path):
    _tree(tmp_path)
    h0 = glob_hash(tmp_path, "**/*.rs")
    (tmp_path / "target/debug/x.rs").write_text("changed")
    assert glob_hash(tmp_path, "**/*.rs") == h0
    (tmp_path / "src/a.rs").write_text("changed")
    h1 = glob_hash(tmp_path, "**/*.rs")
    assert h1 != h0
    (tmp_path / "src/new.rs").write_text("n")
    h2 = glob_hash(tmp_path, "**/*.rs")
    (tmp_path / "src/new.rs").unlink()
    assert h2 != h1 and glob_hash(tmp_path, "**/*.rs") == h1


def test_stable_files_memoizes_only_inside_the_scope(tmp_path):
    _tree(tmp_path)
    with stable_files():
        a = glob_hash(tmp_path, "src/**/*.rs")
        (tmp_path / "src/a.rs").write_text("edited mid-scan")
        assert glob_hash(tmp_path, "src/**/*.rs") == a          # assumed stable inside the scope
    assert glob_hash(tmp_path, "src/**/*.rs") != a              # a fresh read after it
    assert hashing._SCOPE is None


def test_file_hash_cache_sees_same_size_rewrites(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("aaaa")
    h = hashing.file_hash(tmp_path, "f.txt")
    f.write_text("bbbb")
    os.utime(f, ns=(1, 1))    # a different mtime is what tells them apart
    assert hashing.file_hash(tmp_path, "f.txt") != h


def test_blob_at_matches_git_show_and_handles_missing(tmp_path):
    _git(tmp_path, "init", "-q")
    (tmp_path / "f.rs").write_text("héllo\nworld\n")
    (tmp_path / "g.rs").write_text("second\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "c")
    c = _git(tmp_path, "rev-parse", "HEAD").strip()
    assert ledger._blob_at(str(tmp_path), c, "f.rs") == "héllo\nworld\n"
    assert ledger._blob_at(str(tmp_path), c, "nope.rs") == ""
    assert ledger._blob_at(str(tmp_path), c, "g.rs") == "second\n"           # the reader stays in step after a miss
    assert ledger._blob_at(str(tmp_path), c, "also-missing.rs") == ""
    assert ledger._blob_at(str(tmp_path), c, "f.rs") == "héllo\nworld\n"


def test_hunks_only_equals_full_parse_for_the_files_asked_about(tmp_path):
    _git(tmp_path, "init", "-q")
    (tmp_path / "a.rs").write_text("".join(f"l{i}\n" for i in range(30)))
    (tmp_path / "b.rs").write_text("one\ntwo\n")
    (tmp_path / "bin.dat").write_bytes(b"\0\1\2")
    (tmp_path / "script.sh").write_text("x\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "c")
    c = _git(tmp_path, "rev-parse", "HEAD").strip()
    (tmp_path / "a.rs").write_text("".join(f"l{i}\n" if i != 7 else "changed\n" for i in range(30)))
    (tmp_path / "b.rs").write_text("one\ntwo\nthree\n")
    (tmp_path / "bin.dat").write_bytes(b"\0\1\3")
    os.chmod(tmp_path / "script.sh", 0o755)
    full = ledger._hunks_since(tmp_path, c, None)
    part = ledger.hunks_since(tmp_path, c, None, ("a.rs",))
    assert part["a.rs"] == full["a.rs"] == [(8, 1)]
    assert set(part) == set(full) == {"a.rs", "b.rs"}            # binary and mode-only changes are not listed
    assert part["b.rs"] == [(1, 10**9)]                            # not asked about: listed, as wholly changed


def test_locate_candidate_scan_finds_the_block_in_a_big_file():
    lines = [f"filler {i}" for i in range(2000)]
    block = ["alpha", "beta tweak", "gamma", "delta", "epsilon"]
    lines[1234:1239] = ["alpha", "beta", "gamma", "delta", "epsilon"]          # one line differs
    start, ratio = motion.locate(block, lines, hint=10)
    assert start == 1234 and ratio > 0.8
    assert motion.locate(["zz unrelated", "qq nothing"], lines, hint=0)[1] < 0.6


def test_settled_cache_update_is_a_no_op(tmp_path):
    _git(tmp_path, "init", "-q")
    c = ClaimCache(tmp_path)
    calls = []
    c.refresh = lambda: calls.append(1) or []
    c.update()
    with c.settled():
        assert c.update() == {}
        c.update()
    assert len(calls) == 1
    c.update()
    assert len(calls) == 2


def test_refresh_hashes_a_shared_dependency_once(tmp_path, monkeypatch):
    from fractal_harness import cache as cache_mod
    (tmp_path / "f.py").write_text("x = 1\n")
    c = ClaimCache(tmp_path)
    for i in range(5):
        c.put(f"claim {i}", ["f.py"])
    seen = []
    real = cache_mod.read_hashes
    monkeypatch.setattr(cache_mod, "read_hashes", lambda root, reads: seen.append(tuple(reads)) or real(root, reads))
    c.refresh()
    assert seen.count(("f.py",)) == 1

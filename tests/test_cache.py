from pathlib import Path

import pytest

from fractal_harness.cache import CacheError, ClaimCache


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("def handler():\n    auth_middleware()\n")
    (tmp_path / "src" / "db.py").write_text("POOL_SIZE = 10\n")
    return tmp_path


@pytest.fixture
def cache(repo: Path):
    c = ClaimCache(repo)
    yield c
    c.close()


def grep(pattern: str, path: str = "src/*.py", expect="present") -> dict:
    return {"type": "grep", "pattern": pattern, "paths": [path], "expect": expect}


def test_probe_pass_is_verified_and_fail_is_failed(cache):
    ok = cache.put("handlers call auth_middleware", ["src/app.py"], probe=grep("auth_middleware"))
    bad = cache.put("nothing calls eval", ["src/app.py"], probe=grep("handler", expect="absent"))
    assert ok.status == "verified"
    assert bad.status == "failed"


def test_no_probe_is_trusted(cache):
    assert cache.put("db pool is sized for prod", ["src/db.py"]).status == "trusted"


def test_source_change_marks_stale_then_query_reverifies(cache, repo):
    e = cache.put("handlers call auth_middleware", ["src/app.py"], probe=grep("auth_middleware"))
    (repo / "src" / "app.py").write_text("def handler():\n    auth_middleware()\n# comment\n")
    assert cache.refresh() == [e.id]
    assert cache.get(e.id).status == "stale"
    assert "src/app.py" in cache.get(e.id).detail
    [hit] = cache.query("auth_middleware")
    assert hit.status == "verified"


def test_source_change_that_breaks_claim_fails_on_query(cache, repo):
    e = cache.put("handlers call auth_middleware", ["src/app.py"], probe=grep("auth_middleware"))
    (repo / "src" / "app.py").write_text("def handler():\n    pass\n")
    assert cache.query("auth_middleware") == []
    assert cache.get(e.id).status == "failed"


def test_scanned_files_are_dependencies(cache, repo):
    e = cache.put("handlers call auth_middleware", ["src/app.py"], probe=grep("auth_middleware"))
    (repo / "src" / "db.py").write_text("POOL_SIZE = 20\n")   # scanned by the probe's src/*.py
    assert cache.refresh() == [e.id]
    assert cache.resolve([e.id])[0].status == "verified"


def test_unrelated_change_does_not_invalidate(cache, repo):
    e = cache.put("handlers call auth_middleware", ["src/app.py"], probe=grep("auth_middleware", "src/app.py"))
    (repo / "src" / "db.py").write_text("POOL_SIZE = 20\n")
    assert cache.refresh() == []
    assert cache.get(e.id).status == "verified"


def test_trusted_claim_is_not_laundered_by_recheck(cache, repo):
    e = cache.put("db pool is sized for prod", ["src/db.py"])
    (repo / "src" / "db.py").write_text("POOL_SIZE = 1\n")
    cache.refresh()
    assert cache.resolve([e.id])[0].status == "stale"
    assert "re-assertion" in cache.get(e.id).detail
    assert cache.put("db pool is sized for prod", ["src/db.py"]).status == "trusted"


def test_failure_propagates_downstream_as_stale(cache, repo):
    a = cache.put("handlers call auth_middleware", ["src/app.py"], probe=grep("auth_middleware"))
    b = cache.put("every request is authenticated", [], probe=grep("POOL", "src/db.py"), depends_on=[a.id])
    assert b.status == "verified"
    (repo / "src" / "app.py").write_text("def handler():\n    pass\n")
    cache.refresh()
    cache.resolve([a.id])
    assert cache.get(a.id).status == "failed"
    assert cache.get(b.id).status == "stale"
    assert a.id in cache.get(b.id).detail


def test_trusted_dependency_caps_downstream_at_trusted(cache):
    a = cache.put("db pool is sized for prod", ["src/db.py"])
    b = cache.put("pool constant exists", ["src/db.py"], probe=grep("POOL_SIZE"), depends_on=[a.id])
    assert b.status == "trusted"
    assert "depends on trusted" in b.detail


def test_cycles_and_unknown_deps_rejected(cache):
    a = cache.put("claim a", ["src/app.py"])
    with pytest.raises(CacheError):
        cache.put("claim b", ["src/app.py"], depends_on=["nope"])
    b = cache.put("claim b", ["src/app.py"], depends_on=[a.id])
    with pytest.raises(CacheError, match="cycle"):
        cache.put("claim a", ["src/app.py"], depends_on=[b.id])


def test_paths_must_be_repo_relative(cache):
    with pytest.raises(CacheError):
        cache.put("x", ["../etc/passwd"])
    with pytest.raises(CacheError):
        cache.put("x", ["/etc/passwd"])
    assert cache.put("x", ["./src/app.py"]).reads == ["src/app.py"]


def test_command_probe(cache):
    assert cache.put("true exits 0", [], probe={"type": "command", "run": "true"}).status == "verified"
    assert cache.put("false exits 0", [], probe={"type": "command", "run": "false"}).status == "failed"


def test_checker_version_bump_invalidates(repo):
    c1 = ClaimCache(repo, checker_version="1")
    e = c1.put("handlers call auth_middleware", ["src/app.py"], probe=grep("auth_middleware"))
    c1.close()
    c2 = ClaimCache(repo, checker_version="2")
    assert c2.refresh() == [e.id]
    c2.close()


def test_query_by_path_and_stats(cache):
    cache.put("handlers call auth_middleware", ["src/app.py"], probe=grep("auth_middleware"))
    cache.put("pool constant exists", ["src/db.py"], probe=grep("POOL_SIZE"))
    assert [e.reads for e in cache.query(paths=["src/db.py"])] == [["src/db.py"]]
    assert len(cache.query(paths=["src"])) == 2
    s = cache.stats()
    assert s["edges"] == 2 and s["queries"] == 2 and s["queries_with_hits"] == 2


def test_grep_is_line_based_with_exclude(cache, repo):
    (repo / "src" / "old.py").write_text("# SegmentIndex was deleted\nx = 1\n")
    plain = grep(r"\bSegmentIndex\b", expect="absent")
    assert cache.put("no SegmentIndex anywhere", ["src/old.py"], probe=plain).status == "failed"
    code_only = {**plain, "exclude": r"^\s*#"}
    assert cache.put("no SegmentIndex in code", ["src/old.py"], probe=code_only).status == "verified"
    anchored = grep(r"^x = ", expect={"count": 1})
    assert cache.put("x assigned at top level", ["src/old.py"], probe=anchored).status == "verified"


def test_glob_reads_invalidate_on_edit_add_and_delete(cache, repo):
    e = cache.put("no eval in src", ["src/*.py"], probe=grep(r"\beval\(", expect="absent"))
    assert e.status == "verified"
    (repo / "src" / "new.py").write_text("x = 2\n")
    assert cache.refresh() == [e.id]
    assert cache.resolve([e.id])[0].status == "verified"
    (repo / "src" / "new.py").unlink()
    assert cache.refresh() == [e.id]
    assert [x.id for x in cache.query(paths=["src/app.py"])] == [e.id]


def test_all_probe_requires_every_part(cache):
    both = {"type": "all", "probes": [grep("auth_middleware"), grep("POOL_SIZE")]}
    assert cache.put("auth and pool", ["src/*.py"], probe=both).status == "verified"
    one_bad = {"type": "all", "probes": [grep("auth_middleware"), grep("nope")]}
    e = cache.put("auth and nope", ["src/*.py"], probe=one_bad)
    assert e.status == "failed" and e.detail.startswith("#1:")


def test_search_is_ranked_any_term(cache):
    a = cache.put("Walked-edge paint picks the GeoJSON source by sub_edge_ prefix", ["src/app.py"])
    b = cache.put("Pool size constant lives in db.py", ["src/db.py"])
    # "overlay" appears in no claim; ranked search still finds the paint claim
    assert [e.id for e in cache.query("walked overlay paint setFeatureState source")] == [a.id]
    assert [e.id for e in cache.query("what is the pool size?")] == [b.id]
    assert cache.query("walked pool") and len(cache.query("walked pool")) == 2
    assert [e.id for e in cache.query("walked pool", min_matches=2)] == []


def test_search_matches_identifiers_and_paths(cache):
    a = cache.put("GPS filter is _gpsDistanceFilterM = 5", ["src/app.py"])
    assert [e.id for e in cache.query("gpsDistanceFilterM")] == [a.id]
    assert [e.id for e in cache.query("app")] == []  # stopword
    assert [e.id for e in cache.query("filter", paths=["src/app.py"])] == [a.id]


def test_search_stems_word_forms(cache):
    a = cache.put("Quest completion is detected by questIsComplete in quest.py", ["src/app.py"])
    assert [e.id for e in cache.query("how does a quest complete?", min_matches=2)] == [a.id]

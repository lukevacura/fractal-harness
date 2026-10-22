import subprocess
from pathlib import Path

import pytest

from fractal_harness import graph_check, graph_query, ledger
from fractal_harness.cache import ClaimCache


def _git(root: Path, *a: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=root, check=True, capture_output=True)


def _spec(**f) -> str:
    return "---\n" + "".join(f"{k}: {v}\n" for k, v in f.items()) + "---\n# Body\n"


def _repo(tmp_path: Path, files: dict[str, str], specs: dict[str, dict]) -> ClaimCache:
    _git(tmp_path, "init", "-q")
    (tmp_path / ".git" / "info" / "exclude").write_text(".fractal/\n")
    for path, text in files.items():
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text(text)
    c = ClaimCache(tmp_path)
    # registration refuses a parent not yet registered, and a cycle only arises when a spec is edited later:
    # register every node bare, then write the real headers and register again
    for headers in ({n: {k: v for k, v in h.items() if k not in ("parents", "after")} for n, h in specs.items()}, specs):
        for name, header in headers.items():
            (tmp_path / f"{name}.md").write_text(_spec(guarantee="g", **header))
        _git(tmp_path, "add", "-A")
        for name in specs:
            ledger.register_node(c, tmp_path, name, f"{name}.md")
    return c


def _main(tmp_path: Path) -> ClaimCache:
    """FILES/SPECS registered, then `Gone` deleted from the code: a stale export."""
    c = _repo(tmp_path, FILES, SPECS)
    (tmp_path / "src" / "stats.py").write_text(FILES["src/stats.py"].replace("def Gone():\n    pass\n\n", ""))
    return c


def _same(c: ClaimCache, root: Path, only: str | None = None) -> dict:
    want = graph_check.check(c, root, only)
    got = graph_query.check(c, root, only)
    for k in ("dead", "phantom", "missing"):
        assert got[k] == want[k], k
    return got


# a repo that exercises every rule: dead, tested-only, public, planned, missing, self-use, phantom, after, rust
FILES = {
    "src/stats.py": "def mean(x):\n    return x\n\ndef Orphan():\n    pass\n\ndef TestedOnly():\n    pass\n"
                    "def Gone():\n    pass\n\ndef run():\n    pass\n\ndef helper():\n    return 1\n\ndef api():\n    return helper()\n\nclass Box:\n    def put(self):\n        pass\n",
    "src/grow.py": "from src.stats import mean\ndef grow():\n    return mean(1)\n",
    "src/lone.py": "def lone():\n    return 1\n",
    "src/lib.rs": "pub struct Pair;\npub fn used() {}\nfn go() { used(); }\n",
    "src/other.py": "def other():\n    return 1\n",
    "tests/test_x.py": "from src.stats import TestedOnly, Box\n",
}
SPECS = {
    "stats": dict(exports="src/stats.py::mean, src/stats.py::Orphan, src/stats.py::TestedOnly, src/stats.py::helper, "
                          "src/stats.py::Gone, src/stats.py::Box::put, src/stats.py::Soon, src/stats.py::run",
                  planned="src/stats.py::Soon", public="src/stats.py::Orphan"),
    "grow": dict(exports="src/grow.py::grow", parents="stats"),
    "lone": dict(exports="src/lone.py::lone", parents="stats"),          # phantom: lone never names a stats export
    "seq": dict(exports="src/other.py::other", after="stats"),           # `after` is never phantom
    "pair": dict(exports="src/lib.rs::Pair, src/lib.rs::used", parents="grow"),
}


def test_rules_agree_with_graph_check(tmp_path):
    c = _main(tmp_path)
    got = _same(c, tmp_path)
    tested_only = {d["symbol"] for d in got["dead"] if d["tested_only"]}
    assert tested_only == {"src/stats.py::TestedOnly", "src/stats.py::Box"}
    # Orphan is public, helper is used by api, mean by grow, Soon is planned, run is an entry point; the leaves
    # of the graph (nothing calls grow, lone, other) and a Rust pub item only its own file uses are dead
    assert {d["symbol"] for d in got["dead"]} == tested_only | {
        "src/grow.py::grow", "src/lone.py::lone", "src/other.py::other", "src/lib.rs::Pair", "src/lib.rs::used"}
    assert [d["symbol"] for d in got["missing"]] == ["src/stats.py::Gone"]          # Soon is planned, run is an entry name
    assert [(d["node"], d["parent"]) for d in got["phantom"]] == [("lone", "stats"), ("pair", "grow")]


def test_only_filters_by_node(tmp_path):
    c = _main(tmp_path)
    for only in ("stats", "lone", "pair", "grow", "seq"):
        _same(c, tmp_path, only)


def test_public_patterns_and_python_self_use_agree(tmp_path):
    c = _main(tmp_path)
    (tmp_path / ".fractal-public").write_text("src/stats.py::Tested*\nPair\n")
    got = _same(c, tmp_path)
    assert {d["symbol"] for d in got["dead"]} == {      # TestedOnly (glob) and Pair (bare name) are now public
        "src/stats.py::Box", "src/grow.py::grow", "src/lone.py::lone", "src/other.py::other", "src/lib.rs::used"}


def test_planned_symbol_that_now_exists_is_checked(tmp_path):
    c = _main(tmp_path)
    (tmp_path / "src" / "stats.py").write_text((tmp_path / "src" / "stats.py").read_text() + "\ndef Soon():\n    pass\n")
    got = _same(c, tmp_path)
    assert "src/stats.py::Soon" in {d["symbol"] for d in got["dead"]}


def test_mutant_files_and_tests_widen_a_nodes_scope(tmp_path):
    c = _main(tmp_path)
    (tmp_path / "src" / "extra.py").write_text("from src.stats import mean\n")
    (tmp_path / "tests" / "test_lone.py").write_text("from src.lone import lone\nfrom src.stats import mean\n")
    _git(tmp_path, "add", "-A")
    got = _same(c, tmp_path)
    assert [d["node"] for d in got["phantom"]] == ["pair"]                  # lone's own test now names mean
    c.store.put_mutant("pair", "src/extra.py", 1, 1, "m", "killed", "abc", [], "")
    assert graph_query.check(c, tmp_path)["phantom"] == graph_check.check(c, tmp_path)["phantom"]


def test_empty_repo_and_unregistered_header(tmp_path):
    c = _repo(tmp_path, {"src/a.py": "def a():\n    pass\n"}, {})
    assert _same(c, tmp_path) == {"dead": [], "phantom": [], "missing": []}


CHAIN = {"a": {}, "b": dict(parents="a"), "c": dict(parents="b, a"), "d": dict(parents="c"), "e": dict(after="d")}


def _chain(tmp_path, specs):
    return _repo(tmp_path, {"src/x.py": "def x():\n    pass\n"}, {n: {"exports": "src/x.py::x", **h} for n, h in specs.items()})


def test_layers_are_the_longest_chain_of_parents_and_reaches_is_the_closure(tmp_path):
    r = graph_query.layers(_chain(tmp_path, CHAIN), tmp_path)
    assert r["layer"] == {"a": 0, "e": 0, "b": 1, "c": 2, "d": 3}              # `after` adds no layer
    assert r["reaches"] == {"b": ["a"], "c": ["a", "b"], "d": ["a", "b", "c"]}
    assert r["cycles"] == []


def test_a_cycle_through_parents_or_after_is_reported_and_has_no_layer(tmp_path):
    specs = {"a": dict(parents="c"), "b": dict(parents="a"), "c": dict(after="b"), "d": dict(parents="c"), "f": {}}
    r = graph_query.layers(_chain(tmp_path, specs), tmp_path)
    assert r["cycles"] == ["a", "b", "c"]
    assert r["layer"] == {"f": 0}                  # d sits above the cycle: no honest depth
    assert r["reaches"] == {"a": ["c"], "b": ["a", "c"], "d": ["c"]}     # `reaches` follows `parents:` only


def test_short_names_are_ignored_on_both_sides_of_a_parent_edge(tmp_path):
    files = {**FILES, "src/short.py": "def ab():\n    pass\n\ndef longer():\n    pass\n",
             "src/kid.py": "def cd():\n    pass\n\nab = 1\n",
             "tests/test_cd.py": "cd = 1\nmean = 2\n"}
    specs = {**SPECS, "short": dict(exports="src/short.py::ab"),              # a parent whose only export name is 2 chars
             "kid": dict(exports="src/kid.py::cd", parents="short"),
             "par": dict(exports="src/short.py::ab, src/short.py::longer"),
             "kid2": dict(exports="src/kid.py::cd", parents="par, stats")}
    c = _repo(tmp_path, files, specs)
    got = _same(c, tmp_path)
    phantom = {(d["node"], d["parent"]) for d in got["phantom"]}
    assert ("kid", "short") not in phantom                      # nothing long enough to compare: not phantom
    assert ("kid2", "par") in phantom                           # kid.py mentions par's short `ab` but not its `longer`
    # the test names kid2's own 2-char export `cd` and the parent's `mean`; a short name does not put it in scope
    assert ("kid2", "stats") in phantom


def test_only_tests_widen_a_scope_and_phantoms_keep_declaration_order(tmp_path):
    c = _main(tmp_path)
    (tmp_path / "src" / "user.py").write_text("from src.lib import used\nfrom src.grow import grow\n")
    _git(tmp_path, "add", "-A")
    got = _same(c, tmp_path)
    # user.py names pair's own export `used`, and grow: it is not pair's code, so pair -> grow stays phantom
    assert ("pair", "grow") in {(d["node"], d["parent"]) for d in got["phantom"]}
    (tmp_path / "pair.md").write_text(_spec(guarantee="g", exports="src/lib.rs::Pair, src/lib.rs::used", parents="stats, grow"))
    assert [d["parent"] for d in _same(c, tmp_path)["phantom"] if d["node"] == "pair"] == ["stats", "grow"]

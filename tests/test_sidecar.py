import hashlib
import random
import sqlite3
import subprocess
from pathlib import Path

import pytest

from fractal_harness.model import Edge, Invariant, Model, Node, Port, Strength, IN, OUT
from fractal_harness.sidecar import SIDECAR_DIR, Sidecar, SidecarError, canonical, sidecar_dir, snapshot_key
from modelgen import corrupt, decorate, random_model
from test_model import pipe


def git(root: Path, *a: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=root, check=True,
                          capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("def a():\n    return 1\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "init")
    return tmp_path


def test_the_sidecar_lives_in_the_git_directory(repo):
    assert sidecar_dir(repo) == (repo / ".git" / SIDECAR_DIR).resolve()
    with Sidecar(repo) as sc:
        assert sc.path == (repo / ".git" / "fractal" / "model.db").resolve() and sc.path.exists()


def test_a_directory_that_is_not_a_repository_has_no_sidecar(tmp_path):
    with pytest.raises(SidecarError):
        Sidecar(tmp_path)
    with pytest.raises(SidecarError):
        sidecar_dir(tmp_path / "missing")


def test_linked_worktrees_share_one_model(repo, tmp_path_factory):
    other = tmp_path_factory.mktemp("wt") / "wt"
    git(repo, "worktree", "add", "-q", str(other), "-b", "side")
    with Sidecar(repo) as a:
        key = a.save(pipe())
    with Sidecar(other) as b:
        assert b.path == a.path and b.head_key() == key and b.head() == pipe()
    assert not (other / ".git").is_dir()                           # a linked worktree has a .git *file*; nothing was put in it


def tree_state(root: Path) -> dict:
    """Everything an outside observer of the working tree and history could see: file contents (ignored files
    included), status, refs, and the object count. The index is left out: `git status` itself refreshes it."""
    files = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if p.is_file() and not rel.startswith(".git/"):
            files[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    git_files = {p.relative_to(root / ".git").as_posix() for p in (root / ".git").rglob("*")
                 if p.is_file() and not p.relative_to(root / ".git").as_posix().startswith("objects/")
                 and p.name not in ("index", "index.lock")}
    return {"files": files, "git": git_files, "status": git(root, "status", "--porcelain", "--ignored"),
            "refs": git(root, "for-each-ref"), "objects": git(root, "count-objects", "-v"),
            "config": (root / ".git" / "config").read_text(), "hooks": sorted(p.name for p in (root / ".git" / "hooks").iterdir())}


def test_zero_footprint_the_working_tree_and_history_are_byte_identical(repo):
    before = tree_state(repo)
    with Sidecar(repo) as sc:
        sc.save(pipe(), "first")
        sc.save(pipe().without("top/b"), "draft", draft=True)
        sc.head(), sc.projected(), sc.gap_pairs(Strength.PROOF), sc.feeds("top/a"), sc.history()
        sc.query("SELECT count(*) FROM node")
    after = tree_state(repo)
    assert after["files"] == before["files"] and after["status"] == before["status"] == ""
    assert after["refs"] == before["refs"] and after["objects"] == before["objects"]
    assert after["config"] == before["config"] and after["hooks"] == before["hooks"]
    assert {f for f in after["git"] - before["git"]} and all(f.startswith("fractal/") for f in after["git"] - before["git"])
    assert before["git"] <= after["git"]


def test_save_load_head_and_history(repo):
    with Sidecar(repo) as sc:
        assert sc.head() is None and sc.head_key() is None and sc.history() == []
        k1 = sc.save(pipe(), "one")
        assert k1 == snapshot_key(pipe()) and sc.head() == pipe() and sc.load(k1) == pipe()
        assert sc.save(pipe(), "one") == k1 and len(sc.history()) == 1                 # same model, same label: nothing to record
        assert sc.save(pipe(), "two") == k1 and [h["label"] for h in sc.history()] == ["one", "two"]
        smaller = pipe().without("top/b").with_nodes(Node("top/b2", ports=(Port("i", IN), Port("o", OUT))))
        k2 = sc.save(Model.of(smaller.nodes, [e for e in smaller.edges if e.dst[0] != "top"]), "three", draft=True)
        assert k2 != k1 and sc.head_key() == k2 and sc.load(k1) == pipe()              # older snapshots stay loadable
        assert [h["key"] for h in sc.history()] == [k1, k1, k2]
        with pytest.raises(SidecarError):
            sc.load("nope")


def test_the_head_survives_reopening(repo):
    with Sidecar(repo) as sc:
        sc.save(pipe(), "kept")
    with Sidecar(repo) as sc:
        assert sc.head() == pipe() and sc.history()[0]["label"] == "kept" and sc.projected() == pipe()


def test_a_model_that_does_not_validate_is_refused_unless_it_is_a_draft(repo):
    bad = pipe().with_nodes(Node("orphan/child"))
    with Sidecar(repo) as sc:
        with pytest.raises(SidecarError, match="orphan"):
            sc.save(bad)
        assert sc.head() is None and sc.history() == []                                # a refusal leaves no trace
        sc.save(bad, draft=True)
        assert sc.head() == bad and sc.projected() == bad


def test_gaps_never_block_a_save(repo):
    gappy = pipe().with_nodes(Node("top/c", ports=(Port("o", OUT),), invariants=(Invariant("open", "nothing backs this"),)))
    assert gappy.validate() == [] and gappy.gaps()
    with Sidecar(repo) as sc:
        sc.save(gappy)
        assert ("undischarged", "top/c#open") in sc.gap_pairs()


def test_canonical_form_and_key_do_not_depend_on_construction_order():
    m = pipe()
    shuffled = Model.of(reversed(m.nodes), reversed(m.edges))
    assert canonical(m) == canonical(shuffled) and snapshot_key(m) == snapshot_key(shuffled)
    assert snapshot_key(m) != snapshot_key(m.without("top/b"))


@pytest.mark.parametrize("seed", range(25))
def test_the_projection_loses_nothing_and_the_gap_views_match_the_model(repo, seed):
    rng = random.Random(seed)
    m = decorate(random_model(rng), rng)
    assert m.validate() == [], m.validate()
    with Sidecar(repo) as sc:
        sc.save(m)
        assert sc.projected() == m
        assert sc.gap_pairs() == {(g.kind, g.subject) for g in m.gaps()}
        for floor in Strength:
            assert sc.gap_pairs(floor) == {(g.kind, g.subject) for g in m.gaps(floor)}, floor.name


def test_the_gap_views_see_every_kind_of_gap(repo):
    rng = random.Random(3)
    seen = set()
    with Sidecar(repo) as sc:
        for seed in range(60):
            rng = random.Random(seed)
            m = decorate(random_model(rng), rng)
            sc.save(m, f"s{seed}")
            seen |= {k for k, _ in sc.gap_pairs(Strength.CONSTRUCTION)}
    assert seen == {"edge-assumption", "undischarged", "assembly-unjudged", "claim-undischarged", "weak"}


def reach(m: Model) -> dict[str, set[str]]:
    succ: dict[str, set[str]] = {}
    for e in m.edges:
        if m.wire(e) and m.wire(e)[1] == "sibling":
            succ.setdefault(e.src[0], set()).add(e.dst[0])
    out = {}
    for a in succ:
        seen, stack = set(), list(succ[a])
        while stack:
            x = stack.pop()
            if x not in seen:
                seen.add(x)
                stack.extend(succ.get(x, ()))
        out[a] = seen
    return out


@pytest.mark.parametrize("seed", range(15))
def test_feeds_is_the_transitive_closure_of_sibling_edges(repo, seed):
    m = random_model(random.Random(seed))
    want = reach(m)
    with Sidecar(repo) as sc:
        sc.save(m)
        for n in m.nodes:
            assert sc.feeds(n.id) == sorted(want.get(n.id, ()))
            assert sc.fed_by(n.id) == sorted(a for a, bs in want.items() if n.id in bs)


def test_feeds_terminates_on_a_cycle_through_a_state_node(repo):
    two = (Port("i", IN), Port("o", OUT))
    m = Model.of([Node("c"), Node("c/a", ports=two), Node("c/b", ports=two, state=True), Node("c/d", ports=two)],
                 [Edge(("c/a", "o"), ("c/b", "i")), Edge(("c/b", "o"), ("c/a", "i")), Edge(("c/b", "o"), ("c/d", "i"))])
    assert m.validate() == []
    with Sidecar(repo) as sc:
        sc.save(m)
        assert sc.feeds("c/a") == ["c/a", "c/b", "c/d"] and sc.fed_by("c/d") == ["c/a", "c/b"]


def test_queries_cannot_write(repo):
    with Sidecar(repo) as sc:
        sc.save(pipe())
        assert sc.query("SELECT id FROM node WHERE leaf = 1 ORDER BY id") == [("top/a",), ("top/b",)]
        with pytest.raises(sqlite3.OperationalError):
            sc.query("DELETE FROM node")
        with pytest.raises(sqlite3.OperationalError):
            sc.query("INSERT INTO log(key, label, ts) VALUES ('x', 'y', 0)")
        assert sc.projected() == pipe()


def test_a_stale_projection_is_rebuilt_from_the_head(repo):
    with Sidecar(repo) as sc:
        sc.save(pipe())
        sc.db.execute("DROP VIEW feeds")
        sc.db.execute("DROP TABLE edge")
        sc.db.execute("PRAGMA user_version = 0")
    with Sidecar(repo) as sc:                                       # the version moved: the projection is derived, so it is rebuilt
        assert sc.projected() == pipe() and sc.feeds("top/a") == ["top/b"]


def test_a_projection_that_is_current_is_left_alone(repo):
    with Sidecar(repo) as sc:
        sc.save(pipe())
        sc.db.execute("DELETE FROM node WHERE id = 'top/b'")
        sc.db.commit()
    with Sidecar(repo) as sc:
        assert sc.query("SELECT count(*) FROM node") == [(2,)]      # same version: not rebuilt behind the caller's back
        sc.save(pipe(), "again")
        assert sc.query("SELECT count(*) FROM node") == [(3,)]      # a save rebuilds it from the model


def test_every_projection_row_is_for_the_head_only(repo):
    with Sidecar(repo) as sc:
        sc.save(pipe(), "big")
        sc.save(pipe().without("top/b"), "small", draft=True)
        assert [r[0] for r in sc.query("SELECT id FROM node ORDER BY id")] == ["top", "top/a"]
        assert sc.query("SELECT count(*) FROM edge WHERE dst_node = 'top/b' OR src_node = 'top/b'") == [(0,)]


def test_discharges_and_assemblies_are_projected_with_their_strengths(repo):
    m = pipe()
    with Sidecar(repo) as sc:
        sc.save(m)
        assert sc.query("SELECT strength, pointer FROM invariant WHERE ref = 'top/a#sorted'") == \
            [(int(Strength.PROPERTY), "tests/t.py::test_sorted")]
        assert sc.query("SELECT strength FROM preserve WHERE node = 'top/b'") == [(int(Strength.PROPERTY),)]
        assert sc.query("SELECT kind FROM edge WHERE src_node = 'top' AND dst_node = 'top/a'") == [("in",)]
        assert sorted(r[0] for r in sc.query("SELECT kind FROM edge")) == ["in", "out", "sibling"]


@pytest.mark.parametrize("seed", range(25))
def test_gap_views_match_the_model_on_drafts_with_malformed_edges(repo, seed):
    rng = random.Random(seed)
    m = corrupt(decorate(random_model(rng), rng), rng)
    assert m.validate()                                              # a real draft: it does not validate
    with Sidecar(repo) as sc:
        sc.save(m, draft=True)
        assert sc.projected() == m
        for floor in (None, Strength.EXAMPLE, Strength.CONSTRUCTION):
            assert sc.gap_pairs(floor) == {(g.kind, g.subject) for g in m.gaps(floor)}


def test_an_edge_from_a_port_that_does_not_exist_is_malformed_not_a_gap(repo):
    m = pipe().with_edges(Edge(("top/a", "ghost"), ("top/b", "i")))
    assert "edge-endpoint" in {v.rule for v in m.validate()} and m.gaps() == []
    with Sidecar(repo) as sc:
        sc.save(m, draft=True)
        assert sc.gap_pairs() == set()                               # top/b.i requires a#sorted, but nothing real feeds it wrongly


def test_a_container_that_promises_what_it_does_not_own_is_not_a_claim_gap(repo):
    m = pipe()                                                       # top.y carries top/a#sorted and top is a container
    assert m.gaps() == []
    with Sidecar(repo) as sc:
        sc.save(m)
        assert sc.gap_pairs() == set() and sc.query("SELECT count(*) FROM gap_claim") == [(0,)]
        assert sc.gap_pairs(Strength.CONSTRUCTION) == {("weak", "top/a#sorted"), ("weak", "top/b.o")}


def test_a_leaf_preserving_its_own_invariant_weakly_is_not_weak(repo):
    from fractal_harness.model import Preserve
    from dataclasses import replace
    m = pipe()
    a = m.node("top/a")
    m = m.with_nodes(replace(a, preserves=(Preserve("top/a#sorted", None),)))
    assert m.validate() == []
    with Sidecar(repo) as sc:
        sc.save(m)
        assert sc.gap_pairs(Strength.CONSTRUCTION) == {(g.kind, g.subject) for g in m.gaps(Strength.CONSTRUCTION)}
        assert ("claim-undischarged", "top/a.o") not in sc.gap_pairs()


def test_an_output_port_with_several_unbacked_claims_is_one_gap_row(repo):
    two = Node("top/c", ports=(Port("o", OUT, invariants=("top/a#sorted", "top/b#none")),))
    m = pipe().with_nodes(two, Node("top/b", ports=pipe().node("top/b").ports, owns=("src/b.py",),
                                    invariants=(Invariant("none", "x", None),), preserves=pipe().node("top/b").preserves))
    with Sidecar(repo) as sc:
        sc.save(m)
        assert sc.query("SELECT count(*) FROM gap_claim WHERE subject = 'top/c.o'") == [(1,)]


def test_the_directory_can_be_given_and_is_created_with_its_parents(repo, tmp_path):
    where = tmp_path / "elsewhere" / "deeper" / "store"
    with Sidecar(repo, directory=where) as sc:
        sc.save(pipe())
        assert sc.path == where / "model.db" and sc.path.exists()
    assert not (repo / ".git" / "fractal").exists()


def test_a_container_preserve_is_ignored_by_both_engines_whatever_its_strength(repo):
    from dataclasses import replace
    from fractal_harness.model import Discharge, Preserve
    m = pipe()
    m = m.with_nodes(replace(m.node("top"), preserves=(Preserve("top/a#sorted", Discharge(Strength.JUDGED, "x")),)))
    assert m.validate() == []
    with Sidecar(repo) as sc:
        sc.save(m)
        want = {(g.kind, g.subject) for g in m.gaps(Strength.CONSTRUCTION)}
        assert ("weak", "top.y") not in want and sc.gap_pairs(Strength.CONSTRUCTION) == want


def test_edges_are_projected_with_their_container_and_malformed_ones_have_none(repo):
    m = pipe().with_edges(Edge(("top/a", "ghost"), ("top/b", "i")), Edge(("top/a", "o"), ("nowhere", "p")))
    with Sidecar(repo) as sc:
        sc.save(m, draft=True)
        rows = {(a, b, c, d): (cont, kind) for a, b, c, d, cont, kind in
                sc.query("SELECT src_node, src_port, dst_node, dst_port, container, kind FROM edge")}
        assert rows[("top", "x", "top/a", "i")] == ("top", "in") and rows[("top/b", "o", "top", "y")] == ("top", "out")
        assert rows[("top/a", "o", "top/b", "i")] == ("top", "sibling") and rows[("top/a", "ghost", "top/b", "i")] == ("top", "sibling")
        assert rows[("top/a", "o", "nowhere", "p")] == (None, None)


def test_garbage_from_git_never_becomes_a_relative_store_in_the_working_directory(tmp_path, monkeypatch):
    import subprocess as sp
    from fractal_harness import sidecar
    monkeypatch.chdir(tmp_path)

    def fake(out):
        return lambda *a, **k: sp.CompletedProcess(a, 0, stdout=out, stderr="")

    for out in ("", "\n", "relative/.git\n", str(tmp_path / "no-such-dir") + "\n"):
        monkeypatch.setattr(sidecar.subprocess, "run", fake(out))
        with pytest.raises(SidecarError, match="refusing to guess"):
            Sidecar(tmp_path)
    monkeypatch.undo()
    with pytest.raises(SidecarError, match="not absolute"):
        Sidecar(tmp_path, directory=Path("relative-store"))
    assert list(tmp_path.iterdir()) == []                           # nothing was created anywhere

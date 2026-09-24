from pathlib import Path

import pytest

from fractal_harness.cache import ClaimCache

SRC = """import os

def configure():
    # sampling
    GPS_FILTER_M = 5
    return GPS_FILTER_M

def other():
    return 1
"""


@pytest.fixture
def repo(tmp_path: Path):
    (tmp_path / "app.py").write_text(SRC)
    c = ClaimCache(tmp_path)
    e = c.put("GPS filter is 5 m (GPS_FILTER_M in configure())", ["app.py"],
              probe={"type": "grep", "pattern": r"GPS_FILTER_M = 5$", "paths": ["app.py"], "expect": {"count": 1}})
    assert e.status == "verified" and e.anchors and e.anchors[0]["line"] == 4
    yield tmp_path, c, e.id
    c.close()


def edit(root: Path, text: str) -> None:
    (root / "app.py").write_text(text)


def test_unrelated_edit_is_clean(repo):
    root, c, eid = repo
    edit(root, SRC.replace("return 1", "return 2"))
    assert c.update() == {"clean": [eid]}
    assert c.get(eid).status == "verified"


def test_moved_code_rebases_anchor_for_free(repo):
    root, c, eid = repo
    edit(root, "# header\n# more\n" + SRC)
    assert c.update() == {"moved": [eid]}
    e = c.get(eid)
    assert e.status == "verified" and e.anchors[0]["line"] == 6 and e.repair is None


def test_small_change_breaking_probe_is_delta_with_residual(repo):
    root, c, eid = repo
    edit(root, SRC.replace("GPS_FILTER_M = 5", "GPS_FILTER_M = 10").replace("return GPS_FILTER_M", "return GPS_FILTER_M"))
    assert c.update() == {"delta": [eid]}
    e = c.get(eid)
    assert e.status == "failed" and e.repair["kind"] == "delta"
    diff = e.repair["residuals"][0]["diff"]
    assert "-    GPS_FILTER_M = 5" in diff and "+    GPS_FILTER_M = 10" in diff


def test_rewrite_is_scene_cut(repo):
    root, c, eid = repo
    edit(root, "class Settings:\n    def __init__(self):\n        self.interval_s = 30\n")
    assert c.update() == {"scene_cut": [eid]}
    assert c.get(eid).repair["kind"] == "scene_cut"


def test_probe_passing_on_rewritten_code_is_suspect(repo):
    root, c, eid = repo
    edit(root, "class Unrelated:\n    pass\n\nLEGACY = 'x'\nGPS_FILTER_M = 5\nprint('no function anymore')\n")
    assert c.update() == {"suspect": [eid]}
    e = c.get(eid)
    assert e.status == "stale" and e.repair["kind"] == "suspect"


def test_delta_put_counts_toward_keyframe_and_full_put_resets(repo):
    root, c, eid = repo
    e = c.get(eid)
    for n in (1, 2):
        e = c.put(e.post, e.reads, probe=e.probe, delta=True)
        assert e.delta_count == n
    assert c.put(e.post, e.reads, probe=e.probe).delta_count == 0


def test_repair_candidates_demand_and_keyframe(repo):
    from fractal_harness.hook import select
    from fractal_harness.repair import _parse_verdicts, candidates, repair
    root, c, eid = repo
    edit(root, SRC.replace("GPS_FILTER_M = 5", "GPS_FILTER_M = 10"))
    c.update()
    assert candidates(c) == []                      # broken, but nobody asked for it yet
    assert [(e.id, k) for e, k in candidates(c, everything=True)] == [(eid, "delta")]
    select(root, "what GPS filter does configure use?", session_id="s1")   # hook wants it
    assert [e.id for e, _ in candidates(c)] == [eid]
    with c.store.db:
        c.store.db.execute("UPDATE edges SET delta_count = 3 WHERE id = ?", (eid,))
    assert candidates(c)[0][1] == "keyframe"        # keyframe interval reached
    r = repair(root, dry_run=True)
    assert r["entries"] == [{"id": eid, "kind": "keyframe"}] and "re-verify" in r["prompt"]
    assert _parse_verdicts(f"- `{eid}`: STILL_TRUE - probe too strict\n{eid}: VIOLATION x") == {eid: "VIOLATION"}


def test_rewrite_elsewhere_in_file_is_caught(repo):
    root, c, eid = repo
    body = "\n".join(f"def helper_{i}():\n    return {i}" for i in range(12))
    edit(root, SRC + "\n" + body + "\n")          # probed lines untouched, file mostly new
    assert c.update() == {"rewrite": [eid]}
    e = c.get(eid)
    assert e.status == "stale" and e.repair["kind"] == "rewrite" and "app.py" in e.detail


def test_trusted_claim_needs_reassertion_and_is_repairable(tmp_path):
    from fractal_harness.repair import candidates
    (tmp_path / "a.py").write_text("x = 1\n")
    c = ClaimCache(tmp_path)
    e = c.put("x is configured in a.py", ["a.py"])
    (tmp_path / "a.py").write_text("x = 2\n")
    assert c.update() == {"reassert": [e.id]}
    assert [(x.id, k) for x, k in candidates(c, everything=True)] == [(e.id, "keyframe")]
    c.close()


def test_rewrite_flag_persists_until_repaired(repo):
    root, c, eid = repo
    body = "\n".join(f"def helper_{i}():\n    return {i}" for i in range(12))
    edit(root, SRC + "\n" + body + "\n")
    assert c.update() == {"rewrite": [eid]}
    edit(root, SRC + "\n" + body + "\n# unrelated follow-up edit\n")
    c.update()
    assert c.get(eid).repair["kind"] == "rewrite" and c.get(eid).status == "stale"
    e = c.get(eid)
    assert c.put(e.post, e.reads, probe=e.probe).status == "verified"   # a repair re-asserts it

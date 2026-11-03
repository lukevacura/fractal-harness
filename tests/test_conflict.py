"""Contradiction as the signal: block log, conflict events on the parent, agent resolution, bubbling, root paramount."""
import json
import time
from types import SimpleNamespace

import pytest

from fractal_harness import activation, authority, blocklog, conflict, ledger, status, store as store_mod
from fractal_harness.cache import ClaimCache
from fractal_harness.check import check
from fractal_harness.cli import main
from fractal_harness.conflict import ConflictError
from fractal_harness.hook import pre_edit_hook, prompt_hook, stop_hook
from fractal_harness.repair import reverify

from test_authority import HAS_DEF, NEEDS_LEGACY, NO_LEGACY_A, _hook, _propose, _repo
from test_nodes import _git, _spec

BREAK = "x = 1\n"          # an edit to src/mid.py that drops `def f(`
DAY = 86400.0


def _toml(root, threshold=3, days=14, extra=""):
    (root / "fractal.toml").write_text(
        f'[policy]\naccept_policy = "local"\nfriction_threshold = {threshold}\nfriction_window_days = {days}\n{extra}')


def _local(tmp_path, threshold=3, days=14, extra=""):
    """mid holds `def f` locally; top is mid's parent; top has no parent."""
    c = _repo(tmp_path)
    _toml(tmp_path, threshold, days, extra)
    e = _propose(c, "mid has def f", HAS_DEF, ["src/mid.py"])
    authority.accept_as(c, tmp_path, e.id, "mid", "local")
    return c, e


def _leaf(tmp_path, c):
    """A third layer: leaf (parent mid) owning src/leaf.py."""
    (tmp_path / "src" / "leaf.py").write_text("def h():\n    return 3\n")
    (tmp_path / "leaf.md").write_text(_spec(parents="mid", guarantee="Leaf promise.", exports="src/leaf.py::h"))
    ledger.register_node(c, tmp_path, "leaf", "leaf.md")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "leaf")
    e = _propose(c, "leaf has def h", {"type": "grep", "pattern": r"^def h\(", "paths": ["src/leaf.py"]}, ["src/leaf.py"])
    authority.accept_as(c, tmp_path, e.id, "leaf", "local")
    return e


def _block(tmp_path, n=1, rel="src/mid.py"):
    for _ in range(n):
        code, _ = _hook(tmp_path, rel, content=BREAK)
        assert code == 2


def _fresh(tmp_path):
    return ClaimCache(tmp_path)


# --- the block log -------------------------------------------------------------------------------

def test_pre_edit_block_is_logged_and_a_clean_edit_is_not(tmp_path):
    c, e = _local(tmp_path)
    c.close()
    assert _hook(tmp_path, "src/mid.py", content="def f():\n    return 9\n")[0] == 0
    c = _fresh(tmp_path)
    assert c.store.blocks() == []
    c.close()
    t0 = time.time()
    _block(tmp_path)
    c = _fresh(tmp_path)
    [b] = c.store.blocks()
    assert (b["edge_id"], b["authority"], b["file"], b["hook"], b["repeat"]) == (e.id, "mid", "src/mid.py", "pre-edit", 0)
    assert t0 <= b["ts"] <= time.time()
    assert c.store.block_stats(e.id)["total"] == 1
    c.close()


def test_check_failure_is_logged(tmp_path):
    c, e = _local(tmp_path)
    c.close()
    (tmp_path / "src" / "mid.py").write_text(BREAK)
    code, out = check(tmp_path)
    assert code == 1 and e.id in out
    c = _fresh(tmp_path)
    assert [(b["edge_id"], b["hook"], b["authority"]) for b in c.store.blocks()] == [(e.id, "check", "mid")]
    c.close()


def test_stop_hook_failure_is_logged(tmp_path, monkeypatch):
    c, e = _local(tmp_path)
    edge = c.store.get(e.id)
    c.close()
    fake = {"regressions": [], "violations": [edge], "checked": 1, "seconds": 0,
            "results": {e.id: SimpleNamespace(detail="violated", matches=[("src/mid.py", 0)])}}
    monkeypatch.setattr("fractal_harness.behavior.gate", lambda root: fake)
    code, msg = stop_hook(json.dumps({"cwd": str(tmp_path), "session_id": "s1"}))
    assert code == 2 and e.id in msg
    c = _fresh(tmp_path)
    [b] = c.store.blocks()
    assert (b["edge_id"], b["hook"], b["file"]) == (e.id, "stop", "src/mid.py")
    c.close()


def test_block_history_is_bounded_but_counters_are_not(tmp_path, monkeypatch):
    c, e = _local(tmp_path)
    monkeypatch.setattr(store_mod, "BLOCK_KEEP_PER_RULE", 5)
    monkeypatch.setattr(store_mod, "BLOCK_KEEP_TOTAL", 8)
    for i in range(12):
        c.store.block_add(e.id, "mid", f"f{i}", "pre-edit", DAY, now=1000.0 + i)
    rows = c.store.blocks(e.id)
    assert len(rows) == 5 and [r["file"] for r in rows] == [f"f{i}" for i in range(7, 12)]     # newest kept
    assert c.store.block_stats(e.id)["total"] == 12
    for i in range(10):
        c.store.block_add(f"other{i % 2}", "mid", "g", "check", DAY, now=2000.0 + i)
    assert len(c.store.blocks()) == 8
    assert c.store.block_stats("other0")["total"] == 5
    c.close()


def test_repeat_signal_same_rule_same_file_within_window(tmp_path):
    c, e = _local(tmp_path)
    s = c.store
    assert s.block_add(e.id, "mid", "a.py", "pre-edit", DAY, now=1000.0) is False
    assert s.block_add(e.id, "mid", "a.py", "pre-edit", DAY, now=1000.0 + DAY - 1) is True      # inside
    assert s.block_add(e.id, "mid", "b.py", "pre-edit", DAY, now=1000.0 + DAY) is False         # other file
    assert s.block_add(e.id, "mid", "a.py", "pre-edit", DAY, now=1000.0 + 3 * DAY) is False     # outside the window
    assert s.block_add(e.id, "mid", "", "check", DAY, now=1000.0 + 3 * DAY + 1) is False        # no file: no signal
    assert s.block_stats(e.id)["repeats"] == 1
    c.close()


def test_logging_failure_never_breaks_or_changes_a_hook(tmp_path, monkeypatch):
    c, e = _local(tmp_path)
    c.close()

    def boom(*a, **k):
        raise RuntimeError("disk on fire")
    monkeypatch.setattr(store_mod.Store, "block_add", boom)
    code, msg = _hook(tmp_path, "src/mid.py", content=BREAK)
    assert code == 2 and "NOT applied" in msg and "disk on fire" not in msg        # still blocks, same message
    (tmp_path / "src" / "mid.py").write_text(BREAK)
    assert check(tmp_path)[0] == 1
    blocklog.record_many(tmp_path / "nowhere", [(SimpleNamespace(id="x", layer="mid"), "")], "check")   # no store: silent
    blocklog.record(None, SimpleNamespace(id="x", layer="mid"), "f", "check", tmp_path)                # broken store: silent


# --- friction: threshold and window --------------------------------------------------------------

def test_friction_threshold_boundary_n_minus_one_vs_n(tmp_path):
    c, e = _local(tmp_path, threshold=3)
    c.close()
    _block(tmp_path, 2)
    c = _fresh(tmp_path)
    assert conflict.pending(c, tmp_path) == []
    c.close()
    _block(tmp_path)
    c = _fresh(tmp_path)
    [p] = conflict.pending(c, tmp_path)
    assert (p["kind"], p["rule"], p["handler"], p["state"]) == ("friction", e.id, "top", "open")
    assert "3x" in p["why"] and "heuristic" in p["why"]
    c.close()


def test_friction_window_edges(tmp_path):
    c, e = _local(tmp_path, threshold=3, days=2)
    now = 10_000_000.0
    for ts in (now - 2 * DAY, now - 1 * DAY, now - 10):             # exactly at the window edge counts
        c.store.block_add(e.id, "mid", "", "check", 1, now=ts)
    assert len(conflict.pending(c, tmp_path, now=now)) == 1
    assert conflict.pending(c, tmp_path, now=now + 1) == []          # the oldest slid out: two left
    c.close()


def test_default_threshold_and_window_are_permissive(tmp_path):
    from fractal_harness.config import load
    c = _repo(tmp_path, policy="local")
    cfg = load(tmp_path)
    assert (cfg.friction_threshold, cfg.friction_window_days, cfg.human_nodes) == (5, 14, ())
    c.close()


def test_config_validation(tmp_path):
    from fractal_harness.config import ConfigFileError, load
    for bad in ("friction_threshold = 0", "friction_threshold = 2.5", 'friction_threshold = "x"',
                "friction_window_days = -1", "human_nodes = 3", 'human_nodes = [1]'):
        (tmp_path / "fractal.toml").write_text(f"[policy]\n{bad}\n")
        with pytest.raises(ConfigFileError):
            load(tmp_path)
    (tmp_path / "fractal.toml").write_text('[policy]\nhuman_nodes = ["a", "b"]\nfriction_window_days = 0.5\n')
    cfg = load(tmp_path)
    assert cfg.human_nodes == ("a", "b") and cfg.friction_window_days == 0.5 and not cfg.warnings


# --- conflict firing: which node ------------------------------------------------------------------

def test_friction_fires_on_the_parent_of_the_authority_not_the_authority(tmp_path):
    c, e = _local(tmp_path)
    for _ in range(3):
        c.store.block_add(e.id, "mid", "", "check", DAY)
    pend = conflict.pending(c, tmp_path)
    assert activation.KINDS[-1] == "conflict"
    ev = {r["node"]: r["events"] for r in activation.due(c, tmp_path)}
    assert [x["kind"] for x in ev["top"]] == ["conflict"] and "mid" not in ev and "side" not in ev
    assert pend[0]["id"] in ev["top"][0]["detail"]
    c.close()


def test_a_top_layer_local_rule_goes_to_roots_queue_not_a_node(tmp_path):
    c = _repo(tmp_path)
    _toml(tmp_path)
    e = _propose(c, "top has def g", {"type": "grep", "pattern": r"^def g\(", "paths": ["src/top.py"]}, ["src/top.py"])
    authority.accept_as(c, tmp_path, e.id, "top", "local")
    for _ in range(3):
        c.store.block_add(e.id, "top", "", "check", DAY)
    [p] = conflict.pending(c, tmp_path)
    assert p["handler"] == "root" and p["human"] is True
    assert activation.due(c, tmp_path) == []                       # no node is activated: a human is
    r = activation.scan(c, tmp_path)
    assert "root's queue" in activation.render(r)
    c.close()


def test_overlap_with_a_higher_layers_rule_fires_on_the_parent(tmp_path):
    c, e = _local(tmp_path)
    loc = _propose(c, "mid has no legacy import", NO_LEGACY_A, ["src/mid.py"])
    authority.accept_as(c, tmp_path, loc.id, "mid", "local")
    assert [p for p in conflict.pending(c, tmp_path) if p["kind"] == "overlap"] == []
    hi = _propose(c, "mid must import legacy", NEEDS_LEGACY, ["src/mid.py"])
    c.set_rule(hi.id, "enforced")                                  # a human adds a contradicting root rule later
    [p] = [p for p in conflict.pending(c, tmp_path) if p["kind"] == "overlap"]
    assert (p["rule"], p["other"], p["handler"], p["sound"]) == (loc.id, hi.id, "top", True)
    assert "cannot both hold" in p["why"]
    c.close()


def test_a_non_grep_probe_is_not_an_overlap_event(tmp_path):
    c, e = _local(tmp_path)
    hi = c.put("cmd rule", [], kind="invariant", probe={"type": "command", "run": "true"})
    c.set_rule(hi.id, "enforced")
    assert conflict.pending(c, tmp_path) == []
    c.close()


def test_status_treats_a_conflict_as_due(tmp_path):
    c, e = _local(tmp_path)
    for _ in range(3):
        c.store.block_add(e.id, "mid", "", "check", DAY)
    assert status.derive(c, tmp_path, "top")["state"] != "done"
    f = status.gather(c, tmp_path, "top", {}, conflict.pending(c, tmp_path))
    assert [x["kind"] for x in f["events"]] == ["conflict"]
    f["verdicts"] = 1                                   # the fixture records no verdicts, which would classify it `pending` first
    state, why = status.classify(f)
    assert state == "due" and why[0].startswith("conflict:")
    c.close()


def test_no_config_and_no_local_rules_is_unchanged(tmp_path, capsys):
    c = _repo(tmp_path, policy=None)
    e = _propose(c, "no legacy", NO_LEGACY_A, ["src/mid.py"])
    c.set_rule(e.id, "enforced")
    c.close()
    assert not (tmp_path / "fractal.toml").exists()
    for _ in range(8):                                             # a root rule blocking: logged, never an event
        assert _hook(tmp_path, "src/mid.py", content="import legacy\n")[0] == 2
    c = _fresh(tmp_path)
    assert conflict.pending(c, tmp_path) == [] and conflict.detect(c, tmp_path) == []
    r = activation.scan(c, tmp_path)
    assert "conflicts" not in r and r["nodes"] == []
    assert activation.render(r) == "No node is due an activation.\n"
    c.close()
    assert main(["--root", str(tmp_path), "status", "--json"]) == 0
    assert "arbitrations" not in json.loads(capsys.readouterr().out)
    assert prompt_hook(json.dumps({"cwd": str(tmp_path), "prompt": "hello there"})) == ""


# --- decisions -------------------------------------------------------------------------------------

def _friction(tmp_path, c, e, n=3, node="mid"):
    for _ in range(n):
        c.store.block_add(e.id, node, "", "check", DAY)
    return conflict.pending(c, tmp_path)[0]["id"]


def test_keep_records_who_why_and_stays_quiet_until_new_friction(tmp_path):
    c, e = _local(tmp_path, threshold=3)
    cid = _friction(tmp_path, c, e)
    with pytest.raises(ConflictError, match="reason"):
        conflict.resolve(c, tmp_path, cid, "keep", "  ")
    conflict.resolve(c, tmp_path, cid, "keep", "the blocks were legitimate")
    assert conflict.pending(c, tmp_path) == []
    h = [x for x in c.store.rule_history(e.id) if x["action"] == "conflict_keep"]
    assert len(h) == 1 and h[0]["who"] == "top" and h[0]["reason"] == "the blocks were legitimate"
    assert c.store.get(e.id).enforced                              # keep changes nothing about the rule
    time.sleep(0.02)
    for _ in range(2):                                             # N-1 new blocks: still quiet
        c.store.block_add(e.id, "mid", "", "check", DAY)
    assert conflict.pending(c, tmp_path) == []
    c.store.block_add(e.id, "mid", "", "check", DAY)               # N new blocks since the decision: reopens
    [p] = conflict.pending(c, tmp_path)
    assert p["id"] == cid and p["state"] == "open" and p["handler"] == "top"
    c.close()


def test_narrow_adds_exclusions_records_both_probes_and_refuses_a_vacuous_result(tmp_path):
    c, e = _local(tmp_path)
    cid = _friction(tmp_path, c, e)
    with pytest.raises(ConflictError, match="--exclude"):
        conflict.resolve(c, tmp_path, cid, "narrow", "too wide")
    with pytest.raises(ConflictError, match="not hold now"):
        conflict.resolve(c, tmp_path, cid, "narrow", "too wide", exclude=["src/mid.py"])
    assert c.store.get(e.id).probe == HAS_DEF                      # a refused narrowing changes nothing
    conflict.resolve(c, tmp_path, cid, "narrow", "generated section", exclude=["src/gen/*.py"])
    got = c.store.get(e.id)
    assert got.probe["exclude_paths"] == ["src/gen/*.py"] and got.enforced and got.layer == "mid"
    [h] = [x for x in c.store.rule_history(e.id) if x["action"] == "conflict_narrow"]
    assert h["detail"]["old_probe"] == HAS_DEF and h["detail"]["new_probe"] == got.probe and h["who"] == "top"
    assert conflict.pending(c, tmp_path) == []
    c.close()


def test_narrow_refuses_a_rule_with_no_grep_probe(tmp_path):
    with pytest.raises(ConflictError, match="grep"):
        conflict.narrowed_probe({"type": "command", "run": "true"}, ["x"])
    p = conflict.narrowed_probe({"type": "all", "probes": [HAS_DEF, {"type": "command", "run": "true"}]}, ["a"])
    assert p["probes"][0]["exclude_paths"] == ["a"] and "exclude_paths" not in p["probes"][1]


def test_override_demotes_the_local_rule_by_the_handler(tmp_path):
    c, e = _local(tmp_path)
    cid = _friction(tmp_path, c, e)
    conflict.resolve(c, tmp_path, cid, "override", "rule was wrong")
    assert c.store.get(e.id).rule_state == "overridden"
    acts = [(h["action"], h["who"]) for h in c.store.rule_history(e.id)]
    assert ("override", "top") in acts and ("conflict_override", "top") in acts
    assert conflict.pending(c, tmp_path) == []
    c.close()


def test_only_the_handler_may_act_and_root_needs_a_human(tmp_path):
    c, e = _local(tmp_path)
    cid = _friction(tmp_path, c, e)
    with pytest.raises(ConflictError, match="handled by top"):
        conflict.resolve(c, tmp_path, cid, "keep", "x", as_node="side")
    with pytest.raises(ConflictError, match="handled by top"):
        conflict.resolve(c, tmp_path, cid, "keep", "x", as_node="mid")       # not even the rule's own node
    with pytest.raises(ConflictError, match="human decision"):
        conflict.resolve(c, tmp_path, cid, "keep", "x", as_node="root")      # claiming to be the human without a TTY
    conflict.resolve(c, tmp_path, cid, "keep", "x", as_node="root", human=True)
    assert [h["who"] for h in c.store.rule_history(e.id) if h["action"] == "conflict_keep"] == ["root"]
    c.close()


def test_a_flagged_node_forces_a_human(tmp_path):
    c, e = _local(tmp_path, extra='human_nodes = ["top"]\n')
    cid = _friction(tmp_path, c, e)
    [p] = conflict.pending(c, tmp_path)
    assert p["human"] is True and p["handler"] == "top"
    assert "(needs a human)" in activation.due(c, tmp_path)[0]["events"][0]["detail"]
    for kw in ({}, {"as_node": "top"}):
        with pytest.raises(ConflictError, match="human"):
            conflict.resolve(c, tmp_path, cid, "keep", "x", **kw)
    with pytest.raises(ConflictError, match="human"):
        conflict.escalate(c, tmp_path, cid, "pass it up")                    # no bypassing the human by escalating
    conflict.resolve(c, tmp_path, cid, "keep", "ok", human=True)
    assert conflict.pending(c, tmp_path) == []
    c.close()


def test_root_queue_conflicts_need_a_human(tmp_path):
    c = _repo(tmp_path)
    _toml(tmp_path)
    e = _propose(c, "top has def g", {"type": "grep", "pattern": r"^def g\(", "paths": ["src/top.py"]}, ["src/top.py"])
    authority.accept_as(c, tmp_path, e.id, "top", "local")
    cid = _friction(tmp_path, c, e, node="top")
    with pytest.raises(ConflictError, match="human decision"):
        conflict.resolve(c, tmp_path, cid, "keep", "x")
    with pytest.raises(ConflictError, match="already at root"):
        conflict.escalate(c, tmp_path, cid, "x", human=True)
    conflict.resolve(c, tmp_path, cid, "override", "gone", human=True)
    assert c.store.get(e.id).rule_state == "overridden"
    c.close()


def test_escalation_bubbles_layer_by_layer_until_root(tmp_path):
    c, _ = _local(tmp_path)
    leaf = _leaf(tmp_path, c)
    cid = _friction(tmp_path, c, leaf, node="leaf")
    assert conflict.pending(c, tmp_path)[0]["handler"] == "mid"
    with pytest.raises(ConflictError, match="reason"):
        conflict.escalate(c, tmp_path, cid, "")
    with pytest.raises(ConflictError, match="handled by mid"):
        conflict.escalate(c, tmp_path, cid, "up", as_node="top")             # only the current handler may pass it up
    r = conflict.escalate(c, tmp_path, cid, "mid cannot decide this")
    assert (r["handler"], r["state"]) == ("top", "open")
    ev = {x["node"] for x in activation.due(c, tmp_path)}
    assert ev == {"top"}                                                     # it left mid and is now top's event
    r = conflict.escalate(c, tmp_path, cid, "top cannot either", as_node="top")
    assert (r["handler"], r["state"]) == ("root", "awaiting_human")          # reached root: a human
    assert activation.due(c, tmp_path) == []
    steps = [(h["who"], h["detail"]["to"], h["detail"]["handler"]) for h in c.store.rule_history(leaf.id)
             if h["action"] == "conflict_escalate"]
    assert steps == [("mid", "open", "top"), ("top", "awaiting_human", "root")]
    c.close()


def test_agent_can_resolve_at_each_layer_before_root(tmp_path):
    c, _ = _local(tmp_path)
    leaf = _leaf(tmp_path, c)
    cid = _friction(tmp_path, c, leaf, node="leaf")
    conflict.escalate(c, tmp_path, cid, "up")
    conflict.resolve(c, tmp_path, cid, "keep", "top decided", as_node="top")  # no human involved
    assert conflict.pending(c, tmp_path) == []
    c.close()


# --- root rules are paramount -----------------------------------------------------------------------

def _root_overlap(tmp_path):
    c, e = _local(tmp_path)
    loc = _propose(c, "mid has no legacy import", NO_LEGACY_A, ["src/mid.py"])
    authority.accept_as(c, tmp_path, loc.id, "mid", "local")
    hi = _propose(c, "mid must import legacy", NEEDS_LEGACY, ["src/mid.py"])
    c.set_rule(hi.id, "enforced")
    [p] = [p for p in conflict.pending(c, tmp_path) if p["kind"] == "overlap"]
    return c, loc, hi, p["id"]


def test_disagreeing_with_a_root_rule_is_an_arbitration_request_not_an_action(tmp_path):
    c, loc, hi, cid = _root_overlap(tmp_path)
    before = (c.store.get(hi.id).probe, c.store.get(hi.id).rule_state, c.store.get(hi.id).layer)
    r = conflict.escalate(c, tmp_path, cid, "the root rule blocks the legacy migration we are asked to do")
    assert (r["state"], r["handler"]) == ("awaiting_human", "root")         # straight to the human: top was never asked
    assert (c.store.get(hi.id).probe, c.store.get(hi.id).rule_state, c.store.get(hi.id).layer) == before
    for action in ("keep", "narrow", "override"):                             # nothing else an agent can do with it
        with pytest.raises(ConflictError, match="arbitration"):
            conflict.resolve(c, tmp_path, cid, action, "x", exclude=["a"], as_node="top")
    c.close()


def test_the_subject_guard_refuses_any_agent_action_on_a_root_rule(tmp_path):
    c, loc, hi, _ = _root_overlap(tmp_path)
    # a record naming a root rule as the subject of an OPEN conflict handled by a node (it cannot arise through the
    # CLI; this is the defence in depth behind `raise`/`escalate`)
    c.store.conflict_put({"id": "c-forged", "kind": "spec", "rule_id": hi.id, "handler": "top", "state": "open",
                          "detail": {"why": "forged"}})
    for action in ("keep", "narrow", "override"):
        with pytest.raises(ConflictError, match="root \\(human\\) rule"):
            conflict.resolve(c, tmp_path, "c-forged", action, "x", exclude=["a"])
    assert c.store.get(hi.id).rule_state == "enforced" and c.store.get(hi.id).probe == NEEDS_LEGACY
    c.close()


def test_raising_on_a_root_rule_goes_straight_to_arbitration(tmp_path):
    c, loc, hi, _ = _root_overlap(tmp_path)
    r = conflict.raise_conflict(c, tmp_path, hi.id, "the rule's premise is ambiguous", by="mid")
    assert (r["state"], r["handler"]) == ("awaiting_human", "root")
    assert c.store.get(hi.id).probe == NEEDS_LEGACY
    with pytest.raises(ConflictError, match="already raised"):
        conflict.raise_conflict(c, tmp_path, hi.id, "again")
    c.close()


def test_arbitration_needs_a_reason_and_only_a_human(tmp_path, monkeypatch):
    c, loc, hi, cid = _root_overlap(tmp_path)
    conflict.escalate(c, tmp_path, cid, "the rule blocks legitimate work")
    with pytest.raises(ConflictError, match="human decision"):
        conflict.arbitrate(c, cid, "upheld", "fine", human=False)
    with pytest.raises(ConflictError, match="reason"):
        conflict.arbitrate(c, cid, "upheld", " ", human=True)
    with pytest.raises(ConflictError, match="outcome"):
        conflict.arbitrate(c, cid, "maybe", "x", human=True)
    with pytest.raises(ConflictError, match="not an arbitration"):
        conflict.arbitrate(c, "c-nope", "upheld", "x", human=True)
    assert c.store.conflict_get(cid)["state"] == "awaiting_human"            # nothing above changed it
    conflict.arbitrate(c, cid, "upheld", "the rule stands; migrate differently", human=True)
    assert c.store.conflict_get(cid)["state"] == "upheld"
    h = [x for x in c.store.rule_history(loc.id) if x["action"] == "conflict_arbitrate"]
    assert h and h[0]["who"] == "human" and "migrate differently" in h[0]["reason"]
    assert [p for p in conflict.pending(c, tmp_path) if p["id"] == cid] == []
    c.close()


def test_cli_arbitration_is_a_human_step(tmp_path, monkeypatch, capsys):
    c, loc, hi, cid = _root_overlap(tmp_path)
    c.close()
    root = ["--root", str(tmp_path)]
    assert main([*root, "conflict", "escalate", cid, "--reason", "it blocks legitimate work"]) == 0
    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: False))
    assert main([*root, "conflict", "arbitrate", cid, "--upheld", "--reason", "x"]) == 1      # no TTY, no --yes
    assert "human decision" in capsys.readouterr().err
    assert main([*root, "conflict", "arbitrate", cid, "--upheld", "--reason", "x", "--yes"]) == 0
    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: True))
    assert main([*root, "conflict", "arbitrate", cid, "--revised", "--reason", "x"]) == 1      # already decided


# --- surfacing ----------------------------------------------------------------------------------------

def test_arbitration_is_prominent_in_activations_status_invariants_and_the_prompt_hook_once(tmp_path, capsys):
    c, loc, hi, cid = _root_overlap(tmp_path)
    conflict.escalate(c, tmp_path, cid, "the rule blocks legitimate work, see the migration ticket")
    c.close()
    root = ["--root", str(tmp_path)]
    for cmd in (["activations"], ["status"], ["invariants"], ["conflict", "list"]):
        assert main([*root, *cmd]) == 0
        out = capsys.readouterr().out
        assert "ARBITRATION REQUEST" in out and cid in out, cmd
    assert main([*root, "status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["arbitrations"][0]["id"] == cid
    assert main([*root, "--json", "activations"]) == 0
    assert json.loads(capsys.readouterr().out)["conflicts"][0]["state"] == "awaiting_human"
    first = json.loads(prompt_hook(json.dumps({"cwd": str(tmp_path), "prompt": "hello"})))
    text = first["hookSpecificOutput"]["additionalContext"]
    assert "ARBITRATION REQUEST" in text and cid in text and "migration ticket" in text
    assert prompt_hook(json.dumps({"cwd": str(tmp_path), "prompt": "hello"})) == ""     # once
    assert main([*root, "conflict", "list"]) == 0
    assert cid in capsys.readouterr().out                                                  # still listed everywhere else


def test_conflict_list_and_show(tmp_path, capsys):
    c, e = _local(tmp_path)
    cid = _friction(tmp_path, c, e)
    c.close()
    root = ["--root", str(tmp_path)]
    assert main([*root, "--json", "conflict", "list"]) == 0
    assert json.loads(capsys.readouterr().out)["pending"][0]["id"] == cid
    assert main([*root, "conflict", "show", cid]) == 0
    out = capsys.readouterr().out
    assert cid in out and "held by: mid" in out and "blocks: 3 total" in out
    assert main([*root, "conflict", "show", "c-nope"]) == 1


# --- spec ambiguity: never weaken a probe -----------------------------------------------------------------

def test_raise_routes_a_claim_to_its_owners_parent_and_never_touches_the_claim(tmp_path):
    c = _repo(tmp_path)
    cl = c.put("mid defines f returning 3", ["src/mid.py"], probe={"type": "grep", "pattern": r"return 3", "paths": ["src/mid.py"]})
    c.check(cl.id)
    assert c.store.get(cl.id).status == "failed"
    before = c.store.get(cl.id).to_dict()
    r = conflict.raise_conflict(c, tmp_path, cl.id, "the spec says 3 but the contract text is ambiguous", by="mid")
    assert (r["handler"], r["state"]) == ("top", "open")                     # mid owns src/mid.py; top is its parent
    assert c.store.get(cl.id).to_dict() == before                            # claim and probe untouched
    ev = {x["node"]: x["events"] for x in activation.due(c, tmp_path)}
    assert any(x["kind"] == "conflict" for x in ev["top"])
    with pytest.raises(ConflictError, match="reason"):
        conflict.raise_conflict(c, tmp_path, "nope-id", "")
    with pytest.raises(ConflictError, match="not a claim"):
        conflict.raise_conflict(c, tmp_path, "nope-id", "r")
    c.close()


def test_a_claim_no_node_owns_goes_to_roots_queue(tmp_path):
    c = _repo(tmp_path)
    cl = c.put("readme says hi", ["README.md"], probe={"type": "grep", "pattern": "zzz", "paths": ["README.md"]})
    (tmp_path / "README.md").write_text("hi\n")
    r = conflict.raise_conflict(c, tmp_path, cl.id, "ambiguous")
    assert r["handler"] == "root"
    c.close()


def test_repair_offers_the_spec_route_and_records_it_without_healing(tmp_path):
    c = _repo(tmp_path)
    cl = c.put("mid defines f returning 3", ["src/mid.py"], probe={"type": "grep", "pattern": r"return 3", "paths": ["src/mid.py"]})
    c.check(cl.id)
    c.close()
    r = reverify(tmp_path)
    [x] = [x for x in r["remaining"] if x["id"] == cl.id]
    assert "conflict raise" in x["route"] and "never weaken" in x["route"] and "conflict" not in x
    c = _fresh(tmp_path)
    cid = conflict.raise_conflict(c, tmp_path, cl.id, "spec ambiguous")["id"]
    c.close()
    r = reverify(tmp_path)
    [x] = [x for x in r["remaining"] if x["id"] == cl.id]
    assert x["conflict"] == cid and r["model_used"] is False
    c = _fresh(tmp_path)
    assert c.store.get(cl.id).probe["pattern"] == "return 3"                 # still the original probe
    c.close()


def test_the_repair_prompt_forbids_weakening_and_names_the_route():
    from fractal_harness.repair import PROMPT, _parse_verdicts
    assert "NEVER weaken" in PROMPT and "fractal conflict raise" in PROMPT and "SPEC_CONFLICT" in PROMPT
    assert _parse_verdicts("abcdefghijklmnop: SPEC_CONFLICT - the spec is ambiguous") == {"abcdefghijklmnop": "SPEC_CONFLICT"}


# --- end to end through the CLI ---------------------------------------------------------------------------

def test_cli_flow_friction_then_agent_resolution(tmp_path, capsys):
    c, e = _local(tmp_path, threshold=2)
    c.close()
    root = ["--root", str(tmp_path)]
    _block(tmp_path, 2)
    assert main([*root, "activations"]) == 0
    out = capsys.readouterr().out
    assert "conflict" in out and e.id in out
    assert main([*root, "conflict", "list"]) == 0
    cid = [l.split()[0] for l in capsys.readouterr().out.splitlines() if l.startswith("c-")][0]
    with pytest.raises(SystemExit):
        main([*root, "conflict", "resolve", cid, "keep"])                                         # --reason is required
    assert main([*root, "conflict", "resolve", cid, "keep", "--reason", "needed", "--as", "side"]) == 1
    assert main([*root, "conflict", "resolve", cid, "keep", "--reason", "legitimate blocks"]) == 0
    assert main([*root, "activations"]) == 0
    assert "No node is due" in capsys.readouterr().out


# --- survivors of the first mutation pass, now pinned ----------------------------------------------------

def test_with_several_parents_the_first_by_name_handles_it(tmp_path):
    c, _ = _local(tmp_path)
    (tmp_path / "src" / "dual.py").write_text("def d():\n    return 4\n")
    (tmp_path / "dual.md").write_text(_spec(parents="top, mid", guarantee="Dual.", exports="src/dual.py::d"))
    ledger.register_node(c, tmp_path, "dual", "dual.md")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "dual")
    e = _propose(c, "dual has def d", {"type": "grep", "pattern": r"^def d\(", "paths": ["src/dual.py"]}, ["src/dual.py"])
    authority.accept_as(c, tmp_path, e.id, "dual", "local")
    cid = _friction(tmp_path, c, e, node="dual")
    assert conflict.pending(c, tmp_path)[0]["handler"] == "mid"
    assert conflict.parent_of(c.store.all_nodes(), "dual") == "mid"
    c.close()


def test_escalating_a_conflict_with_a_root_rule_skips_the_layers_in_between(tmp_path):
    c, _ = _local(tmp_path)
    leaf = _leaf(tmp_path, c)
    hi = _propose(c, "leaf must not define h", {"type": "grep", "pattern": r"^def h\(", "paths": ["src/leaf.py"], "expect": "absent"},
                  ["src/leaf.py"])
    c.set_rule(hi.id, "enforced")
    [p] = [p for p in conflict.pending(c, tmp_path) if p["kind"] == "overlap"]
    assert p["handler"] == "mid"                                  # two layers below root
    r = conflict.escalate(c, tmp_path, p["id"], "the root rule forbids the very function the leaf exists to provide")
    assert (r["handler"], r["state"]) == ("root", "awaiting_human")
    c.close()


def test_a_narrowing_that_leaves_an_absent_rule_checking_nothing_is_refused(tmp_path):
    c, _ = _local(tmp_path)
    loc = _propose(c, "mid has no legacy import", NO_LEGACY_A, ["src/mid.py"])
    authority.accept_as(c, tmp_path, loc.id, "mid", "local")
    cid = _friction(tmp_path, c, loc)
    with pytest.raises(ConflictError, match="check nothing"):
        conflict.resolve(c, tmp_path, cid, "narrow", "x", exclude=["src/mid.py"])
    assert "exclude_paths" not in c.store.get(loc.id).probe
    c.close()


def test_repeat_window_includes_its_exact_edge(tmp_path):
    c, e = _local(tmp_path)
    assert c.store.block_add(e.id, "mid", "a.py", "check", DAY, now=5000.0) is False
    assert c.store.block_add(e.id, "mid", "a.py", "check", DAY, now=5000.0 + DAY) is True
    c.close()

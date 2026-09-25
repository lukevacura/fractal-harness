import json
from pathlib import Path

from fractal_harness.cache import ClaimCache
from fractal_harness.hook import prompt_hook, select, stop_hook
from fractal_harness.record import pending


def _grep(pattern):
    return {"type": "grep", "pattern": pattern, "paths": ["*.py"]}


def _repo(tmp_path: Path) -> Path:
    (tmp_path / "app.py").write_text("GPS_FILTER = 5\ndef billing(): pass\n")
    c = ClaimCache(tmp_path)
    c.put("GPS distance filter is 5 metres (GPS_FILTER in app.py)", ["app.py"], probe=_grep("GPS_FILTER = 5"))
    c.put("GPS distance filter rationale is battery life", ["app.py"])  # trusted: never injected
    c.put("Billing runs through billing() in app.py", ["app.py"], probe=_grep("def billing"))
    c.close()
    return tmp_path


def _ctx(root: Path, prompt: str) -> str:
    out = prompt_hook(json.dumps({"prompt": prompt, "cwd": str(root)}))
    return json.loads(out)["hookSpecificOutput"]["additionalContext"] if out else ""


def test_injects_only_strong_verified_matches(tmp_path: Path):
    root = _repo(tmp_path)
    ctx = _ctx(root, "what GPS distance filter do we use?")
    assert "GPS distance filter is 5 metres" in ctx
    assert "battery" not in ctx and "Billing" not in ctx
    assert "do not re-read" in ctx  # guidance travels with the claims


def test_miss_injects_nothing(tmp_path: Path):
    root = _repo(tmp_path)
    assert _ctx(root, "how does fog of war reveal work?") == ""
    assert select(root, "filter") == []  # single term: below MIN_MATCHES


def test_silent_without_store(tmp_path: Path):
    assert _ctx(tmp_path, "what GPS distance filter do we use?") == ""
    assert not (tmp_path / ".fractal").exists()


def test_never_raises_on_bad_input():
    assert prompt_hook("not json") == ""


def test_stop_hook_queues_and_is_disabled_in_recorder(tmp_path: Path, monkeypatch):
    root = _repo(tmp_path)
    for sid in ("s1", "s1", "s2"):
        stop_hook(json.dumps({"session_id": sid, "transcript_path": f"/t/{sid}.jsonl", "cwd": str(root)}))
    assert sorted(e["session_id"] for e in pending(root)) == ["s1", "s2"]
    monkeypatch.setenv("FRACTAL_NO_HOOKS", "1")
    stop_hook(json.dumps({"session_id": "s3", "transcript_path": "/t/s3.jsonl", "cwd": str(root)}))
    assert _ctx(root, "what GPS distance filter do we use?") == ""
    assert len(pending(root)) == 2


def test_stats_report_real_use_hit_rate(tmp_path: Path):
    root = _repo(tmp_path)
    prompt_hook(json.dumps({"prompt": "what GPS distance filter do we use?", "cwd": str(root), "session_id": "s1"}))
    prompt_hook(json.dumps({"prompt": "how does fog of war reveal work?", "cwd": str(root), "session_id": "s2"}))
    c = ClaimCache(root)
    s = c.stats()
    c.close()
    assert (s["prompts_seen"], s["prompts_with_claims"], s["hit_rate"], s["claims_injected"]) == (2, 1, 0.5, 1)


# --- rules first, with evidence ---------------------------------------------------------------

def _rules_repo(tmp_path: Path) -> Path:
    import subprocess
    root = _repo(tmp_path)
    (root / "legacy_free.py").write_text("import os\n")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    c = ClaimCache(root)
    wide = c.put("no file imports legacy", ["*.py"], kind="invariant",
                 probe={"type": "grep", "pattern": r"^import legacy", "paths": ["*.py"], "expect": "absent"})
    c.set_rule(wide.id, "enforced")
    c.put("proposal only", ["app.py"], kind="invariant",
          probe={"type": "grep", "pattern": "GPS_FILTER", "paths": ["app.py"]})
    c.close()
    return root


def test_rules_come_first_with_evidence_and_facts_have_evidence(tmp_path):
    root = _rules_repo(tmp_path)
    ctx = _ctx(root, "what GPS distance filter do we use?")
    assert ctx.index("RULES") < ctx.index("FACTS")
    assert "no file imports legacy" in ctx and "✓ holds now:" in ctx
    assert "✓ app.py:1: GPS_FILTER = 5" in ctx
    assert "proposal only" not in ctx


def test_enforced_repo_rules_show_even_without_matching_facts(tmp_path):
    root = _rules_repo(tmp_path)
    ctx = _ctx(root, "refactor the build pipeline")
    assert ctx.startswith("RULES") and "FACTS" not in ctx


def test_violated_rule_is_flagged(tmp_path):
    root = _rules_repo(tmp_path)
    (root / "legacy_free.py").write_text("import legacy\n")
    ctx = _ctx(root, "anything about the build")
    assert "✗ VIOLATED NOW" in ctx


def test_rules_are_framed_as_guarantees_and_context_fits_budget(tmp_path):
    from fractal_harness.hook import render
    root = _rules_repo(tmp_path)
    ctx = _ctx(root, "what GPS distance filter do we use?")
    assert "GUARANTEED" in ctx and "do not need to re-verify" in ctx
    c = ClaimCache(root)
    rules = c.invariants(("enforced",))
    facts = [e for e in c.store.all() if e.kind != "invariant" and e.status == "verified"]
    c.close()
    small = render(rules, facts * 20, budget=900)
    assert len(small) <= 900 or small.count("\n- ") <= len(rules) + 1
    assert small.startswith("RULES")


def test_blocked_edit_waste_and_rereads_are_measured(tmp_path):
    import json as _json
    from fractal_harness.hook import pre_edit_hook
    from fractal_harness.record import measure, Session
    root = _rules_repo(tmp_path)
    code, _ = pre_edit_hook(_json.dumps({"tool_name": "Write", "cwd": str(root),
                                         "tool_input": {"file_path": str(root / "bad.py"), "content": "import legacy\n"}}))
    assert code == 2
    _ctx_sid = prompt_hook(_json.dumps({"prompt": "what GPS distance filter do we use?", "cwd": str(root),
                                        "session_id": "s9"}))
    measure(root, [Session(source="t", session_id="s9", files=["app.py", "legacy_free.py", "other.txt"])])
    c = ClaimCache(root)
    s = c.stats()
    c.close()
    assert s["edits_blocked"] == 1 and s["blocked_chars"] == len("import legacy\n")
    assert s["fact_rereads"] == 1 and s["rule_rereads"] == 2 and s["avg_context_chars"] > 0


def test_rules_govern_new_files_named_in_the_prompt(tmp_path):
    import subprocess
    root = _repo(tmp_path)
    (root / "pkg").mkdir()
    (root / "pkg" / "core.py").write_text("x = 1\n")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    c = ClaimCache(root)
    r = c.put("pkg never prints", ["pkg/*.py"], kind="invariant",
              probe={"type": "grep", "pattern": r"\bprint\(", "paths": ["pkg/*.py"], "expect": "absent"})
    c.set_rule(r.id, "enforced")
    c.close()
    assert "pkg never prints" in _ctx(root, "Add a new module pkg/alerts.py that formats alert lines")
    assert _ctx(root, "Add a new module elsewhere/alerts.py that formats alert lines") == ""

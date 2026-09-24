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

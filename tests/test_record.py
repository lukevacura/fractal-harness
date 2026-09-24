import json
from pathlib import Path

from fractal_harness.cache import ClaimCache
from fractal_harness.record import _save_done, parse_transcript, record


def _write(path: Path, events: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(e) for e in events))
    return path


def test_parse_stream_json_and_transcript(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\n")
    events = [
        {"type": "user", "message": {"role": "user", "content": "How is x set?"}},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Read", "input": {"file_path": str(tmp_path / "src" / "a.py")}},
            {"type": "tool_use", "name": "Grep", "input": {"pattern": "x", "path": "src/missing.py"}}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "x is 1 in src/a.py"}]}},
    ]
    s = parse_transcript(_write(tmp_path / "t.jsonl", events), tmp_path)
    assert s.prompt == "How is x set?" and s.answer == "x is 1 in src/a.py" and s.files == ["src/a.py"]
    stream = events[1:] + [{"type": "result", "result": "final answer"}]
    s = parse_transcript(_write(tmp_path / "s.jsonl", stream), tmp_path)
    assert s.prompt == "" and s.answer == "final answer" and s.files == ["src/a.py"]


def _session(tmp_path: Path, name: str, question: str, files: list[str], session_id: str) -> Path:
    for f in files:
        (tmp_path / f).write_text(f"# {f}\n")
    calls = [{"type": "tool_use", "name": "Read", "input": {"file_path": f}} for f in files]
    return _write(tmp_path / name, [
        {"type": "user", "session_id": session_id, "message": {"content": question}},
        {"type": "assistant", "message": {"content": [*calls, {"type": "text", "text": "the answer"}]}},
    ])


def test_miss_that_explored_is_recorded(tmp_path: Path):
    t = _session(tmp_path, "t.jsonl", "How is the pool sized?", ["a.py", "b.py"], "s1")
    r = record(tmp_path, [t], dry_run=True)
    assert r["sessions"] == 1 and "How is the pool sized?" in r["prompt"]
    assert "Files explored: a.py, b.py" in r["prompt"]


def test_light_session_skipped_without_model_call(tmp_path: Path):
    t = _session(tmp_path, "t.jsonl", "How is the pool sized?", ["a.py"], "s1")
    r = record(tmp_path, [t])
    assert r["sessions"] == 0 and r["cost_usd"] == 0.0
    assert r["skipped"][0]["reason"] == "explored too little"
    assert record(tmp_path, [tmp_path / "missing.jsonl"])["sessions"] == 0


def test_hit_skipped_when_cache_covered_exploration(tmp_path: Path):
    t = _session(tmp_path, "t.jsonl", "How is the pool sized?", ["a.py", "b.py", "c.py"], "s1")
    c = ClaimCache(tmp_path)
    e = c.put("pool sizing lives in a.py and b.py", ["a.py", "b.py"],
              probe={"type": "grep", "pattern": "a.py", "paths": ["a.py"]})
    c.store.log("inject", None, session_id="s1", claims=1, ids=[e.id])
    c.close()
    r = record(tmp_path, [t], dry_run=True)
    assert r["sessions"] == 0 and r["skipped"][0]["reason"].startswith("hit")
    # a partial hit that still explored enough uncovered files is recorded, minus covered files
    t2 = _session(tmp_path, "t2.jsonl", "How is the pool sized?", ["a.py", "c.py", "d.py"], "s1")
    r2 = record(tmp_path, [t2], dry_run=True)
    assert r2["sessions"] == 1 and "Files explored: c.py, d.py" in r2["prompt"]


def test_duplicate_questions_skipped_within_and_across_batches(tmp_path: Path):
    t1 = _session(tmp_path, "t1.jsonl", "How does the app detect quest completion?", ["a.py", "b.py"], "s1")
    t2 = _session(tmp_path, "t2.jsonl", "How does the app detect quest completion", ["a.py", "b.py"], "s2")
    r = record(tmp_path, [t1, t2], dry_run=True)
    assert r["sessions"] == 1 and r["skipped"][0]["reason"].startswith("duplicate")
    _save_done(tmp_path, ["s1"], ["How does the app detect quest completion?"])
    assert record(tmp_path, [t2], dry_run=True)["sessions"] == 0
    assert record(tmp_path, [t2], dry_run=True, force=True)["sessions"] == 1


def test_heavy_exploration_of_covered_files_is_recorded(tmp_path: Path):
    t = _session(tmp_path, "t.jsonl", "Trace the flow through main", ["a.py"], "s1")
    events = [json.loads(l) for l in t.read_text().splitlines()]
    events[1]["message"]["content"][:0] = [
        {"type": "tool_use", "name": "Grep", "input": {"pattern": f"p{i}", "path": "a.py"}} for i in range(6)]
    _write(t, events)
    r = record(tmp_path, [t], dry_run=True)
    assert r["sessions"] == 1

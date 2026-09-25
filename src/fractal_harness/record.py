"""Out-of-band claim recording: learn from finished sessions without touching them.

The Stop hook only appends the session's transcript path to `.fractal/queue.jsonl` (instant,
no model call). `fractal record` later reads queued transcripts, and runs one headless
Claude session that verifies and records claims about what those sessions had to explore.
Its cost is investment, never charged to the task that triggered it.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from .cache import STORE_DIR, ClaimCache
from .regions import affects
from .store import tokens

QUEUE = "queue.jsonl"
DONE = "recorded.json"
NO_HOOKS_ENV = "FRACTAL_NO_HOOKS"  # set for the recorder's own session so it never queues itself
# A session is worth recording only if it explored at least this many files that no injected
# claim covered. Below that, the cache already served it (a hit) or there was little to learn.
MIN_NEW_FILES = 2
# ...or made at least this many exploration calls (Read/Grep/Glob/Bash): injected claims
# cover whole files at best, so heavy searching inside "covered" files means they fell short.
MIN_EXPLORATION_CALLS = 6
EXPLORATION_TOOLS = {"Read", "Grep", "Glob", "Bash"}
# Questions at least this similar (token Jaccard) to an already-recorded one are skipped.
DUPLICATE_SIMILARITY = 0.6

PROMPT = """\
You maintain this repo's claim cache: short, checkable statements that let future sessions
answer questions without re-reading code. Below are recent sessions: the question asked,
the files the session had to read or search to answer it, and its final answer.

For each session, record the durable facts a future session would need to answer similar
questions WITHOUT exploring. Work like this:

1. `fractal query "<keywords>"` first; skip facts already covered by a verified claim.
2. Confirm each fact in the source yourself (Read/Grep). Never record something only
   because the session's answer says it.
3. Record with `fractal put "<claim>" --read <file> [--read <file>...] --probe '<json>'`.
   - One fact per claim, stated as a contract usable without opening the file.
   - `--read`: every file the claim depends on (globs allowed, e.g. 'app/lib/**/*.dart').
   - Probe: {"type":"grep","pattern":"<regex>","paths":["<glob>"],"expect":"present"|"absent"|{"count":n}|{"min":n}}
     grep is line-based; add "exclude":"<regex>" to skip lines (e.g. comments).
     {"type":"all","probes":[...]} when a claim has several checkable parts.
   - The probe must cover every checkable statement in the claim and must fail if the claim
     becomes false. If part of a claim cannot be probed, record that part separately without
     --probe (it is stored as trusted) or leave it out.
4. At most {max_per_session} claims per session. Rank by exploration saved: sessions are listed
   most expensive first, and a property a session spent many calls establishing (searching
   that something is never used, tracing which module owns a behavior) is the best
   candidate, because recording it removes that exploration from every future session.
5. If a session revealed a RULE the code follows and must keep following (a boundary, a
   forbidden call, a required element), propose it with
   `fractal propose "<rule>" --read <file> --probe '<json>'`, where the probe fails when the
   rule is violated and passes now. Proposals are reviewed by a human; never run
   `fractal accept` or `fractal reject`. Check existing rules first with `fractal invariants`.
Do not modify any files. Finish with a one-line summary.

Sessions:
{sessions}
"""


@dataclass
class Session:
    source: str
    session_id: str = ""
    prompt: str = ""
    answer: str = ""
    files: list[str] = field(default_factory=list)
    exploration_calls: int = 0


FILE_RE = re.compile(r"[\w./-]+\.[A-Za-z0-9]{1,8}")


def _rel(path: str, root: Path) -> str | None:
    p = Path(path)
    if p.is_absolute():
        try:
            p = p.resolve().relative_to(root)
        except ValueError:
            return None
    rel = p.as_posix().lstrip("./")
    return rel if (root / rel).is_file() else None


def parse_transcript(path: Path, root: Path) -> Session:
    """Reads a Claude Code transcript or `claude -p --output-format stream-json` output."""
    s = Session(source=str(path))
    files: dict[str, None] = {}
    for line in path.read_text(errors="replace").splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("session_id") and not s.session_id:
            s.session_id = ev["session_id"]
        msg = ev.get("message") or {}
        content = msg.get("content")
        if ev.get("type") == "user":
            if isinstance(content, str) and not s.prompt:
                s.prompt = content
            elif isinstance(content, list) and not s.prompt:
                texts = [b.get("text", "") for b in content if b.get("type") == "text"]
                if texts:
                    s.prompt = "\n".join(texts)
        elif ev.get("type") == "assistant" and isinstance(content, list):
            texts = [b.get("text", "") for b in content if b.get("type") == "text"]
            if texts:
                s.answer = "\n".join(texts)
            for b in content:
                if b.get("type") != "tool_use":
                    continue
                if b.get("name") in EXPLORATION_TOOLS:
                    s.exploration_calls += 1
                inp = b.get("input", {})
                for cand in [inp.get("file_path"), inp.get("path"), *FILE_RE.findall(json.dumps(inp))]:
                    rel = _rel(cand, root) if isinstance(cand, str) and cand else None
                    if rel:
                        files.setdefault(rel)
        elif ev.get("type") == "result" and ev.get("result"):
            s.answer = ev["result"]
    s.files = list(files)
    return s


def enqueue(root: Path, session_id: str, transcript: str) -> None:
    store = root / STORE_DIR
    if not store.is_dir():
        return
    with (store / QUEUE).open("a") as f:
        f.write(json.dumps({"session_id": session_id, "transcript": transcript, "ts": time.time()}) + "\n")


def _load_done(root: Path) -> dict:
    path = root / STORE_DIR / DONE
    if not path.exists():
        return {"sessions": [], "questions": []}
    data = json.loads(path.read_text())
    if isinstance(data, list):  # earlier format: session ids only
        return {"sessions": data, "questions": []}
    return data


def _save_done(root: Path, sessions: list[str], questions: list[str]) -> None:
    done = _load_done(root)
    done["sessions"] = sorted(set(done["sessions"]) | set(sessions))
    done["questions"] = done["questions"] + [q for q in questions if q not in done["questions"]]
    (root / STORE_DIR / DONE).write_text(json.dumps(done, indent=1))


def pending(root: Path) -> list[dict]:
    """Latest queue entry per session, excluding sessions already recorded."""
    q = root / STORE_DIR / QUEUE
    if not q.exists():
        return []
    done = set(_load_done(root)["sessions"])
    latest: dict[str, dict] = {}
    for line in q.read_text().splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e["session_id"] not in done:
            latest[e["session_id"]] = e
    return list(latest.values())


def _similar(a: str, b: str) -> float:
    ta, tb = set(tokens(a)), set(tokens(b))
    return len(ta & tb) / len(ta | tb) if ta and tb else 0.0


def _covered_files(cache: ClaimCache, session_id: str) -> set[str] | None:
    """Files covered by claims the hook injected into this session; None if it never ran."""
    events = [e for e in cache.store.events("inject") if e.get("session_id") == session_id]
    if not events:
        return None
    reads = [r for ev in events for i in ev.get("ids", []) if (e := cache.get(i)) for r in e.reads]
    return set(reads)


def measure(root: Path, sessions: list[Session]) -> None:
    """Log per-session waste metrics: re-reads of files the injected facts and rules covered.

    A re-read of a file an injected verified fact covered, or that an injected enforced rule
    guarantees, is badput the injection was meant to prevent.
    """
    from .cache import dependencies
    cache = ClaimCache(root)
    try:
        for s in sessions:
            if not s.session_id:
                continue
            events = [e for e in cache.store.events("inject") if e.get("session_id") == s.session_id]
            fact_ids = {i for ev in events for i in ev.get("ids", [])}
            rule_ids = {i for ev in events for i in ev.get("rules", [])}
            fact_files = {r for i in fact_ids if (c := cache.get(i)) for r in c.reads}
            rule_deps = [d for i in rule_ids if (c := cache.get(i)) for d in dependencies(c)]
            cache.store.log("session_stats", None, session_id=s.session_id, files_explored=len(s.files),
                            exploration_calls=s.exploration_calls,
                            fact_rereads=sum(affects(list(fact_files), f) for f in s.files) if fact_files else 0,
                            rule_rereads=sum(affects(rule_deps, f) for f in s.files) if rule_deps else 0)
    finally:
        cache.close()


def triage(root: Path, sessions: list[Session], force: bool = False) -> tuple[list[Session], list[dict]]:
    """Split sessions into (worth recording, skipped with reasons)."""
    keep, skipped = [], []
    prior = _load_done(root)["questions"]
    cache = ClaimCache(root)
    try:
        for s in sessions:
            if not s.files or not s.answer:
                skipped.append({"source": s.source, "reason": "nothing explored"})
                continue
            covered = _covered_files(cache, s.session_id) if s.session_id else None
            new_files = [f for f in s.files if not (covered and affects(list(covered), f))]
            heavy = s.exploration_calls >= MIN_EXPLORATION_CALLS
            if not force and len(new_files) < MIN_NEW_FILES and not heavy:
                reason = "hit: cache covered the exploration" if covered else "explored too little"
                skipped.append({"source": s.source, "reason": reason, "new_files": len(new_files),
                                "exploration_calls": s.exploration_calls})
                continue
            similar = max((_similar(s.prompt, q) for q in prior + [k.prompt for k in keep]), default=0.0)
            if not force and s.prompt and similar >= DUPLICATE_SIMILARITY:
                skipped.append({"source": s.source, "reason": f"duplicate question ({similar:.2f})"})
                continue
            if not heavy:
                s.files = new_files  # point the recorder at what the cache did not cover
            keep.append(s)
    finally:
        cache.close()
    return keep, skipped


def record(root: Path, transcripts: list[Path] | None = None, model: str = "sonnet",
           max_per_session: int = 4, dry_run: bool = False, force: bool = False) -> dict:
    """Extract claims from queued (or given) transcripts in one headless session.

    Sessions the cache already served, sessions that explored little, and repeats of
    already-recorded questions are skipped; if none remain, no model call is made.
    """
    root = root.resolve()
    queued = [] if transcripts else pending(root)
    paths = transcripts or [Path(e["transcript"]) for e in queued]
    parsed = [parse_transcript(p, root) for p in paths if p.exists()]
    if not dry_run:
        measure(root, parsed)
    sessions, skipped = triage(root, parsed, force)
    queued_ids = [e["session_id"] for e in queued]
    if not sessions:
        if not dry_run:
            _save_done(root, queued_ids, [])
            if parsed:
                cache = ClaimCache(root)
                cache.store.log("record", None, sessions=0, skipped=len(skipped), claims_added=0, cost_usd=0.0)
                cache.close()
        return {"sessions": 0, "skipped": skipped, "claims_added": 0, "cost_usd": 0.0}

    blocks = []
    sessions = sorted(sessions, key=lambda s: -s.exploration_calls)   # most exploration saved first
    for i, s in enumerate(sessions, 1):
        blocks.append(f"### Session {i}\nQuestion: {s.prompt.strip()[:1500] or '(not recorded)'}\n"
                      f"Exploration calls: {s.exploration_calls}\n"
                      f"Files explored: {', '.join(s.files[:40])}\n"
                      f"Answer:\n{s.answer.strip()[:4000]}\n")
    prompt = PROMPT.replace("{max_per_session}", str(max_per_session)).replace("{sessions}", "\n".join(blocks))
    if dry_run:
        return {"sessions": len(sessions), "skipped": skipped, "prompt": prompt}

    cache = ClaimCache(root)
    before = {e.id for e in cache.store.all()}
    cache.close()
    cmd = ["claude", "-p", prompt, "--output-format", "json", "--model", model,
           "--strict-mcp-config", "--allowedTools", "Read", "Grep", "Glob", "Bash(fractal:*)",
           "--disallowedTools", "Edit", "Write", "NotebookEdit",
           "--no-session-persistence", "--max-turns", str(15 + 10 * len(sessions))]
    env = {**os.environ, NO_HOOKS_ENV: "1", "FRACTAL_ROOT": str(root)}
    proc = subprocess.run(cmd, cwd=root, capture_output=True, text=True, env=env, timeout=3600)
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError:
        result = {}
    cache = ClaimCache(root)
    added = [e for e in cache.store.all() if e.id not in before]
    cost = result.get("total_cost_usd") or 0.0
    cache.store.log("record", None, sessions=len(sessions), skipped=len(skipped),
                    claims_added=len(added), cost_usd=cost)
    cache.close()
    _save_done(root, queued_ids, [s.prompt for s in sessions if s.prompt])
    return {"sessions": len(sessions), "skipped": skipped, "claims_added": len(added), "cost_usd": cost,
            "by_status": {s: sum(e.status == s for e in added) for s in ("verified", "trusted", "failed")},
            "summary": (result.get("result") or proc.stderr[-300:]).strip()[-500:]}

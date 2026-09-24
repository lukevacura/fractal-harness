"""Claude Code hooks: push relevant claims into context instead of waiting for the agent to pull.

`fractal hook prompt` is a UserPromptSubmit hook. It ranks verified claims against the
prompt and returns the strong matches as additionalContext, so the agent starts with them:
no tool loading, no extra turn, no reliance on the agent choosing to query. On a miss it
outputs nothing, so the session is exactly what it would be without the cache.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from .cache import STORE_DIR, ClaimCache
from .store import Edge, tokens

MAX_CLAIMS = 6
# A claim is injected only if it matches at least MIN_MATCHES distinct prompt terms and its
# BM25 score is within RELATIVE_CUTOFF of the best match. Anything weaker is treated as a
# miss: nothing is injected, and the session is indistinguishable from one without the cache.
MIN_MATCHES = 2
RELATIVE_CUTOFF = 0.5

HEADER = (
    "Verified facts about this repo, from its claim cache, matching your prompt. Each was checked "
    "against the current source by a deterministic probe moments ago, so treat it as already "
    "confirmed: do not re-read or re-search the cited files to re-verify it. Read those files only "
    "for details a fact does not state."
)


def _line(e: Edge) -> str:
    pre = f"{{{e.pre}}} ⟹ " if e.pre else ""
    return f"- {pre}{e.post} (source: {', '.join(e.reads) or '-'})"


def select(root: Path, prompt: str, session_id: str | None = None, log: bool = True) -> list[Edge]:
    """The verified claims the hook would inject for `prompt` (empty = miss).

    Logs an `inject` event with the session id and claim ids, so `fractal record` can tell
    which sessions the cache already served.
    """
    if not (root / STORE_DIR / "edges.db").exists():
        return []  # repo not initialized; never create a store from a hook
    if len(tokens(prompt)) < MIN_MATCHES:
        return []
    cache = ClaimCache(root)
    try:
        cache.refresh()
        scored = cache.store.ranked(prompt, MIN_MATCHES)
        hits: list[Edge] = []
        if scored:
            best = scored[0][1]
            ids = [i for i, score, _ in scored if score <= best * RELATIVE_CUTOFF]  # bm25 is negative
            resolved = cache.resolve(ids)
            hits = [e for e in resolved if e.status == "verified"][:MAX_CLAIMS]
            # Claims this prompt wanted but that need repair: `fractal repair` fixes demanded ones.
            wanted = [e.id for e in resolved if e.repair and e.status in ("failed", "stale")]
            if log and wanted:
                cache.store.log("demand", None, session_id=session_id, ids=wanted)
        if log:
            cache.store.log("inject", None, session_id=session_id, claims=len(hits),
                            ids=[e.id for e in hits])
        return hits
    finally:
        cache.close()


def context_for(root: Path, prompt: str, session_id: str | None = None) -> str | None:
    hits = select(root, prompt, session_id)
    if not hits:
        return None
    return "\n".join([HEADER, *(_line(e) for e in hits)])


def _disabled() -> bool:
    from .record import NO_HOOKS_ENV
    return bool(os.environ.get(NO_HOOKS_ENV))


def stop_hook(stdin: str) -> None:
    """Stop hook: queue the session's transcript for `fractal record`. No model call, no output."""
    if _disabled():
        return
    try:
        from .record import enqueue
        data = json.loads(stdin or "{}")
        root = Path(os.environ.get("FRACTAL_ROOT") or os.environ.get("CLAUDE_PROJECT_DIR")
                    or data.get("cwd") or os.getcwd())
        if data.get("transcript_path") and data.get("session_id"):
            enqueue(root.resolve(), data["session_id"], data["transcript_path"])
    except Exception as e:
        print(f"fractal hook: {e}", file=sys.stderr)


def prompt_hook(stdin: str) -> str:
    """Returns the hook's stdout (possibly empty)."""
    if _disabled():
        return ""
    try:
        data = json.loads(stdin or "{}")
        root = Path(os.environ.get("FRACTAL_ROOT") or os.environ.get("CLAUDE_PROJECT_DIR")
                    or data.get("cwd") or os.getcwd())
        ctx = context_for(root.resolve(), data.get("prompt", ""), data.get("session_id"))
    except Exception as e:  # a broken cache must never block the user's prompt
        print(f"fractal hook: {e}", file=sys.stderr)
        return ""
    if not ctx:
        return ""
    return json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                              "additionalContext": ctx}})

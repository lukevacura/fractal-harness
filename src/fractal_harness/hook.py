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
            # Proposed or rejected rules are not facts and not (yet) rules: never inject them.
            hits = [e for e in resolved if e.status == "verified"
                    and (e.kind != "invariant" or e.enforced)][:MAX_CLAIMS]
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


EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
MAX_VIOLATION_LINES = 5


def _rel_in_repo(root: Path, path: str | None) -> str | None:
    if not path:
        return None
    p = Path(path)
    p = p if p.is_absolute() else root / p
    try:
        return p.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return None


def edit_violations(root: Path, rel: str) -> tuple[int, list[str]]:
    """Re-check the enforced invariants whose verdict depends on `rel`.

    Returns (rules checked, violation messages). Runs only those rules' probes (ms), updates
    their status in the store, and logs the check.
    """
    from . import probes
    from .cache import dependencies
    from .regions import affects
    cache = ClaimCache(root)
    try:
        rules = [e for e in cache.invariants(("enforced",)) if e.probe and affects(dependencies(e), rel)]
        messages = []
        for e in rules:
            result = probes.run(e.probe, cache.root)
            if not result.passed:
                where = []
                for f, line in result.matches[:MAX_VIOLATION_LINES]:
                    try:
                        text = (cache.root / f).read_text(errors="replace").splitlines()[line].strip()
                    except (OSError, IndexError):
                        text = ""
                    where.append(f"    {f}:{line + 1}: {text[:160]}")
                messages.append("\n".join([f"- [{e.id}] {e.post}", f"    probe: {result.detail}", *where]))
            cache.check(e.id)
        cache.store.log("edit_check", None, file=rel, rules=len(rules), violations=len(messages))
        return len(rules), messages
    finally:
        cache.close()


def edit_hook(stdin: str) -> tuple[int, str]:
    """PostToolUse hook for file edits: (exit code, stderr). Exit 2 feeds stderr back to the
    agent so it fixes a violation of an enforced invariant right away; the edit itself has
    already happened and is not undone."""
    if _disabled():
        return 0, ""
    try:
        data = json.loads(stdin or "{}")
        if data.get("tool_name") not in EDIT_TOOLS:
            return 0, ""
        root = Path(os.environ.get("FRACTAL_ROOT") or os.environ.get("CLAUDE_PROJECT_DIR")
                    or data.get("cwd") or os.getcwd()).resolve()
        if not (root / STORE_DIR / "edges.db").exists():
            return 0, ""
        inp = data.get("tool_input") or {}
        rel = _rel_in_repo(root, inp.get("file_path") or inp.get("notebook_path"))
        if rel is None:
            return 0, ""
        _, messages = edit_violations(root, rel)
    except Exception as e:  # a broken cache must never break the agent's edit loop
        return 0, f"fractal hook: {e}"
    if not messages:
        return 0, ""
    return 2, "\n".join([
        f"fractal: your edit to {rel} violates {len(messages)} enforced invariant(s):",
        *messages,
        "Fix the code so every rule holds. These rules are human-owned: do not edit, remove, or "
        "work around the claims or their probes. If a rule itself should change, stop and tell the user.",
    ])


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

"""Claude Code hooks: the invariant-alignment loop, plus claim injection and session queueing.

  prompt     (UserPromptSubmit) before acting: inject the enforced RULES that govern the code
             the task likely touches (repo-wide rules, rules governing files linked to the
             prompt), then verified FACTS matching the prompt, each with its evidence.
  pre-edit   (PreToolUse on Edit/Write/MultiEdit) while acting: apply the proposed edit in
             memory and run the enforced rules that scan that file; a violating edit is
             blocked before it is written.
  edit       (PostToolUse) backstop after any edit: re-check the rules that scan the file,
             including command probes the in-memory check cannot run.
  stop       (Stop) queue the session for `fractal record`.

A prompt with no governing rules and no matching facts injects nothing: without enforced
rules, a miss is identical to a session without the cache. Every hook fails open: a broken
cache never blocks the user or the agent.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from .cache import STORE_DIR, ClaimCache, dependencies
from .store import Edge, tokens

MAX_FACTS = 6
MAX_RULES = 6
MAX_REPO_RULES = 4           # repo-wide rules always shown; keep them few and short
MIN_MATCHES = 2              # a fact must share at least this many distinct terms with the prompt
RELATIVE_CUTOFF = 0.5        # ...and score within this fraction of the best match (bm25 is negative)
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
MAX_VIOLATION_LINES = 5

RULES_HEADER = (
    "RULES: enforced by this project's owner. Your edits are checked against them before they are "
    "written and at commit. Do not violate or work around them (never edit or remove the rules or "
    "their probes); if the task requires breaking one, stop and ask the user."
)
FACTS_HEADER = (
    "FACTS: verified against the current source by a deterministic probe moments ago. Treat them as "
    "confirmed: do not re-read or re-search the cited lines to re-verify them; read those files only "
    "for details a fact does not state."
)


# --- shared plumbing ---------------------------------------------------------------------

def _disabled() -> bool:
    from .record import NO_HOOKS_ENV
    return bool(os.environ.get(NO_HOOKS_ENV))


def _root(data: dict) -> Path:
    return Path(os.environ.get("FRACTAL_ROOT") or os.environ.get("CLAUDE_PROJECT_DIR")
                or data.get("cwd") or os.getcwd()).resolve()


def _has_store(root: Path) -> bool:
    return (root / STORE_DIR / "edges.db").exists()   # never create a store from a hook


def _rel_in_repo(root: Path, path: str | None) -> str | None:
    if not path:
        return None
    p = Path(path)
    p = p if p.is_absolute() else root / p
    try:
        return p.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return None


def evidence(e: Edge) -> str:
    """Why the claim holds right now: the line its probe matched, or the probe's result."""
    if e.anchors:
        a = e.anchors[0]
        text = a["block"][a.get("offset", 0)].strip() if a.get("block") else ""
        return f"{a['file']}:{a['line'] + 1}: {text[:140]}"
    return e.detail or "checked"


# --- prompt: rules first, then facts ------------------------------------------------------

def _linked_files(prompt: str, files: list[str], facts: list[Edge]) -> set[str]:
    """Files the task likely touches: named in the prompt (path or file name), or read by a matched fact."""
    low = prompt.lower()
    named = {f for f in files if f.lower() in low or (len(Path(f).name) >= 5 and Path(f).name.lower() in low)}
    return named | {r for e in facts for r in e.reads if not any(c in r for c in "*?[")}


def select_context(root: Path, prompt: str, session_id: str | None = None,
                   log: bool = True) -> tuple[list[Edge], list[Edge]]:
    """(rules, facts) the prompt hook would inject. Both empty = a miss."""
    from .regions import affects, governs, placement, tracked_files
    if not _has_store(root):
        return [], []
    cache = ClaimCache(root)
    try:
        cache.refresh()
        facts: list[Edge] = []
        if len(tokens(prompt)) >= MIN_MATCHES:
            scored = cache.store.ranked(prompt, MIN_MATCHES)
            if scored:
                best = scored[0][1]
                resolved = cache.resolve([i for i, score, _ in scored if score <= best * RELATIVE_CUTOFF])
                facts = [e for e in resolved if e.status == "verified" and e.kind != "invariant"][:MAX_FACTS]
                # Claims the prompt wanted but that need repair: `fractal repair` fixes demanded ones.
                wanted = [e.id for e in resolved if e.repair and e.status in ("failed", "stale")]
                if log and wanted:
                    cache.store.log("demand", None, session_id=session_id, ids=wanted)
        rules: list[Edge] = []
        enforced = cache.invariants(("enforced",))
        if enforced:
            cache.resolve([e.id for e in enforced if e.status == "stale"])
            enforced = [cache.get(e.id) for e in enforced]
            files = tracked_files(cache.root)
            linked = _linked_files(prompt, files, facts)
            place = {e.id: placement(dependencies(e), files) for e in enforced}
            repo_wide = [e for e in enforced if place[e.id] == ""][:MAX_REPO_RULES]
            governing = [e for e in enforced if e not in repo_wide and any(
                affects(dependencies(e), f) or governs(place[e.id], f) for f in linked)]
            rules = (repo_wide + governing)[:MAX_RULES]
        if log:
            cache.store.log("inject", None, session_id=session_id, claims=len(facts),
                            ids=[e.id for e in facts], rules=[e.id for e in rules])
        return rules, facts
    finally:
        cache.close()


def select(root: Path, prompt: str, session_id: str | None = None, log: bool = True) -> list[Edge]:
    """Everything the prompt hook would inject, rules first."""
    rules, facts = select_context(root, prompt, session_id, log)
    return rules + facts


def _rule_line(e: Edge) -> str:
    if e.status == "failed":
        return f"- {e.post}\n  ✗ VIOLATED NOW: {e.detail}"
    return f"- {e.post}\n  ✓ holds now: {evidence(e)}"


def _fact_line(e: Edge) -> str:
    pre = f"{{{e.pre}}} ⟹ " if e.pre else ""
    return f"- {pre}{e.post}\n  ✓ {evidence(e)}"


def context_for(root: Path, prompt: str, session_id: str | None = None) -> str | None:
    rules, facts = select_context(root, prompt, session_id)
    parts = []
    if rules:
        parts += [RULES_HEADER, *(_rule_line(e) for e in rules)]
    if facts:
        parts += ([""] if parts else []) + [FACTS_HEADER, *(_fact_line(e) for e in facts)]
    return "\n".join(parts) or None


def prompt_hook(stdin: str) -> str:
    """UserPromptSubmit: returns the hook's stdout (possibly empty)."""
    if _disabled():
        return ""
    try:
        data = json.loads(stdin or "{}")
        ctx = context_for(_root(data), data.get("prompt", ""), data.get("session_id"))
    except Exception as e:  # a broken cache must never block the user's prompt
        print(f"fractal hook: {e}", file=sys.stderr)
        return ""
    if not ctx:
        return ""
    return json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": ctx}})


# --- edits: checked before they are written, and after ------------------------------------

def _violation(e: Edge, detail: str, matches: list[tuple[str, int]], text_of) -> str:
    where = []
    for f, line in matches[:MAX_VIOLATION_LINES]:
        lines = text_of(f)
        where.append(f"    {f}:{line + 1}: {lines[line].strip()[:160] if line < len(lines) else ''}")
    return "\n".join([f"- [{e.id}] {e.post}", f"    probe: {detail}", *where])


def _governing_rules(cache: ClaimCache, rel: str) -> list[Edge]:
    from .regions import affects
    return [e for e in cache.invariants(("enforced",)) if e.probe and affects(dependencies(e), rel)]


def edit_violations(root: Path, rel: str) -> tuple[int, list[str]]:
    """After an edit: re-check the enforced rules that scan `rel`; update their status."""
    from . import probes
    cache = ClaimCache(root)
    try:
        rules = _governing_rules(cache, rel)
        messages = []
        for e in rules:
            result = probes.run(e.probe, cache.root)
            if not result.passed:
                messages.append(_violation(e, result.detail, result.matches, lambda f: _read_lines(cache.root, f)))
            cache.check(e.id)
        cache.store.log("edit_check", None, file=rel, rules=len(rules), violations=len(messages))
        return len(rules), messages
    finally:
        cache.close()


def _read_lines(root: Path, rel: str) -> list[str]:
    try:
        return (root / rel).read_text(errors="replace").splitlines()
    except OSError:
        return []


def proposed_content(root: Path, rel: str, tool: str, inp: dict) -> str | None:
    """The file's content if the edit were applied, or None if it can't be computed (the tool
    will fail or report on its own)."""
    if tool == "Write":
        return inp.get("content")
    try:
        text = (root / rel).read_text(errors="replace")
    except OSError:
        return None
    edits = inp.get("edits") if tool == "MultiEdit" else [inp]
    for ed in edits or []:
        old, new = ed.get("old_string"), ed.get("new_string", "")
        if not old or old not in text:
            return None
        text = text.replace(old, new) if ed.get("replace_all") else text.replace(old, new, 1)
    return text


def pre_edit_violations(root: Path, rel: str, content: str) -> tuple[int, list[str]]:
    """Before an edit: run the enforced rules that scan `rel` against the edited content in memory."""
    from . import probes
    cache = ClaimCache(root)
    try:
        rules = [e for e in _governing_rules(cache, rel) if _in_memory_checkable(e.probe)]
        overlay = {rel: content}
        messages = []
        for e in rules:
            result = probes.run(e.probe, cache.root, overlay)
            if not result.passed:
                text_of = lambda f: content.splitlines() if f == rel else _read_lines(cache.root, f)
                messages.append(_violation(e, result.detail, result.matches, text_of))
        cache.store.log("pre_edit_check", None, file=rel, rules=len(rules), violations=len(messages))
        return len(rules), messages
    finally:
        cache.close()


def _in_memory_checkable(probe: dict) -> bool:
    if probe.get("type") == "all":
        return all(_in_memory_checkable(p) for p in probe["probes"])
    return probe.get("type") == "grep"


def pre_edit_hook(stdin: str) -> tuple[int, str]:
    """PreToolUse: (exit code, stderr). Exit 2 blocks the edit before it is written and tells
    the agent which rule it would break."""
    if _disabled():
        return 0, ""
    try:
        data = json.loads(stdin or "{}")
        tool = data.get("tool_name")
        if tool not in ("Edit", "Write", "MultiEdit"):
            return 0, ""
        root = _root(data)
        if not _has_store(root):
            return 0, ""
        inp = data.get("tool_input") or {}
        rel = _rel_in_repo(root, inp.get("file_path"))
        content = proposed_content(root, rel, tool, inp) if rel else None
        if content is None:
            return 0, ""
        _, messages = pre_edit_violations(root, rel, content)
    except Exception as e:  # never break the agent's edit loop
        return 0, f"fractal hook: {e}"
    if not messages:
        return 0, ""
    return 2, "\n".join([
        f"fractal: this edit to {rel} was NOT applied: it would violate {len(messages)} enforced invariant(s):",
        *messages,
        "Change the edit so every rule holds. These rules are human-owned: do not edit, remove, or work "
        "around the rules or their probes. If the task requires breaking a rule, stop and ask the user.",
    ])


def edit_hook(stdin: str) -> tuple[int, str]:
    """PostToolUse backstop: (exit code, stderr). Exit 2 feeds the violation back to the agent;
    the edit has already happened and is not undone."""
    if _disabled():
        return 0, ""
    try:
        data = json.loads(stdin or "{}")
        if data.get("tool_name") not in EDIT_TOOLS:
            return 0, ""
        root = _root(data)
        if not _has_store(root):
            return 0, ""
        inp = data.get("tool_input") or {}
        rel = _rel_in_repo(root, inp.get("file_path") or inp.get("notebook_path"))
        if rel is None:
            return 0, ""
        _, messages = edit_violations(root, rel)
    except Exception as e:
        return 0, f"fractal hook: {e}"
    if not messages:
        return 0, ""
    return 2, "\n".join([
        f"fractal: your edit to {rel} violates {len(messages)} enforced invariant(s):",
        *messages,
        "Fix the code so every rule holds. These rules are human-owned: do not edit, remove, or "
        "work around the rules or their probes. If a rule itself should change, stop and tell the user.",
    ])


def stop_hook(stdin: str) -> None:
    """Stop: queue the session's transcript for `fractal record`. No model call, no output."""
    if _disabled():
        return
    try:
        from .record import enqueue
        data = json.loads(stdin or "{}")
        if data.get("transcript_path") and data.get("session_id"):
            enqueue(_root(data), data["session_id"], data["transcript_path"])
    except Exception as e:
        print(f"fractal hook: {e}", file=sys.stderr)

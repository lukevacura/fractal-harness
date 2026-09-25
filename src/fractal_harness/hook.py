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
MAX_CONTEXT_CHARS = 3000     # injected text is overhead too: rules are kept first, facts trimmed

RULES_HEADER = (
    "RULES: enforced by this project's owner. Every edit is checked against them before it is written "
    "and again at commit, so they are GUARANTEED to hold: you do not need to re-verify them (no "
    "grepping or test runs to confirm them) and you do not need defensive code or tests for what they "
    "guarantee. Do not violate or work around them (never edit or remove the rules or their probes); "
    "if the task requires breaking one, stop and ask the user."
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
    import re
    m = re.match(r"0 matching lines in (\d+) files \(expected count 0\)", e.detail or "")
    if m:
        return f"no violations in {m.group(1)} files"
    return e.detail or "checked"


# --- prompt: rules first, then facts ------------------------------------------------------

def _linked_files(prompt: str, files: list[str], facts: list[Edge]) -> set[str]:
    """Files the task likely touches: named in the prompt (path or file name), or read by a matched fact."""
    import re
    low = prompt.lower()
    named = {f for f in files if f.lower() in low or (len(Path(f).name) >= 5 and Path(f).name.lower() in low)}
    # paths the task mentions that may not exist yet ("a new module ledger/alerts.py")
    mentioned = {m.strip("`'\".,:;()") for m in re.findall(r"[\w.-]+(?:/[\w.-]+)+", prompt)}
    return named | mentioned | {r for e in facts for r in e.reads if not any(c in r for c in "*?[")}


def select_context(root: Path, prompt: str, session_id: str | None = None,
                   log: bool = True) -> tuple[list[Edge], list[Edge]]:
    """(rules, facts) the prompt hook would inject. Both empty = a miss."""
    from .regions import affects, governs, placement, tracked_files
    if not _has_store(root):
        return [], []
    cache = ClaimCache(root, run_slow=False)
    try:
        cache.refresh()
        facts: list[Edge] = []
        if len(tokens(prompt)) >= MIN_MATCHES:
            scored = cache.store.ranked(prompt, MIN_MATCHES)
            if scored:
                best = scored[0][1]
                resolved = cache.resolve([i for i, score, _ in scored if score <= best * RELATIVE_CUTOFF])
                facts = [e for e in resolved if e.status == "verified"
                         and e.kind not in ("invariant", "behavior")][:MAX_FACTS]
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


def render(rules: list[Edge], facts: list[Edge], budget: int = MAX_CONTEXT_CHARS) -> str:
    """Rules first, then facts, within a character budget (facts are dropped first)."""
    def build(rs: list[Edge], fs: list[Edge]) -> str:
        parts = [RULES_HEADER, *(_rule_line(e) for e in rs)] if rs else []
        if fs:
            parts += ([""] if parts else []) + [FACTS_HEADER, *(_fact_line(e) for e in fs)]
        return "\n".join(parts)
    while facts and len(build(rules, facts)) > budget:
        facts = facts[:-1]
    while len(rules) > 1 and len(build(rules, facts)) > budget:
        rules = rules[:-1]
    return build(rules, facts)


def _has_graph(root: Path) -> bool:
    cache = ClaimCache(root, run_slow=False)
    try:
        return any(e.kind == "behavior" for e in cache.store.all())
    finally:
        cache.close()


def context_for(root: Path, prompt: str, session_id: str | None = None) -> str | None:
    """With a behavioral graph, the task map (regions, contracts, assumptions, rules); otherwise
    rules and facts. Either way, nothing on a miss."""
    if not _has_store(root):
        return None
    if _has_graph(root):
        from .taskmap import render as render_map, task_map
        text = render_map(task_map(root, prompt))
        rules, facts = [], []
    else:
        rules, facts = select_context(root, prompt, session_id)
        text = render(rules, facts)
    if text and _has_store(root):
        cache = ClaimCache(root, run_slow=False)
        try:
            cache.store.log("context", None, session_id=session_id, chars=len(text), rules=len(rules),
                            facts=len(facts))
        finally:
            cache.close()
    return text or None


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
    """Enforced rules with fast probes that scan `rel` (slow, behavioral ones run at turn end)."""
    from .probes import is_slow
    from .regions import affects
    return [e for e in cache.invariants(("enforced",))
            if e.probe and not is_slow(e.probe) and affects(dependencies(e), rel)]


def edit_violations(root: Path, rel: str) -> tuple[int, list[str]]:
    """After an edit: re-check the enforced rules that scan `rel`; update their status."""
    from . import probes
    cache = ClaimCache(root, run_slow=False)
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


def pre_edit_violations(root: Path, rel: str, content: str, generated: int = 0) -> tuple[int, list[str]]:
    """Before an edit: run the enforced rules that scan `rel` against the edited content in memory."""
    from . import probes
    cache = ClaimCache(root, run_slow=False)
    try:
        rules = [e for e in _governing_rules(cache, rel) if _in_memory_checkable(e.probe)]
        overlay = {rel: content}
        messages = []
        for e in rules:
            result = probes.run(e.probe, cache.root, overlay)
            if not result.passed:
                text_of = lambda f: content.splitlines() if f == rel else _read_lines(cache.root, f)
                messages.append(_violation(e, result.detail, result.matches, text_of))
        cache.store.log("pre_edit_check", None, file=rel, rules=len(rules), violations=len(messages),
                        blocked_chars=generated if messages else 0)
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
        generated = len(inp.get("content") or "") if tool == "Write" else sum(
            len(e.get("new_string") or "") for e in (inp.get("edits") if tool == "MultiEdit" else [inp]) or [])
        _, messages = pre_edit_violations(root, rel, content, generated)
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


def stop_hook(stdin: str) -> tuple[int, str]:
    """Stop: queue the session for `fractal record`, then run the turn-end behavioral gate.

    The gate runs the behavioral claims (tests, test-backed invariants) whose dependencies the
    uncommitted changes touch. On a regression (a claim that passed at its last verdict and
    fails now) or a violated enforced invariant it exits 2, so the agent keeps working and
    fixes it before handing back. After MAX_GATE_BLOCKS blocks in a session it reports
    instead of blocking, so a stubborn failure can't trap the agent in a loop.
    """
    if _disabled():
        return 0, ""
    try:
        from .behavior import MAX_GATE_BLOCKS, gate
        data = json.loads(stdin or "{}")
        root = _root(data)
        if data.get("transcript_path") and data.get("session_id"):
            from .record import enqueue
            enqueue(root, data["session_id"], data["transcript_path"])
        if not _has_store(root):
            return 0, ""
        result = gate(root)
        problems = result["regressions"] + result["violations"]
        if not problems:
            return 0, ""
        cache = ClaimCache(root, run_slow=False)
        try:
            sid = data.get("session_id")
            blocks = sum(1 for e in cache.store.events("gate_block") if e.get("session_id") == sid)
            blocking = blocks < MAX_GATE_BLOCKS
            if blocking:
                cache.store.log("gate_block", None, session_id=sid, problems=[e.id for e in problems])
        finally:
            cache.close()
        lines = [f"fractal turn-end check: {len(problems)} behavioral contract(s) broken by the uncommitted changes "
                 f"({result['checked']} checked in {result['seconds']}s):"]
        for e in result["violations"]:
            lines.append(f"- ENFORCED RULE violated [{e.id}]: {e.post}\n    {result['results'][e.id].detail}")
        for e in result["regressions"]:
            lines.append(f"- REGRESSION [{e.id}] {e.probe['file']}: passed before, fails now\n"
                         f"    {result['results'][e.id].detail}")
        if blocking:
            lines.append("Fix the code so these pass before finishing. Enforced rules are human-owned: never edit "
                         "them. A failing test may only be changed if the behavior change is intended by the task; "
                         "say so explicitly. If you can't resolve it, stop and explain to the user.")
            return 2, "\n".join(lines)
        lines.append("(Not blocking: this session already hit the turn-end gate twice. Tell the user what is still broken.)")
        return 0, "\n".join(lines)
    except Exception as e:
        return 0, f"fractal hook: {e}"

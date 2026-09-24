"""`fractal init` / `fractal doctor`: wire a target repo up for Claude Code. Idempotent."""

from __future__ import annotations

import json
import shutil
from importlib import resources
from pathlib import Path

from .cache import STORE_DIR, ClaimCache

SERVER = "fractal-claims"   # the MCP server earlier versions registered; init removes it
SKILL = "fractal-onboard"
PROMPT_HOOK = "fractal hook prompt"
STOP_HOOK = "fractal hook stop"
EDIT_HOOK = "fractal hook edit"
EDIT_MATCHER = "Edit|Write|MultiEdit|NotebookEdit"
PRE_EDIT_HOOK = "fractal hook pre-edit"
PRE_EDIT_MATCHER = "Edit|Write|MultiEdit"
CLI_PERMISSION = "Bash(fractal:*)"
# Accepting or rejecting a rule is a human decision: deny these to agents even though the
# rest of the CLI is allowed (deny wins over allow).
HUMAN_ONLY = ["Bash(fractal accept:*)", "Bash(fractal reject:*)"]
# Marker of the CLAUDE.md block earlier versions wrote; init now removes it. Standing
# instructions change agent behavior even on a cache miss, so all guidance travels inside
# the injected context instead.
BEGIN, END = "<!-- fractal-harness:begin -->", "<!-- fractal-harness:end -->"


def _template(name: str) -> str:
    return resources.files("fractal_harness").joinpath("templates", name).read_text()


def _load_json(path: Path) -> dict:
    if not path.exists() or not path.read_text().strip():
        return {}
    return json.loads(path.read_text())


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def _add_unique(items: list, value: object) -> bool:
    if value in items:
        return False
    items.append(value)
    return True


def _has_hook(settings: dict, event: str, command: str) -> bool:
    return any(h.get("command") == command
               for g in settings.get("hooks", {}).get(event, []) for h in g.get("hooks", []))


GIT_HOOK = """#!/bin/sh
# Installed by `fractal init --git-hook`: fail the commit when a claimed invariant is violated.
command -v fractal >/dev/null 2>&1 || exit 0
exec fractal check
"""


def _install_git_hook(root: Path) -> str | None:
    import subprocess
    try:
        hooks = subprocess.run(["git", "rev-parse", "--git-path", "hooks"], cwd=root,
                               capture_output=True, text=True, check=True).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "git hook   skipped (not a git repo)"
    path = (root / hooks / "pre-commit").resolve()
    if path.exists():
        if "fractal check" in path.read_text():
            return None
        return f"git hook   skipped: {path} exists; add `fractal check` to it yourself"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(GIT_HOOK)
    path.chmod(0o755)
    return f"git hook   {path} runs `fractal check`"


def init(root: Path, settings: bool = True, git_hook: bool = False) -> list[str]:
    """Set up `root`; returns a line per change made.

    A session that misses the cache is identical to one without it: no CLAUDE.md text, no MCP
    tools, and a skill that only runs when invoked. Earlier versions' CLAUDE.md block and
    fractal-claims MCP server are removed.
    """
    done: list[str] = []

    ClaimCache(root).close()
    done.append(f"store      {STORE_DIR}/edges.db")

    gitignore = root / ".gitignore"
    lines = gitignore.read_text().splitlines() if gitignore.exists() else []
    if not any(l.strip().rstrip("/") == STORE_DIR for l in lines):
        with gitignore.open("a") as f:
            if lines and lines[-1].strip():
                f.write("\n")
            f.write(f"{STORE_DIR}/\n")
        done.append(f".gitignore +{STORE_DIR}/")

    mcp_path = root / ".mcp.json"
    m = _load_json(mcp_path)
    servers = m.get("mcpServers", {})
    if SERVER in servers:
        del servers[SERVER]
        if not servers and set(m) <= {"mcpServers"}:
            mcp_path.unlink()
            done.append(f".mcp.json  removed (only held {SERVER})")
        else:
            _write_json(mcp_path, m)
            done.append(f".mcp.json  -server {SERVER}")

    skill_path = root / ".claude" / "skills" / SKILL / "SKILL.md"
    skill = _template("onboard_skill.md")
    if not skill_path.exists() or skill_path.read_text() != skill:
        skill_path.parent.mkdir(parents=True, exist_ok=True)
        skill_path.write_text(skill)
        done.append(f"skill      .claude/skills/{SKILL}/SKILL.md (/{SKILL}, user-invoked only)")

    if settings:
        settings_path = root / ".claude" / "settings.json"
        s = _load_json(settings_path)
        before = json.dumps(s, sort_keys=True)
        allow = s.setdefault("permissions", {}).setdefault("allow", [])
        _add_unique(allow, CLI_PERMISSION)
        deny = s["permissions"].setdefault("deny", [])
        for rule in HUMAN_ONLY:
            _add_unique(deny, rule)
        enabled = s.get("enabledMcpjsonServers", [])
        if SERVER in enabled:
            enabled.remove(SERVER)
            if not enabled:
                del s["enabledMcpjsonServers"]
        if f"mcp__{SERVER}" in allow:
            allow.remove(f"mcp__{SERVER}")
        for event, command, matcher in (("UserPromptSubmit", PROMPT_HOOK, None), ("Stop", STOP_HOOK, None),
                                        ("PreToolUse", PRE_EDIT_HOOK, PRE_EDIT_MATCHER),
                                        ("PostToolUse", EDIT_HOOK, EDIT_MATCHER)):
            if not _has_hook(s, event, command):
                group = {"hooks": [{"type": "command", "command": command}]}
                if matcher:
                    group = {"matcher": matcher, **group}
                s.setdefault("hooks", {}).setdefault(event, []).append(group)
        if json.dumps(s, sort_keys=True) != before:
            _write_json(settings_path, s)
            done.append("settings   .claude/settings.json (prompt hook injects rules and facts, pre/post-edit "
                        "hooks enforce invariants, stop hook queues sessions, allow the fractal CLI except "
                        "accept/reject)")

    claude_md = root / "CLAUDE.md"
    text = claude_md.read_text() if claude_md.exists() else ""
    if BEGIN in text and END in text:
        start, end = text.index(BEGIN), text.index(END) + len(END)
        head, tail = text[:start].strip("\n"), text[end:].strip("\n")
        new = "\n\n".join(part for part in (head, tail) if part)
        claude_md.write_text(new + "\n" if new else "")
        done.append("CLAUDE.md  removed block from an earlier fractal version")

    if git_hook and (msg := _install_git_hook(root)):
        done.append(msg)

    return done


def doctor(root: Path) -> list[tuple[bool, str]]:
    """Checks that `root` is ready; returns (ok, message) pairs."""
    checks: list[tuple[bool, str]] = []
    exe = shutil.which("fractal")
    checks.append((exe is not None, f"`fractal` on PATH: {exe or 'not found (uv tool install <harness path>)'}"))

    skill = root / ".claude" / "skills" / SKILL / "SKILL.md"
    checks.append((skill.exists(), f"skill /{SKILL} installed"))
    if skill.exists():
        checks.append((skill.read_text() == _template("onboard_skill.md"),
                       f"skill /{SKILL} up to date (re-run `fractal init` if not)"))

    try:
        s = _load_json(root / ".claude" / "settings.json")
    except json.JSONDecodeError:
        s = {}
    checks.append((_has_hook(s, "UserPromptSubmit", PROMPT_HOOK), "UserPromptSubmit hook injects claims"))
    checks.append((_has_hook(s, "Stop", STOP_HOOK), "Stop hook queues sessions for `fractal record`"))
    checks.append((_has_hook(s, "PreToolUse", PRE_EDIT_HOOK), "PreToolUse hook blocks edits that would violate rules"))
    checks.append((_has_hook(s, "PostToolUse", EDIT_HOOK), "PostToolUse hook re-checks rules after edits"))
    deny = s.get("permissions", {}).get("deny", [])
    checks.append((all(r in deny for r in HUMAN_ONLY), "agents are denied `fractal accept`/`reject`"))
    md = root / "CLAUDE.md"
    checks.append((not (md.exists() and BEGIN in md.read_text()), "CLAUDE.md has no fractal block"))

    gi = root / ".gitignore"
    ignored = gi.exists() and any(l.strip().rstrip("/") == STORE_DIR for l in gi.read_text().splitlines())
    checks.append((ignored, f"{STORE_DIR}/ gitignored"))

    cache = ClaimCache(root)
    try:
        s = cache.stats()
    finally:
        cache.close()
    checks.append((True, f"store: {s['edges']} claims {s['by_status']}"))
    return checks

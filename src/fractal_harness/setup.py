"""`fractal init` / `fractal doctor`: wire a target repo up for Claude Code. Idempotent."""

from __future__ import annotations

import json
import os
import shutil
from importlib import resources
from pathlib import Path

from .cache import STORE_DIR, ClaimCache

SERVER = "fractal-claims"   # the MCP server earlier versions registered; init removes it
SKILL = "fractal-onboard"
WORK_SKILL = "fractal-work"
VERIFY_SKILL = "fractal-verify"
PROMPT_HOOK = "fractal hook prompt"
STOP_HOOK = "fractal hook stop"
EDIT_HOOK = "fractal hook edit"
EDIT_MATCHER = "Edit|Write|MultiEdit|NotebookEdit"
PRE_EDIT_HOOK = "fractal hook pre-edit"
PRE_EDIT_MATCHER = "Edit|Write|MultiEdit"
AGENT_HOOK = "fractal hook agent"
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


LOCAL_SETTINGS = ".claude/settings.local.json"
# Everything fractal puts in a repo's working tree, and so must never be committed: the store, the
# per-user settings carrying its hooks, and (legacy) skills it used to install into the repo.
KEEP_OUT = (f"{STORE_DIR}/", LOCAL_SETTINGS)
LEGACY_SKILL_DIRS = (SKILL, WORK_SKILL, VERIFY_SKILL)


def config_dir() -> Path:
    """The user-level Claude config dir, where skills install (outside every repo)."""
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def _git_path(root: Path, what: str) -> Path | None:
    import subprocess
    try:
        out = subprocess.run(["git", "rev-parse", "--git-path", what], cwd=root, capture_output=True,
                             text=True, check=True).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    return (root / out).resolve()


def forbidden_staged(paths: list[str]) -> list[str]:
    """Staged paths that are fractal's own files: they must never be committed."""
    return [p for p in paths if p.startswith(f"{STORE_DIR}/") or p == LOCAL_SETTINGS
            or any(p.startswith(f".claude/skills/{d}/") for d in LEGACY_SKILL_DIRS)]


def _is_ours(command: str) -> bool:
    return command.startswith("fractal hook ")


def _strip_ours(s: dict) -> bool:
    """Remove fractal's hooks and permissions from a (shared) settings dict; True if anything went."""
    changed = False
    for event in list(s.get("hooks", {})):
        groups = []
        for g in s["hooks"][event]:
            kept = [h for h in g.get("hooks", []) if not _is_ours(h.get("command", ""))]
            changed |= len(kept) != len(g.get("hooks", []))
            if kept:
                groups.append({**g, "hooks": kept})
        if groups:
            s["hooks"][event] = groups
        else:
            del s["hooks"][event]
    if not s.get("hooks", True):
        del s["hooks"]
    perms = s.get("permissions", {})
    for key, ours in (("allow", [CLI_PERMISSION]), ("deny", HUMAN_ONLY)):
        for rule in ours:
            if rule in perms.get(key, []):
                perms[key].remove(rule)
                changed = True
        if key in perms and not perms[key]:
            del perms[key]
    if "permissions" in s and not s["permissions"]:
        del s["permissions"]
    return changed


def _add_unique(items: list, value: object) -> bool:
    if value in items:
        return False
    items.append(value)
    return True


def _has_hook(settings: dict, event: str, command: str) -> bool:
    return any(h.get("command") == command
               for g in settings.get("hooks", {}).get(event, []) for h in g.get("hooks", []))


COMMIT_MSG_HOOK = """#!/bin/sh
# Installed by `fractal init --git-hook`: fail the commit when its message violates an enforced rule.
command -v fractal >/dev/null 2>&1 || exit 0
exec fractal check --message "$1"
"""
GIT_HOOK = """#!/bin/sh
# Installed by `fractal init --git-hook`: fail the commit when a claimed invariant is violated.
command -v fractal >/dev/null 2>&1 || exit 0
exec fractal check
"""


def _install_git_hook(root: Path) -> list[str]:
    import subprocess
    try:
        hooks = subprocess.run(["git", "rev-parse", "--git-path", "hooks"], cwd=root,
                               capture_output=True, text=True, check=True).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ["git hook   skipped (not a git repo)"]
    done = []
    for name, body, command in (("pre-commit", GIT_HOOK, "fractal check"),
                                ("commit-msg", COMMIT_MSG_HOOK, "fractal check --message")):
        path = (root / hooks / name).resolve()
        if path.exists():
            if command not in path.read_text():
                done.append(f"git hook   skipped: {path} exists; add `{command}` to it yourself")
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        path.chmod(0o755)
        done.append(f"git hook   {path} runs `{command}`")
    return done


def init(root: Path, settings: bool = True, git_hook: bool = False) -> list[str]:
    """Set up `root`; returns a line per change made.

    A session that misses the cache is identical to one without it: no CLAUDE.md text, no MCP
    tools, and a skill that only runs when invoked. Earlier versions' CLAUDE.md block and
    fractal-claims MCP server are removed.
    """
    done: list[str] = []

    ClaimCache(root).close()
    done.append(f"store      {STORE_DIR}/edges.db")

    exclude = _git_path(root, "info/exclude")   # local to this clone: no tracked file is edited
    if exclude is not None:
        have = exclude.read_text().splitlines() if exclude.exists() else []
        missing = [k for k in KEEP_OUT if k not in {l.strip() for l in have}]
        if missing:
            exclude.parent.mkdir(parents=True, exist_ok=True)
            with exclude.open("a") as f:
                if have and have[-1].strip():
                    f.write("\n")
                f.writelines(f"{k}\n" for k in missing)
            done.append(f"exclude    .git/info/exclude +{', '.join(missing)} (local; .gitignore untouched)")

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

    for name, template in ((SKILL, "onboard_skill.md"), (WORK_SKILL, "work_skill.md"),
                           (VERIFY_SKILL, "verify_skill.md")):
        skill_path = config_dir() / "skills" / name / "SKILL.md"
        skill = _template(template)
        if not skill_path.exists() or skill_path.read_text() != skill:
            skill_path.parent.mkdir(parents=True, exist_ok=True)
            skill_path.write_text(skill)
            done.append(f"skill      {skill_path} (/{name}, user-level, user-invoked only)")
        legacy = root / ".claude" / "skills" / name
        if legacy.is_dir():   # an earlier version installed it in the repo
            shutil.rmtree(legacy)
            done.append(f"skill      removed {legacy.relative_to(root)} from the repo (git sees a deletion)")

    if settings:
        shared_path = root / ".claude" / "settings.json"
        shared = _load_json(shared_path)
        before_shared = json.dumps(shared, sort_keys=True)
        enabled = shared.get("enabledMcpjsonServers", [])
        if SERVER in enabled:
            enabled.remove(SERVER)
            if not enabled:
                del shared["enabledMcpjsonServers"]
        legacy_allow = shared.get("permissions", {}).get("allow", [])
        if f"mcp__{SERVER}" in legacy_allow:
            legacy_allow.remove(f"mcp__{SERVER}")
        had_ours = _strip_ours(shared)    # earlier versions wired the hooks into the shared file
        if json.dumps(shared, sort_keys=True) != before_shared:
            _write_json(shared_path, shared)
            done.append("settings   .claude/settings.json" + (": fractal hooks and permissions moved out "
                        "(commit this removal)" if had_ours else ": legacy MCP entries removed"))

        local_path = root / LOCAL_SETTINGS
        s = _load_json(local_path)
        before = json.dumps(s, sort_keys=True)
        allow = s.setdefault("permissions", {}).setdefault("allow", [])
        _add_unique(allow, CLI_PERMISSION)
        deny = s["permissions"].setdefault("deny", [])
        for rule in HUMAN_ONLY:
            _add_unique(deny, rule)
        for event, command, matcher in (("UserPromptSubmit", PROMPT_HOOK, None), ("Stop", STOP_HOOK, None),
                                        ("PreToolUse", PRE_EDIT_HOOK, PRE_EDIT_MATCHER),
                                        ("SubagentStart", AGENT_HOOK, None),
                                        ("PostToolUse", EDIT_HOOK, EDIT_MATCHER)):
            if not _has_hook(s, event, command):
                group = {"hooks": [{"type": "command", "command": command}]}
                if matcher:
                    group = {"matcher": matcher, **group}
                s.setdefault("hooks", {}).setdefault(event, []).append(group)
        if json.dumps(s, sort_keys=True) != before:
            _write_json(local_path, s)
            done.append(f"settings   {LOCAL_SETTINGS} (untracked: prompt/subagent hooks inject rules and facts, "
                        "pre/post-edit hooks enforce invariants, stop hook queues sessions, allow the fractal "
                        "CLI except accept/reject)")

    claude_md = root / "CLAUDE.md"
    text = claude_md.read_text() if claude_md.exists() else ""
    if BEGIN in text and END in text:
        start, end = text.index(BEGIN), text.index(END) + len(END)
        head, tail = text[:start].strip("\n"), text[end:].strip("\n")
        new = "\n\n".join(part for part in (head, tail) if part)
        claude_md.write_text(new + "\n" if new else "")
        done.append("CLAUDE.md  removed block from an earlier fractal version")

    if git_hook:
        done += _install_git_hook(root)

    return done


def _worktree_checks(root: Path) -> list[tuple[bool, str]]:
    """Per worktree: uncommitted changes against its own branch head, and leftover proptest-regressions/."""
    import subprocess
    def git(cwd: str, *args: str) -> str:
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True).stdout
    trees, cur = [], {}
    for line in git(str(root), "worktree", "list", "--porcelain").splitlines() + [""]:
        if not line:
            if cur.get("worktree"):
                trees.append(cur)
            cur = {}
        else:
            k, _, v = line.partition(" ")
            cur[k] = v
    out, bad = [], 0
    for t in trees:
        path, name = t["worktree"], t.get("branch", "detached").removeprefix("refs/heads/")
        dirty = [l for l in git(path, "status", "--porcelain", "--untracked-files=no").splitlines() if l.strip()]
        leftovers = sorted({l for l in git(path, "ls-files", "--cached", "--others", "--exclude-standard").splitlines()
                            if "proptest-regressions/" in l})
        if dirty:
            bad += 1
            out.append((False, f"worktree {path} ({name}): {len(dirty)} uncommitted change(s) against its branch head, "
                               f"e.g. {dirty[0][3:]}"))
        if leftovers:
            bad += 1
            out.append((False, f"worktree {path} ({name}): leftover {leftovers[0]}" +
                               (f" (+{len(leftovers) - 1} more)" if len(leftovers) > 1 else "")))
    if len(trees) > 1 or bad:
        out.append((bad == 0, f"{len(trees)} worktree(s), {'all clean' if not bad else f'{bad} problem(s)'}"))
    return out


def doctor(root: Path) -> list[tuple[bool, str]]:
    """Checks that `root` is ready; returns (ok, message) pairs."""
    checks: list[tuple[bool, str]] = []
    exe = shutil.which("fractal")
    checks.append((exe is not None, f"`fractal` on PATH: {exe or 'not found (uv tool install <harness path>)'}"))

    skill = config_dir() / "skills" / SKILL / "SKILL.md"
    checks.append((skill.exists(), f"skill /{SKILL} installed (user-level)"))
    if skill.exists():
        checks.append((skill.read_text() == _template("onboard_skill.md"),
                       f"skill /{SKILL} up to date (re-run `fractal init` if not)"))

    try:
        s = _load_json(root / LOCAL_SETTINGS)
    except json.JSONDecodeError:
        s = {}
    checks.append((_has_hook(s, "UserPromptSubmit", PROMPT_HOOK), "UserPromptSubmit hook injects claims"))
    checks.append((_has_hook(s, "Stop", STOP_HOOK), "Stop hook queues sessions for `fractal record`"))
    checks.append((_has_hook(s, "PreToolUse", PRE_EDIT_HOOK), "PreToolUse hook blocks edits that would violate rules"))
    checks.append((_has_hook(s, "SubagentStart", AGENT_HOOK), "SubagentStart hook gives started subagents the rules and facts"))
    checks.append((_has_hook(s, "PostToolUse", EDIT_HOOK), "PostToolUse hook re-checks rules after edits"))
    deny = s.get("permissions", {}).get("deny", [])
    checks.append((all(r in deny for r in HUMAN_ONLY), "agents are denied `fractal accept`/`reject`"))
    md = root / "CLAUDE.md"
    checks.append((not (md.exists() and BEGIN in md.read_text()), "CLAUDE.md has no fractal block"))

    import subprocess
    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)
    if git("rev-parse", "--git-dir").returncode == 0:
        for k in KEEP_OUT:
            checks.append((git("check-ignore", "-q", k.rstrip("/") + ("/x" if k.endswith("/") else "")).returncode == 0,
                           f"{k} excluded from git"))
        tracked = [p for p in git("ls-files", "--", STORE_DIR, LOCAL_SETTINGS, *(
            f".claude/skills/{d}" for d in LEGACY_SKILL_DIRS)).stdout.splitlines() if p]
        checks.append((not tracked, "no fractal files tracked by git" + (f": {tracked[:3]}" if tracked else "")))
        shared = _load_json(root / ".claude" / "settings.json") if (root / ".claude" / "settings.json").exists() else {}
        checks.append((not any(_has_hook(shared, ev, c) for ev, c in (
            ("UserPromptSubmit", PROMPT_HOOK), ("Stop", STOP_HOOK), ("PreToolUse", PRE_EDIT_HOOK),
            ("SubagentStart", AGENT_HOOK), ("PostToolUse", EDIT_HOOK))),
            "no fractal hooks in the shared .claude/settings.json (re-run `fractal init` to move them)"))

    if git("rev-parse", "--git-dir").returncode == 0:
        checks += _worktree_checks(root)

    cache = ClaimCache(root)
    try:
        s = cache.stats()
    finally:
        cache.close()
    checks.append((True, f"store: {s['edges']} claims {s['by_status']}"))
    return checks

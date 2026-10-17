import json
import os
import subprocess
from pathlib import Path

from fractal_harness.setup import BEGIN, END, SERVER, SKILL, doctor, forbidden_staged, init


def _settings(root: Path) -> dict:
    return json.loads((root / ".claude" / "settings.local.json").read_text())


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True).stdout


def _skill(name: str = SKILL) -> Path:
    return Path(os.environ["CLAUDE_CONFIG_DIR"]) / "skills" / name / "SKILL.md"


def test_init_default_leaves_misses_untouched(tmp_path: Path):
    _git(tmp_path, "init", "-q")
    init(tmp_path)
    assert (tmp_path / ".fractal" / "edges.db").exists()
    assert not (tmp_path / ".gitignore").exists()        # no tracked file is edited
    assert ".fractal/" in (tmp_path / ".git" / "info" / "exclude").read_text()
    assert not (tmp_path / ".mcp.json").exists()          # no MCP tools by default
    assert not (tmp_path / "CLAUDE.md").exists()          # no standing instructions
    assert "disable-model-invocation: true" in _skill().read_text()
    assert not (tmp_path / ".claude" / "skills").exists()   # skills live outside the repo
    s = _settings(tmp_path)
    assert s["permissions"]["allow"] == ["Bash(fractal:*)"]
    assert s["permissions"]["deny"] == ["Bash(fractal accept:*)", "Bash(fractal reject:*)"]
    assert s["hooks"]["PostToolUse"][0] == {"matcher": "Edit|Write|MultiEdit|NotebookEdit",
                                            "hooks": [{"type": "command", "command": "fractal hook edit"}]}
    assert s["hooks"]["PreToolUse"][0] == {"matcher": "Edit|Write|MultiEdit",
                                           "hooks": [{"type": "command", "command": "fractal hook pre-edit"}]}
    assert s["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"] == "fractal hook prompt"
    assert s["hooks"]["Stop"][0]["hooks"][0]["command"] == "fractal hook stop"
    assert "enabledMcpjsonServers" not in s


def test_init_is_idempotent(tmp_path: Path):
    _git(tmp_path, "init", "-q")
    init(tmp_path)
    snapshot = {p: p.read_bytes() for p in tmp_path.rglob("*")
                if p.is_file() and ".fractal" not in p.parts and ".git" not in p.parts}
    assert init(tmp_path) == ["store      .fractal/edges.db"]
    assert {p: p.read_bytes() for p in snapshot} == snapshot


def test_init_removes_legacy_mcp_registration(tmp_path: Path):
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {SERVER: {"command": "fractal"}}}))
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(json.dumps(
        {"enabledMcpjsonServers": [SERVER], "permissions": {"allow": [f"mcp__{SERVER}"]}}))
    init(tmp_path)
    assert not (tmp_path / ".mcp.json").exists()
    shared = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert shared == {"permissions": {}} or "enabledMcpjsonServers" not in shared
    assert f"mcp__{SERVER}" not in json.dumps(shared)


def test_init_preserves_existing_config(tmp_path: Path):
    _git(tmp_path, "init", "-q")
    (tmp_path / ".gitignore").write_text("node_modules")
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {"other": {"command": "x"}, SERVER: {}}}))
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(json.dumps({"permissions": {"allow": ["Bash(ls)"]}}))
    init(tmp_path)
    assert (tmp_path / ".gitignore").read_text() == "node_modules"
    assert json.loads((tmp_path / ".mcp.json").read_text())["mcpServers"] == {"other": {"command": "x"}}
    assert json.loads((tmp_path / ".claude" / "settings.json").read_text())["permissions"]["allow"] == ["Bash(ls)"]
    assert _settings(tmp_path)["permissions"]["allow"] == ["Bash(fractal:*)"]


def test_init_removes_block_from_earlier_versions(tmp_path: Path):
    (tmp_path / "CLAUDE.md").write_text(f"# Project\n\nNotes.\n\n{BEGIN}\nold text\n{END}\n\nMore.\n")
    changes = init(tmp_path)
    assert any("CLAUDE.md" in c for c in changes)
    assert (tmp_path / "CLAUDE.md").read_text() == "# Project\n\nNotes.\n\nMore.\n"


def test_init_no_settings(tmp_path: Path):
    init(tmp_path, settings=False)
    assert not (tmp_path / ".claude" / "settings.local.json").exists()


def test_doctor_after_init(tmp_path: Path):
    _git(tmp_path, "init", "-q")
    init(tmp_path)
    results = {msg.split(":")[0]: ok for ok, msg in doctor(tmp_path)}
    assert all(ok for key, ok in results.items() if not key.startswith("`fractal` on PATH"))


def test_init_moves_earlier_in_repo_files_out(tmp_path: Path):
    _git(tmp_path, "init", "-q")
    (tmp_path / ".claude" / "skills" / SKILL).mkdir(parents=True)
    (tmp_path / ".claude" / "skills" / SKILL / "SKILL.md").write_text("old")
    (tmp_path / ".claude" / "settings.json").write_text(json.dumps({
        "permissions": {"allow": ["Bash(ls)", "Bash(fractal:*)"], "deny": ["Bash(fractal accept:*)"]},
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "fractal hook stop"},
                                      {"type": "command", "command": "mine"}]}],
                  "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "fractal hook prompt"}]}]}}))
    init(tmp_path)
    shared = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert shared == {"permissions": {"allow": ["Bash(ls)"]},
                      "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "mine"}]}]}}
    assert not (tmp_path / ".claude" / "skills" / SKILL).exists() and _skill().exists()
    assert _settings(tmp_path)["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"] == "fractal hook prompt"
    assert not any("tracked" in m or "shared" in m for ok, m in doctor(tmp_path) if not ok and "PATH" not in m)


def test_nothing_fractal_shows_in_git_status(tmp_path: Path):
    _git(tmp_path, "init", "-q")
    init(tmp_path)
    assert _git(tmp_path, "status", "--porcelain") == ""


def test_forbidden_staged_paths():
    assert forbidden_staged([".fractal/edges.db", ".claude/settings.local.json", "src/a.py",
                             f".claude/skills/{SKILL}/SKILL.md", ".claude/settings.json"]) == [
        ".fractal/edges.db", ".claude/settings.local.json", f".claude/skills/{SKILL}/SKILL.md"]


def test_verify_skill_is_installed_and_its_example_records(tmp_path: Path):
    import re
    from fractal_harness.cli import main
    from fractal_harness.setup import VERIFY_SKILL
    _git(tmp_path, "init", "-q")
    init(tmp_path)
    text = _skill(VERIFY_SKILL).read_text()
    assert "disable-model-invocation: true" in text and f"name: {VERIFY_SKILL}" in text
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "x.rs").write_text("".join(f"l{i}\n" for i in range(80)))
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "c")
    example = re.search(r"<<'JSON'\n(.*?)\n\s*JSON", text, re.S).group(1)
    (tmp_path / "v.json").write_text(example)
    assert main(["--root", str(tmp_path), "ledger", "record", "n", "--json", str(tmp_path / "v.json")]) == 0
    assert main(["--root", str(tmp_path), "verify-plan", "n"]) == 0


def test_doctor_flags_dirty_worktrees_and_leftover_proptest_regressions(tmp_path: Path):
    import subprocess
    def git(cwd, *a):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=cwd, check=True, capture_output=True)
    main = tmp_path / "main"
    main.mkdir()
    git(main, "init", "-q")
    (main / "a.txt").write_text("1\n")
    git(main, "add", "-A")
    git(main, "commit", "-qm", "c1")
    wt = tmp_path / "wt"
    git(main, "worktree", "add", "-q", "-b", "node-x", str(wt))
    msgs = [m for ok, m in doctor(main) if "worktree" in m]
    assert msgs == ["2 worktree(s), all clean"]
    (wt / "a.txt").write_text("2\n")
    (wt / "proptest-regressions").mkdir()
    (wt / "proptest-regressions" / "x.txt").write_text("seed\n")
    results = [(ok, m) for ok, m in doctor(main) if "worktree" in m]
    assert any(not ok and "1 uncommitted" in m and "node-x" in m for ok, m in results)
    assert any(not ok and "leftover proptest-regressions/x.txt" in m for ok, m in results)
    assert results[-1] == (False, "2 worktree(s), 2 problem(s)")

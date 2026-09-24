import json
from pathlib import Path

from fractal_harness.setup import BEGIN, END, SERVER, SKILL, doctor, init


def _settings(root: Path) -> dict:
    return json.loads((root / ".claude" / "settings.json").read_text())


def test_init_default_leaves_misses_untouched(tmp_path: Path):
    init(tmp_path)
    assert (tmp_path / ".fractal" / "edges.db").exists()
    assert ".fractal/" in (tmp_path / ".gitignore").read_text()
    assert not (tmp_path / ".mcp.json").exists()          # no MCP tools by default
    assert not (tmp_path / "CLAUDE.md").exists()          # no standing instructions
    skill = (tmp_path / ".claude" / "skills" / SKILL / "SKILL.md").read_text()
    assert "disable-model-invocation: true" in skill
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
    init(tmp_path)
    snapshot = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file() and ".fractal" not in p.parts}
    assert init(tmp_path) == ["store      .fractal/edges.db"]
    assert {p: p.read_bytes() for p in snapshot} == snapshot


def test_init_removes_legacy_mcp_registration(tmp_path: Path):
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {SERVER: {"command": "fractal"}}}))
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(json.dumps(
        {"enabledMcpjsonServers": [SERVER], "permissions": {"allow": [f"mcp__{SERVER}"]}}))
    init(tmp_path)
    assert not (tmp_path / ".mcp.json").exists()
    s = _settings(tmp_path)
    assert f"mcp__{SERVER}" not in s["permissions"]["allow"] and "enabledMcpjsonServers" not in s


def test_init_preserves_existing_config(tmp_path: Path):
    (tmp_path / ".gitignore").write_text("node_modules")
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {"other": {"command": "x"}, SERVER: {}}}))
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(json.dumps({"permissions": {"allow": ["Bash(ls)"]}}))
    init(tmp_path)
    assert (tmp_path / ".gitignore").read_text() == "node_modules\n.fractal/\n"
    assert json.loads((tmp_path / ".mcp.json").read_text())["mcpServers"] == {"other": {"command": "x"}}
    assert _settings(tmp_path)["permissions"]["allow"] == ["Bash(ls)", "Bash(fractal:*)"]


def test_init_removes_block_from_earlier_versions(tmp_path: Path):
    (tmp_path / "CLAUDE.md").write_text(f"# Project\n\nNotes.\n\n{BEGIN}\nold text\n{END}\n\nMore.\n")
    changes = init(tmp_path)
    assert any("CLAUDE.md" in c for c in changes)
    assert (tmp_path / "CLAUDE.md").read_text() == "# Project\n\nNotes.\n\nMore.\n"


def test_init_no_settings(tmp_path: Path):
    init(tmp_path, settings=False)
    assert not (tmp_path / ".claude" / "settings.json").exists()


def test_doctor_after_init(tmp_path: Path):
    init(tmp_path)
    results = {msg.split(":")[0]: ok for ok, msg in doctor(tmp_path)}
    assert all(ok for key, ok in results.items() if not key.startswith("`fractal` on PATH"))

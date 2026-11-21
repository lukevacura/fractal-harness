"""The rule store (.fractal/) cannot be written behind the CLI's back: the PreToolUse hook blocks
Edit/Write/MultiEdit/NotebookEdit into the store and Bash commands that would write to it."""

import json
from pathlib import Path

import pytest

from fractal_harness.cache import ClaimCache
from fractal_harness.hook import pre_edit_hook
from fractal_harness.setup import PRE_EDIT_HOOK, PRE_EDIT_MATCHER, doctor, init


@pytest.fixture(autouse=True)
def _no_project_env(monkeypatch):
    # The real hook runs under Claude Code with CLAUDE_PROJECT_DIR set: keep tests on their own tmp repo.
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.delenv("FRACTAL_ROOT", raising=False)
    monkeypatch.delenv("FRACTAL_NO_HOOKS", raising=False)


def _store(root: Path) -> Path:
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "a.py").write_text("x = 1\n")
    ClaimCache(root).close()                       # creates .fractal/edges.db
    return root


def _tool(root: Path, tool: str, inp: dict, cwd: Path | None = None) -> tuple[int, str]:
    return pre_edit_hook(json.dumps({"tool_name": tool, "cwd": str(cwd or root), "tool_input": inp}))


def _bash(root: Path, command: str, cwd: Path | None = None) -> tuple[int, str]:
    return _tool(root, "Bash", {"command": command}, cwd)


def _blocked(result: tuple[int, str]) -> bool:
    code, msg = result
    if code == 2:
        assert "fractal" in msg and "ask the user" in msg, msg
    return code == 2


# --- edit tools ------------------------------------------------------------------------

@pytest.mark.parametrize("tool,inp", [
    ("Edit", {"file_path": ".fractal/edges.db", "old_string": "a", "new_string": "b"}),
    ("Write", {"file_path": ".fractal/config.toml", "content": "x"}),
    ("MultiEdit", {"file_path": ".fractal/queue.jsonl", "edits": [{"old_string": "a", "new_string": "b"}]}),
    ("NotebookEdit", {"notebook_path": ".fractal/n.ipynb", "new_source": "x"}),
    ("Write", {"file_path": "src/../.fractal/new.json", "content": "{}"}),
])
def test_edit_tools_into_store_are_blocked(tmp_path, tool, inp):
    root = _store(tmp_path)
    inp = {**inp, **{k: str(root / v) for k, v in inp.items() if k in ("file_path", "notebook_path")}}
    code, msg = _tool(root, tool, inp)
    assert code == 2
    assert "only through the fractal CLI" in msg and "ask the user" in msg


def test_edit_via_symlink_into_store_is_blocked(tmp_path):
    root = _store(tmp_path)
    (root / "link").symlink_to(root / ".fractal")
    assert _blocked(_tool(root, "Write", {"file_path": str(root / "link" / "edges.db"), "content": "x"}))


def test_edit_relative_path_from_subdir_is_blocked(tmp_path):
    root = _store(tmp_path)
    assert _blocked(_tool(root, "Write", {"file_path": "../.fractal/x", "content": "x"}, cwd=root / "src"))


def test_edit_outside_store_is_allowed(tmp_path):
    root = _store(tmp_path)
    for rel in (".fractal-notes/x", "docs/.fractal.md", "src/a.py", "fractal/x"):
        assert _tool(root, "Write", {"file_path": str(root / rel), "content": "y = 2\n"}) == (0, ""), rel


# --- bash: blocked forms ---------------------------------------------------------------

BLOCKED = [
    'sqlite3 .fractal/edges.db "UPDATE edges SET status=\'enforced\'"',
    "sqlite3 .fractal/edges.db",                                   # interactive: could write
    "sqlite3 -batch .fractal/edges.db 'DELETE FROM edges'",
    "sqlite3 /tmp/x.db \"ATTACH '.fractal/edges.db' AS r; DELETE FROM r.edges\"",
    "rm -rf .fractal",
    "rm .fractal/edges.db",
    "rm -rf .",                                                     # removes the store with its parent
    "mv .fractal/edges.db /tmp/old.db",
    "mv /tmp/new.db .fractal/edges.db",
    "cp other.db .fractal/edges.db",
    "cp -r backup/ .fractal",
    "rsync -a backup/ .fractal/",
    "echo x | tee .fractal/edges.db",
    "dd if=/dev/zero of=.fractal/edges.db bs=1 count=1",
    "truncate -s 0 .fractal/edges.db",
    "touch .fractal/edges.db",
    "chmod 000 .fractal/edges.db",
    "ln -sf /tmp/x.db .fractal/edges.db",
    "echo x > .fractal/edges.db",
    "echo x >> .fractal/queue.jsonl",
    "echo x>.fractal/edges.db",
    "cat foo 2> .fractal/log",
    "sed -i '' 's/a/b/' .fractal/config.toml",
    "perl -i -pe 's/a/b/' .fractal/config.toml",
    "git checkout -- .fractal/edges.db",
    "git restore .fractal",
    "git clean -fdx",                                               # deletes ignored files: the store
    "python -c \"import sqlite3; sqlite3.connect('.fractal/edges.db').execute('DELETE FROM edges')\"",
    "python3 -c 'import shutil; shutil.rmtree(\".fractal\")'",
    "node -e \"require('fs').unlinkSync('.fractal/edges.db')\"",
    "ruby -e 'File.delete(\".fractal/edges.db\")'",
    "perl -e 'unlink \".fractal/edges.db\"'",
    "sh -c 'rm .fractal/edges.db'",
    "bash -c \"echo x > .fractal/edges.db\"",
    "ls && rm .fractal/edges.db",
    "true; rm .fractal/edges.db",
    "false || rm .fractal/edges.db",
    "echo ok\nrm .fractal/edges.db",
    "(cd /tmp && rm -rf $OLDPWD/.fractal) ; (rm .fractal/edges.db)",
    "echo $(rm .fractal/edges.db)",
    "echo \"$(rm -rf .fractal)\"",
    "echo `rm .fractal/edges.db`",
    "cd .fractal && rm edges.db",
    "sudo rm .fractal/edges.db",
    "FOO=1 rm .fractal/edges.db",
    "find .fractal -name '*.db' -delete",
    "ls .fractal | xargs rm",
    "rm -rf .fr*",
    "shred -u .fractal/edges.db",
    "D=.fractal; rm $D/edges.db",
    "export D=.fractal && rm -rf ${D}",
    "sqlite3 -readonly .fractal/edges.db '.backup .fractal/copy.db'",
    "mv a.txt .fractal/",
    "for f in .fractal/*; do rm $f; done",
]


@pytest.mark.parametrize("command", BLOCKED)
def test_bash_store_writes_are_blocked(tmp_path, command):
    root = _store(tmp_path)
    code, msg = _bash(root, command)
    assert code == 2, command
    assert "only through the fractal CLI" in msg and "ask the user" in msg


def test_bash_absolute_and_dotdot_paths_are_blocked(tmp_path):
    root = _store(tmp_path)
    assert _blocked(_bash(root, f"rm {root}/.fractal/edges.db"))
    assert _blocked(_bash(root, "rm src/../.fractal/edges.db"))


def test_bash_symlinked_store_path_is_blocked(tmp_path):
    root = _store(tmp_path)
    (root / "link").symlink_to(root / ".fractal")
    assert _blocked(_bash(root, "rm link/edges.db"))


# --- bash: allowed forms ---------------------------------------------------------------

ALLOWED = [
    "cat .fractal/edges.db",
    "ls -la .fractal",
    "grep -r rule .fractal",
    "sqlite3 -readonly .fractal/edges.db 'SELECT count(*) FROM edges'",
    "sqlite3 'file:.fractal/edges.db?mode=ro' 'SELECT * FROM edges'",
    "cp .fractal/edges.db /tmp/backup.db",                          # copying out reads the store
    "dd if=.fractal/edges.db of=/tmp/backup.db",
    "head .fractal/queue.jsonl | wc -l",
    "cat .fractal/queue.jsonl > /tmp/q.jsonl",
    "sed -n 1p .fractal/config.toml",
    "find .fractal -name '*.db'",
    "git status",
    "fractal rm abc123",
    "fractal doctor --root .fractal/..",                            # the CLI is the sanctioned door
    "uv run fractal list .fractal",
    "fractal list > /tmp/rules.txt",
    "cd src && fractal put 'x' --reads a.py",
    "rm -rf build dist",
    "rm -rf .fractal-notes",
    "echo x > .fractal-notes/x",
    "echo x > docs/.fractal.md",
    "cp a.txt .fractal-notes/",
    "python -c 'print(1)'",
    "python -c 'import fractal_harness'",
    "sh -c 'echo hi > /tmp/x'",
    "echo hello",
    "pytest -q",
    "git clean -n -x",
    "sqlite3 -readonly .fractal/edges.db .tables",
    "mv a.txt .",
    "D=build; rm -rf $D",
]


@pytest.mark.parametrize("command", ALLOWED)
def test_bash_read_only_and_unrelated_commands_are_allowed(tmp_path, command):
    root = _store(tmp_path)
    (root / "docs").mkdir()
    assert _bash(root, command) == (0, ""), command


def test_store_found_from_subdirectory_cwd(tmp_path):
    root = _store(tmp_path)
    sub = root / "src"
    assert _blocked(_bash(root, "rm ../.fractal/edges.db", cwd=sub))
    assert _blocked(_tool(root, "Write", {"file_path": "../.fractal/x", "content": "x"}, cwd=sub))
    assert _bash(sub, "cat ../.fractal/edges.db", cwd=sub) == (0, "")
    assert _bash(sub, "rm a.py", cwd=sub) == (0, "")


def test_without_a_store_everything_is_allowed(tmp_path):
    (tmp_path / "src").mkdir()
    for command in ("rm -rf .fractal", "echo x > .fractal/edges.db", "sqlite3 .fractal/edges.db 'DELETE FROM edges'"):
        assert _bash(tmp_path, command) == (0, ""), command
    assert _tool(tmp_path, "Write", {"file_path": str(tmp_path / ".fractal/x"), "content": "x"}) == (0, "")


def test_malformed_input_never_crashes(tmp_path):
    root = _store(tmp_path)
    assert pre_edit_hook("not json")[0] == 0
    assert _tool(root, "Bash", {})[0] == 0
    assert _tool(root, "Bash", {"command": None})[0] == 0
    assert _bash(root, "echo 'unterminated")[0] in (0, 2)       # unparseable: never an exception


def test_block_is_logged(tmp_path):
    root = _store(tmp_path)
    assert _blocked(_bash(root, "rm .fractal/edges.db"))
    c = ClaimCache(root)
    try:
        events = c.store.events("store_write_blocked")
    finally:
        c.close()
    assert events and events[-1]["command"] == "rm .fractal/edges.db"


def test_disabled_hooks_allow(tmp_path, monkeypatch):
    root = _store(tmp_path)
    monkeypatch.setenv("FRACTAL_NO_HOOKS", "1")
    assert _bash(root, "rm .fractal/edges.db") == (0, "")


# --- registration ----------------------------------------------------------------------

def _settings(root: Path) -> dict:
    return json.loads((root / ".claude" / "settings.local.json").read_text())


def test_matcher_covers_bash_and_notebook_edit():
    tools = PRE_EDIT_MATCHER.split("|")
    assert {"Edit", "Write", "MultiEdit", "NotebookEdit", "Bash"} <= set(tools)


def test_init_upgrades_an_old_pre_edit_matcher_and_doctor_notices(tmp_path):
    import subprocess
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    init(tmp_path)
    s = _settings(tmp_path)
    s["hooks"]["PreToolUse"][0]["matcher"] = "Edit|Write|MultiEdit"     # what earlier versions installed
    (tmp_path / ".claude" / "settings.local.json").write_text(json.dumps(s))
    checks = dict((msg, ok) for ok, msg in doctor(tmp_path))
    guard = [m for m in checks if "store" in m and "Bash" in m]
    assert guard and not checks[guard[0]]
    init(tmp_path)
    groups = _settings(tmp_path)["hooks"]["PreToolUse"]
    ours = [g for g in groups if any(h.get("command") == PRE_EDIT_HOOK for h in g["hooks"])]
    assert len(ours) == 1 and ours[0]["matcher"] == PRE_EDIT_MATCHER
    checks = dict((msg, ok) for ok, msg in doctor(tmp_path))
    assert checks[guard[0]]


# --- human-only CLI verbs (root decisions) ---------------------------------------------
# The CLI is the door, but some verbs are a human's decision. An agent's Bash cannot confirm on a real
# terminal; the hook also refuses the verbs (and the ways to fake a terminal) before they run.

def _human_blocked(result: tuple[int, str]) -> bool:
    code, msg = result
    if code == 2:
        assert "human decision" in msg and "root" in msg and "ask the user" in msg and "own terminal" in msg, msg
    return code == 2


HUMAN_BLOCKED = [
    "fractal accept r1",
    "fractal accept r1 r2",
    "fractal accept --as root r1",
    "fractal reject r1",
    "fractal reject --as n1 r1",
    "fractal override r1 --reason x",
    "fractal override r1 --by root --reason x",
    "fractal override r1 --by=root --reason x",
    "fractal override r1 --b root --reason x",                       # argparse prefix of --by
    "fractal override r1 --undo --reason x",
    "fractal conflict arbitrate c1 --upheld --reason x",
    "fractal conflict resolve c1 keep --reason x",
    "fractal conflict resolve c1 keep --reason x --as root",
    "fractal pin r1",
    "fractal pin --policy",
    "fractal accept --as n1 r1 --yes",                              # --yes in any fractal call
    "fractal accept --as n1 r1 --ye",                               # argparse prefix of --yes
    "fractal conflict escalate c1 --reason x --yes",
    "fractal invariants --yes",
    "fractal --root . accept r1",
    "fractal --root=. --json accept r1",
    "fractal --json reject r1",
    ".venv/bin/fractal accept r1",
    "/usr/local/bin/fractal accept r1",
    "./fractal reject r1",
    "python -m fractal_harness accept r1",
    "python3 -m fractal_harness.cli reject r1",
    "python3 -I -m fractal_harness.cli override r1 --reason x",
    "python -mfractal_harness.cli accept r1",
    "uv run fractal accept r1",
    "uv run --quiet fractal accept r1",
    "uv run --with pytest fractal accept r1",
    "uv run python -m fractal_harness.cli accept r1",
    "uvx fractal accept r1",
    "uvx --from fractal-harness fractal accept r1",
    "env FOO=1 fractal accept r1",
    "sudo fractal accept r1",
    "command fractal accept r1",
    "\\fractal accept r1",
    "'fractal' accept r1",
    "\"frac\"'tal' accept r1",
    "frac\\tal reject r1",
    "F=fractal; $F accept r1",
    "fractal invariants && fractal accept r1",
    "echo y | fractal accept r1",
    "yes | fractal accept r1",
    "fractal invariants\nfractal accept r1",
    "echo $(fractal accept r1)",
    "echo r1 | xargs fractal accept",
    "find . -name x -exec fractal accept {} \\;",
    "timeout 5 fractal accept r1",
    "sh -c 'fractal accept r1'",
    "bash -lc \"fractal reject r1\"",
    "zsh -c 'cd src && fractal accept r1'",
    "eval 'fractal accept r1'",
    "ssh -t localhost 'fractal accept r1'",
    "echo 'fractal accept r1' | sh",
    "bash <<'EOF'\nfractal reject r1\nEOF",
    # interpreter code that reaches the human verbs through the library
    "python -c \"from fractal_harness.cli import main; main(['accept', 'r1'])\"",
    "python3 -c 'import fractal_harness.cli as c; c.main([\"invariants\"])'",
    "python -c 'from fractal_harness import rules; rules.set_rule(None, \"r1\", \"enforced\")'",
    "python -c 'from fractal_harness.authority import human_set; human_set()'",
    "python -c 'from fractal_harness import authority as a; a.record_human_accept(1)'",
    "python -c 'import fractal_harness.authority as a; a.override(c, \"r1\", \"root\", \"x\")'",
    "python -c 'import fractal_harness.conflict as k; k.arbitrate(c, \"c1\")'",
    "python -c \"import subprocess; subprocess.run(['fractal', 'accept', 'r1'])\"",
    "node -e \"require('child_process').execSync('fractal accept r1')\"",
    "perl -e 'system(\"fractal reject r1\")'",
    "python - <<'EOF'\nfrom fractal_harness.cli import main\nmain(['accept', 'r1'])\nEOF",
    "python3 <<< 'from fractal_harness.cli import main; main([\"accept\", \"r1\"])'",
    "echo 'from fractal_harness.cli import main; main([\"reject\", \"r1\"])' | python3",
    # pty wrappers: a fake terminal to satisfy a confirmation
    "script -q /dev/null fractal accept r1",
    "script -q /dev/null fractal invariants",
    "script -qc 'fractal accept r1' /dev/null",
    "expect -c 'spawn fractal accept r1; send \"y\\r\"; interact'",
    "unbuffer fractal accept r1",
    "socat - EXEC:'fractal accept r1',pty,setsid,ctty",
    "ptyrun fractal accept r1",
    "tmux send-keys -t main 'fractal accept r1' Enter",
    "tmux new-session -d 'fractal accept r1'",
    "screen -S s -X stuff 'fractal accept r1\\n'",
    "python3 -c \"import pty; pty.spawn(['fractal', 'invariants'])\"",
    "python -c \"import pty, sys; pty.spawn([sys.executable, '-m', 'fractal_harness.cli', 'accept', 'r1'])\"",
    "perl -MIO::Pty -e 'my $p = IO::Pty->new; exec(\"fractal\", \"accept\", \"r1\")'",
    # lift: replacing a rule's probe is a human decision
    "fractal lift r1 --probe '{}' --reason x",
    "fractal --root . lift r1 --probe x --reason y",
    "python -c 'from fractal_harness import rules; rules.change_probe(c, \"r1\", {}, actor=\"human\", action=\"lift\", reason=\"\")'",
    # change: at root (no --by NODE) it is the human's
    "fractal change r1 --drop --reason x",
    "fractal change r1 --probe '{}' --by root --reason x",
]


@pytest.mark.parametrize("command", HUMAN_BLOCKED)
def test_human_only_verbs_are_blocked_in_agent_bash(tmp_path, command):
    root = _store(tmp_path)
    assert _human_blocked(_bash(root, command)), command


HUMAN_ALLOWED = [
    "fractal invariants",
    "fractal invariants --state proposed",
    "fractal --json invariants",
    "fractal --root . check",
    "fractal check --strict",
    "fractal propose 'no x in y' --grep x",
    "fractal accept --as n1 r1",
    "fractal accept --as=n1 r1 r2",
    "fractal accept r1 --as n1",
    "fractal override r1 --by n1 --reason x",
    "fractal override r1 --by=n1 --reason x",
    "fractal conflict list",
    "fractal conflict show c1",
    "fractal conflict raise r1 --reason x",
    "fractal conflict resolve c1 keep --reason x --as n1",
    "fractal conflict escalate c1 --reason x",
    "fractal conflict escalate c1 --reason x --as n1",
    "fractal status",
    "fractal ratchet r1",
    "fractal ratchet r1 r2 --by n1",
    "fractal change r1 --drop --by n1 --reason x",
    "fractal propose 'no x' --debt --probe '{}'",
    "python -m fractal_harness.cli invariants",
    "uv run fractal accept --as n1 r1",
    "python -c 'import fractal_harness'",
    "python -c 'import pty'",
    "python -m pytest -q tests/test_store_guard.py",
    "PYTHONPATH=src python -m pytest -q -k accept",
    "grep -rn 'def accept' src/fractal_harness",
    "echo fractal accept is for humans",
    "git commit -m 'fractal accept: document the human verb'",
    "script -q /tmp/typescript ls",
    "tmux ls",
    "rg 'pin' src",
]


@pytest.mark.parametrize("command", HUMAN_ALLOWED)
def test_agent_verbs_and_read_only_calls_are_allowed(tmp_path, command):
    root = _store(tmp_path)
    assert _bash(root, command) == (0, ""), command


def test_human_verbs_without_a_store_are_not_the_hooks_business(tmp_path):
    (tmp_path / "src").mkdir()
    assert _bash(tmp_path, "fractal accept r1") == (0, "")
    assert _bash(tmp_path, "script -q /dev/null fractal accept r1") == (0, "")


def test_human_verb_block_is_logged(tmp_path):
    root = _store(tmp_path)
    assert _human_blocked(_bash(root, "fractal accept r1"))
    c = ClaimCache(root)
    try:
        events = c.store.events("store_write_blocked")
    finally:
        c.close()
    assert events and events[-1]["command"] == "fractal accept r1"


# --- fractal.toml: [policy] decides who may accept rules ---------------------------------

def _config(root: Path) -> Path:
    (root / "fractal.toml").write_text('[policy]\naccept_policy = "human_only"\n')
    return root


def _policy_blocked(result: tuple[int, str]) -> bool:
    code, msg = result
    if code == 2:
        assert "fractal.toml" in msg and "[policy]" in msg and "human decision" in msg and "ask the user" in msg, msg
    return code == 2


@pytest.mark.parametrize("tool,inp", [
    ("Edit", {"file_path": "fractal.toml", "old_string": "human_only", "new_string": "local"}),
    ("Write", {"file_path": "fractal.toml", "content": "[policy]\naccept_policy = 'local'\n"}),
    ("MultiEdit", {"file_path": "fractal.toml", "edits": [{"old_string": "a", "new_string": "b"}]}),
    ("Write", {"file_path": "src/../fractal.toml", "content": ""}),
])
def test_edit_tools_on_fractal_toml_are_blocked(tmp_path, tool, inp):
    root = _config(_store(tmp_path))
    inp = {**inp, "file_path": str(root / inp["file_path"])}
    assert _policy_blocked(_tool(root, tool, inp))


def test_relative_edit_of_fractal_toml_from_subdir_is_blocked(tmp_path):
    root = _config(_store(tmp_path))
    assert _policy_blocked(_tool(root, "Write", {"file_path": "../fractal.toml", "content": ""}, cwd=root / "src"))


def test_creating_fractal_toml_is_blocked_when_a_store_exists(tmp_path):
    root = _store(tmp_path)
    assert _policy_blocked(_tool(root, "Write", {"file_path": str(root / "fractal.toml"), "content": "[policy]\n"}))


def test_other_toml_files_are_editable(tmp_path):
    root = _config(_store(tmp_path))
    for rel in ("pyproject.toml", "src/fractal.toml.bak", "docs/my-fractal.toml"):
        assert _tool(root, "Write", {"file_path": str(root / rel), "content": ""}) == (0, ""), rel


TOML_BLOCKED = [
    "echo 'accept_policy = \"local\"' >> fractal.toml",
    "printf '[policy]\\n' > fractal.toml",
    "sed -i '' 's/human_only/local/' fractal.toml",
    "sed -i.bak 's/human_only/local/' ./fractal.toml",
    "perl -pi -e 's/human_only/local/' fractal.toml",
    "cp /tmp/loose.toml fractal.toml",
    "mv /tmp/loose.toml fractal.toml",
    "rm fractal.toml",
    "echo x | tee fractal.toml",
    "truncate -s 0 fractal.toml",
    "git checkout HEAD~1 -- fractal.toml",
    "python -c \"open('fractal.toml', 'w').write('[policy]')\"",
    "cd src && echo x > ../fractal.toml",
    "ln -sf /tmp/loose.toml fractal.toml",
]


@pytest.mark.parametrize("command", TOML_BLOCKED)
def test_bash_writes_to_fractal_toml_are_blocked(tmp_path, command):
    root = _config(_store(tmp_path))
    assert _policy_blocked(_bash(root, command)), command


TOML_ALLOWED = [
    "cat fractal.toml",
    "grep accept_policy fractal.toml",
    "head -5 fractal.toml",
    "cp fractal.toml /tmp/backup.toml",
    "git diff fractal.toml",
    "git log -p -- fractal.toml",
    "cat fractal.toml | wc -l",
    "echo x > docs/fractal.toml.md",
    "echo x > my-fractal.toml",
]


@pytest.mark.parametrize("command", TOML_ALLOWED)
def test_reading_fractal_toml_is_allowed(tmp_path, command):
    root = _config(_store(tmp_path))
    (root / "docs").mkdir()
    assert _bash(root, command) == (0, ""), command


def test_fractal_toml_without_a_store_is_unaffected(tmp_path):
    (tmp_path / "src").mkdir()
    _config(tmp_path)
    assert _bash(tmp_path, "echo x >> fractal.toml") == (0, "")
    assert _tool(tmp_path, "Write", {"file_path": str(tmp_path / "fractal.toml"), "content": ""}) == (0, "")


def test_store_message_unchanged_for_store_writes(tmp_path):
    root = _config(_store(tmp_path))
    code, msg = _bash(root, "rm .fractal/edges.db")
    assert code == 2 and "only through the fractal CLI" in msg

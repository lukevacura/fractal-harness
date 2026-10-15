import subprocess
from pathlib import Path

from fractal_harness.cache import ClaimCache
from fractal_harness.check import check
from fractal_harness.setup import init


def _repo(tmp_path: Path) -> ClaimCache:
    (tmp_path / "app.py").write_text("import db\nPOOL = 10\n")
    c = ClaimCache(tmp_path)
    rule = c.put("app never imports legacy", ["*.py"], kind="invariant",
                 probe={"type": "grep", "pattern": r"^import legacy\b", "paths": ["*.py"], "expect": "absent"})
    c.set_rule(rule.id, "enforced")
    c.put("POOL is 10", ["app.py"], probe={"type": "grep", "pattern": r"^POOL = 10$", "paths": ["app.py"]})
    return c


def test_no_store_passes(tmp_path):
    assert check(tmp_path) == (0, "")


def test_clean_repo_passes(tmp_path):
    _repo(tmp_path).close()
    assert check(tmp_path)[0] == 0


def test_violated_invariant_fails(tmp_path):
    _repo(tmp_path).close()
    (tmp_path / "app.py").write_text("import db\nimport legacy\nPOOL = 10\n")
    code, report = check(tmp_path)
    assert code == 1 and "VIOLATION" in report and "never imports legacy" in report


def test_broken_knowledge_claim_warns_unless_strict(tmp_path):
    _repo(tmp_path).close()
    (tmp_path / "app.py").write_text("import db\nPOOL = 12\n")
    code, report = check(tmp_path)
    assert code == 0 and "need repair" in report
    assert check(tmp_path, strict=True)[0] == 1


def test_git_hook_installed_once_and_never_clobbers(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    changes = init(tmp_path, settings=False, git_hook=True)
    hook = tmp_path / ".git" / "hooks" / "pre-commit"
    assert any("git hook" in c for c in changes) and "fractal check" in hook.read_text()
    assert not any("git hook" in c for c in init(tmp_path, settings=False, git_hook=True))
    hook.write_text("#!/bin/sh\necho mine\n")
    assert any("skipped" in c for c in init(tmp_path, settings=False, git_hook=True))
    assert hook.read_text() == "#!/bin/sh\necho mine\n"


def test_edit_to_any_scanned_file_is_seen(tmp_path):
    (tmp_path / "app.py").write_text("import db\n")
    (tmp_path / "other.py").write_text("x = 1\n")
    c = ClaimCache(tmp_path)
    # declared reads name only app.py, but the probe scans every *.py file
    rule = c.put("nothing imports legacy", ["app.py"], kind="invariant",
                 probe={"type": "grep", "pattern": r"^import legacy\b", "paths": ["*.py"], "expect": "absent"})
    c.set_rule(rule.id, "enforced")
    c.close()
    (tmp_path / "other.py").write_text("import legacy\n")
    code, report = check(tmp_path)
    assert code == 1 and "VIOLATION" in report


def test_fixing_the_code_clears_a_violation(tmp_path):
    _repo(tmp_path).close()
    (tmp_path / "app.py").write_text("import db\nimport legacy\nPOOL = 10\n")
    assert check(tmp_path)[0] == 1
    (tmp_path / "app.py").write_text("import db\nPOOL = 10\n")
    assert check(tmp_path) == (0, "")


def test_proposed_rules_never_block(tmp_path):
    (tmp_path / "app.py").write_text("import legacy\n")
    c = ClaimCache(tmp_path)
    rule = c.put("never import legacy", ["*.py"], kind="invariant",
                 probe={"type": "grep", "pattern": r"^import legacy\b", "paths": ["*.py"], "expect": "absent"})
    assert rule.rule_state == "proposed"
    c.close()
    code, report = check(tmp_path)
    assert code == 0 and "proposed rule" in report and "would be violated" in report


def test_lifecycle_states_and_reassertion_keeps_human_decision(tmp_path):
    (tmp_path / "app.py").write_text("x = 1\n")
    c = ClaimCache(tmp_path)
    probe = {"type": "grep", "pattern": r"^import legacy\b", "paths": ["*.py"], "expect": "absent"}
    r = c.put("never import legacy", ["*.py"], kind="invariant", probe=probe)
    assert c.set_rule(r.id, "enforced").enforced
    assert c.put("never import legacy", ["*.py"], kind="invariant", probe=probe).enforced   # agent re-put
    c.set_rule(r.id, "rejected")
    assert c.put("never import legacy", ["*.py"], kind="invariant", probe=probe).rule_state == "rejected"
    k = c.put("x is one", ["app.py"], probe={"type": "grep", "pattern": "x = 1", "paths": ["app.py"]})
    assert k.rule_state is None
    import pytest
    from fractal_harness.cache import CacheError
    with pytest.raises(CacheError):
        c.set_rule(k.id, "enforced")
    assert [e.id for e in c.invariants(("rejected",))] == [r.id]
    c.close()


def test_repair_never_touches_enforced_rules(tmp_path):
    from fractal_harness.repair import candidates
    _repo(tmp_path).close()
    (tmp_path / "app.py").write_text("import db\nimport legacy\nPOOL = 12\n")
    c = ClaimCache(tmp_path)
    c.update()
    kinds = {e.kind for e, _ in candidates(c, everything=True)}
    assert kinds == {"knowledge"}          # the broken POOL fact, never the violated rule
    c.close()


def test_existing_invariants_migrate_as_enforced(tmp_path):
    import sqlite3
    (tmp_path / "a.py").write_text("x\n")
    c = ClaimCache(tmp_path)
    r = c.put("rule", ["a.py"], kind="invariant")
    c.close()
    db = sqlite3.connect(tmp_path / ".fractal" / "edges.db")
    db.execute("ALTER TABLE edges DROP COLUMN rule_state")      # simulate a pre-lifecycle store
    db.commit(); db.close()
    c = ClaimCache(tmp_path)
    assert c.get(r.id).rule_state == "enforced"
    c.close()


def _git(tmp_path: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)


def _rule(tmp_path: Path, text: str, probe: dict, enforce: bool = True):
    c = ClaimCache(tmp_path)
    r = c.put(text, [], kind="invariant", probe=probe)
    if enforce:
        c.set_rule(r.id, "enforced")
    c.close()


def test_staged_paths_probe_blocks_forbidden_path(tmp_path):
    _git(tmp_path, "init", "-q")
    _rule(tmp_path, "never stage proptest-regressions",
          {"type": "staged_paths", "forbid": "(^|/)proptest-regressions/"})
    (tmp_path / "ok.rs").write_text("x")
    (tmp_path / "proptest-regressions").mkdir()
    (tmp_path / "proptest-regressions" / "a.txt").write_text("x")
    _git(tmp_path, "add", "ok.rs")
    assert check(tmp_path)[0] == 0
    _git(tmp_path, "add", "proptest-regressions")
    code, report = check(tmp_path)
    assert code == 1 and "VIOLATION" in report and "proptest-regressions/a.txt" in report


def test_staged_paths_only_and_proposed_rule_does_not_block(tmp_path):
    _git(tmp_path, "init", "-q")
    _rule(tmp_path, "commits touch only src", {"type": "staged_paths", "only": ["src/**"]}, enforce=False)
    (tmp_path / "other.txt").write_text("x")
    _git(tmp_path, "add", "other.txt")
    code, report = check(tmp_path)
    assert code == 0 and "proposed rule" in report


def test_commit_message_probe(tmp_path):
    from fractal_harness.check import check_message
    _rule(tmp_path, "no invariant ids in commit messages",
          {"type": "commit_message", "forbid": r"\b[ABDNOST][0-9]+\b"})
    msg = tmp_path / "MSG"
    msg.write_text("Add ledger (D16)\n\n# Please enter the commit message\n")
    code, report = check_message(tmp_path, str(msg))
    assert code == 1 and "D16" in report
    msg.write_text("Add ledger\n\n# D17 only in a comment line\n")
    assert check_message(tmp_path, str(msg))[0] == 0


def test_gate_probes_are_vacuous_outside_a_commit_and_validated(tmp_path):
    import pytest
    from fractal_harness import probes
    p = {"type": "commit_message", "forbid": "x"}
    assert probes.run(p, tmp_path).passed
    assert probes.run({"type": "all", "probes": [p, {"type": "staged_paths", "forbid": "y"}]}, tmp_path).passed
    for bad in ({"type": "commit_message"}, {"type": "staged_paths", "only": "src"},
                {"type": "commit_message", "forbid": "("}):
        with pytest.raises(probes.ProbeError):
            probes.validate(bad)
    c = ClaimCache(tmp_path)
    e = c.put("no ids", [], kind="invariant", probe=p)
    assert e.status == "verified"
    c.close()


def test_init_installs_commit_msg_hook(tmp_path):
    _git(tmp_path, "init", "-q")
    init(tmp_path, settings=False, git_hook=True)
    assert "fractal check --message" in (tmp_path / ".git" / "hooks" / "commit-msg").read_text()

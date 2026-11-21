"""Typed probes the checker can reason about (instead of shell commands): `paths` (files present / absent / named /
unique) and the `frozen` staged gate (append-only), plus their absorption in loss.py."""
import json
import subprocess

import pytest

from fractal_harness import loss, probes
from fractal_harness.check import staged_status
from fractal_harness.probes import ProbeError


def _tree(root, files):
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)


def P(paths, **kw):
    return {"type": "paths", "paths": paths, **kw}


# --- paths ----------------------------------------------------------------------------------------------------

def test_paths_absent_and_present(tmp_path):
    _tree(tmp_path, {"src/a.py": "", "src/x/proptest-regressions/b.txt": ""})
    assert not probes.run(P(["**/proptest-regressions/**"], expect="absent"), tmp_path).passed
    assert probes.run(P(["**/proptest-regressions/**", "!src/x/**"], expect="absent"), tmp_path).passed
    assert probes.run(P(["src/*.py"]), tmp_path).passed
    assert not probes.run(P(["lib/*.py"]), tmp_path).passed
    assert probes.run(P(["src/*.py"], expect={"max": 1}), tmp_path).passed


def test_paths_counts_a_file_a_pending_edit_creates(tmp_path):
    _tree(tmp_path, {"src/a.py": ""})
    rule = P(["**/proptest-regressions/**"], expect="absent")
    assert probes.run(rule, tmp_path).passed
    assert not probes.run(rule, tmp_path, overlay={"src/proptest-regressions/c.txt": "x"}).passed


def test_paths_ignores_git_ignored_directories(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    _tree(tmp_path, {".gitignore": "target/\n", "target/proptest-regressions/x.txt": "", "src/a.py": ""})
    assert probes.run(P(["**/proptest-regressions/**"], expect="absent"), tmp_path).passed


def test_paths_ignores_git_ignored_files(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    _tree(tmp_path, {".gitignore": ".DS_Store\n", "m/001_a.sql": "", "m/.DS_Store": "", "m/.keep": ""})
    r = probes.run(P(["m/*"], unless_name=r"[0-9]{3}_\w+\.sql", expect="absent"), tmp_path)
    assert not r.passed and "m/.keep" in r.detail and ".DS_Store" not in r.detail
    assert not probes.run(P(["m/*"], unless_name=r"[0-9]{3}_\w+\.sql", expect="absent"), tmp_path,
                          overlay={"m/bad.sql": ""}).passed


def test_paths_naming_convention(tmp_path):
    _tree(tmp_path, {"m/001_init.sql": "", "m/002_users.sql": ""})
    rule = P(["m/*"], unless_name=r"[0-9]{3}_[a-z0-9_]+\.sql", expect="absent")
    assert probes.run(rule, tmp_path).passed
    _tree(tmp_path, {"m/001_init.sql.orig": ""})                 # the whole name must match, not a part of it
    r = probes.run(rule, tmp_path)
    assert not r.passed and "001_init.sql.orig" in r.detail
    (tmp_path / "m/001_init.sql.orig").unlink()
    _tree(tmp_path, {"m/3_Bad.sql": ""})
    r = probes.run(rule, tmp_path)
    assert not r.passed and "m/3_Bad.sql" in r.detail


def test_paths_unique_key(tmp_path):
    _tree(tmp_path, {"m/001_init.sql": "", "m/002_users.sql": ""})
    rule = P(["m/*.sql"], unique=r"^[0-9]{3}")
    assert probes.run(rule, tmp_path).passed
    _tree(tmp_path, {"m/002_orders.sql": ""})
    r = probes.run(rule, tmp_path)
    assert not r.passed and "002_orders" in r.detail and "002_users" in r.detail


def test_paths_naming_rule_over_no_files_is_vacuous(tmp_path):
    r = probes.run(P(["m/*"], unless_name="x", expect="absent"), tmp_path)
    assert r.passed and r.vacuous


@pytest.mark.parametrize("bad", [P([]), P(["a"], unless_name="("), P(["a"], unless_name="x", unique="y"),
                                 {"type": "paths", "paths": "a"}])
def test_paths_validation(bad):
    with pytest.raises(ProbeError):
        probes.validate(bad)


def test_paths_is_exact_evidence_and_depends_on_its_globs():
    from fractal_harness.cache import dependencies
    from fractal_harness.store import Edge
    rule = P(["m/*.sql", "!m/0.sql"], unique="^[0-9]+")
    assert probes.evidence_class(rule) == "exact"
    e = Edge.__new__(Edge)
    e.reads, e.probe = [], rule
    assert dependencies(e) == ["m/*.sql"]


# --- frozen (append-only) ------------------------------------------------------------------------------------

FROZEN = {"type": "staged_paths", "frozen": ["db/migrations/*.sql"]}


def test_frozen_allows_adding_and_refuses_changing():
    assert probes.run(FROZEN, None, ctx={"staged": ["db/migrations/003.sql"],
                                         "staged_status": {"db/migrations/003.sql": "A"}}).passed
    for how in "MDR":
        r = probes.run(FROZEN, None, ctx={"staged": [], "staged_status": {"db/migrations/001.sql": how}})
        assert not r.passed and "frozen" in r.detail
    assert probes.run(FROZEN, None, ctx={"staged": [], "staged_status": {"src/a.py": "M"}}).passed


def test_frozen_holds_vacuously_outside_a_commit():
    assert probes.run(FROZEN, None, ctx={}).passed


def test_staged_status_reads_modifications_deletions_and_renames(tmp_path):
    g = lambda *a: subprocess.run(["git", *a], cwd=tmp_path, check=True, capture_output=True)
    g("init", "-q")
    g("config", "user.email", "t@example.com")
    g("config", "user.name", "t")
    _tree(tmp_path, {"m/1.sql": "one\n" * 20, "m/2.sql": "two\n", "m/3.sql": "three\n"})
    g("add", ".")
    g("commit", "-qm", "init")
    (tmp_path / "m/2.sql").write_text("changed\n")
    (tmp_path / "m/3.sql").unlink()
    g("mv", "m/1.sql", "m/1b.sql")
    _tree(tmp_path, {"m/4.sql": "four\n"})
    g("add", "-A")
    assert staged_status(tmp_path) == {"m/1.sql": "R", "m/1b.sql": "A", "m/2.sql": "M", "m/3.sql": "D", "m/4.sql": "A"}


def test_check_refuses_a_commit_that_edits_a_frozen_file(tmp_path):
    from fractal_harness.cache import ClaimCache
    from fractal_harness.check import check
    g = lambda *a: subprocess.run(["git", *a], cwd=tmp_path, check=True, capture_output=True)
    g("init", "-q")
    g("config", "user.email", "t@example.com")
    g("config", "user.name", "t")
    _tree(tmp_path, {"db/migrations/001.sql": "create\n", ".gitignore": ".fractal/\n"})
    g("add", ".")
    g("commit", "-qm", "init")
    c = ClaimCache(tmp_path)
    e = c.put("migrations are append-only", [], kind="invariant", probe=FROZEN)
    c.set_rule(e.id, "enforced")
    c.close()
    _tree(tmp_path, {"db/migrations/002.sql": "alter\n"})
    g("add", ".")
    assert check(tmp_path)[0] == 0
    (tmp_path / "db/migrations/001.sql").write_text("create table x\n")
    g("add", ".")
    code, out = check(tmp_path)
    assert code == 1 and "001.sql" in out and "frozen" in out


# --- absorption of the new types -----------------------------------------------------------------------------

def test_a_paths_absent_rule_is_absorbed_by_wider_globs_only():
    lost = P(["crates/**/proptest-regressions/**"], expect="absent")
    assert loss.assess_probes(lost, None, [P(["**/proptest-regressions/**"], expect="absent")]).absorbed
    assert not loss.assess_probes(lost, None, [P(["fuzz/**/proptest-regressions/**"], expect="absent")]).absorbed


def test_a_naming_rule_is_absorbed_only_by_the_same_name_regex():
    lost = P(["m/*"], unless_name=r"[0-9]{3}_.*\.sql", expect="absent")
    assert loss.assess_probes(lost, None, [P(["m/**"], unless_name=r"[0-9]{3}_.*\.sql", expect="absent")]).absorbed
    assert not loss.assess_probes(lost, None, [P(["m/**"], unless_name=r".*\.sql", expect="absent")]).absorbed
    assert not loss.assess_probes(lost, None, [P(["m/**"], expect="absent")]).absorbed     # different count


def test_only_in_absorbs_a_grep_absent_outside_its_owner():
    owner = {"type": "only_in", "pattern": "UnsafeCell", "paths": ["crates/core/src/**/*.rs"],
             "files": ["crates/core/src/sync.rs"]}
    lifted = {"type": "grep", "pattern": "UnsafeCell", "paths": ["crates/core/src/**/*.rs", "!crates/core/src/sync.rs"],
              "expect": "absent"}
    assert loss.assess_probes(lifted, None, [owner]).absorbed
    assert not loss.assess_probes(owner, None, [lifted]).absorbed      # the lifted grep says nothing about presence
    keeps = {"type": "grep", "pattern": "UnsafeCell", "paths": ["crates/core/src/sync.rs"], "expect": "present"}
    assert loss.assess_probes(owner, None, [lifted, keeps]).absorbed


def test_only_in_presence_is_not_provable_when_its_files_are_outside_its_paths():
    owner = {"type": "only_in", "pattern": "X", "paths": ["src/**/*.rs"], "files": ["lib/x.rs"]}
    other = {"type": "grep", "pattern": "X", "paths": ["lib/x.rs"], "expect": "present"}
    assert not loss.assess_probes(owner, None, [owner | {"paths": ["src/**/*.rs", "lib/*.rs"]}, other]).absorbed


@pytest.mark.parametrize("lost,absorber,ok", [
    ({"type": "staged_paths", "forbid": "regress"}, {"type": "staged_paths", "forbid": "regress", "only": ["**"]}, True),
    ({"type": "staged_paths", "forbid": "regress"}, {"type": "staged_paths", "forbid": "regressions"}, False),
    ({"type": "staged_paths", "only": ["src/**"]}, {"type": "staged_paths", "only": ["src/core/**"]}, True),
    ({"type": "staged_paths", "only": ["src/core/**"]}, {"type": "staged_paths", "only": ["src/**"]}, False),
    ({"type": "staged_paths", "frozen": ["db/m/*.sql"]}, {"type": "staged_paths", "frozen": ["db/**"]}, True),
    ({"type": "staged_paths", "frozen": ["db/**"]}, {"type": "staged_paths", "frozen": ["db/m/*.sql"]}, False),
    ({"type": "commit_message", "require": "^Node: "}, {"type": "commit_message", "require": "^Node: ", "forbid": "x"}, True),
    ({"type": "commit_message", "forbid": "x", "require": "y"}, {"type": "commit_message", "forbid": "x"}, False),
])
def test_gate_absorption(lost, absorber, ok):
    assert loss.assess_probes(lost, None, [absorber]).absorbed is ok


def test_a_probe_change_is_judged_against_the_new_probe_too():
    before = {"type": "grep", "pattern": "eval", "paths": ["src/core/**/*.py"], "expect": {"max": 5}}
    tighter = {**before, "expect": {"max": 3}}
    wider = {**before, "paths": ["src/**/*.py"]}
    looser = {**before, "expect": {"max": 7}}
    assert loss.assess_probes(before, tighter, [tighter]).absorbed
    assert loss.assess_probes(before, wider, [wider]).absorbed
    assert not loss.assess_probes(before, looser, [looser]).absorbed
    assert not loss.assess_probes(before, None, []).absorbed


def test_a_command_rule_is_still_not_provable():
    r = loss.assess_probes({"type": "command", "run": "true"}, None, [P(["**"], expect="absent")])
    assert not r.absorbed and "not provable" in r.why


def test_staged_paths_are_read_unquoted(tmp_path):
    """Without -z git quotes a non-ASCII name ("db/002_\\303\\251.sql"), and a gate matched the quoted string."""
    from fractal_harness.check import staged_paths
    g = lambda *a: subprocess.run(["git", *a], cwd=tmp_path, check=True, capture_output=True)
    g("init", "-q")
    _tree(tmp_path, {"db/002_é.sql": "x\n", "a b.txt": "y\n"})
    g("add", ".")
    assert sorted(staged_paths(tmp_path)) == ["a b.txt", "db/002_é.sql"]

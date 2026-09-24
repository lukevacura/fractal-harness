import subprocess
from pathlib import Path

from fractal_harness.cache import ClaimCache
from fractal_harness.manifest import manifest


def _repo(tmp_path: Path) -> Path:
    for rel, text in {"core/io.py": "def read(): pass\n", "core/model.py": "class Txn: pass\n",
                      "cmds/export.py": "def run(): pass\n", "cmds/alerts.py": "def run(): pass\n"}.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    g = lambda pat, path, **kw: {"type": "grep", "pattern": pat, "paths": [path], **kw}
    c = ClaimCache(tmp_path)
    c.put("commands live one per module in cmds/ and read data through core/io.py", ["cmds/*.py", "core/io.py"],
          probe=g("def run", "cmds/*.py"))
    c.put("core.io.read() loads the ledger", ["core/io.py"], kind="interface", probe=g("def read", "core/io.py"))
    c.put("export writes CSV", ["cmds/export.py"], probe=g("def run", "cmds/export.py"))
    c.put("Txn is the record type", ["core/model.py"], probe=g("class Txn", "core/model.py"))
    rule = c.put("commands never call eval", ["cmds/*.py"], kind="invariant",
                 probe=g(r"\beval\(", "cmds/*.py", expect="absent"))
    c.set_rule(rule.id, "enforced")
    c.put("core never imports cmds", ["core/*.py"], kind="invariant",
          probe=g(r"import cmds", "core/*.py", expect="absent"))
    c.close()
    return tmp_path


def test_whole_repo_manifest(tmp_path):
    m = manifest(_repo(tmp_path))
    assert m.startswith("# Manifest: whole repo")
    assert m.index("## Rules") < m.index("## Proposed rules") < m.index("## This region")
    assert "commands never call eval" in m and "export writes CSV" in m


def test_region_manifest_rules_first_big_picture_and_neighbours(tmp_path):
    m = manifest(_repo(tmp_path), "cmds/export.py")
    rules, big, this, neigh = (m.index(h) for h in ("## Rules", "## Big picture", "## This region",
                                                      "## Neighbouring interfaces"))
    assert rules < big < this < neigh
    assert "commands never call eval" in m[rules:big]
    assert "commands live one per module" in m[big:this]          # spans cmds/ and core/: placed at root
    assert "export writes CSV" in m[this:neigh] and "core.io.read()" in m[neigh:]
    assert "Txn" not in m and "core never imports cmds" not in m   # other region's fact and proposal

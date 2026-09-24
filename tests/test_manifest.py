from pathlib import Path

from fractal_harness.cache import ClaimCache
from fractal_harness.manifest import manifest


def _repo(tmp_path: Path) -> Path:
    for rel, text in {"core/io.py": "def read(): pass\n", "core/model.py": "class Txn: pass\n",
                      "cmds/export.py": "def run(): pass\n", "cmds/alerts.py": "def run(): pass\n"}.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)
    c = ClaimCache(tmp_path)
    g = lambda pat, path: {"type": "grep", "pattern": pat, "paths": [path]}
    c.put("commands live one per module in cmds/ and read data through core/io.py", ["cmds/*.py", "core/io.py"],
          probe=g("def run", "cmds/*.py"))
    c.put("core.io.read() loads the ledger", ["core/io.py"], kind="interface", probe=g("def read", "core/io.py"))
    c.put("export writes CSV", ["cmds/export.py"], probe=g("def run", "cmds/export.py"), level=2)
    c.put("Txn is the record type", ["core/model.py"], probe=g("class Txn", "core/model.py"), level=3)
    c.close()
    return tmp_path


def test_whole_repo_manifest_lists_everything(tmp_path):
    m = manifest(_repo(tmp_path))
    assert m.startswith("# Manifest: whole repo") and "export writes CSV" in m and "Txn is the record type" in m


def test_region_manifest_has_big_picture_and_neighbour_interfaces(tmp_path):
    m = manifest(_repo(tmp_path), ["cmds/export.py"])
    big, this, neigh = m.index("## Big picture"), m.index("## This region"), m.index("## Neighbouring interfaces")
    assert big < this < neigh
    assert "commands live one per module" in m[big:this] and "export writes CSV" in m[this:neigh]
    assert "core.io.read()" in m[neigh:] and "Txn" not in m


def test_level_caps_detail(tmp_path):
    m = manifest(_repo(tmp_path), level=2)
    assert "export writes CSV" in m and "Txn is the record type" not in m

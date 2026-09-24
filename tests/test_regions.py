from pathlib import Path

from fractal_harness.regions import affects, ancestors, build, depth, governs, owner, placement

FILES = ["app/lib/main.dart", "app/lib/quest.dart", "app/lib/geo/geo.dart", "app/test/t.dart",
         "pipeline/build.py", "pipeline/util.py", "README.md"]


def test_placement_is_smallest_enclosing_region():
    assert placement(["app/lib/main.dart"], FILES) == "app/lib/main.dart"
    assert placement(["app/lib/main.dart", "app/lib/quest.dart"], FILES) == "app/lib"
    assert placement(["app/lib/**/*.dart"], FILES) == "app/lib"
    assert placement(["app/lib/main.dart", "pipeline/build.py"], FILES) == ""
    assert placement(["app/lib", "app/lib/geo/geo.dart"], FILES) == "app/lib"
    assert placement(["app/new/*.dart"], FILES) == "app/new"          # nothing matches yet: literal prefix


def test_affects_and_governs():
    deps = ["app/lib/**/*.dart", "README.md"]
    assert affects(deps, "app/lib/geo/geo.dart") and affects(deps, "app/lib/brand_new.dart")
    assert affects(deps, "README.md") and not affects(deps, "app/test/t.dart")
    assert affects(["pipeline"], "pipeline/util.py")
    assert governs("", "anything") and governs("app/lib", "app/lib/geo/geo.dart") and not governs("app/lib", "app/libx.dart")
    assert ancestors("app/lib/main.dart") == ["", "app", "app/lib", "app/lib/main.dart"]


def test_capacity_ownership_and_depth(tmp_path: Path):
    sizes = {"app/lib/main.dart": 900, "app/lib/quest.dart": 300, "app/lib/geo/geo.dart": 50,
             "app/test/t.dart": 100, "pipeline/build.py": 200, "pipeline/util.py": 100, "README.md": 10}
    for rel, n in sizes.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("x\n" * n)
    tree = build(tmp_path, files=list(sizes))
    assert tree.lines == 1660 and tree.children["app"].lines == 1350
    # root (1660) and app (1350) and app/lib (1250) exceed 1000; pipeline (300) fits
    assert owner(tree, "pipeline/util.py", capacity=1000) == "pipeline"
    assert owner(tree, "app/lib/geo/geo.dart", capacity=1000) == "app/lib/geo"
    assert owner(tree, "app/test/t.dart", capacity=1000) == "app/test"
    assert owner(tree, "app/lib/main.dart", capacity=1000) == "app/lib/main.dart"
    assert depth(tree, "app/lib/geo", capacity=1000) == 3 and depth(tree, "pipeline", capacity=1000) == 1


def test_glob_semantics_match_path_glob(tmp_path: Path):
    from fractal_harness.regions import glob_match
    for rel in FILES:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("x\n")
    for pattern in ["app/lib/**/*.dart", "app/**", "*.md", "pipeline/*.py", "app/*/t.dart", "**/*.py"]:
        expected = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.glob(pattern) if p.is_file())
        assert sorted(f for f in FILES if glob_match(pattern, f)) == expected, pattern

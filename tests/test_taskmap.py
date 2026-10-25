from pathlib import Path

from fractal_harness.behavior import baseline, import_tests
from fractal_harness.taskmap import render, task_map

from test_behavior import FILES, repo  # noqa: F401  (fixture)


def _prep(repo: Path) -> Path:
    (repo / "pkg" / "orphan.py").write_text("def lonely():\n    return 0\n")
    import_tests(repo)
    baseline(repo)
    return repo


def test_map_finds_regions_contracts_upstream_and_downstream(repo):
    m = task_map(_prep(repo), "Make `quad` handle negative numbers", capacity=1)
    core = next(r for r in m.regions if r.path == "pkg/core.py")
    assert "defines `quad`" in core.why
    assert [e.probe["file"] for e in core.contracts] == ["tests/test_core.py"]
    assert "pkg/util.py" in core.relies_on                     # upstream assumption
    text = render(m)
    assert text.startswith("TASK MAP") and "tests/test_core.py (1 tests)" in text and "relies on: pkg/util.py" in text


def test_editing_an_upstream_file_lists_downstream_contracts(repo):
    m = task_map(_prep(repo), "Change double() in pkg/util.py to use shifts", capacity=1)
    util = next(r for r in m.regions if r.path == "pkg/util.py")
    assert util.contracts == []                                 # no test imports util directly
    assert "pkg/util.py" in m.weak
    assert [e.probe["file"] for e in m.downstream] == ["tests/test_core.py"]


def test_weak_spot_and_empty_map(repo):
    _prep(repo)
    m = task_map(repo, "Give lonely a docstring in pkg/orphan.py", capacity=1)
    assert m.weak == ["pkg/orphan.py"] and "weak spot" in render(m)
    assert task_map(repo, "update the marketing copy").empty


def test_paths_outside_the_repo_seed_nothing(repo):
    _prep(repo)
    for named in ("/private/tmp/claude-501/proj/tasks/abc.output", "~/Downloads/notes/draft.md",
                  "../sibling/pkg/new.py", "pkg/../../etc/passwd.txt"):
        assert task_map(repo, f"Read the output at {named}").empty, named
    assert not task_map(repo, "Add pkg/brand_new.py with a helper").empty    # a new file inside the repo still seeds


# ---- neighbour signatures, layout, and trimming -------------------------------------------------------------------

from fractal_harness.taskmap import HEADER, HOW_TO, IFACE_NOTE, MAX_IFACE_CHARS, OMITTED_ROOM  # noqa: E402

STATIC = HEADER + "\n" + HOW_TO
UTIL = "def double(x):\n    return 2 * x\n\ndef triple(x):\n    return 3 * x\n\ndef _hidden():\n    pass\n"


def _quad_map(repo: Path):
    (repo / "pkg" / "util.py").write_text(UTIL)
    return task_map(_prep(repo), "Make `quad` handle negative numbers", capacity=1)


def test_the_map_offers_signatures_of_the_neighbour_names_a_region_uses_not_their_bodies(repo):
    m = _quad_map(repo)
    core = next(r for r in m.regions if r.path == "pkg/core.py")
    assert core.interfaces == {"pkg/util.py": ["def double(x)"]}            # triple is unused here, _hidden is private
    text = render(m)
    assert "      pkg/util.py: def double(x)" in text
    assert text.index("relies on: pkg/util.py") < text.index("      pkg/util.py: def double(x)")
    assert "triple" not in text and "return 2 * x" not in text and "_hidden" not in text


def test_the_signatures_note_appears_only_when_signatures_do(repo):
    assert IFACE_NOTE in render(_quad_map(repo))
    assert IFACE_NOTE not in render(task_map(repo, "Give lonely a docstring in pkg/orphan.py", capacity=1))
    assert render(task_map(repo, "Give lonely a docstring in pkg/orphan.py", capacity=1)).startswith(STATIC)


def test_the_static_start_is_shared_by_every_map_and_signatures_follow_the_note(repo):
    a = render(_quad_map(repo))
    b = render(task_map(repo, "Give lonely a docstring in pkg/orphan.py", capacity=1))
    assert a.startswith(STATIC) and b.startswith(STATIC)
    assert a.split("\n")[2] == IFACE_NOTE                                  # the note is part of the head, before the regions


def test_over_budget_signatures_go_first_then_tail_lines_and_never_the_static_start(repo):
    m = _quad_map(repo)
    full = render(m)
    sig = "      pkg/util.py: def double(x)"
    assert sig in full
    squeezed = render(m, budget=len(full) - 1)                             # one character over: only the signature line is dropped
    assert sig not in squeezed and IFACE_NOTE not in squeezed and "relies on: pkg/util.py" in squeezed
    assert "omitted" not in squeezed
    assert len(squeezed) <= len(full) - 1
    tiny = render(m, budget=len(STATIC) + OMITTED_ROOM + 40)
    assert tiny.startswith(STATIC) and len(tiny) <= len(STATIC) + OMITTED_ROOM + 40 and "more lines omitted)" in tiny
    assert tiny.split("\n")[-1].startswith("(+") and "relies on" not in tiny
    nothing_fits = render(m, budget=10)                                    # the static start is never cut, even if it alone is over
    assert nothing_fits.startswith(STATIC) and nothing_fits.endswith("more lines omitted)")


def test_signatures_are_capped_across_the_whole_map(repo):
    names = [f"fn{n:02d}" for n in range(30)]
    (repo / "pkg" / "util.py").write_text("".join(f"def {n}(argument_one, argument_two):\n    return 1\n\n" for n in names))
    (repo / "pkg" / "core.py").write_text("from pkg.util import *\n\ndef quad(x):\n    return " + " + ".join(f"{n}(x, x)" for n in names) + "\n")
    m = task_map(_prep(repo), "Make `quad` handle negative numbers", capacity=1)
    core = next(r for r in m.regions if r.path == "pkg/core.py")
    assert len(core.interfaces["pkg/util.py"]) == 5                       # MAX_PER_FILE
    text = render(m, budget=20000)
    sig_lines = [l for l in text.split("\n") if l.startswith("      pkg/util.py: ")]
    assert len(sig_lines) == 5 and sum(len(l) for l in sig_lines) <= MAX_IFACE_CHARS


def test_an_unreadable_upstream_file_yields_no_signatures_and_no_crash(repo):
    m = _quad_map(repo)
    (repo / "pkg" / "util.py").unlink()
    again = task_map(repo, "Make `quad` handle negative numbers", capacity=1)
    core = next(r for r in again.regions if r.path == "pkg/core.py")
    assert "pkg/util.py" not in core.interfaces
    assert render(again).startswith(STATIC)

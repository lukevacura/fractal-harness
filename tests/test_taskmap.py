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


# ---- render, pinned on hand-built maps ---------------------------------------------------------------------------

from fractal_harness.store import Edge  # noqa: E402
from fractal_harness.taskmap import Region, TaskMap  # noqa: E402


def edge(post="p", file="tests/t.py", detail="3 passed"):
    return Edge(id=post, kind="behavior", pre="", post=post, reads=[], writes=[], probe={"file": file}, status="verified",
                fingerprint=None, hashes={}, detail=detail, parent_id=None, created_at=0, updated_at=0)


def test_render_numbers_regions_from_one_names_the_root_and_caps_every_list():
    big = Region("", why=["a", "b", "c", "d"], contracts=[edge(file=f"tests/t{i}.py") for i in range(6)],
                 relies_on={f"up{i}.py": [edge(file=f"tests/u{i}.py"), edge(file="tests/x.py")] for i in range(6)},
                 interfaces={"up0.py": ["sig0"], "up3.py": ["sig3"], "up4.py": ["sig4"]},
                 rules=[edge(f"rule{i}") for i in range(5)], facts=[edge(f"fact{i}") for i in range(4)])
    small = Region("pkg/b", why=["x"])
    text = render(TaskMap([big, small], [edge(file=f"tests/d{i}.py") for i in range(7)], []), budget=10**6)
    lines = text.split("\n")
    assert "1. (repo root): a; b; c" in lines and "2. pkg/b: x" in lines            # numbered from 1, why capped at three
    assert "   contracts (definition of done): " + ", ".join(f"tests/t{i}.py (3 tests)" for i in range(4)) + " +2 more" in lines
    assert "   contracts: NONE directly cover this region (weak spot: add a test for what you change)" in lines
    assert any(l.startswith("   relies on: up0.py [tests/u0.py, tests/x.py]; up1.py") and "up3.py" in l and "up4.py" not in l for l in lines)
    assert "      up3.py: sig3" in lines and "      up0.py: sig0" in lines and not any("sig4" in l for l in lines)   # only shown upstreams
    assert [l for l in lines if "RULE" in l] == [f"   RULE (enforced): rule{i}" for i in range(3)]
    assert [l for l in lines if l.startswith("   fact:")] == ["   fact: fact0", "   fact: fact1"]
    assert "Must not break: 7 downstream contracts depend on these files (e.g. tests/d0.py, tests/d1.py, tests/d2.py, tests/d3.py)." in lines


def test_the_more_count_starts_at_one_extra_contract():
    five = Region("p", contracts=[edge(file=f"tests/t{i}.py") for i in range(5)])
    line = render(TaskMap([five], [], []), budget=10**6).split("contracts (definition of done):")[1].split("\n")[0]
    assert line.endswith("tests/t3.py (3 tests) +1 more") and "tests/t4.py" not in line


def test_a_contract_is_counted_by_its_tests_or_labelled_by_its_status_and_exactly_four_have_no_more_line():
    r = Region("p", contracts=[edge(file="tests/a.py", detail="12 passed"), edge(file="tests/b.py", detail="boom")])
    text = render(TaskMap([r], [], []), budget=10**6)
    assert "tests/a.py (12 tests), tests/b.py (verified)" in text and "more)" not in text
    four = Region("p", contracts=[edge(file=f"tests/t{i}.py") for i in range(4)])
    assert " +" not in render(TaskMap([four], [], []), budget=10**6).split("contracts (definition of done):")[1].split("\n")[0]


def test_signature_lines_stop_at_the_cap_exactly():
    prefix = "      a.py: "
    sigs = ["s%02d" % i + "x" * (100 - len(prefix) - 3) for i in range(20)]            # each rendered line is exactly 100 characters
    assert all(len(prefix + g) == 100 for g in sigs)
    reg = Region("p", contracts=[edge()], relies_on={"a.py": []}, interfaces={"a.py": sigs})
    lines = render(TaskMap([reg], [], []), budget=10**6).split("\n")
    shown = [l for l in lines if l.startswith(prefix)]
    assert len(shown) == MAX_IFACE_CHARS // 100 == 12 and shown[0].startswith(prefix + "s00") and shown[-1].startswith(prefix + "s11")


def test_budget_boundaries_are_exact_and_the_marker_counts_what_was_dropped():
    core = Region("p", why=["w"], contracts=[edge()], relies_on={"up.py": [edge()]}, rules=[edge("rule one")])
    m = TaskMap([core], [edge(file="tests/d.py")], [])
    full = render(m, budget=10**6)
    assert render(m, budget=len(full)) == full                                         # exactly at budget: nothing dropped
    body = full.split("\n")[2:]
    assert len(body) == 5 and "omitted" not in full
    head = [HEADER, HOW_TO]
    for kept in range(len(body)):
        size = len("\n".join(head + body[:kept]))
        got = render(m, budget=size + OMITTED_ROOM)                                   # exactly room for `kept` lines and the marker
        assert got == "\n".join(head + body[:kept] + [f"(+{len(body) - kept} more lines omitted)"]), kept
        assert render(m, budget=size + OMITTED_ROOM - 1).count("\n") < got.count("\n") + 1 or kept == 0


def test_trimming_takes_the_last_signature_first_then_the_note_then_facts_then_the_rest():
    reg = Region("p", why=["w"], contracts=[edge()], relies_on={"up.py": [edge()]},
                 interfaces={"up.py": ["sig_one", "sig_two", "sig_three"]}, facts=[edge("fact one")], rules=[edge("rule one")])
    m = TaskMap([reg], [], [])
    full = render(m, budget=10**6)
    gone = {}
    for budget in range(len(full), 0, -1):
        text = render(m, budget=budget)
        for token in ("sig_three", "sig_two", "sig_one", IFACE_NOTE, "fact: fact one", "RULE (enforced)", "relies on", "contracts (definition of done)"):
            if token not in text:
                gone.setdefault(token, budget)
    order = ["sig_three", "sig_two", "sig_one", "fact: fact one", "RULE (enforced)", "relies on", "contracts (definition of done)"]
    assert [gone[t] for t in order] == sorted((gone[t] for t in order), reverse=True) and len(set(gone[t] for t in order)) == len(order)
    assert gone[IFACE_NOTE] == gone["sig_one"]                                        # the note leaves with the last signature
    assert render(m, budget=len(full)) == full and "sig_three" not in render(m, budget=len(full) - 1)   # signatures stay at exactly the budget


def test_signatures_can_be_turned_off_for_a_comparison(repo, monkeypatch):
    from fractal_harness import taskmap
    assert "pkg/util.py: def double(x)" in render(_quad_map(repo))
    monkeypatch.setattr(taskmap, "MAX_IFACE_CHARS", 0)
    off = task_map(repo, "Make `quad` handle negative numbers", capacity=1)
    assert next(r for r in off.regions if r.path == "pkg/core.py").interfaces == {}
    text = render(off)
    assert "def double" not in text and IFACE_NOTE not in text and "relies on: pkg/util.py" in text


def test_the_signature_budget_comes_from_the_environment(monkeypatch):
    import importlib
    from fractal_harness import taskmap
    try:
        monkeypatch.setenv("FRACTAL_IFACE_CHARS", "0")
        assert importlib.reload(taskmap).MAX_IFACE_CHARS == 0
        monkeypatch.setenv("FRACTAL_IFACE_CHARS", "300")
        assert importlib.reload(taskmap).MAX_IFACE_CHARS == 300
        monkeypatch.delenv("FRACTAL_IFACE_CHARS")
        assert importlib.reload(taskmap).MAX_IFACE_CHARS == 1200
    finally:
        monkeypatch.delenv("FRACTAL_IFACE_CHARS", raising=False)
        importlib.reload(taskmap)


def test_a_primed_map_repeats_only_its_header_and_the_task_specific_lines(repo):
    from fractal_harness.taskmap import HEADER, HOW_TO
    m = _quad_map(repo)
    full, primed = render(m), render(m, primed=True)
    assert HOW_TO in full and HOW_TO not in primed and IFACE_NOTE not in primed
    assert primed.split("\n")[0] == HEADER
    assert primed.split("\n")[1:] == full.split("\n")[3:]                 # the same regions, contracts and signatures
    assert render(task_map(repo, "nothing relevant here at all", capacity=1), primed=True) == ""


def test_a_file_name_only_counts_when_the_task_writes_it_as_a_whole_word(repo):
    (repo / "pkg" / "check.py").write_text("def verify(x):\n    return x\n")
    (repo / "pkg" / "graph_check.py").write_text("def walk(x):\n    return x\n")
    seeded = lambda prompt: {f for r in task_map(repo, prompt, capacity=1).regions for f in r.files}
    assert seeded("Fix graph_check.py so it walks deeper") == {"pkg/graph_check.py"}
    assert seeded("Fix check.py so it verifies more") == {"pkg/check.py"}
    assert seeded("Fix pkg/check.py and graph_check.py") == {"pkg/check.py", "pkg/graph_check.py"}
    assert seeded("Fix check.pyc handling") == set()


def test_names_file_boundaries():
    from fractal_harness.regions import names_file
    assert names_file("edit graph_check.py", "graph_check.py")
    assert names_file("edit `src/a/graph_check.py`.", "src/a/graph_check.py")
    assert names_file("edit a/graph_check.py", "src/a/graph_check.py")          # a partial path still names it
    assert not names_file("edit graph_check.py", "check.py")
    assert not names_file("edit my-check.py", "check.py")
    assert not names_file("edit check.pyc", "check.py")
    assert not names_file("edit s.py", "x/s.py")                                 # too short to route by name alone...
    assert names_file("edit x/s.py", "x/s.py")                                   # ...but its path counts
    assert names_file("edit short.py", "x/short.py") and not names_file("edit short.py", "x/short.py", min_name=9)
    assert names_file("edit a_b.py", "x/a_b.py") and not names_file("edit a_b.py", "x/a_b.py", min_name=7)   # the limit is inclusive
    assert names_file("edit src/foo.py", "Src/Foo.py")                           # case does not matter

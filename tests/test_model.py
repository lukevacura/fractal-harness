import random
from dataclasses import replace

import pytest

from modelgen import random_model

from fractal_harness.model import (FORMAT_VERSION, Discharge, Edge, Invariant, Model, ModelFormatError, Node, Port,
                                   Preserve, Strength, covers, diff, local_name, parent_of, ref_of, split_ref, IN, OUT)

SORTED = Invariant("sorted", "output is sorted", Discharge(Strength.PROPERTY, "tests/t.py::test_sorted"))
KEEPS = Preserve("top/a#sorted", Discharge(Strength.PROPERTY, "tests/t.py::test_b_keeps_order"))


def rules(m: Model) -> list[str]:
    return sorted({v.rule for v in m.validate()})


def pipe() -> Model:
    """top = a -> b. a owns and discharges `sorted`; b carries it on and records its own discharge; top promises it."""
    return Model.of([
        Node("top", ports=(Port("x", IN), Port("y", OUT, invariants=("top/a#sorted",)))),
        Node("top/a", ports=(Port("i", IN), Port("o", OUT, invariants=("top/a#sorted",))), invariants=(SORTED,),
             owns=("src/a.py",)),
        Node("top/b", ports=(Port("i", IN, invariants=("top/a#sorted",)), Port("o", OUT, invariants=("top/a#sorted",))),
             owns=("src/b.py",), preserves=(KEEPS,)),
    ], [Edge(("top", "x"), ("top/a", "i")), Edge(("top/a", "o"), ("top/b", "i")), Edge(("top/b", "o"), ("top", "y"))])


def tweak(m: Model, node_id: str, **fields) -> Model:
    return m.with_nodes(replace(m.node(node_id), **fields))


def test_a_well_formed_model_has_no_violations_and_no_gaps():
    assert pipe().validate() == [] and pipe().gaps() == []


def test_ids_derive_containment():
    assert parent_of("a/b/c") == "a/b" and parent_of("a") == "" and local_name("a/b/c") == "c"
    assert split_ref(ref_of("a/b", "sorted")) == ("a/b", "sorted")


@pytest.mark.parametrize("bad", ["", "a//b", "a/ b", "a/b/", "/a"])
def test_malformed_ids(bad):
    assert "id" in rules(Model.of([Node(bad)]))


def test_orphan_and_duplicate_ids():
    assert rules(Model.of([Node("a/b")])) == ["orphan"]
    assert rules(Model.of([Node("a"), Node("a", intent="x")])) == ["id-dup"]


def test_port_and_invariant_duplicates_and_bad_direction():
    assert rules(Model.of([Node("a", ports=(Port("p", IN), Port("p", OUT)))])) == ["port-dup"]
    assert rules(Model.of([Node("a", ports=(Port("p", "sideways"),))])) == ["port-direction"]
    assert rules(Model.of([Node("a", invariants=(Invariant("i", "x"), Invariant("i", "y")))])) == ["invariant-dup"]


def test_dangling_references_on_ports_assemblies_and_preserves():
    m = pipe().with_nodes(Node("top/c", ports=(Port("p", IN, invariants=("top/zz#nope",)),)))
    assert [(v.rule, v.subject) for v in m.validate()] == [("ref-dangling", "top/c.p")]
    bad_assembly = tweak(pipe(), "top", invariants=(Invariant("all", "s", assembled_from=("top/a#gone",)),))
    assert [v.rule for v in bad_assembly.validate()] == ["ref-dangling"]
    bad_preserve = tweak(pipe(), "top/b", preserves=(KEEPS, Preserve("top/a#gone")))
    assert sorted(v.rule for v in bad_preserve.validate()) == ["preserve-stray", "ref-dangling"]


def test_assembled_invariants_may_only_be_defined_over_descendants():
    ok = tweak(pipe(), "top", invariants=(Invariant("all", "s", assembled_from=("top/a#sorted",)),))
    assert ok.validate() == []
    sideways = tweak(pipe(), "top/b", invariants=(Invariant("copy", "s", assembled_from=("top/a#sorted",)),))
    assert [(v.rule, v.subject) for v in sideways.validate()] == [("assembled-up", "top/b#copy")]
    upward = tweak(ok, "top/a", invariants=(SORTED, Invariant("up", "s", assembled_from=("top#all",))))
    assert "assembled-up" in rules(upward)


def test_edges_must_name_real_ports_in_a_wireable_relation_and_direction():
    m = pipe()
    assert rules(m.with_edges(Edge(("top/a", "ghost"), ("top/b", "o")))) == ["edge-endpoint"]
    far = m.with_nodes(Node("other", ports=(Port("p", IN),)), Node("other/deep", ports=(Port("q", IN),)))
    assert "edge-level" in rules(far.with_edges(Edge(("top/a", "o"), ("other/deep", "q"))))
    assert rules(m.with_edges(Edge(("top/a", "i"), ("top/b", "o")))) == ["edge-direction"]        # an input feeding an output
    assert rules(m.with_edges(Edge(("top", "y"), ("top/a", "o")))) == ["edge-direction"]          # an output passed inward


def _loop() -> Model:
    m = pipe()
    m = tweak(m, "top/a", ports=m.node("top/a").ports + (Port("back", IN),))
    m = tweak(m, "top/b", ports=m.node("top/b").ports + (Port("fb", OUT),))
    return m.with_edges(Edge(("top/b", "fb"), ("top/a", "back")))


def test_cycles_are_violations_unless_they_pass_through_a_state_node():
    loop = _loop()
    assert rules(loop) == ["cycle"] and "top/a, top/b" in loop.validate()[0].message
    assert tweak(loop, "top/b", state=True).validate() == []
    selfloop = Model.of([Node("n", ports=(Port("i", IN), Port("o", OUT)))], [Edge(("n", "o"), ("n", "i"))])
    assert rules(selfloop) == ["cycle"]


def test_a_cycle_below_a_node_is_reported_for_the_right_container():
    two = (Port("i", IN), Port("o", OUT))
    m = pipe().with_nodes(Node("top/a/x", ports=two), Node("top/a/y", ports=two))
    m = m.with_edges(Edge(("top/a/x", "o"), ("top/a/y", "i")), Edge(("top/a/y", "o"), ("top/a/x", "i")))
    assert [v.subject for v in m.validate() if v.rule == "cycle"] == ["top/a"]


def test_covers():
    assert covers("src", "src/a.py") and covers("src/", "src/a.py") and covers("src/a.py", "src/a.py")
    assert not covers("src", "srcx/a.py") and not covers("src/a.py", "src")
    assert covers("src/a.py", "src/a.py::f") and covers("src/a.py::T", "src/a.py::T::m")
    assert not covers("src/a.py::f", "src/a.py") and not covers("src/a.py::f", "src/a.py::g")
    assert not covers("src/a.py::T", "src/a.py::Tx") and not covers("", "a")


def test_children_may_narrow_but_never_widen_what_their_container_allows():
    allowed = tweak(pipe(), "top", owns=("src",))
    assert allowed.validate() == []
    wide = tweak(allowed, "top/b", owns=("lib/b.py",))
    assert [(v.rule, v.subject) for v in wide.validate()] == [("owns-widen", "top/b")]
    assert tweak(pipe(), "top/b", owns=("lib/b.py",)).validate() == []              # no allowance declared: unconstrained


def test_leaves_may_not_overlap_but_siblings_in_one_file_may_split_symbols():
    assert [v.rule for v in tweak(pipe(), "top/b", owns=("src/a.py",)).validate()] == ["owns-overlap"]
    assert [v.rule for v in tweak(pipe(), "top/a", owns=("src",)).validate()] == ["owns-overlap"]    # a directory covers b's file
    split = tweak(tweak(pipe(), "top/a", owns=("src/f.py::one",)), "top/b", owns=("src/f.py::two",))
    assert split.validate() == []
    whole = tweak(tweak(pipe(), "top/a", owns=("src/f.py",)), "top/b", owns=("src/f.py::two",))
    assert [v.rule for v in whole.validate()] == ["owns-overlap"]


def test_internal_nodes_do_not_count_as_overlapping_their_children():
    assert tweak(pipe(), "top", owns=("src/a.py", "src/b.py")).validate() == []


def test_a_node_with_an_inner_graph_must_wire_each_port_inward():
    m = tweak(pipe(), "top", ports=pipe().node("top").ports + (Port("z", IN), Port("w", OUT)))
    assert sorted(v.subject for v in m.validate()) == ["top.w", "top.z"]
    assert Model.of([Node("leaf", ports=(Port("z", IN),))]).validate() == []        # a leaf has nothing inside


def test_preserve_must_be_carried_by_an_output_port():
    stray = tweak(pipe(), "top/b", ports=(Port("i", IN, invariants=("top/a#sorted",)), Port("o", OUT)))
    assert [v.rule for v in stray.validate()] == ["preserve-stray"]


def test_edge_assumptions_are_gaps_in_every_direction():
    m = pipe()
    extra = Invariant("extra", "also", Discharge(Strength.EXAMPLE, "t.py::t"))
    greedy = tweak(tweak(m, "top/a", invariants=(SORTED, extra)), "top/b",
                   ports=(Port("i", IN, invariants=("top/a#sorted", "top/a#extra")), m.node("top/b").ports[1]))
    assert [(g.kind, g.subject) for g in greedy.gaps()] == [("edge-assumption", "top/a.o -> top/b.i")]
    assert "top/a#extra" in greedy.gaps()[0].message
    child_assumes = tweak(m, "top/a", ports=(Port("i", IN, invariants=("top/a#sorted",)), m.node("top/a").ports[1]))
    assert [g.subject for g in child_assumes.gaps()] == ["top.x -> top/a.i"]        # a child assumes more than its container provides
    over_promised = tweak(m, "top/b", ports=(m.node("top/b").ports[0], Port("o", OUT)), preserves=())
    assert [g.subject for g in over_promised.gaps()] == ["top/b.o -> top.y"]        # the container promises what its child lacks
    under_promised = tweak(m, "top", ports=(Port("x", IN, invariants=("top/a#sorted",)), Port("y", OUT)))
    assert under_promised.gaps() == []                                               # providing more than anyone requires is no gap


def test_a_leaf_must_discharge_what_it_carries_but_does_not_own():
    m = pipe()
    assert [(g.kind, g.subject) for g in tweak(m, "top/b", preserves=()).gaps()] == [("claim-undischarged", "top/b.o")]
    undone = tweak(m, "top/b", preserves=(Preserve(KEEPS.ref),))
    assert [g.kind for g in undone.gaps()] == ["claim-undischarged"]
    assert [g.kind for g in m.gaps(floor=Strength.PROOF) if g.subject == "top/b.o"] == ["weak"]
    assert tweak(m, "top/b", preserves=(replace(KEEPS, discharge=Discharge(Strength.CONSTRUCTION)),)).gaps(floor=Strength.PROOF) \
        == [g for g in m.gaps(floor=Strength.PROOF) if g.subject != "top/b.o"]
    owner = m.node("top/a")                      # an owner needs no preserve for its own invariant
    assert not [g for g in m.gaps() if g.subject.startswith(owner.id)]


def test_a_container_needs_no_preserve_because_its_wiring_backs_it():
    assert [g for g in pipe().gaps() if g.subject.startswith("top.")] == []


def test_assembled_invariants_are_unjudged_until_someone_discharges_them():
    assembled = Invariant("all", "s", assembled_from=("top/a#sorted",))
    m = tweak(pipe(), "top", invariants=(assembled,))
    assert [(g.kind, g.subject) for g in m.gaps()] == [("assembly-unjudged", "top#all")]
    judged = replace(assembled, discharge=Discharge(Strength.JUDGED, "checker"))
    assert tweak(pipe(), "top", invariants=(judged,)).gaps() == []


def test_undischarged_and_weak_invariants():
    m = tweak(pipe(), "top/a", invariants=(Invariant("sorted", "output is sorted"),))
    assert [(g.kind, g.subject) for g in m.gaps()] == [("undischarged", "top/a#sorted")]
    assert [g.subject for g in pipe().gaps(floor=Strength.PROOF) if g.kind == "weak"] == ["top/a#sorted", "top/b.o"]
    assert pipe().gaps(floor=Strength.PROPERTY) == []


def test_interface_digest_depends_on_ports_and_carried_statements_only():
    m = pipe()
    base = m.interface_digest("top/a")
    reworded = tweak(m, "top/a", invariants=(replace(SORTED, statement="output is sorted ascending"),))
    assert reworded.interface_digest("top/a") != base                                       # the statement is part of the interface
    assert reworded.interface_digest("top/b") != m.interface_digest("top/b")                # b's ports carry it too
    stronger = tweak(m, "top/a", invariants=(replace(SORTED, discharge=Discharge(Strength.PROOF, "x")),))
    assert stronger.interface_digest("top/a") == base                                       # how it is discharged is not
    retyped = tweak(m, "top/a", ports=(Port("i", IN, "int"), m.node("top/a").ports[1]))
    assert retyped.interface_digest("top/a") != base
    assert tweak(m, "top/a", intent="different words").interface_digest("top/a") == base    # intent is prose, not interface
    assert m.interface_digest("nope") is None


def test_early_cutoff_a_leaf_change_that_keeps_its_interface_invalidates_ancestors_only():
    m = pipe()
    c = {"src/a.py": "1", "src/b.py": "1", "tests/t.py": "1"}
    before = m.keys(c)

    def changed(edit: dict) -> set[str]:
        after = m.keys({**c, **edit})
        return {i for i in before if before[i] != after[i]}

    assert changed({"src/a.py": "2"}) == {"top/a", "top"}                    # b merely connects to a: untouched
    assert changed({"src/b.py": "2"}) == {"top/b", "top"}                    # nothing is downstream of b
    assert changed({"tests/t.py": "2"}) == {"top/a", "top/b", "top"}         # one file discharges both nodes' invariants
    assert changed({"src/unrelated.py": "2"}) == set()


def test_an_interface_change_reaches_the_consumers_and_the_ancestors():
    m = pipe()
    reworded = tweak(m, "top/a", invariants=(replace(SORTED, statement="sorted descending"),))
    d = diff(m, reworded)
    assert d.interface_changed == {"top/a", "top/b", "top"}       # top's output port carries it too
    assert d.key_changed == {"top/a", "top/b", "top"} and d.added == d.removed == frozenset()


def test_a_preserve_is_part_of_its_nodes_key_and_only_its_nodes():
    m = pipe()
    c = {"tests/t.py": "1"}
    other = tweak(m, "top/b", preserves=(replace(KEEPS, discharge=Discharge(Strength.PROOF, "tests/t.py::test_b_keeps_order")),))
    assert {i for i, k in m.keys(c).items() if k != other.keys(c)[i]} == {"top/b", "top"}


def test_keys_ignore_where_a_subtree_lives():
    m = pipe().with_nodes(Node("zone"))
    moved = m.rename("top", "zone/top")
    assert moved.validate() == []
    k0, k1 = m.keys(), moved.keys()
    assert k0["top"] == k1["zone/top"] and k0["top/a"] == k1["zone/top/a"] and k0["top/b"] == k1["zone/top/b"]
    assert moved.node("top") is None and moved.node("zone/top/b").preserves[0].ref == "zone/top/a#sorted"


def test_rename_rewrites_references_into_the_subtree_from_outside_it():
    outside = Node("o", ports=(Port("p", IN, invariants=("top/a#sorted",)),))
    m = pipe().with_nodes(outside)
    moved = m.rename("top/a", "top/c")
    assert moved.node("o").ports[0].invariants == ("top/c#sorted",)
    assert [e.src for e in moved.edges if e.dst == ("top/b", "i")] == [("top/c", "o")]


def test_rename_refuses_a_missing_source_and_a_taken_target():
    with pytest.raises(ValueError):
        pipe().rename("nope", "x")
    with pytest.raises(ValueError):
        pipe().rename("top/a", "top/b")
    with pytest.raises(ValueError):
        pipe().with_nodes(Node("x/y"), Node("x")).rename("top", "x")


def test_diff_reports_added_and_removed_nodes():
    m = pipe()
    smaller = m.without("top/b")
    d = diff(m, smaller)
    assert d.removed == {"top/b"} and {"top/b", "top"} <= d.key_changed
    assert diff(smaller, m).added == {"top/b"}


def test_without_removes_descendants_and_touching_edges():
    gone = pipe().with_nodes(Node("top/a/x")).without("top/a")
    assert [n.id for n in gone.nodes] == ["top", "top/b"]
    assert all(e.src[0] != "top/a" and e.dst[0] != "top/a" for e in gone.edges)


def test_with_nodes_replaces_by_id_and_edges_deduplicate():
    m = pipe()
    assert len(tweak(m, "top/b", intent="x").nodes) == 3
    assert len(m.with_edges(*m.edges).edges) == len(m.edges)
    assert m == Model.of(reversed(m.nodes), reversed(m.edges)) and hash(m) == hash(Model.of(m.nodes, m.edges))
    assert m != tweak(m, "top/b", intent="x") and m != "model"


def test_round_trip_and_canonical_form():
    m = pipe()
    assert Model.from_dict(m.to_dict()) == m
    shuffled = Model.of(reversed(m.nodes), reversed(m.edges))
    assert shuffled.to_dict() == m.to_dict() and shuffled.keys() == m.keys()
    assert Model.from_dict(_loop().to_dict()) == _loop()


@pytest.mark.parametrize("seed", range(40))
def test_random_models_are_well_formed_and_their_keys_are_order_independent_and_cut_off_early(seed):
    rng = random.Random(seed)
    m = random_model(rng)
    assert m.validate() == [], m.validate()
    assert Model.from_dict(m.to_dict()) == m
    content = {f"src/{i}.py": str(rng.random()) for i in range(1, 40)}
    shuffled = Model.of(rng.sample(m.nodes, len(m.nodes)), rng.sample(m.edges, len(m.edges)))
    assert shuffled.keys(content) == m.keys(content)
    leaf = rng.choice([n for n in m.nodes if n.id in m.leaves() and n.owns])
    d = diff(m, m, content, dict(content, **{leaf.owns[0]: "edited"}))
    ancestors = {leaf.id} | {leaf.id.rsplit("/", k)[0] for k in range(1, leaf.id.count("/") + 1)}
    assert d.key_changed == ancestors and d.interface_changed == frozenset()
    root = rng.choice([n.id for n in m.nodes if "/" not in n.id])
    renamed = m.rename(root, f"renamed-{root}")                 # same container, so sibling wiring survives
    assert renamed.validate() == []
    assert renamed.keys(content)[f"renamed-{root}"] == m.keys(content)[root]


def test_the_model_is_immutable():
    import dataclasses
    m = pipe()
    for obj, attr in ((m.node("top"), "intent"), (m.node("top").ports[0], "name"), (SORTED, "statement"), (KEEPS, "ref"),
                      (SORTED.discharge, "pointer"), (m.edges[0], "src")):
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(obj, attr, "x")


def test_equal_things_are_equal_whatever_order_they_were_written_in():
    assert Port("p", IN, invariants=("b#x", "a#x")) == Port("p", IN, invariants=("a#x", "b#x"))
    assert Invariant("i", "s", assembled_from=("b#x", "a#x")) == Invariant("i", "s", assembled_from=("a#x", "b#x"))
    assert Port("p", IN, invariants=("b#x", "a#x")).invariants == ("a#x", "b#x")
    a, b = Port("a", IN), Port("b", OUT)
    assert Node("n", ports=(b, a)) == Node("n", ports=(a, b)) and Node("n", ports=(b, a)).ports == (a, b)
    i, j = Invariant("i", "s"), Invariant("j", "t")
    assert Node("n", invariants=(j, i)).invariants == (i, j)
    assert Node("n", owns=("b", "a")).owns == ("a", "b")
    p, q = Preserve("a#x"), Preserve("b#x")
    assert Node("n", preserves=(q, p)).preserves == (p, q)


def test_rename_refuses_a_target_whose_prefix_is_already_occupied():
    occupied = Model.of([Node("x/y"), Node("top")])         # `x` itself is absent, but something lives under it
    with pytest.raises(ValueError):
        occupied.rename("top", "x")
    assert occupied.rename("top", "xx").node("xx") is not None      # a mere shared spelling is not a collision


def test_validate_is_sorted_and_cycle_messages_are_stable():
    nodes = [Node(f"c/{n}", ports=(Port("i", IN), Port("o", OUT))) for n in "dcba"] + [Node("c")]
    ring = list("abcd")
    edges = [Edge((f"c/{ring[k]}", "o"), (f"c/{ring[(k + 1) % 4]}", "i")) for k in range(4)]
    m = Model.of(nodes + [Node("z/b/q"), Node("z/a/q")], edges)
    out = m.validate()
    assert out == sorted(out, key=lambda v: (v.rule, v.subject, v.message))
    cycle = next(v for v in out if v.rule == "cycle")
    assert cycle.subject == "c" and cycle.message.startswith("c/a, c/b, c/c, c/d wait")
    assert [v.subject for v in out if v.rule == "orphan"] == ["z/a/q", "z/b/q"]
    dups = Model.of([Node("m", ports=(Port("i", IN), Port("i", OUT), Port("o", IN), Port("o", OUT))),
                     Node("n", invariants=(Invariant("b", "x"), Invariant("b", "y"), Invariant("a", "x"), Invariant("a", "y")))])
    assert [(v.rule, v.message) for v in dups.validate()] == [("invariant-dup", "two invariants named a"),
                                                              ("invariant-dup", "two invariants named b"),
                                                              ("port-dup", "two ports named i"), ("port-dup", "two ports named o")]


def test_reports_are_immutable_too():
    import dataclasses
    v = pipe().with_edges(Edge(("top/a", "ghost"), ("top/b", "i"))).validate()[0]
    g = tweak(pipe(), "top/b", preserves=()).gaps()[0]
    d = diff(pipe(), pipe())
    for obj, attr in ((v, "rule"), (g, "kind"), (d, "added")):
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(obj, attr, "x")


def test_an_edge_to_a_missing_node_is_a_violation_and_gaps_skip_malformed_edges():
    m = pipe().with_edges(Edge(("top/a", "o"), ("top/ghost", "i")), Edge(("ghost", "o"), ("top/b", "o")))
    assert sorted((v.rule, v.subject) for v in m.validate()) == [("edge-endpoint", "ghost.o -> top/b.o"),
                                                                 ("edge-endpoint", "top/a.o -> top/ghost.i")]
    far = pipe().with_nodes(Node("other", ports=(Port("p", IN, invariants=("top/a#sorted",)),)))
    far = far.with_edges(Edge(("top/a", "o"), ("other", "p")))
    assert [v.rule for v in far.validate()] == ["edge-level"]
    assert far.gaps() == [] and m.gaps() == []                      # a malformed edge is not also an evidence gap


def test_gap_messages_list_every_missing_invariant_in_order():
    names = ["f", "c", "a", "e", "b", "d"]                      # six, so a set's arbitrary order cannot match by luck
    need = tuple(Invariant(n, n, Discharge(Strength.EXAMPLE, "t::t")) for n in names)
    m = tweak(pipe(), "top/a", invariants=(SORTED, *need))
    m = tweak(m, "top/b", ports=(Port("i", IN, invariants=tuple(f"top/a#{n}" for n in ["sorted", *names])), m.node("top/b").ports[1]))
    listed = ", ".join(f"top/a#{n}" for n in sorted(names))
    assert m.gaps()[0].message == f"top/b.i requires {listed}, which top/a.o does not provide"


def test_round_trip_keeps_absent_discharges_assemblies_defaults_and_unpreserved_claims():
    m = pipe().with_nodes(Node("top/c", state=True, intent="state",
                               invariants=(Invariant("open", "no discharge"),
                                           Invariant("sum", "assembled", assembled_from=("top/a#sorted",))),
                               preserves=(Preserve("top/a#sorted"),)))
    again = Model.from_dict(m.to_dict())
    assert again == m and again.node("top/c").state is True
    assert again.node("top/c").invariants[0].discharge is None and again.node("top/c").preserves[0].discharge is None
    assert m.to_dict()["nodes"][-1]["preserves"][0]["discharge"] is None
    bare = Model.from_dict({"nodes": [{"id": "n"}]})
    assert bare.node("n") == Node("n") and bare.node("n").state is False and bare.edges == ()
    assert Model.from_dict({}).nodes == ()


def test_keys_do_not_depend_on_how_references_happen_to_sort():
    """Renaming can flip the order of two refs; the key must follow what they say, not how they are spelled."""
    a = Invariant("p", "B-statement", Discharge(Strength.EXAMPLE, "t::t"))
    z = Invariant("p", "A-statement", Discharge(Strength.EXAMPLE, "t::t"))
    m = Model.of([Node("m/aa", invariants=(a,)), Node("m/zz", invariants=(z,)), Node("m"),
                  Node("m/c", ports=(Port("o", OUT, invariants=("m/aa#p", "m/zz#p")),),
                       preserves=(Preserve("m/aa#p", Discharge(Strength.EXAMPLE, "t::a")),
                                  Preserve("m/zz#p", Discharge(Strength.EXAMPLE, "t::b"))))])
    moved = m.rename("m/aa", "m/zzz")
    assert m.interface_digest("m/c") == moved.interface_digest("m/c")
    assert m.keys()["m/c"] == moved.keys()["m/c"]



# ---- wiring is typed and counted, ports bind to code, content is per symbol, the format is versioned -------------

def port(m: Model, node_id: str, name: str, **fields) -> Model:
    node = m.node(node_id)
    return tweak(m, node_id, ports=tuple(replace(p, **fields) if p.name == name else p for p in node.ports))


def with_c() -> Model:
    """pipe() plus a third leaf `c`, wired to nothing."""
    return pipe().with_nodes(Node("top/c", ports=(Port("i", IN), Port("o", OUT)), owns=("src/c.py",)))


def test_edge_types_must_agree_and_an_empty_type_matches_anything():
    m = pipe()
    assert port(port(m, "top/a", "o", type="int"), "top/b", "i", type="int").validate() == []
    bad = port(port(m, "top/a", "o", type="int"), "top/b", "i", type="str")
    assert [(v.rule, v.subject) for v in bad.validate()] == [("edge-type", "top/a.o -> top/b.i")]
    assert "'int' feeds 'str'" in bad.validate()[0].message
    assert port(m, "top/a", "o", type="int").validate() == []                  # the far end is untyped
    assert port(m, "top/b", "i", type="int").validate() == []
    inward = port(port(m, "top", "x", type="str"), "top/a", "i", type="int")   # boundary edges are checked the same way
    assert [(v.rule, v.subject) for v in inward.validate()] == [("edge-type", "top.x -> top/a.i")]
    outward = port(port(m, "top/b", "o", type="int"), "top", "y", type="str")
    assert [(v.rule, v.subject) for v in outward.validate()] == [("edge-type", "top/b.o -> top.y")]


def test_a_port_fed_by_two_edges_must_declare_many():
    m = with_c().with_edges(Edge(("top/c", "o"), ("top/b", "i")))
    assert [(v.rule, v.subject) for v in m.validate()] == [("fan-in", "top/b.i")]
    assert "2 edges" in m.validate()[0].message
    assert port(m, "top/b", "i", many=True).validate() == []
    two_children = with_c().with_edges(Edge(("top/c", "o"), ("top", "y")))        # a container output fed by two children
    assert [(v.rule, v.subject) for v in two_children.validate()] == [("fan-in", "top.y")]
    assert with_c().with_edges(Edge(("top/a", "o"), ("top/c", "i"))).validate() == []   # fan-out is fine


def test_below_the_top_an_unfed_input_and_an_unused_output_are_gaps():
    assert pipe().gaps() == []
    assert [(g.kind, g.subject) for g in with_c().gaps()] == [("unfed-input", "top/c.i"), ("unused-output", "top/c.o")]
    lone = Model.of([Node("lone", ports=(Port("i", IN), Port("o", OUT)))])        # the top level faces the outside world
    assert lone.gaps() == []
    two = (Port("i", IN), Port("o", OUT))
    inner = Model.of([Node("t"), Node("t/k", ports=two), Node("t/k/x", ports=two, owns=("x.py",))],
                     [Edge(("t/k", "i"), ("t/k/x", "i")), Edge(("t/k/x", "o"), ("t/k", "o"))])
    assert inner.validate() == []                                                  # a nested container's own ports count
    assert {(g.kind, g.subject) for g in inner.gaps()} == {("unfed-input", "t/k.i"), ("unused-output", "t/k.o")}
    fed = inner.with_nodes(Node("t", ports=two)).with_edges(Edge(("t", "i"), ("t/k", "i")), Edge(("t/k", "o"), ("t", "o")))
    assert fed.validate() == [] and fed.gaps() == []


def bound() -> Model:
    return port(pipe(), "top/a", "o", binds="src/a.py::produce")


def test_a_bound_port_must_name_code_its_leaf_owns_and_only_leaves_bind():
    assert bound().validate() == []
    assert port(pipe(), "top/a", "o", binds="src/a.py").validate() == []             # the whole file it owns
    elsewhere = port(pipe(), "top/a", "o", binds="src/b.py::produce")
    assert [(v.rule, v.subject) for v in elsewhere.validate()] == [("bind-unowned", "top/a.o")]
    by_symbol = tweak(pipe(), "top/a", owns=("src/a.py::produce",))
    assert port(by_symbol, "top/a", "o", binds="src/a.py::produce::helper").validate() == []   # a member of what it owns
    assert [v.rule for v in port(by_symbol, "top/a", "o", binds="src/a.py::other").validate()] == ["bind-unowned"]
    container = port(pipe(), "top", "y", binds="src/a.py::x")
    assert [(v.rule, v.subject) for v in container.validate()] == [("bind-internal", "top.y")]


def test_a_signature_change_is_an_interface_change_that_reaches_consumers():
    m = bound()
    s1, s2 = {"src/a.py::produce": "v1"}, {"src/a.py::produce": "v2"}
    assert m.interface_digest("top/a", s1) != m.interface_digest("top/a", s2)
    assert m.interface_digest("top/b", s1) == m.interface_digest("top/b", s2)         # b is not bound to it
    d = diff(m, m, {}, {}, s1, s2)
    assert d.interface_changed == {"top/a"} and d.key_changed == {"top/a", "top/b", "top"}
    body_only = diff(m, m, {"src/a.py": "1"}, {"src/a.py": "2"}, s1, s1)               # a body edit, same signature: cut off at a
    assert body_only.interface_changed == frozenset() and body_only.key_changed == {"top/a", "top"}
    assert pipe().keys(signatures=s1) == pipe().keys(signatures=s2)                    # an unbound port ignores signatures
    assert m.interface_digest("top/a") == m.interface_digest("top/a", {}) == m.interface_digest("top/a", {"src/a.py::produce": ""})
    assert m.subtree_key("top/a", signatures=s1) != m.subtree_key("top/a", signatures=s2)


def test_stale_bindings_name_the_ports_whose_symbol_the_code_lost():
    assert bound().stale_bindings({}) == [("top/a.o", "src/a.py::produce")]
    assert bound().stale_bindings({"src/a.py::produce": "x"}) == [] and pipe().stale_bindings({}) == []


def test_leaves_that_split_a_file_are_keyed_by_their_own_symbol():
    m = tweak(tweak(pipe(), "top/a", owns=("src/f.py::one",)), "top/b", owns=("src/f.py::two",))
    base = {"src/f.py::one": "1", "src/f.py::two": "1", "src/f.py": "x", "tests/t.py": "1"}
    before = m.keys(base)

    def changed(edit: dict, start=base) -> set[str]:
        after = m.keys({**start, **edit})
        return {i for i in before if before[i] != after[i]}

    assert changed({"src/f.py::one": "2"}) == {"top/a", "top"}
    assert changed({"src/f.py::two": "2"}) == {"top/b", "top"}
    file_only = {"src/f.py": "x", "tests/t.py": "1"}                                     # no per-symbol hashes: the file is the fallback
    fb = m.keys(file_only)
    assert {i for i in fb if fb[i] != m.keys({**file_only, "src/f.py": "y"})[i]} == {"top/a", "top/b", "top"}
    assert changed({"tests/t.py::test_unrelated": "2"}) == set()                        # the discharging test is per symbol too
    assert changed({"tests/t.py::test_sorted": "2"}) == {"top/a", "top"}
    assert changed({"tests/t.py::test_b_keeps_order": "2"}) == {"top/b", "top"}


def test_the_format_is_versioned_and_older_documents_migrate():
    m = port(port(pipe(), "top/b", "i", many=True), "top/a", "o", binds="src/a.py::produce")
    assert m.to_dict()["version"] == FORMAT_VERSION
    legacy = m.to_dict()
    del legacy["version"]
    for n in legacy["nodes"]:
        for p in n["ports"]:
            del p["many"], p["binds"]
    plain = pipe()
    legacy_plain = plain.to_dict()
    del legacy_plain["version"]
    for n in legacy_plain["nodes"]:
        for p in n["ports"]:
            del p["many"], p["binds"]
    assert Model.from_dict(legacy_plain) == plain                       # a version-0 document: no version, ports without many/binds
    assert Model.from_dict(legacy) != m and Model.from_dict(m.to_dict()) == m
    for bad in (FORMAT_VERSION + 1, -1, "1", 1.0, None, True):
        with pytest.raises(ModelFormatError):
            Model.from_dict({**m.to_dict(), "version": bad})
    assert Model.from_dict({**m.to_dict(), "version": FORMAT_VERSION}) == m


def test_many_and_binds_survive_rename_and_change_the_interface():
    m = port(port(pipe(), "top/b", "i", many=True), "top/a", "o", binds="src/a.py::produce")
    assert m.validate() == []
    moved = m.with_nodes(Node("zone")).rename("top", "zone/top")
    assert moved.node("zone/top/b").ports[0].many is True and moved.node("zone/top/a").ports[1].binds == "src/a.py::produce"
    assert m.interface_digest("top/b") != pipe().interface_digest("top/b")              # `many` is part of the interface
    assert moved.keys({"src/a.py": "1"}, {"src/a.py::produce": "s"})["zone/top"] == m.keys({"src/a.py": "1"}, {"src/a.py::produce": "s"})["top"]


def test_a_bind_only_needs_one_of_the_leafs_owned_entries_to_cover_it():
    m = tweak(pipe(), "top/a", owns=("src/a.py", "src/a2.py"))
    assert port(m, "top/a", "o", binds="src/a2.py::produce").validate() == []
    assert port(m, "top/a", "o", binds="src/a.py::produce").validate() == []
    assert [v.rule for v in port(m, "top/a", "o", binds="src/a3.py::produce").validate()] == ["bind-unowned"]


def test_stale_bindings_are_listed_in_port_order_even_where_ids_sort_oddly():
    two = (Port("i", IN, binds="x.py::g"), Port("o", OUT, binds="x.py::f"))
    m = Model.of([Node("a", ports=two, owns=("x.py",)), Node("a-x", ports=two, owns=("y.py",)),
                  Node("b", ports=(Port("o", OUT, binds="x.py::f"),), owns=("z.py",))])
    got = [subject for subject, _ in m.stale_bindings({})]
    assert got == sorted(got) and len(got) == 5 and got[0] == "a-x.i"      # "a-x.i" < "a.i" as strings, though "a" < "a-x" as ids

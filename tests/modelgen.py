"""Random models for property tests: a well-formed tree, and a decorator that adds gaps without breaking it."""

import random
from dataclasses import replace

from fractal_harness.model import (Discharge, Edge, Invariant, Model, Node, Port, Preserve, Strength, ref_of, split_ref,
                                   IN, OUT)


def random_model(rng: random.Random) -> Model:
    """A random well-formed tree: each container's ports are wired to its first and last child, siblings
    are chained at random, leaves own disjoint files and some own an invariant."""
    nodes, edges, counter = [], [], 0

    def grow(parent: str, depth: int) -> list[str]:
        nonlocal counter
        kids = []
        for _ in range(rng.randint(1, 3)):
            counter += 1
            nid = f"{parent}/n{counter}" if parent else f"n{counter}"
            kids.append(nid)
            leaf = depth >= 2 or rng.random() < 0.4
            inv = ()
            if rng.random() < 0.6:
                inv = (Invariant("p", f"prop {counter}", Discharge(rng.choice(list(Strength)), f"t/{counter}.py::t")),)
            nodes.append(Node(nid, ports=(Port("i", IN), Port("o", OUT, invariants=(ref_of(nid, "p"),) if inv else ())),
                              invariants=inv, owns=(f"src/{counter}.py",) if leaf else ()))
            if not leaf:
                inner = grow(nid, depth + 1)
                edges.append(Edge((nid, "i"), (inner[0], "i")))
                edges.append(Edge((inner[-1], "o"), (nid, "o")))
        for a, b in zip(kids, kids[1:]):
            if rng.random() < 0.7:
                edges.append(Edge((a, "o"), (b, "i")))
        return kids

    grow("", 0)
    return Model.of(nodes, edges)


def decorate(m: Model, rng: random.Random) -> Model:
    """Add things that leave `validate()` empty but open gaps: stripped discharges, assumptions nobody
    provides, claims a leaf may or may not discharge (at any strength), and assembled invariants."""
    refs = [ref_of(n.id, i.name) for n in m.nodes for i in n.invariants]
    leaves = set(m.leaves())
    out = []
    for n in m.nodes:
        ports, invs, keeps = list(n.ports), list(n.invariants), list(n.preserves)
        invs = [replace(i, discharge=None) if rng.random() < 0.2 else i for i in invs]
        if refs and rng.random() < 0.3:
            extra = rng.choice(refs)
            ports = [replace(p, invariants=p.invariants + (extra,)) if p.direction == IN and extra not in p.invariants else p
                     for p in ports]
        foreign = [r for r in refs if split_ref(r)[0] != n.id]
        for _ in range(rng.randint(0, 2) if foreign else 0):            # an output port may carry several claims
            if rng.random() < 0.6:
                ref = rng.choice(foreign)
                ports = [replace(p, invariants=p.invariants + (ref,)) if p.direction == OUT and ref not in p.invariants else p
                         for p in ports]
                if rng.random() < (0.6 if n.id in leaves else 0.5) and all(k.ref != ref for k in keeps):   # containers too: it must mean nothing there
                    keeps.append(Preserve(ref, None if rng.random() < 0.3 else Discharge(rng.choice(list(Strength)), "t::c")))
        own = [r for r in refs if split_ref(r)[0] == n.id]
        if n.id in leaves and own and rng.random() < 0.3:                # preserving what you own is legal and means nothing
            keeps.append(Preserve(own[0], Discharge(rng.choice(list(Strength)), "t::own")))
        below = [r for r in refs if split_ref(r)[0].startswith(n.id + "/")]
        if n.id not in leaves and below and rng.random() < 0.5:
            invs.append(Invariant("agg", "assembled", None if rng.random() < 0.5 else Discharge(Strength.JUDGED, "checker"),
                                  tuple(rng.sample(below, rng.randint(1, min(2, len(below)))))))
        out.append(replace(n, ports=tuple(ports), invariants=tuple(invs), preserves=tuple(keeps)))
    return Model.of(out, m.edges)


def corrupt(m: Model, rng: random.Random) -> Model:
    """A draft: add edges that are malformed (a missing node or port, across levels, the wrong way round),
    some of them feeding ports that require invariants. `validate()` will not be empty."""
    ids = [n.id for n in m.nodes]
    bad = list(m.edges)
    for _ in range(rng.randint(2, 6)):
        a, b = rng.choice(ids), rng.choice(ids)
        pa, pb = rng.choice(["i", "o", "ghost"]), rng.choice(["i", "o", "ghost"])
        a = a if rng.random() < 0.85 else "ghost-node"
        b = b if rng.random() < 0.85 else "ghost-node"
        bad.append(Edge((a, pa), (b, pb)))
    return Model.of(m.nodes, bad)

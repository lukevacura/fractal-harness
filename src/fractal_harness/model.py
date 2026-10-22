"""The model: the code graph as immutable, content-addressed data.

A node is a typed transformation: input and output ports, the data invariants it owns, and the code it
owns. A node with children is made of an inner graph (zoom in); a node with none is a leaf, and leaves are
what code implements. Everything here is pure: no filesystem, no git, no store. Persistence and population
come later and sit on top of it.

Two kinds of invariant, kept apart:

  * metadata (structure): containment, ownership, wiring, acyclicity. Checked exactly by `Model.validate`.
    Containment is a tree *by construction*: a node's id is its container's id plus a segment, so a node
    cannot have two containers or none.
  * data (about values): `Invariant`, owned by exactly one node, shared by reference (`owner#name`).
    A port lists the invariants that hold of what crosses it. Whether they really hold is a judgment, at
    the strength of the `Discharge` recorded for it; `Model.gaps` lists what is unbacked.

Flows: ownership allowances trickle down (a child may narrow what its container allows, never widen it);
an assembled invariant is defined over references to invariants of *descendants* and so flows up.

Wiring is typed and counted: the two ends of an edge must agree on a type (an empty type matches anything),
and a port fed by more than one edge must say it is `many`. A leaf's port may be bound to the symbol that
implements it (`Port.binds`), so a change to the code's signature is a change to the interface.

Digests (see `interface_digest`, `Model.keys`): the interface digest is all other nodes' keys may depend
on; the subtree key adds the node's internals. A change deep inside a leaf that leaves its interface
alone changes the keys of that leaf and its ancestors, never of the nodes that merely connect to it
(early cutoff). That cutoff is only sound if the interface digest covers everything a neighbour can
observe, which is why bound ports fold in the signature of their symbol (supplied by the caller as
`signatures`). Content is hashed per owned entry: a leaf that owns `f.py::one` is not re-keyed by an edit to
`f.py::two` when the caller supplies a hash for each symbol (a bare file hash is the fallback).
Keys use names relative to the container and the statements of referenced invariants, not their ids, so
moving a whole subtree (`Model.rename`) changes no key inside it.

`to_dict` carries a format `version`; `from_dict` migrates older documents and refuses newer ones.

What this does not do: prove that an assembled invariant follows from its parts. A gap says "nothing here
backs this"; a judge, a test or a proof does the backing.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from enum import IntEnum
from functools import cached_property
from typing import Iterable, Mapping

FORMAT_VERSION = 1
ID_SEGMENT = re.compile(r"^[A-Za-z0-9_.\-]+$")
IN, OUT = "in", "out"
Endpoint = tuple[str, str]          # (node id, port name)


class ModelFormatError(ValueError):
    """A serialized model this code cannot read."""


class Strength(IntEnum):
    """How an invariant is discharged, weakest to strongest."""
    JUDGED = 1          # a human or checker agent says so
    PROBE = 2           # a structural or grep probe
    EXAMPLE = 3         # example tests
    PROPERTY = 4        # property-based tests or fuzzing
    PROOF = 5           # a proof or model-checking run
    CONSTRUCTION = 6    # the type system or privacy makes a violation unrepresentable


@dataclass(frozen=True)
class Discharge:
    strength: Strength
    pointer: str = ""               # `path::name` of the test, type, proof or probe that backs it


@dataclass(frozen=True)
class Invariant:
    name: str
    statement: str
    discharge: Discharge | None = None
    assembled_from: tuple[str, ...] = ()    # refs to invariants of descendants this one is defined over

    def __post_init__(self) -> None:
        object.__setattr__(self, "assembled_from", tuple(sorted(self.assembled_from)))


@dataclass(frozen=True)
class Preserve:
    """A leaf's claim that it keeps an invariant it does not own: the reference, and how the leaf discharges it."""
    ref: str
    discharge: Discharge | None = None


@dataclass(frozen=True)
class Port:
    name: str
    direction: str                  # IN | OUT
    type: str = ""
    invariants: tuple[str, ...] = ()        # refs (`owner#name`) of what holds of values crossing this port
    many: bool = False                      # fed by several edges on purpose; otherwise one feeder at most
    binds: str = ""                         # on a leaf: the `path::Symbol` that implements this port

    def __post_init__(self) -> None:
        object.__setattr__(self, "invariants", tuple(sorted(self.invariants)))


@dataclass(frozen=True)
class Node:
    id: str
    intent: str = ""
    ports: tuple[Port, ...] = ()
    invariants: tuple[Invariant, ...] = ()  # owned here
    owns: tuple[str, ...] = ()              # `path` or `path::Symbol`; on an internal node, what its children may own
    state: bool = False                     # a state node may sit on a cycle
    preserves: tuple[Preserve, ...] = ()    # discharges of invariants this node's output ports carry but it does not own

    def __post_init__(self) -> None:
        """Canonical order, so two nodes that say the same thing are equal."""
        object.__setattr__(self, "ports", tuple(sorted(self.ports, key=lambda p: (p.name, p.direction))))
        object.__setattr__(self, "invariants", tuple(sorted(self.invariants, key=lambda i: i.name)))
        object.__setattr__(self, "owns", tuple(sorted(self.owns)))
        object.__setattr__(self, "preserves", tuple(sorted(self.preserves, key=lambda pr: pr.ref)))


@dataclass(frozen=True)
class Edge:
    src: Endpoint
    dst: Endpoint


@dataclass(frozen=True)
class Violation:
    rule: str
    subject: str
    message: str


@dataclass(frozen=True)
class Gap:
    kind: str
    subject: str
    message: str


@dataclass(frozen=True)
class Delta:
    added: frozenset[str]
    removed: frozenset[str]
    interface_changed: frozenset[str]       # a node whose interface digest differs (added and removed included)
    key_changed: frozenset[str]             # a node whose subtree key differs (added and removed included)


def parent_of(node_id: str) -> str:
    return node_id.rpartition("/")[0]


def local_name(node_id: str) -> str:
    return node_id.rpartition("/")[2]


def ref_of(owner: str, name: str) -> str:
    return f"{owner}#{name}"


def split_ref(ref: str) -> tuple[str, str]:
    owner, _, name = ref.partition("#")
    return owner, name


def _split_owns(entry: str) -> tuple[str, str]:
    path, _, symbol = entry.partition("::")
    return path, symbol


def covers(a: str, b: str) -> bool:
    """Whether ownership entry `a` includes everything entry `b` names: a directory covers what is under it,
    a file covers its symbols, a symbol covers its members."""
    ap, asym = _split_owns(a)
    bp, bsym = _split_owns(b)
    if not ap or not (bp == ap or bp.startswith(ap.rstrip("/") + "/")):
        return False
    return not asym or (bp == ap and (bsym == asym or bsym.startswith(asym + "::")))


# Older documents differ from the current format only by lacking fields whose absence means the default
# (version 0 to 1: ports gained `many` and `binds`), so a migration rewrites nothing. A format that does
# change shape adds its step here.
MIGRATIONS = {0: lambda d: d}


def _hash(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]


class Model:
    """An immutable graph. Build with `Model.of`; derive new models with `with_nodes` and `with_edges`."""

    def __init__(self, nodes: tuple[Node, ...], edges: tuple[Edge, ...]):
        self.nodes = tuple(sorted(nodes, key=lambda n: n.id))
        self.edges = tuple(sorted(set(edges), key=lambda e: (e.src, e.dst)))

    @classmethod
    def of(cls, nodes: Iterable[Node] = (), edges: Iterable[Edge] = ()) -> "Model":
        return cls(tuple(nodes), tuple(edges))

    def with_nodes(self, *nodes: Node) -> "Model":
        """A model with these nodes added, or replacing the nodes of the same id."""
        replaced = {n.id for n in nodes}
        return Model.of([n for n in self.nodes if n.id not in replaced] + list(nodes), self.edges)

    def without(self, *ids: str) -> "Model":
        """A model without these nodes, their descendants, and every edge that touches one of them."""
        gone = {i for i in ids} | {d for i in ids for d in self.descendants(i)}
        return Model.of([n for n in self.nodes if n.id not in gone],
                        [e for e in self.edges if e.src[0] not in gone and e.dst[0] not in gone])

    def rename(self, old: str, new: str) -> "Model":
        """Move the subtree at `old` to `new`, rewriting every id, edge endpoint and reference that names it.
        Content is untouched, so no key inside the subtree changes. Raises if `old` is absent or `new` is taken."""
        if self.node(old) is None:
            raise ValueError(f"no node {old}")
        if self.node(new) is not None or any(n.id.startswith(new + "/") for n in self.nodes):
            raise ValueError(f"{new} is already in the model")

        def mv(i: str) -> str:
            return new + i[len(old):] if i == old or i.startswith(old + "/") else i

        def mv_ref(r: str) -> str:
            owner, _, name = r.partition("#")
            return f"{mv(owner)}#{name}"

        def mv_inv(i: Invariant) -> Invariant:
            return Invariant(i.name, i.statement, i.discharge, tuple(mv_ref(r) for r in i.assembled_from))

        nodes = [Node(mv(n.id), n.intent, tuple(replace(p, invariants=tuple(mv_ref(r) for r in p.invariants)) for p in n.ports),
                      tuple(mv_inv(i) for i in n.invariants), n.owns, n.state,
                      tuple(Preserve(mv_ref(pr.ref), pr.discharge) for pr in n.preserves)) for n in self.nodes]
        return Model.of(nodes, [Edge((mv(e.src[0]), e.src[1]), (mv(e.dst[0]), e.dst[1])) for e in self.edges])

    def with_edges(self, *edges: Edge) -> "Model":
        return Model.of(self.nodes, list(self.edges) + list(edges))

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Model) and self.nodes == other.nodes and self.edges == other.edges

    def __hash__(self) -> int:
        return hash((self.nodes, self.edges))

    # ---- structure -------------------------------------------------------------------------------

    @cached_property
    def _by_id(self) -> dict[str, Node]:
        return {n.id: n for n in self.nodes}

    @cached_property
    def _children(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for n in self.nodes:
            out.setdefault(parent_of(n.id), []).append(n.id)
        return out

    def node(self, node_id: str) -> Node | None:
        return self._by_id.get(node_id)

    def children(self, node_id: str) -> list[str]:
        return list(self._children.get(node_id, ()))

    def descendants(self, node_id: str) -> list[str]:
        prefix = node_id + "/"
        return [n.id for n in self.nodes if n.id.startswith(prefix)]

    def leaves(self) -> list[str]:
        return [n.id for n in self.nodes if n.id not in self._children]

    def invariant(self, ref: str) -> Invariant | None:
        owner, name = split_ref(ref)
        node = self.node(owner)
        return next((i for i in node.invariants if i.name == name), None) if node else None

    def _port(self, endpoint: Endpoint) -> Port | None:
        node = self.node(endpoint[0])
        return next((p for p in node.ports if p.name == endpoint[1]), None) if node else None

    def wire(self, e: Edge) -> tuple[str, str] | None:
        """(container id, kind) of an edge, or None if its endpoints are not in a wireable relation. `sibling`
        joins an output to an input of two children of one container; `in` passes a container's input to a
        child's input; `out` passes a child's output to the container's output. The top level is container ''."""
        a, b = e.src[0], e.dst[0]
        if parent_of(a) == parent_of(b):
            return parent_of(a), "sibling"
        if a == parent_of(b):
            return a, "in"
        if b == parent_of(a):
            return b, "out"
        return None

    # ---- metadata invariants -----------------------------------------------------------------------

    def validate(self) -> list[Violation]:
        """Every metadata rule, exactly. An empty list means the structure is well-formed; it says nothing
        about whether any data invariant holds."""
        v: list[Violation] = []
        seen: set[str] = set()
        for n in self.nodes:
            if not n.id or not all(ID_SEGMENT.match(s) for s in n.id.split("/")):
                v.append(Violation("id", n.id, f"malformed id {n.id!r}"))
            if n.id in seen:
                v.append(Violation("id-dup", n.id, "two nodes share this id"))
            seen.add(n.id)
            if parent_of(n.id) and parent_of(n.id) not in self._by_id:
                v.append(Violation("orphan", n.id, f"its container {parent_of(n.id)} is not in the model"))
            names = [p.name for p in n.ports]
            for dup in {x for x in names if names.count(x) > 1}:
                v.append(Violation("port-dup", n.id, f"two ports named {dup}"))
            for p in n.ports:
                if p.direction not in (IN, OUT):
                    v.append(Violation("port-direction", n.id, f"port {p.name} has direction {p.direction!r}"))
            inv = [i.name for i in n.invariants]
            for dup in {x for x in inv if inv.count(x) > 1}:
                v.append(Violation("invariant-dup", n.id, f"two invariants named {dup}"))
        v += (self._check_refs() + self._check_edges() + self._check_fan_in() + self._check_cycles() + self._check_owns()
              + self._check_binds() + self._check_boundary())
        return sorted(v, key=lambda x: (x.rule, x.subject, x.message))

    def _check_refs(self) -> list[Violation]:
        v = []
        for n in self.nodes:
            for p in n.ports:
                for ref in p.invariants:
                    if self.invariant(ref) is None:
                        v.append(Violation("ref-dangling", f"{n.id}.{p.name}", f"{ref} names no invariant"))
            carried = {r for p in n.ports if p.direction == OUT for r in p.invariants}
            for pr in n.preserves:
                if self.invariant(pr.ref) is None:
                    v.append(Violation("ref-dangling", f"{n.id}", f"preserves {pr.ref}, which names no invariant"))
                if pr.ref not in carried:
                    v.append(Violation("preserve-stray", n.id, f"preserves {pr.ref}, which no output port carries"))
            for i in n.invariants:
                for ref in i.assembled_from:
                    owner = split_ref(ref)[0]
                    if self.invariant(ref) is None:
                        v.append(Violation("ref-dangling", ref_of(n.id, i.name), f"{ref} names no invariant"))
                    elif not owner.startswith(n.id + "/"):
                        v.append(Violation("assembled-up", ref_of(n.id, i.name),
                                           f"assembled from {ref}, which is not owned by a descendant"))
        return v

    def _check_edges(self) -> list[Violation]:
        v = []
        for e in self.edges:
            subject = f"{e.src[0]}.{e.src[1]} -> {e.dst[0]}.{e.dst[1]}"
            sp, dp = self._port(e.src), self._port(e.dst)
            if sp is None or dp is None:
                v.append(Violation("edge-endpoint", subject, "an endpoint names no node or port"))
                continue
            wire = self.wire(e)
            if wire is None:
                v.append(Violation("edge-level", subject, "endpoints are neither siblings nor a container and its child"))
                continue
            want = {"sibling": (OUT, IN), "in": (IN, IN), "out": (OUT, OUT)}[wire[1]]
            if (sp.direction, dp.direction) != want:
                v.append(Violation("edge-direction", subject,
                                   f"a {wire[1]} edge runs {want[0]} -> {want[1]}, not {sp.direction} -> {dp.direction}"))
            if sp.type and dp.type and sp.type != dp.type:
                v.append(Violation("edge-type", subject, f"{sp.type!r} feeds {dp.type!r}"))
        return v

    def _check_fan_in(self) -> list[Violation]:
        feeders: dict[Endpoint, int] = {}
        for e in self.edges:
            feeders[e.dst] = feeders.get(e.dst, 0) + 1
        v = []
        for dst, count in feeders.items():
            port = self._port(dst)
            if port is not None and count > 1 and not port.many:
                v.append(Violation("fan-in", f"{dst[0]}.{dst[1]}", f"{count} edges feed it and it is not declared `many`"))
        return v

    def _check_binds(self) -> list[Violation]:
        v = []
        for n in self.nodes:
            for p in n.ports:
                if not p.binds:
                    continue
                if n.id in self._children:
                    v.append(Violation("bind-internal", f"{n.id}.{p.name}",
                                       "a node with an inner graph is implemented by its wiring, not by a symbol"))
                elif not any(covers(o, p.binds) for o in n.owns):
                    v.append(Violation("bind-unowned", f"{n.id}.{p.name}", f"{p.binds} is not code {n.id} owns"))
        return v

    def _check_cycles(self) -> list[Violation]:
        v = []
        for container, kids in self._children.items():
            movers = {k for k in kids if not self._by_id[k].state}
            succ: dict[str, set[str]] = {k: set() for k in movers}
            for e in self.edges:
                if e.src[0] in movers and e.dst[0] in movers and self.wire(e) == (container, "sibling"):
                    succ[e.src[0]].add(e.dst[0])

            def reach(start: str) -> set[str]:
                seen, stack = set(), list(succ[start])
                while stack:
                    x = stack.pop()
                    if x not in seen:
                        seen.add(x)
                        stack.extend(succ[x])
                return seen

            members = sorted(k for k in movers if k in reach(k))
            if members:
                v.append(Violation("cycle", container or "<top>",
                                   f"{', '.join(members)} wait on themselves; a cycle must pass through a state node"))
        return v

    def _check_owns(self) -> list[Violation]:
        v = []
        for n in self.nodes:
            parent = self.node(parent_of(n.id))
            if parent and parent.owns:
                for entry in n.owns:
                    if not any(covers(allow, entry) for allow in parent.owns):
                        v.append(Violation("owns-widen", n.id, f"{entry} is outside what {parent.id} allows"))
        leaves = [self._by_id[i] for i in self.leaves()]
        for i, a in enumerate(leaves):
            for b in leaves[i + 1:]:
                for x in a.owns:
                    for y in b.owns:
                        if covers(x, y) or covers(y, x):
                            v.append(Violation("owns-overlap", a.id, f"{x} overlaps {y} owned by {b.id}"))
        return v

    def _check_boundary(self) -> list[Violation]:
        v = []
        for n in self.nodes:
            if n.id not in self._children:
                continue
            for p in n.ports:
                wired = any((e.src == (n.id, p.name) and p.direction == IN) or
                            (e.dst == (n.id, p.name) and p.direction == OUT) for e in self.edges)
                if not wired:
                    v.append(Violation("boundary-unwired", f"{n.id}.{p.name}",
                                       "a node with an inner graph must wire each of its ports inward"))
        return v

    # ---- data invariants: what is unbacked ---------------------------------------------------------

    def gaps(self, floor: Strength | None = None) -> list[Gap]:
        """What nothing here backs. Not violations: a model can be well-formed and full of gaps.
        `edge-assumption`: a port requires invariants the port feeding it does not provide.
        `claim-undischarged`: a leaf's output port carries an invariant it does not own and the leaf records no
        discharge for it. `undischarged`: an invariant with no discharge and no assembly. `assembly-unjudged`: an assembled
        invariant nobody has judged. `weak`: discharged below `floor`. `unfed-input` / `unused-output`: below
        the top level, an input nothing feeds or an output nothing consumes (the top level faces the outside
        world, so its ports are exempt)."""
        g: list[Gap] = []
        fed, used = {e.dst for e in self.edges}, {e.src for e in self.edges}
        for n in self.nodes:
            if not parent_of(n.id):
                continue
            for p in n.ports:
                if p.direction == IN and (n.id, p.name) not in fed:
                    g.append(Gap("unfed-input", f"{n.id}.{p.name}", f"nothing feeds {n.id}.{p.name}"))
                if p.direction == OUT and (n.id, p.name) not in used:
                    g.append(Gap("unused-output", f"{n.id}.{p.name}", f"nothing consumes {n.id}.{p.name}"))
        for e in self.edges:
            sp, dp = self._port(e.src), self._port(e.dst)
            if sp is None or dp is None or self.wire(e) is None:
                continue
            missing = sorted(set(dp.invariants) - set(sp.invariants))
            if missing:
                g.append(Gap("edge-assumption", f"{e.src[0]}.{e.src[1]} -> {e.dst[0]}.{e.dst[1]}",
                             f"{e.dst[0]}.{e.dst[1]} requires {', '.join(missing)}, which {e.src[0]}.{e.src[1]} does not provide"))
        for n in self.nodes:
            if n.id not in self._children:       # a container's promises are backed by its wiring; a leaf must discharge its own
                kept = {pr.ref: pr for pr in n.preserves}
                for p in n.ports:
                    for ref in (p.invariants if p.direction == OUT else ()):
                        if split_ref(ref)[0] == n.id:
                            continue
                        pr = kept.get(ref)
                        if pr is None or pr.discharge is None:
                            g.append(Gap("claim-undischarged", f"{n.id}.{p.name}", f"{n.id} carries {ref} on {p.name} but records no discharge for it"))
                        elif floor is not None and pr.discharge.strength < floor:
                            g.append(Gap("weak", f"{n.id}.{p.name}", f"{n.id}'s discharge of {ref} is {pr.discharge.strength.name}, below {floor.name}"))
            for i in n.invariants:
                ref = ref_of(n.id, i.name)
                if i.discharge is None:
                    kind = "assembly-unjudged" if i.assembled_from else "undischarged"
                    g.append(Gap(kind, ref, f"{ref} has no discharge"))
                elif floor is not None and i.discharge.strength < floor:
                    g.append(Gap("weak", ref, f"{ref} is discharged at {i.discharge.strength.name}, below {floor.name}"))
        return sorted(g, key=lambda x: (x.kind, x.subject, x.message))

    # ---- digests -------------------------------------------------------------------------------------

    def _statements(self, refs: Iterable[str]) -> list[str | None]:
        """The statements of referenced invariants (None if dangling): content, not location."""
        return sorted((i.statement if (i := self.invariant(r)) else None) or "" for r in refs)

    def interface_digest(self, node_id: str, signatures: Mapping[str, str] | None = None) -> str | None:
        """What other nodes may depend on: port names, directions, types, whether a port takes several feeders,
        the statements of the invariants each port carries, and the signature of the symbol a port is bound to
        (`signatures` maps `path::Symbol` to a hash of its signature; a bound symbol it lacks hashes as empty).
        None for a node not in the model."""
        node = self.node(node_id)
        if node is None:
            return None
        signatures = signatures or {}
        ports = []
        for p in sorted(node.ports, key=lambda p: (p.direction, p.name)):
            ports.append([p.direction, p.name, p.type, p.many, self._statements(p.invariants),
                          signatures.get(p.binds, "") if p.binds else None])
        return _hash(ports)

    def stale_bindings(self, signatures: Mapping[str, str]) -> list[tuple[str, str]]:
        """(port, symbol) for every bound port whose symbol `signatures` no longer has: the code moved on."""
        return sorted((f"{n.id}.{p.name}", p.binds) for n in self.nodes for p in n.ports
                      if p.binds and p.binds not in signatures)

    def keys(self, content: Mapping[str, str] | None = None,
             signatures: Mapping[str, str] | None = None) -> dict[str, str]:
        """The subtree key of every node. `content` maps an owned entry (`path` or `path::Symbol`, and a
        discharge pointer) to the hash of its text; an entry it lacks falls back to the hash of its file, and a
        file it lacks hashes as empty. `signatures` is what `interface_digest` takes."""
        content = content or {}
        out: dict[str, str] = {}
        for node in sorted(self.nodes, key=lambda n: -n.id.count("/")):
            out[node.id] = self._key(node, content, signatures, out)
        return out

    def _key(self, node: Node, content: Mapping[str, str], signatures: Mapping[str, str] | None,
             done: dict[str, str]) -> str:
        def rel(endpoint: Endpoint) -> list[str]:
            return [".", endpoint[1]] if endpoint[0] == node.id else [local_name(endpoint[0]), endpoint[1]]

        def digest_of(entry: str) -> list[str]:
            return [entry, content.get(entry, content.get(_split_owns(entry)[0], ""))]

        wires, upstream = [], []
        for e in self.edges:
            wire = self.wire(e)
            if wire and wire[0] == node.id:
                wires.append([rel(e.src), rel(e.dst)])
        for e in self.edges:       # what feeds this node from outside: only the feeder's interface, never its internals
            wire = self.wire(e)
            if wire and wire[1] == "sibling" and e.dst[0] == node.id:
                upstream.append([e.dst[1], local_name(e.src[0]), e.src[1], self.interface_digest(e.src[0], signatures)])
        # Parts are sorted here even where `Model` already orders them: a key must never depend on that.
        payload = {
            "interface": self.interface_digest(node.id, signatures),
            "state": node.state,
            "owns": [digest_of(o) for o in node.owns],
            "invariants": sorted([i.name, i.statement, self._statements(i.assembled_from),
                                  [int(i.discharge.strength), i.discharge.pointer] + digest_of(i.discharge.pointer)
                                  if i.discharge else None] for i in node.invariants),
            "preserves": sorted([self._statements([pr.ref]),
                                 [int(pr.discharge.strength), pr.discharge.pointer] + digest_of(pr.discharge.pointer)
                                 if pr.discharge else None] for pr in node.preserves),
            "children": [[local_name(c), done[c]] for c in self.children(node.id)],
            "wires": sorted(wires),
            "upstream": sorted(upstream),
        }
        return _hash(payload)

    def subtree_key(self, node_id: str, content: Mapping[str, str] | None = None,
                    signatures: Mapping[str, str] | None = None) -> str | None:
        return self.keys(content, signatures).get(node_id)

    # ---- serialization -------------------------------------------------------------------------------

    def to_dict(self) -> dict:
        def inv(i: Invariant) -> dict:
            d = {"name": i.name, "statement": i.statement}
            if i.assembled_from:
                d["assembled_from"] = list(i.assembled_from)
            if i.discharge:
                d["discharge"] = {"strength": i.discharge.strength.name, "pointer": i.discharge.pointer}
            return d

        def dis(d: Discharge | None):
            return {"strength": d.strength.name, "pointer": d.pointer} if d else None

        return {"version": FORMAT_VERSION,
                "nodes": [{"id": n.id, "intent": n.intent, "state": n.state, "owns": list(n.owns),
                           "preserves": [{"ref": pr.ref, "discharge": dis(pr.discharge)}
                                         for pr in n.preserves],
                           "ports": [{"name": p.name, "direction": p.direction, "type": p.type,
                                      "invariants": list(p.invariants), "many": p.many, "binds": p.binds}
                                     for p in n.ports],
                           "invariants": [inv(i) for i in n.invariants]}
                          for n in self.nodes],
                "edges": [{"src": list(e.src), "dst": list(e.dst)} for e in self.edges]}

    @classmethod
    def from_dict(cls, d: dict) -> "Model":
        version = d.get("version", 0)
        if type(version) is not int or not 0 <= version <= FORMAT_VERSION:
            raise ModelFormatError(f"model format {version!r} is not one this code reads (it reads 0 to {FORMAT_VERSION})")
        for v in range(version, FORMAT_VERSION):
            d = MIGRATIONS[v](d)

        def inv(x: dict) -> Invariant:
            dis = x.get("discharge")
            return Invariant(x["name"], x["statement"], Discharge(Strength[dis["strength"]], dis.get("pointer", "")) if dis else None,
                             tuple(x.get("assembled_from", ())))

        nodes = [Node(n["id"], n.get("intent", ""),
                      tuple(Port(p["name"], p["direction"], p.get("type", ""), tuple(p.get("invariants", ())),
                                 bool(p.get("many", False)), p.get("binds", "")) for p in n.get("ports", ())),
                      tuple(inv(i) for i in n.get("invariants", ())), tuple(n.get("owns", ())), bool(n.get("state", False)),
                      tuple(Preserve(x["ref"], Discharge(Strength[x["discharge"]["strength"]], x["discharge"].get("pointer", ""))
                                     if x.get("discharge") else None) for x in n.get("preserves", ())))
                 for n in d.get("nodes", ())]
        return cls.of(nodes, [Edge(tuple(e["src"]), tuple(e["dst"])) for e in d.get("edges", ())])


def diff(old: Model, new: Model, old_content: Mapping[str, str] | None = None,
         new_content: Mapping[str, str] | None = None, old_signatures: Mapping[str, str] | None = None,
         new_signatures: Mapping[str, str] | None = None) -> Delta:
    """What changed between two models, in the two senses invalidation cares about."""
    ok, nk = old.keys(old_content, old_signatures), new.keys(new_content, new_signatures)
    ids = set(ok) | set(nk)
    return Delta(
        added=frozenset(set(nk) - set(ok)), removed=frozenset(set(ok) - set(nk)),
        interface_changed=frozenset(i for i in ids if old.interface_digest(i, old_signatures)
                                    != new.interface_digest(i, new_signatures)),
        key_changed=frozenset(i for i in ids if ok.get(i) != nk.get(i)))

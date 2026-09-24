"""Recursive (fractal) planning: every zoom level is its own DAG.

`decompose(node)` sees only one node's boundary (its contract, the interface it must
provide, the files it may write, and the interfaces it may call) and returns either LEAF
or a sub-DAG of 2-4 children whose external interface equals the node's: endpoints stay
fixed when zooming in. Siblings are decomposed concurrently, so planning latency grows
with depth, not with the number of nodes.

After planning:
  skeleton   the harness writes every leaf's interface stubs mechanically (no model)
  fill       leaf agents implement their stubs in parallel worktrees, each writing its own
             unit test that fakes its dependencies at their interfaces
  check      in parallel with the leaves, a checker agent per internal node writes a test of
             that node's contract through the real code (no fakes), from the contract alone
  merge      apply every passing leaf patch plus the checker tests, then run each node's
             check; a failing node whose children pass localizes the fault to one cut
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .planner import (_accept, _agent, _drop_worktree, _exclude_store, _worktree, changed_files, git,
                      leftover_stubs, outside, run_test)
from fractal_harness.cache import STORE_DIR

DECOMPOSE_PROMPT = """\
You plan one node of a recursive, contract-first implementation. Do NOT write code files.

Overall task (context only):
{task}

THIS NODE: {path}
Contract: {contract}
Interface this node must provide (exact stubs; empty means none):
{interface}
Files this node may write: {writes}
Interfaces this node may call (provided elsewhere, already fixed):
{uses}
{known}
Decide one of:
(a) LEAF: one agent can implement this node in one pass (roughly under {leaf_lines} lines
    of change across at most 2 files). Prefer LEAF whenever that holds.{force_leaf}
(b) SPLIT into children forming a DAG: one child per natural unit of work at this zoom
    level, however many that is (at least 2). Grouping rule: when a cluster of units has
    many interfaces among themselves but a small interface to everything else, make the
    cluster ONE child and let its own planner split it later; that decides its internal
    interfaces in parallel with the other children instead of here. Rules:
    - Children's write sets are disjoint, drawn from this node's files (new files allowed
      under the same directories; never test files).
    - Every stub in this node's interface is owned by exactly one child, copied verbatim.
    - Children may add new interfaces between themselves; each is owned by one child.
    - A child that calls a sibling lists it in depends_on and uses only its stubs.

Stubs are Python code: a complete `def`/`class` header with full type hints and a docstring
stating the contract precisely (inputs, outputs, errors, edge cases, exact output formats),
with body `raise NotImplementedError`. Data classes may be declared in full (fields only).
Include the imports each stub needs in "imports". A change to an existing function (e.g.
adding a CLI subcommand) needs no stub; describe it in the contract.

{retry}Explore the repo only as much as needed. Reply with ONLY a JSON object in a ```json block:
{{"leaf": true}}
or
{{"leaf": false, "children": [
  {{"id": "<short-id>", "contract": "<1-3 sentences>", "writes": ["<file>"],
    "depends_on": ["<sibling id>"],
    "interface": [{{"file": "<file>", "imports": ["from decimal import Decimal"], "stub": "<code>"}}]}}
]}}
"""

LEAF_PROMPT = """\
You implement ONE leaf of a planned change. Other agents implement the other leaves at the
same time in separate copies of the repo; you will never see their code.

Overall task (context only):
{task}

YOUR LEAF: {path}
Contract: {contract}
{known}Files you may modify: {writes}
You may also create your own unit test: {test}
Interfaces you may call (implemented by others; stubs with docstrings are in the code):
{uses}

1. Implement EVERY stub in your files fully and correctly per its docstring (production
   quality). Your leaf is rejected if any `raise NotImplementedError` remains in your files.
   Also make any change to existing code your contract describes.
2. Write {test}: unit tests of your contract. Replace anything you call from other leaves
   with fakes (monkeypatch) so your tests pass whatever the others do.
3. Done when `{cmd}` passes. Never modify any other file.
Finish with a one-line summary.
"""

CHECK_PROMPT = """\
You write the acceptance test for one node of a planned change. Its implementation does
not exist yet; other agents are writing it now. You only have the contract and interfaces.

Overall task (context only):
{task}

NODE: {path}
Contract: {contract}
Interfaces inside this node (exact stubs; module paths from the file names):
{interfaces}

Write {test}: 3-6 focused tests of the most important observable behavior the contract
states (exact formats, errors), through the real code with NO fakes (temporary files and
directories for data are fine). Match the interfaces exactly. You cannot run the tests
against an implementation, so keep them simple and syntactically valid. Look at existing
code only as needed. Only create {test}.
Finish with a one-line summary.
"""


@dataclass
class Stub:
    file: str
    stub: str
    imports: list[str] = field(default_factory=list)


@dataclass
class Node:
    path: str                                   # "root/alerts/format"
    contract: str
    writes: list[str]
    interface: list[Stub] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)    # sibling paths
    children: list["Node"] = field(default_factory=list)
    leaf: bool = False
    depth: int = 0

    @property
    def slug(self) -> str:
        return re.sub(r"\W+", "_", self.path.split("/", 1)[-1] if "/" in self.path else "root").strip("_")

    def walk(self):
        yield self
        for c in self.children:
            yield from c.walk()

    def to_dict(self) -> dict:
        return {"path": self.path, "contract": self.contract, "writes": self.writes, "leaf": self.leaf,
                "depends_on": self.depends_on, "interface": [asdict(s) for s in self.interface],
                "children": [c.to_dict() for c in self.children]}


class DecomposeError(ValueError):
    pass


def defined_names(stub: str) -> list[str]:
    try:
        tree = ast.parse(stub)
    except SyntaxError as e:
        raise DecomposeError(f"stub is not valid Python: {e}") from e
    return [n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]


def _json_block(text: str) -> dict:
    m = re.search(r"```json\s*(\{.*?\})\s*```", text, re.S) or re.search(r"(\{.*\})", text, re.S)
    if not m:
        raise DecomposeError("decomposer returned no JSON")
    return json.loads(m.group(1))


def validate_split(parent: Node, raw: dict, max_children: int | None = None) -> list[Node]:
    """Children for `parent` from a decomposer reply, enforcing that endpoints stay fixed.

    Width is the planner's call (one child per natural unit); `max_children` is only an
    optional cost limit, not a structural rule.
    """
    kids_raw = raw.get("children") or []
    if len(kids_raw) < 2:
        raise DecomposeError(f"{parent.path}: a split needs at least 2 children (use leaf instead)")
    if max_children and len(kids_raw) > max_children:
        raise DecomposeError(f"{parent.path}: {len(kids_raw)} children exceeds the cost limit of "
                             f"{max_children}; group related units into intermediate nodes")
    kids, owner = [], {}
    ids = [str(k["id"]) for k in kids_raw]
    if len(set(ids)) != len(ids):
        raise DecomposeError(f"{parent.path}: duplicate child ids")
    parent_dirs = {str(Path(w).parent) for w in parent.writes}
    for k in kids_raw:
        writes = list(k["writes"])
        for w in writes:
            if w.startswith("tests/") or "/test_" in w or Path(w).name.startswith("test_"):
                raise DecomposeError(f"{k['id']}: may not own test file {w}")
            allowed = parent.path == "root" or w in parent.writes or str(Path(w).parent) in parent_dirs
            if not allowed:
                raise DecomposeError(f"{k['id']}: writes {w} outside parent's files {parent.writes}")
            if w in owner:
                raise DecomposeError(f"write sets overlap on {w}: {owner[w]} and {k['id']}")
            owner[w] = k["id"]
        stubs = [Stub(s["file"], s["stub"], list(s.get("imports", []))) for s in k.get("interface", [])]
        for s in stubs:
            if s.file not in writes:
                raise DecomposeError(f"{k['id']}: stub in {s.file}, not in its write set")
            defined_names(s.stub)
        deps = [str(d) for d in k.get("depends_on", [])]
        for d in deps:
            if d not in ids or d == k["id"]:
                raise DecomposeError(f"{k['id']}: bad dependency {d}")
        kids.append(Node(f"{parent.path}/{k['id']}", k["contract"], writes, stubs,
                         [f"{parent.path}/{d}" for d in deps], depth=parent.depth + 1))
    provided = {n: k.path for k in kids for s in k.interface for n in defined_names(s.stub)}
    for s in parent.interface:
        for n in defined_names(s.stub):
            if n not in provided:
                raise DecomposeError(f"{parent.path}: interface name {n} not provided by any child")
    _acyclic(kids)
    return kids


def _acyclic(kids: list[Node]) -> None:
    by = {k.path: k for k in kids}
    state: dict[str, int] = {}

    def visit(p: str) -> None:
        if state.get(p) == 1:
            raise DecomposeError(f"dependency cycle through {p}")
        if state.get(p) == 2:
            return
        state[p] = 1
        for d in by[p].depends_on:
            visit(d)
        state[p] = 2

    for p in by:
        visit(p)


def _render(stubs: list[Stub]) -> str:
    return "\n".join(f"# {s.file}\n{s.stub}" for s in stubs) or "(none)"


ROOT_GROUPS_NOTE = """
    THIS IS THE ROOT: split into at most {n} coarse groups. Give each group only the
    interfaces OTHER groups call; leave everything internal to a group (its modules, helper
    functions, internal types) to that group's own planner, which runs in parallel with the
    others. Do not answer LEAF at the root."""


KNOWN = """
What is already known about this code: a verified manifest (every claim was checked against
the current source). Rely on it; read code only for details it does not cover.
{manifest}
"""


def decompose(root: Path, wt: Path, node: Node, task: str, uses: list[Stub], test_cmd: str, model: str,
              max_depth: int, leaf_lines: int, max_children: int | None = None,
              root_groups: int | None = None, manifest: str | None = None) -> dict:
    """Ask for LEAF or a validated split; on an invalid split, retry once with the error.

    `root_groups` forces a coarse root: at most that many groups, internals deferred to
    sub-planners (depth instead of width)."""
    force = "" if node.depth < max_depth else "\n    Maximum depth reached: you MUST answer LEAF."
    if node.depth == 0 and root_groups:
        force += ROOT_GROUPS_NOTE.format(n=root_groups)
        max_children = root_groups
    runs, retry, error = [], "", None
    for _ in range(2):
        prompt = DECOMPOSE_PROMPT.format(task=task, path=node.path, contract=node.contract,
                                         interface=_render(node.interface), writes=", ".join(node.writes) or "any",
                                         uses=_render(uses), leaf_lines=leaf_lines, force_leaf=force, retry=retry,
                                         known=KNOWN.format(manifest=manifest) if manifest else "")
        run = _agent(wt, prompt, test_cmd, model, max_turns=30, edit=False)
        runs.append(run)
        try:
            raw = _json_block(run["result"])
            children = [] if raw.get("leaf") or node.depth >= max_depth else validate_split(node, raw, max_children)
            return {"leaf": not children, "children": children, "runs": runs, "error": None}
        except (DecomposeError, KeyError, TypeError, json.JSONDecodeError) as e:
            error = str(e)
            retry = (f"YOUR PREVIOUS ANSWER WAS REJECTED: {error}\nFix that and answer again.\n\n")
    return {"leaf": True, "children": [], "runs": runs, "error": error}


def plan_tree(root: Path, task: str, test_cmd: str, model: str = "sonnet", max_depth: int = 2,
              leaf_lines: int = 150, max_children: int | None = None,
              on_split: Callable[[Node], None] | None = None, root_groups: int | None = None,
              manifest: str | None = None) -> dict:
    """Level-synchronous recursive decomposition. Returns the tree plus timing per level.

    `on_split(node)` is called as soon as a node is split, so work that only needs the
    node's contract and its children's interfaces (its checker test) can start at once.
    """
    root = root.resolve()
    _exclude_store(root)
    head = git(root, "rev-parse", "HEAD")
    wt = _worktree(root, f"rplan-{uuid.uuid4().hex[:6]}", head)
    tree = Node("root", task, ["**"])
    levels, cost, errors, retries = [], 0.0, [], 0
    try:
        frontier = [tree]
        while frontier:
            t0 = time.time()

            def one(n: Node) -> tuple[Node, dict]:
                siblings = [s for c in _parent(tree, n).children for s in c.interface] if n is not tree else []
                external = [s for s in siblings if s not in n.interface]
                return n, decompose(root, wt, n, task, external, test_cmd, model, max_depth, leaf_lines,
                                    max_children, root_groups, manifest)

            with ThreadPoolExecutor(max(1, len(frontier))) as pool:
                replies = list(pool.map(one, frontier))
            nxt = []
            for n, rep in replies:
                cost += sum(r["cost_usd"] for r in rep["runs"])
                retries += len(rep["runs"]) - 1
                if rep["error"]:
                    errors.append(f"{n.path}: {rep['error']}")   # two rejections: one pass instead
                if rep["leaf"]:
                    n.leaf = True
                    continue
                n.children = rep["children"]
                nxt.extend(n.children)
                if on_split:
                    on_split(n)
            levels.append({"depth": frontier[0].depth, "nodes": len(frontier),
                           "wall_s": round(time.time() - t0, 1)})
            frontier = nxt
    finally:
        _drop_worktree(root, wt)
    return {"tree": tree, "levels": levels, "cost_usd": cost, "errors": errors, "retries": retries,
            "plan_s": round(sum(l["wall_s"] for l in levels), 1), "head": head}


def _parent(tree: Node, n: Node) -> Node:
    for x in tree.walk():
        if n in x.children:
            return x
    return tree


def _leaves(tree: Node) -> list[Node]:
    return [n for n in tree.walk() if n.leaf]


def _uses(tree: Node, leaf: Node) -> list[Stub]:
    """Interfaces a leaf may call: every stub owned by any other leaf."""
    return [s for n in _leaves(tree) if n is not leaf for s in n.interface]


def write_skeleton(root: Path, tree: Node, head: str) -> str:
    """Write every leaf's stubs into the code mechanically and commit. Returns the commit sha."""
    wt = _worktree(root, f"rskel-{uuid.uuid4().hex[:6]}", head)
    try:
        by_file: dict[str, list[Stub]] = {}
        for n in _leaves(tree):
            for s in n.interface:
                by_file.setdefault(s.file, []).append(s)
        for rel, stubs in by_file.items():
            path = wt / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            existing = path.read_text() if path.exists() else ""
            imports = [i for s in stubs for i in s.imports if i not in existing]
            head_lines = [] if existing else ["from __future__ import annotations", ""]
            body = "\n\n\n".join(s.stub.strip() for s in stubs)
            text = existing.rstrip() + ("\n\n\n" if existing else "") + "\n".join(
                head_lines + list(dict.fromkeys(imports))) + ("\n\n\n" if imports or head_lines else "") + body + "\n"
            path.write_text(text)
            init = path.parent / "__init__.py"
            if path.suffix == ".py" and not init.exists() and path.parent != wt:
                init.write_text("")
        git(wt, "add", "-A")
        git(wt, "-c", "user.name=fractal", "-c", "user.email=fractal@localhost", "commit", "-q",
            "--allow-empty", "-m", "fractal recursive skeleton")
        sha = git(wt, "rev-parse", "HEAD")
        git(root, "update-ref", f"refs/fractal/rskel-{sha[:8]}", sha)
    finally:
        _drop_worktree(root, wt)
    return sha


def _owner_context(root: Path, leaf: Node, use_manifest: bool) -> str:
    """The leaf owner's refreshable context: the manifest for its region, if claims exist."""
    if not use_manifest:
        return ""
    from fractal_harness.manifest import manifest
    from fractal_harness.regions import placement, tracked_files
    return KNOWN.format(manifest=manifest(root, placement(leaf.writes, tracked_files(root))))


def _fill_leaf(root: Path, tree: Node, leaf: Node, task: str, skeleton: str, test_cmd: str, model: str,
               max_turns: int = 60, use_manifest: bool = False) -> dict:
    wt = _worktree(root, f"rleaf-{leaf.slug}-{uuid.uuid4().hex[:4]}", skeleton)
    test = f"tests/test_leaf_{leaf.slug}.py"
    try:
        run = _agent(wt, LEAF_PROMPT.format(task=task, path=leaf.path, contract=leaf.contract,
                                            writes=", ".join(leaf.writes), test=test,
                                            uses=_render(_uses(tree, leaf)),
                                            known=_owner_context(root, leaf, use_manifest),
                                            cmd=test_cmd.replace("{test}", test)), test_cmd, model,
                     max_turns=max_turns)
        ok, out = run_test(wt, test_cmd, test)
        stubs = leftover_stubs(wt, leaf.writes)
        files = changed_files(wt, skeleton)
        patch = git(wt, "diff", "--cached", "--binary", skeleton)
    finally:
        _drop_worktree(root, wt)
    return {"node": leaf.path, "kind": "leaf", "passed": ok and not stubs, "test_passed": ok,
            "leftover_stubs": stubs, "violations": outside(files, leaf.writes + [test]),
            "patch": patch + "\n" if patch else "", "test": test, "test_output": "" if ok else out[-400:],
            **{k: run[k] for k in ("cost_usd", "turns", "duration_s")}}


CHECK_MAX_TURNS = 15


def write_check(root: Path, node: Node, task: str, base: str, test_cmd: str, model: str) -> dict:
    """Write a node's no-fakes contract test from its contract and its children's interfaces.
    Needs no skeleton, so it can start the moment the node is split."""
    wt = _worktree(root, f"rcheck-{node.slug}-{uuid.uuid4().hex[:4]}", base)
    test = f"tests/test_node_{node.slug}.py"
    interfaces = _render([s for c in node.children for s in c.interface] or node.interface)
    try:
        run = _agent(wt, CHECK_PROMPT.format(task=task, path=node.path, contract=node.contract,
                                             interfaces=interfaces, test=test), test_cmd, model,
                     max_turns=CHECK_MAX_TURNS)
        files = changed_files(wt, base)
        patch = git(wt, "diff", "--cached", "--binary", base, "--", test)
    finally:
        _drop_worktree(root, wt)
    return {"node": node.path, "kind": "check", "test": test, "exists": test in files,
            "violations": [f for f in files if f != test], "patch": patch + "\n" if patch else "",
            **{k: run[k] for k in ("cost_usd", "turns", "duration_s")}}


def run_tree(root: Path, planned: dict, task: str, test_cmd: str, model: str = "sonnet",
             acceptance: str | None = None, workers: int = 8, checks: list[Future] | None = None,
             leaf_max_turns: int = 60, use_manifest: bool = False) -> dict:
    """Skeleton, parallel leaves, merge. `checks` are checker futures started during planning;
    if None, checkers for every internal node start now alongside the leaves."""
    root = root.resolve()
    tree: Node = planned["tree"]
    t0 = time.time()
    skeleton = write_skeleton(root, tree, planned["head"])
    skel_s = round(time.time() - t0, 1)
    leaves = _leaves(tree)
    internal = [n for n in tree.walk() if not n.leaf]
    t1 = time.time()
    with ThreadPoolExecutor(max(1, min(workers, len(leaves) + len(internal)))) as pool:
        futs = [pool.submit(_fill_leaf, root, tree, l, task, skeleton, test_cmd, model, leaf_max_turns,
                            use_manifest) for l in leaves]
        if checks is None:
            futs += [pool.submit(write_check, root, n, task, planned["head"], test_cmd, model) for n in internal]
        results = [f.result() for f in futs]
        leaves_s = round(time.time() - t1, 1)
        results += [f.result() for f in (checks or [])]
    fill_s = round(time.time() - t1, 1)

    wt = _worktree(root, f"rmerge-{uuid.uuid4().hex[:6]}", skeleton)
    try:
        applied = []
        for r in results:
            usable = r["patch"] and (r["kind"] == "check" or (r["passed"] and not r["violations"]))
            if usable:
                p = subprocess.run(["git", "apply", "--index", "-"], cwd=wt, input=r["patch"], text=True,
                                   capture_output=True)
                if p.returncode == 0:
                    applied.append(r["node"] + (" (check)" if r["kind"] == "check" else ""))
        node_checks = {r["node"]: run_test(wt, test_cmd, r["test"])[0]
                       for r in results if r["kind"] == "check" and r["exists"]}
        leaf_tests = {r["node"]: run_test(wt, test_cmd, r["test"])[0] for r in results if r["kind"] == "leaf"}
        full, _ = run_test(wt, test_cmd, "")
        accepted = _accept(wt, test_cmd, acceptance)
    finally:
        _drop_worktree(root, wt)
    return {"skeleton": skeleton, "skeleton_s": skel_s, "fill_s": fill_s, "leaves_s": leaves_s,
            "cost_usd": sum(r["cost_usd"] for r in results), "applied": applied,
            "leaves": [{k: v for k, v in r.items() if k != "patch"} for r in results if r["kind"] == "leaf"],
            "checks": [{k: v for k, v in r.items() if k != "patch"} for r in results if r["kind"] == "check"],
            "merge": {"leaf_tests": leaf_tests, "node_checks": node_checks, "full_suite": full,
                      "acceptance": accepted}}


def build(root: Path, task: str, test_cmd: str, model: str = "sonnet", plan_model: str | None = None,
          max_depth: int = 2, max_children: int | None = None, acceptance: str | None = None,
          root_groups: int | None = None, use_manifest: bool = False, leaf_max_turns: int = 60,
          plan_only: bool = False, protocol: str = "tests", check_model: str | None = None) -> dict:
    """protocol: "tests" (leaves write faked-dependency unit tests) or "implement" (leaves only
    implement; independent checkers verify each cut; one retry at failing cuts)."""
    """Plan recursively, starting each internal node's checker the moment it is split, then
    fill leaves in parallel and merge. Returns planning and run results with timings."""
    root = root.resolve()
    t0 = time.time()
    pool = ThreadPoolExecutor(8)
    checks: list[Future] = []
    head = git(root, "rev-parse", "HEAD")
    text = None
    if use_manifest:
        from fractal_harness.manifest import manifest
        text = manifest(root)
    early: dict[str, Future] = {}

    def on_split(n: Node) -> None:
        f = pool.submit(write_check, root, n, task, head, test_cmd, check_model or model)
        checks.append(f)
        early[n.path] = f

    planned = plan_tree(root, task, test_cmd, plan_model or model, max_depth, max_children=max_children,
                        on_split=None if plan_only else on_split, root_groups=root_groups, manifest=text)
    if plan_only:
        pool.shutdown(wait=True)
        return {"planned": planned, "run": None, "total_s": round(time.time() - t0, 1),
                "cost_usd": planned["cost_usd"]}
    if protocol == "implement":
        run = run_tree_implement(root, planned, task, test_cmd, model, acceptance, checks=early,
                                 leaf_max_turns=leaf_max_turns, use_manifest=use_manifest, check_model=check_model)
    else:
        run = run_tree(root, planned, task, test_cmd, model, acceptance, checks=checks,
                       leaf_max_turns=leaf_max_turns, use_manifest=use_manifest)
    pool.shutdown(wait=True)
    return {"planned": planned, "run": run, "total_s": round(time.time() - t0, 1),
            "cost_usd": planned["cost_usd"] + run["cost_usd"]}


# --- implement-only leaves (verification belongs to checkers, level by level) --------------

IMPLEMENT_PROMPT = """\
You implement ONE leaf of a planned change. Other agents implement the other leaves at the
same time in separate copies of the repo.

Overall task (context only):
{task}

YOUR LEAF: {path}
Contract: {contract}
{known}Files you may modify: {writes}
Interfaces you may call (implemented by others): use them exactly as their signatures and
docstrings say. Do not guess how they work beyond that, and do not test them.
{uses}

Implement EVERY stub in your files fully and correctly per its docstring, and make any change
to existing code your contract describes. Rules:
- Do not write tests; your work is checked independently.
- Do not change stub signatures. Do not modify any other file.
- You may check that your code imports: `{py} -c "import {modules}"`.
Finish with a one-line summary.{feedback}
"""

RETRY_FEEDBACK = """

RETRY: your previous implementation is already in your files. After merging every leaf, these
checks failed:
{failures}
Fix your files only where the failure comes from your code; if it does not, change nothing."""


def _module(rel: str) -> str | None:
    return rel[:-3].replace("/", ".").removesuffix(".__init__") if rel.endswith(".py") else None


def signature_mismatches(wt: Path, stubs: list[Stub]) -> list[str]:
    """Stubbed functions whose argument list or return annotation changed in the implementation."""
    out = []
    for s in stubs:
        want = {n.name: n for n in ast.parse(s.stub).body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        path = wt / s.file
        try:
            have = {n.name: n for n in ast.walk(ast.parse(path.read_text()))
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        except (OSError, SyntaxError) as e:
            out.append(f"{s.file}: {e}")
            continue
        for name, w in want.items():
            h = have.get(name)
            if h is None:
                out.append(f"{s.file}: {name} missing")
            elif ast.dump(h.args) != ast.dump(w.args) or ast.dump(h.returns or ast.Constant(None)) != \
                    ast.dump(w.returns or ast.Constant(None)):
                out.append(f"{s.file}: {name} signature changed")
    return out


def import_errors(wt: Path, test_cmd: str, writes: list[str]) -> list[str]:
    import shlex
    py = shlex.split(test_cmd.replace("{test}", ""))[0]
    errs = []
    for rel in writes:
        mod = _module(rel)
        if mod and (wt / rel).exists():
            p = subprocess.run([py, "-c", f"import {mod}"], cwd=wt, capture_output=True, text=True, timeout=60)
            if p.returncode:
                errs.append(f"{mod}: {p.stderr.strip().splitlines()[-1] if p.stderr.strip() else 'import failed'}")
    return errs


def implement_leaf(root: Path, tree: Node, leaf: Node, task: str, base: str, test_cmd: str, model: str,
                   max_turns: int = 30, use_manifest: bool = False, feedback: str = "") -> dict:
    """One implement-only leaf pass in a worktree off `base`; gated mechanically, no tests."""
    import shlex
    wt = _worktree(root, f"rimpl-{leaf.slug}-{uuid.uuid4().hex[:4]}", base)
    py = shlex.split(test_cmd.replace("{test}", ""))[0]
    modules = ", ".join(m for m in (_module(w) for w in leaf.writes) if m) or "your module"
    try:
        run = _agent(wt, IMPLEMENT_PROMPT.format(
            task=task, path=leaf.path, contract=leaf.contract, known=_owner_context(root, leaf, use_manifest),
            writes=", ".join(leaf.writes), uses=_render(_uses(tree, leaf)), py=py, modules=modules,
            feedback=feedback), test_cmd, model, max_turns=max_turns)
        stubs = leftover_stubs(wt, leaf.writes)
        sigs = signature_mismatches(wt, leaf.interface)
        imports = import_errors(wt, test_cmd, leaf.writes)
        files = changed_files(wt, base)
        patch = git(wt, "diff", "--cached", "--binary", base)
    finally:
        _drop_worktree(root, wt)
    violations = outside(files, leaf.writes)
    gate = {"leftover_stubs": stubs, "signature_changes": sigs, "import_errors": imports, "violations": violations}
    return {"node": leaf.path, "kind": "leaf", "passed": not any(gate.values()), **gate,
            "patch": patch + "\n" if patch else "", **{k: run[k] for k in ("cost_usd", "turns", "duration_s")}}


def _checked_nodes(tree: Node) -> list[Node]:
    """Internal nodes (composition) plus leaves with no dependencies (testable alone, no fakes)."""
    return [n for n in tree.walk() if not n.leaf or not n.depends_on]


def _apply(wt: Path, patch: str) -> bool:
    if not patch.strip():
        return True
    return subprocess.run(["git", "apply", "--index", "-"], cwd=wt, input=patch, text=True,
                          capture_output=True).returncode == 0


def corroborated_cuts(failing: list[str], baseline_ok: bool) -> list[str]:
    """Failing checks worth a retry, as the deepest failing node of each corroborated branch.

    One checker's test failing alone may be the test's fault. A failure is corroborated when
    another check on the same branch (an ancestor or descendant node) also fails, or when the
    repo's original tests broke. Retrying at the deepest failing node keeps the fix local.
    """
    def related(a: str, b: str) -> bool:
        return a != b and (a.startswith(b + "/") or b.startswith(a + "/"))

    confirmed = [p for p in failing if not baseline_ok or any(related(p, q) for q in failing)]
    return [p for p in confirmed if not any(q.startswith(p + "/") for q in confirmed)]


def baseline_tests(root: Path, head: str) -> list[str]:
    """Test files that existed before the change (the repo's own suite)."""
    out = git(root, "ls-tree", "-r", "--name-only", head, "tests", check=False)
    return [f for f in out.splitlines() if f.endswith(".py") and Path(f).name.startswith("test_")]


def run_tree_implement(root: Path, planned: dict, task: str, test_cmd: str, model: str = "sonnet",
                       acceptance: str | None = None, checks: dict[str, Future] | None = None,
                       leaf_max_turns: int = 30, use_manifest: bool = False, retry: bool = True,
                       check_model: str | None = None) -> dict:
    """Implement-only leaves, then level-by-level checks, then one retry at each corroborated
    failing cut (see corroborated_cuts)."""
    root = root.resolve()
    tree: Node = planned["tree"]
    skeleton = write_skeleton(root, tree, planned["head"])
    leaves = _leaves(tree)
    t1 = time.time()
    with ThreadPoolExecutor(8) as pool:
        missing = [n for n in _checked_nodes(tree) if n.path not in (checks or {})]
        extra = {n.path: pool.submit(write_check, root, n, task, planned["head"], test_cmd, check_model or model)
                 for n in missing}
        first = list(pool.map(lambda l: implement_leaf(root, tree, l, task, skeleton, test_cmd, model,
                                                       leaf_max_turns, use_manifest), leaves))
        leaves_s = round(time.time() - t1, 1)
        check_results = {p: f.result() for p, f in {**(checks or {}), **extra}.items()}
    wt = _worktree(root, f"rmerge-{uuid.uuid4().hex[:6]}", skeleton)
    rounds, retried = [], []
    try:
        for r in check_results.values():
            _apply(wt, r["patch"])
        for r in first:
            if r["passed"]:
                _apply(wt, r["patch"])

        base_tests = " ".join(baseline_tests(root, planned["head"]))

        def evaluate() -> dict:
            node = {p: run_test(wt, test_cmd, r["test"]) for p, r in check_results.items() if r["exists"]}
            baseline_ok = run_test(wt, test_cmd.replace("{test}", base_tests), "")[0] if base_tests else True
            return {"checks": {p: ok for p, (ok, _) in node.items()},
                    "outputs": {p: out for p, (ok, out) in node.items() if not ok},
                    "baseline_ok": baseline_ok,
                    "full_suite": run_test(wt, test_cmd, "")[0], "acceptance": _accept(wt, test_cmd, acceptance)}

        ev = evaluate()
        rounds.append({k: v for k, v in ev.items() if k != "outputs"})
        failing = [p for p, ok in ev["checks"].items() if not ok]
        cuts = corroborated_cuts(failing, ev["baseline_ok"])
        rounds[-1]["retry_cuts"] = cuts
        gate_failed = [r["node"] for r in first if not r["passed"]]
        if retry and (cuts or gate_failed):
            git(wt, "-c", "user.name=fractal", "-c", "user.email=fractal@localhost", "commit", "-q",
                "--allow-empty", "-m", "fractal merge (first pass)")
            merged = git(wt, "rev-parse", "HEAD")
            by_path = {n.path: n for n in tree.walk()}
            feedback: dict[str, list[str]] = {}
            for p in cuts:                          # the failing cut: every leaf under that node
                for l in by_path[p].walk():
                    if l.leaf:
                        feedback.setdefault(l.path, []).append(f"{p} check:\n{ev['outputs'][p][-1200:]}")
            for r in first:
                if not r["passed"]:
                    problems = {k: r[k] for k in ("leftover_stubs", "signature_changes", "import_errors",
                                                  "violations") if r[k]}
                    feedback.setdefault(r["node"], []).append(f"your first pass was rejected: {problems}")
            with ThreadPoolExecutor(8) as pool:
                second = list(pool.map(lambda path: implement_leaf(
                    root, tree, by_path[path], task, merged, test_cmd, model, leaf_max_turns, use_manifest,
                    RETRY_FEEDBACK.format(failures="\n\n".join(feedback[path]))), feedback))
            for r in second:
                if r["passed"]:
                    _apply(wt, r["patch"])
            retried = second
            ev = evaluate()
            rounds.append({k: v for k, v in ev.items() if k != "outputs"})
    finally:
        _drop_worktree(root, wt)
    strip = lambda rs: [{k: v for k, v in r.items() if k != "patch"} for r in rs]
    all_runs = first + retried + list(check_results.values())
    return {"skeleton": skeleton, "leaves_s": leaves_s, "fill_s": round(time.time() - t1, 1),
            "cost_usd": sum(r["cost_usd"] for r in all_runs), "leaves": strip(first), "retries": strip(retried),
            "checks": strip(list(check_results.values())), "rounds": rounds,
            "merge": {"node_checks": ev["checks"], "full_suite": ev["full_suite"], "acceptance": ev["acceptance"]}}

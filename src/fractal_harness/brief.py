"""`fractal brief <node>`: the prompt for an agent that will work on a node, generated, not hand-written.

Deterministic from data already in the repo and the ledger (no model): the node's spec, the decisions and
parent contracts it declares as premises, the enforced rules that govern it (all via `context.py`), and why the
node is due (`status.derive`, read-only). The agent gets only that: its node's spec and premises, never the
whole graph. Same inputs give the same text, so a brief can be cached and diffed; its digest is recorded so a
filed report can say which brief it answers.

Sections, in order: repo root (the wrong-repo fix: an absolute path and a check before anything else), why the
node is due, task and acceptance, spec and premises, reporting, heavy runs. The three prose sections have
defaults below; `[brief]` in fractal.toml replaces one (`task = "..."`) or extends it (`task_extra = "..."`).
`{node}`, `{root}`, `{report_path}`, `{file_cmd}` are filled in. Nothing here names a project.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path

from .cache import STORE_DIR, ClaimCache, store_root
from .config import load
from .symbols import SymbolError, read_ref

NODE_RE = re.compile(r"^[A-Za-z0-9][\w.-]*$")
DEFAULT_REPORT_DIR = f"{STORE_DIR}/reports/scratch"

DEFAULTS = {
    "task": (
        "Do the work this node's spec describes, bounded by that spec and by the premises below (assume exactly\n"
        "those and nothing more). The node is verified when its verdicts are recorded and `fractal status {node}`\n"
        "says `done`:\n"
        "- Choose mutants of the code you wrote or changed and run them with `fractal mutate {node} --json <mutants.json>\n"
        "  --cmd '<test command> {{tests}}'`. It applies each one in a scratch worktree, runs the tests, and records\n"
        "  killed/survived. Do not write your own mutant scripts.\n"
        "- A survivor you can argue is equivalent: `fractal ledger record {node} --file F --span N --mutant M\n"
        "  --result equivalent --argument '<why>'`. Otherwise strengthen the tests and run it again.\n"
        "- Record any other verdict with `fractal ledger record {node} ...`; `fractal verify-plan {node}` shows what is stale."),
    "reporting": (
        "Write your report (what you did, the verdicts, anything you were unsure of) to:\n"
        "    {report_path}\n"
        "Write it there, as a file, even if you work in an isolated worktree: it is untracked scratch space, not part\n"
        "of any commit. Do not write reports into the repo's docs yourself. Your orchestrator files it with:\n"
        "    {file_cmd}\n"
        "Also give the report's key findings in your final message."),
    "heavy": (
        "Anything long or heavy (a full benchmark, a sanitizer or fuzz run, a large test matrix) goes through\n"
        "`fractal run [--node {node}] [--timeout S] --detach <command>`, which prints a run id and returns at once.\n"
        "Do not wait on long local background jobs and do not sleep-loop. Poll with `fractal run status <id>`;\n"
        "read output with `fractal run log <id>`. Do other work or finish your report while it runs."),
}
TITLES = {"task": "Task and acceptance", "reporting": "Reporting", "heavy": "Heavy runs"}


def report_path(root: Path, node: str) -> Path:
    """Where the agent writes its report: `[brief] report_dir` (default untracked, in the main checkout so an
    isolated worktree can still reach it), absolute so no working directory changes its meaning."""
    d = Path(load(root).brief.get("report_dir", DEFAULT_REPORT_DIR)).expanduser()
    return ((d if d.is_absolute() else store_root(root) / d) / f"{node}.md").resolve()


def _git_common(root: Path) -> str:
    p = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=root,
                       capture_output=True, text=True)
    return p.stdout.strip() if p.returncode == 0 else ""


def root_block(root: Path) -> list[str]:
    common = _git_common(root)
    out = ["## Repo root (do this before anything else)", "",
           "This work belongs to the repository at:", "", f"    {root}", "",
           f"First `cd {root}`, then run `git rev-parse --show-toplevel` and confirm it prints exactly that path"
           + (f" (in an isolated worktree of this repo, confirm instead that `git rev-parse --path-format=absolute "
              f"--git-common-dir` prints `{common}`)." if common else "."),
           "If it does not match, stop and say so; do not read or edit files in any other repository.", ""]
    return out


def _fill(text: str, **kw: str) -> str:
    for k, v in kw.items():
        text = text.replace("{" + k + "}", v)
    return text.replace("{{", "{").replace("}}", "}")


def section(root: Path, name: str, node: str) -> str:
    """The prose section `name`: the default, or `[brief] <name>` instead of it, plus `[brief] <name>_extra`."""
    b = load(root).brief
    text = b.get(name, DEFAULTS[name]).strip("\n")
    if extra := b.get(f"{name}_extra", "").strip("\n"):
        text += "\n" + extra
    rp = report_path(root, node)
    return _fill(text, node=node, root=str(root), report_path=str(rp), file_cmd=f"fractal report file {rp} --node {node}")


def _git(root: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def where_lines(cache: ClaimCache, root: Path, node: str) -> list[str]:
    """Where the node's work lives: the branch registered with it (or `node-<name>`), its tip, and every worktree
    checked out on that branch, plus whether this checkout is one of them. Read-only facts from git; the
    answer to "which branch and which worktree do I work in?" so an agent does not guess."""
    from .ledger import branch_of
    branch = branch_of(cache, node)
    registered = branch is not None
    branch = branch or f"node-{node}"
    tip = _git(root, "rev-parse", "--short", f"refs/heads/{branch}")
    trees: list[tuple[str, str]] = []
    path = None
    for line in _git(root, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            path = line[9:]
        elif line.startswith("branch ") and path:
            trees.append((path, line[7:].removeprefix("refs/heads/")))
    here = _git(root, "rev-parse", "--show-toplevel")
    on = [p for p, b in trees if b == branch]
    out = [f"- Branch: `{branch}` ({'registered with this node' if registered else 'default for this node; none registered'})"
           + (f", tip {tip}." if tip else ", which does not exist yet.")]
    if on:
        out += [f"- Worktree on it: {', '.join(f'`{p}`' for p in sorted(on))}"
                + (" (this checkout)." if here in on else f"; this checkout is `{here}` on `{dict(trees).get(here, 'a detached HEAD')}`.")]
    else:
        out += [f"- No worktree has `{branch}` checked out; this checkout is `{here}` on `{dict(trees).get(here, 'a detached HEAD')}`."]
    return out


def build(cache: ClaimCache, root: Path, node: str) -> dict:
    """{node, text, digest, state}. Raises SymbolError for an unregistered node or an unreadable spec."""
    from . import status
    from .context import MAX_RULES, premise_sections, premises_of
    from .hook import RULES_HEADER, _agent_query, _rule_line, select_context
    from .nodes import body
    if not NODE_RE.match(node):
        raise SymbolError(f"bad node name {node!r}")
    info = cache.store.get_node(node)
    if info is None:
        raise SymbolError(f"{node} is not registered (`fractal ledger node {node} --spec <spec>`)")
    spec = read_ref(root, info["spec"])
    if spec is None:
        raise SymbolError(f"{node}: spec {info['spec']} is not readable")
    d = status.derive(cache, root, node)
    out = [f"# Brief: node {node}", "", *root_block(root),
           "## Why this node is due", "", f"State: {d['state']}.", *(f"- {r}" for r in d["reasons"]), *where_lines(cache, root, node), "",
           f"## {TITLES['task']}", "", section(root, "task", node), "",
           f"## Spec: {info['spec']}", "", body(spec).strip(), "",
           *premise_sections(cache, root, spec)]
    rules, _ = select_context(root, _agent_query(root, spec), log=False)
    if rules:
        out += ["## Rules", "", RULES_HEADER, *(_rule_line(e) for e in rules[:MAX_RULES]), ""]
    for name in ("reporting", "heavy"):
        out += [f"## {TITLES[name]}", "", section(root, name, node), ""]
    text = "\n".join(out).rstrip() + "\n"
    digest = hashlib.sha256(text.encode()).hexdigest()[:12]
    return {"node": node, "state": d["state"], "digest": digest, "text": text + f"\nbrief-digest: {digest}\n"}


def remember(root: Path, node: str, digest: str) -> None:
    """Note which brief a node's agent was last given, so `report file` can cite it. Untracked, best effort."""
    d = store_root(root) / STORE_DIR / "reports"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"brief-{node}.json").write_text(json.dumps({"node": node, "digest": digest}))


def last_digest(root: Path, node: str) -> str | None:
    f = store_root(root) / STORE_DIR / "reports" / f"brief-{node}.json"
    try:
        return json.loads(f.read_text())["digest"]
    except (OSError, ValueError, KeyError):
        return None

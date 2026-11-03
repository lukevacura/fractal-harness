"""`fractal report file|list`: bring an agent's report from a scratch path into the repo's docs, with provenance.

Agents under worktree isolation cannot write into docs, so a report is written to a scratch path (see
`brief.py`) and the orchestrator files it. The destination is `--to <root:path>` or `[brief] report_to`
(default `.fractal/filed/{node}`, untracked); a destination without a file suffix is a directory and gets the
scratch file's name. Resolution reuses `config.resolve` (a named root must exist and the result must stay
inside it); a plain repo-relative destination gets the same containment check against the repo root, which
`config.resolve_path` alone does not apply. SOUND: path arithmetic and a copy.

The filed copy starts with a comment header: node, commit, UTC timestamp, the digest of the brief the node's
agent was last given (from `fractal brief`), and the scratch file's name. Each filing is appended to
`.fractal/reports/index.jsonl`, which `report list` reads (per checkout store, not a scan of the docs).
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path

from .brief import NODE_RE, last_digest
from .cache import STORE_DIR, ClaimCache, store_root
from .config import _REF, ConfigError, load
from .symbols import SymbolError

DEFAULT_REPORT_TO = f"{STORE_DIR}/filed/{{node}}"


def _index(root: Path) -> Path:
    return store_root(root) / STORE_DIR / "reports" / "index.jsonl"


def destination(root: Path, node: str, scratch: Path, to: str | None = None) -> Path:
    """The file the report will be written to. Raises ConfigError if it escapes its root."""
    cfg = load(root)
    ref = (to or cfg.brief.get("report_to", DEFAULT_REPORT_TO)).replace("{node}", node)
    m = _REF.match(ref)
    f = cfg.resolve(ref) if m else (root / ref).resolve()
    if not m and f != root.resolve() and root.resolve() not in f.parents:
        raise ConfigError(f"{ref!r} escapes the repo root ({root})")
    suffixed = bool(Path(ref.split(":", 1)[1] if m else ref).suffix)
    if ref.endswith("/") or not suffixed:
        f = f / scratch.name        # a file name has no separators, so this stays inside the checked directory
    return f


def file_report(cache: ClaimCache, root: Path, scratch: str, node: str, to: str | None = None, force: bool = False) -> dict:
    from . import ledger
    if not NODE_RE.match(node):
        raise SymbolError(f"bad node name {node!r}")
    src = Path(scratch).expanduser().resolve()
    if not src.is_file():
        raise SymbolError(f"no such report file: {scratch}")
    dest = destination(root, node, src, to)
    if dest.exists() and not force:
        raise SymbolError(f"{dest} exists (--force to replace it)")
    try:
        commit = ledger.node_commit(cache, root, node)
    except SymbolError:
        commit = ledger.resolve_commit(root, "HEAD")
    digest = last_digest(root, node)
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    header = (f"<!-- fractal-report\n  node: {node}\n  commit: {commit}\n  filed: {ts}\n"
              f"  brief-digest: {digest or 'none'}\n  source: {src.name}\n-->\n\n")
    text = src.read_text(errors="replace")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(header + text)
    row = {"node": node, "path": str(dest), "commit": commit, "filed": ts, "brief_digest": digest}
    idx = _index(root)
    idx.parent.mkdir(parents=True, exist_ok=True)
    with idx.open("a") as fh:
        fh.write(json.dumps(row) + "\n")
    return row


def list_reports(root: Path, node: str | None = None) -> list[dict]:
    try:
        rows = [json.loads(ln) for ln in _index(root).read_text().splitlines() if ln.strip()]
    except OSError:
        return []
    rows = [{**r, "exists": Path(r["path"]).is_file()} for r in rows if node is None or r["node"] == node]
    return sorted(rows, key=lambda r: (r["filed"], r["path"]))


def render(rows: list[dict]) -> str:
    if not rows:
        return "no filed reports\n"
    return "".join(f"{r['filed']}  {r['node']}  {r['path']}  commit {r['commit'][:10]}"
                   f"  brief {r['brief_digest'] or 'none'}" + ("" if r["exists"] else "  (file is gone)") + "\n" for r in rows)


def doctor_checks(root: Path) -> list[tuple[bool, str]]:
    """[brief] report_dir: HEURISTIC (warn only) that it is untracked, so scratch reports do not get committed."""
    try:
        d = load(root).brief.get("report_dir")
    except ConfigError:
        return []
    if not d:
        return []
    from .brief import report_path
    p = report_path(root, "x")
    if root.resolve() not in p.parents:
        return [(True, f"brief report_dir {p.parent} is outside the repo (nothing to ignore)")]
    ig = subprocess.run(["git", "check-ignore", "-q", str(p.relative_to(root.resolve()))], cwd=root).returncode == 0
    return [(True, f"brief report_dir {p.parent} " + ("is git-ignored" if ig else
            "is NOT git-ignored: WARN [heuristic], scratch reports could be committed"))]

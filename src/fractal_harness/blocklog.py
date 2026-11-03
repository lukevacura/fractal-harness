"""The block log: each time an enforced rule stops work, record it (rule, authority, file, hook, time).

Sources: the pre-edit hook (an edit that would break a rule), `fractal check`, and the Stop hook. The log is a
HEURISTIC signal: counts only ever raise `conflict` activation events (conflict.py), they never block anything and
never decide an outcome. It is bounded (a capped recent history per rule and overall, plus never-pruned per-rule
counters; see store.BLOCK_KEEP_*) and it is best-effort: every function here swallows its own errors, so logging can
neither break a hook nor change its exit code. It writes only when something was actually blocked.
"""

from __future__ import annotations

from pathlib import Path

DEFAULT_WINDOW_DAYS = 14.0
HOOKS = ("pre-edit", "check", "stop")


def window_seconds(root: Path) -> float:
    try:
        from .config import load
        return load(root).friction_window_days * 86400
    except Exception:
        return DEFAULT_WINDOW_DAYS * 86400


def record(store, edge, file: str, hook: str, root: Path) -> None:
    """Log one block of `edge` on a store that is already open. Never raises."""
    try:
        store.block_add(edge.id, edge.layer, file or "", hook, window_seconds(root))
    except Exception:
        pass


def record_many(root: Path, rows: list[tuple], hook: str) -> None:
    """Log blocks for [(edge, file)] on a store opened here (callers that hold none). Never raises."""
    if not rows:
        return
    try:
        from .cache import STORE_DIR, store_root
        from .store import Store
        path = store_root(root) / STORE_DIR / "edges.db"
        if not path.exists():
            return
        store = Store(path)
        try:
            for edge, file in rows:
                record(store, edge, file, hook, root)
        finally:
            store.db.close()
    except Exception:
        pass

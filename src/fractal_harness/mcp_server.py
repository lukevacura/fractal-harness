"""MCP server exposing the claim cache to any agent harness (stdio transport)."""

from __future__ import annotations

import threading
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from .cache import ClaimCache

INSTRUCTIONS = """\
A verified, self-invalidating cache of claims about this codebase.

Before exploring code, call claims_query for the area you are about to read; a
`verified` claim was checked by a deterministic probe against the current
source, so you can rely on it instead of re-reading those files. `trusted`
claims were not machine-checked: treat them as hints.

After you learn something durable (an invariant, where something lives, how a
workflow is done), record it with claims_put. List every file the claim depends
on in `reads`, and attach a probe whenever the claim can be checked
mechanically (a grep pattern or a command that exits 0), so it stays verified
across future changes.

Before doing a step whose outcome is mechanically checkable, call claims_needed
with that outcome and a probe; skip the step if it reports needed=false.
"""


def build(root: Path) -> MCPServer:
    cache = ClaimCache(root)
    lock = threading.Lock()  # tools run on worker threads; the cache is not thread-safe
    mcp = MCPServer("fractal-claims", instructions=INSTRUCTIONS)

    @mcp.tool()
    def claims_query(text: str = "", paths: list[str] | None = None,
                     include_unverified: bool = False, limit: int = 20) -> list[dict]:
        """Find claims about the codebase by keyword and/or source path.

        Stale matches are re-verified before returning. By default only
        `verified` and `trusted` claims are returned.
        """
        statuses = None if include_unverified else ("verified", "trusted")
        with lock:
            return [e.to_dict() for e in cache.query(text, paths, statuses, limit)]

    @mcp.tool()
    def claims_put(claim: str, reads: list[str], probe: dict | None = None, pre: str = "",
                   kind: str = "knowledge", writes: list[str] | None = None,
                   depends_on: list[str] | None = None, parent_id: str | None = None) -> dict:
        """Record a claim and check it immediately.

        claim: the postcondition, stated so it can be relied on without reading the source.
        reads: repo-relative files or globs the claim depends on; editing, adding, or
          removing a matching file invalidates it.
        probe: optional deterministic check, one of
          {"type": "grep", "pattern": "<regex>", "paths": ["<glob>"],
           "expect": "present" | "absent" | {"count": n} | {"min": n} | {"max": n}}
          {"type": "command", "run": "<shell command>", "expect_exit": 0}
          {"type": "all", "probes": [<probe>, ...]}
          grep is line-based; add "exclude": "<regex>" to skip lines (e.g. comments).
          Every checkable statement in the claim must be covered by the probe.
          Without a probe the claim is stored as `trusted`, not `verified`.
        pre: precondition (for workflow/task edges).
        kind: "knowledge" (fact about code), "workflow" (how to do something), or "task".
        depends_on: ids of claims this one relies on.
        """
        with lock:
            return cache.put(claim, reads, pre=pre, kind=kind, probe=probe, writes=writes,
                             depends_on=depends_on, parent_id=parent_id).to_dict()

    @mcp.tool()
    def claims_needed(claim: str, reads: list[str], probe: dict | None = None, pre: str = "",
                      kind: str = "task", deliberate: bool = False) -> dict:
        """Before doing a step, check whether it is needed at all.

        State the step's outcome as `claim` with a probe that passes only once the
        outcome holds. Returns needed=false if the outcome is already a verified
        claim or the probe passes right now (the step would be redundant, like
        sorting an already-sorted list); in that case skip the step. Set
        deliberate=true for intentional redundancy (e.g. re-validation at a trust
        boundary) so it is never pruned.
        """
        with lock:
            return cache.needed(claim, reads, pre=pre, kind=kind, probe=probe, deliberate=deliberate)

    @mcp.tool()
    def claims_verify(ids: list[str]) -> list[dict]:
        """Force a fresh verdict (re-run probes) on the given claims and their dependencies."""
        with lock:
            return [e.to_dict() for e in cache.verify(ids)]

    @mcp.tool()
    def claims_stats() -> dict:
        """Counts by status plus query/check telemetry."""
        with lock:
            cache.refresh()
            return cache.stats()

    return mcp


def serve(root: Path) -> None:
    build(root).run()

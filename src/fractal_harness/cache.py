"""The claim cache: put, check, invalidate, and lazily re-verify edges.

Trust rules:
  - An edge with a probe is `verified` only if the probe passed at the current
    fingerprint and every dependency is `verified`.
  - An edge without a probe, or depending on a `trusted` edge, is at most `trusted`.
  - A trusted-only edge whose sources changed stays `stale` until re-asserted
    with `put`; re-checking it would launder the change.
  - An edge whose dependency is not good is `stale` (blocked), never rewritten.
"""

from __future__ import annotations

from pathlib import Path

from . import CHECKER_VERSION, probes
from .hashing import edge_id, fingerprint, read_hashes
from .store import GOOD, Edge, Store

STORE_DIR = ".fractal"


class CacheError(ValueError):
    pass


class ClaimCache:
    def __init__(self, root: Path | str, checker_version: str = CHECKER_VERSION):
        self.root = Path(root).resolve()
        self.checker_version = checker_version
        self.store = Store(self.root / STORE_DIR / "edges.db")

    def close(self) -> None:
        self.store.close()

    # --- writes --------------------------------------------------------------

    def put(self, post: str, reads: list[str], *, pre: str = "", kind: str = "knowledge",
            probe: dict | None = None, writes: list[str] | None = None,
            depends_on: list[str] | None = None, parent_id: str | None = None) -> Edge:
        """Assert a claim and check it immediately. Re-putting an existing claim re-asserts it."""
        if not post.strip():
            raise CacheError("claim (post) must be non-empty")
        if probe is not None:
            probes.validate(probe)
        reads = sorted(set(_norm(r) for r in reads))
        depends_on = sorted(set(depends_on or []))
        eid = edge_id(kind, pre, post)
        for d in depends_on:
            if self.store.get(d) is None:
                raise CacheError(f"unknown dependency: {d}")
            if d == eid or eid in self._ancestors(d):
                raise CacheError(f"dependency {d} would create a cycle")
        if parent_id and self.store.get(parent_id) is None:
            raise CacheError(f"unknown parent: {parent_id}")
        edge = Edge(
            id=eid, kind=kind, pre=pre.strip(), post=post.strip(), reads=reads,
            writes=sorted(set(_norm(w) for w in writes or [])), probe=probe,
            status="pending", fingerprint=None, hashes={}, detail="",
            parent_id=parent_id, created_at=0, updated_at=0, depends_on=depends_on,
        )
        self.store.upsert(edge)
        self.store.log("put", eid, edge_kind=kind, has_probe=probe is not None)
        return self.check(eid)

    def needed(self, post: str, reads: list[str], *, pre: str = "", kind: str = "task",
               probe: dict | None = None, deliberate: bool = False) -> dict:
        """Probe-first pruning: is a step whose outcome is `post` actually needed?

        Redundant when the postcondition already holds (P ⟹ Q before any work):
        either the same claim is cached as verified, or its probe passes right now.
        A passing probe also records the claim as verified. Deliberately redundant
        steps (e.g. re-validation at a trust boundary) are never pruned.
        """
        eid = edge_id(kind, pre, post)
        if deliberate:
            verdict = {"needed": True, "reason": "deliberate: never pruned"}
        else:
            verdict = None
            if self.store.get(eid):
                self.refresh()
                if self.resolve([eid])[0].status == "verified":
                    verdict = {"needed": False, "reason": "cached: claim already verified"}
            if verdict is None and probe is not None:
                result = probes.run(probe, self.root)
                if result.passed:
                    self.put(post, reads, pre=pre, kind=kind, probe=probe)
                    verdict = {"needed": False, "reason": f"already holds: {result.detail}"}
                else:
                    verdict = {"needed": True, "reason": f"probe fails: {result.detail}"}
            if verdict is None:
                verdict = {"needed": True, "reason": "no probe: cannot show it already holds"}
        self.store.log("prune", eid, needed=verdict["needed"], reason=verdict["reason"].split(":")[0])
        return {"id": eid, **verdict}

    def delete(self, eid: str) -> list[str]:
        """Remove an edge; its dependents become stale (blocked)."""
        dependents = self.store.dependents(eid)
        self.store.delete(eid)
        self.store.log("delete", eid)
        for d in dependents:
            self.store.set_status(d, "stale", f"upstream {eid} was deleted")
        return self._propagate()

    # --- verdicts ------------------------------------------------------------

    def check(self, eid: str) -> Edge:
        """Compute a fresh verdict for one edge (runs its probe)."""
        e = self._require(eid)
        blocked = [d for d in e.depends_on if self._status(d) not in GOOD]
        if blocked:
            self.store.set_status(eid, "stale", "blocked by upstream: " + ", ".join(
                f"{d} ({self._status(d)})" for d in blocked))
        else:
            hashes = read_hashes(self.root, e.reads)
            fp = fingerprint(hashes, e.probe, self.checker_version)
            trusted_deps = [d for d in e.depends_on if self._status(d) == "trusted"]
            if e.probe is None:
                if e.fingerprint and e.fingerprint != fp:
                    changed = _changed(e.hashes, hashes)
                    self.store.set_status(eid, "stale", "sources changed: " + ", ".join(changed)
                                          + "; trusted claim needs re-assertion")
                else:
                    self.store.set_status(eid, "trusted", "no probe", fp, hashes)
            else:
                result = probes.run(e.probe, self.root)
                if not result.passed:
                    self.store.set_status(eid, "failed", result.detail, fp, hashes)
                elif trusted_deps:
                    self.store.set_status(eid, "trusted", "probe passed; depends on trusted: "
                                          + ", ".join(trusted_deps), fp, hashes)
                else:
                    self.store.set_status(eid, "verified", result.detail, fp, hashes)
        e = self._require(eid)
        self.store.log("check", eid, status=e.status)
        self._propagate()
        return self._require(eid)

    def refresh(self) -> list[str]:
        """Cheap invalidation scan: no probes run. Returns ids newly marked stale."""
        stale = []
        memo: dict[str, str] = {}
        for e in self.store.all():
            if e.status not in GOOD:
                continue
            hashes = {r: memo.setdefault(r, read_hashes(self.root, [r])[r]) for r in e.reads}
            if fingerprint(hashes, e.probe, self.checker_version) != e.fingerprint:
                changed = _changed(e.hashes, hashes) or ["checker version"]
                self.store.set_status(e.id, "stale", "sources changed: " + ", ".join(changed))
                self.store.log("stale", e.id, reason="sources", changed=changed)
                stale.append(e.id)
        return stale + self._propagate()

    def resolve(self, ids: list[str]) -> list[Edge]:
        """Lazily re-verify: re-check stale/failed edges (and their deps), deps first."""
        for eid in self._topo(ids):
            if self._status(eid) in ("stale", "failed"):
                self.check(eid)
        return [self._require(i) for i in ids]

    def verify(self, ids: list[str] | None = None) -> list[Edge]:
        """Force a fresh verdict on the given edges (default: all), deps first."""
        ids = ids or [e.id for e in self.store.all()]
        for eid in self._topo(ids):
            self.check(eid)
        return [self._require(i) for i in ids]

    # --- reads ---------------------------------------------------------------

    def query(self, text: str = "", paths: list[str] | None = None,
              statuses: list[str] | None = GOOD, limit: int = 20, min_matches: int = 1) -> list[Edge]:
        """Find claims (ranked), re-verifying stale matches on the way out."""
        self.refresh()
        matches = self.store.search(text, [_norm(p) for p in paths or []], None, limit=10_000,
                                    min_matches=min_matches)
        self.resolve([e.id for e in matches if e.status in ("stale", "failed")])
        result = [self._require(e.id) for e in matches]
        if statuses:
            result = [e for e in result if e.status in statuses]
        result = result[:limit]
        self.store.log("query", None, text=text, paths=paths or [], hits=len(result))
        return result

    def get(self, eid: str) -> Edge | None:
        return self.store.get(eid)

    def children(self, eid: str) -> list[Edge]:
        return [e for e in self.store.all() if e.parent_id == eid]

    def stats(self) -> dict:
        edges = self.store.all()
        by_status = {s: 0 for s in ("pending", "verified", "trusted", "stale", "failed")}
        for e in edges:
            by_status[e.status] += 1
        queries = self.store.events("query")
        checks = self.store.events("check")
        prunes = self.store.events("prune")
        redundant = sum(1 for p in prunes if not p["needed"])
        return {
            "edges": len(edges),
            "by_status": by_status,
            "queries": len(queries),
            "queries_with_hits": sum(1 for q in queries if q["hits"]),
            "checks": len(checks),
            "checks_failed": sum(1 for c in checks if c["status"] == "failed"),
            "stale_events": len(self.store.events("stale")),
            "steps_checked": len(prunes),
            "steps_redundant": redundant,
            "redundancy_rate": round(redundant / len(prunes), 3) if prunes else None,
        }

    # --- internals -----------------------------------------------------------

    def _require(self, eid: str) -> Edge:
        e = self.store.get(eid)
        if e is None:
            raise CacheError(f"unknown edge: {eid}")
        return e

    def _status(self, eid: str) -> str:
        e = self.store.get(eid)
        return e.status if e else "missing"

    def _ancestors(self, eid: str) -> set[str]:
        seen, stack = set(), [eid]
        while stack:
            for d in self._require(stack.pop()).depends_on:
                if d not in seen:
                    seen.add(d)
                    stack.append(d)
        return seen

    def _topo(self, ids: list[str]) -> list[str]:
        """ids plus their transitive deps, dependencies first."""
        order, seen = [], set()

        def visit(i: str) -> None:
            if i in seen:
                return
            seen.add(i)
            for d in self._require(i).depends_on:
                visit(d)
            order.append(i)

        for i in ids:
            visit(i)
        return order

    def _propagate(self) -> list[str]:
        """Mark good edges stale while a dependency is not good; cap at trusted under trusted deps."""
        newly = []
        changed = True
        while changed:
            changed = False
            for e in self.store.all():
                if e.status not in GOOD:
                    continue
                statuses = {d: self._status(d) for d in e.depends_on}
                bad = [d for d, s in statuses.items() if s not in GOOD]
                trusted = [d for d, s in statuses.items() if s == "trusted"]
                if e.status == "verified" and trusted and not bad:
                    self.store.set_status(e.id, "trusted", "probe passed; depends on trusted: "
                                          + ", ".join(trusted))
                    changed = True
                    continue
                if bad:
                    self.store.set_status(e.id, "stale", "blocked by upstream: " + ", ".join(bad))
                    self.store.log("stale", e.id, reason="upstream", upstream=bad)
                    newly.append(e.id)
                    changed = True
        return newly


def _norm(path: str) -> str:
    """Repo-relative, no leading './', no trailing '/'. Rejects paths escaping the repo."""
    p = path.strip()
    while p.startswith("./"):
        p = p[2:]
    p = p.rstrip("/")
    if not p or p.startswith("/") or ".." in p.split("/"):
        raise CacheError(f"path must be repo-relative: {path!r}")
    return p


def _changed(old: dict[str, str], new: dict[str, str]) -> list[str]:
    return sorted(k for k in set(old) | set(new) if old.get(k) != new.get(k))

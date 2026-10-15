"""`fractal check`: a commit/CI gate built from claims. Local, deterministic, no model call.

Re-checks every claim affected by the working tree's changes (`update`). A failing
*enforced* invariant is a violation: the code broke a rule a human accepted, so the check
fails. A failing *proposed* invariant is reported but never blocks. Other broken claims mean
the cache needs repair, which is a warning unless `strict`.
"""

from __future__ import annotations

from pathlib import Path

from .cache import STORE_DIR, ClaimCache


def staged_paths(root: Path) -> list[str] | None:
    """Paths staged for commit (None outside a git repo)."""
    import subprocess
    try:
        out = subprocess.run(["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"], cwd=root,
                             capture_output=True, text=True, check=True).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    return [l for l in out.splitlines() if l]


def gate_violations(edges: list, root: Path, ctx: dict) -> tuple[list, list]:
    """(enforced violations, proposed-rule notes) among the rules with a gate probe, evaluated on `ctx`."""
    from . import probes
    enforced, proposed = [], []
    for e in edges:
        if e.kind != "invariant" or e.rule_state not in ("enforced", "proposed") or not probes.gate_leaves(e.probe):
            continue
        r = probes.run(e.probe, root, ctx=ctx)
        if not r.passed:
            (enforced if e.enforced else proposed).append((e, r.detail))
    return enforced, proposed


def check_message(root: Path, message_file: str) -> tuple[int, str]:
    """`fractal check --message FILE` (a commit-msg hook): rules over the message being committed."""
    if not (root / STORE_DIR / "edges.db").exists():
        return 0, ""
    cache = ClaimCache(root, run_slow=False)
    try:
        edges = cache.store.all()
    finally:
        cache.close()
    bad, _ = gate_violations(edges, root, {"message": Path(message_file).read_text(errors="replace")})
    lines = [f"VIOLATION {e.id}: {e.post}\n          probe: {d}" for e, d in bad]
    if bad:
        lines.append(f"fractal: {len(bad)} invariant(s) violated by the commit message. Reword it and commit again.")
    return (1 if bad else 0), "\n".join(lines)


def check(root: Path, strict: bool = False) -> tuple[int, str]:
    """(exit code, report). Exit 0 when the repo has no claim store."""
    if not (root / STORE_DIR / "edges.db").exists():
        return 0, ""
    from .behavior import gate
    behavioral = gate(root)            # tests affected by the changes, batched per runner
    cache = ClaimCache(root)
    try:
        cache.update()
        edges = cache.store.all()
    finally:
        cache.close()
    violations = [e for e in edges if e.enforced and e.status == "failed"]
    regressions = behavioral["regressions"]
    proposed_failing = [e for e in edges if e.kind == "invariant" and e.rule_state == "proposed"
                        and e.status == "failed"]
    needs_repair = [e for e in edges if e.kind not in ("invariant", "behavior") and e.status in ("failed", "stale")
                    and e.repair is not None]
    staged = staged_paths(root)
    from .setup import forbidden_staged
    own = forbidden_staged(staged or [])
    gate_bad, gate_proposed = gate_violations(edges, root, {"staged": staged})
    lines = [f"note: proposed rule {e.id} would be violated (not enforced): {e.post}" for e in proposed_failing]
    lines += [f"note: proposed rule {e.id} would be violated (not enforced): {e.post}\n          probe: {d}"
              for e, d in gate_proposed]
    lines += [f"VIOLATION {e.id}: {e.post}\n          probe: {d}" for e, d in gate_bad]
    if own:
        lines.append("fractal: these are fractal's own files and must never be committed: " + ", ".join(own[:5])
                     + "\n         unstage them (`git restore --staged <path>`); `fractal init` keeps them out of git")
    for e in violations:
        lines.append(f"VIOLATION {e.id}: {e.post}\n          probe: {e.detail}")
    if needs_repair:
        kinds: dict[str, int] = {}
        for e in needs_repair:
            kinds[e.repair["kind"]] = kinds.get(e.repair["kind"], 0) + 1
        summary = ", ".join(f"{n} {k}" for k, n in sorted(kinds.items()))
        lines.append(f"fractal: {len(needs_repair)} claim(s) need repair ({summary}); "
                     "run `fractal repair --all` or let them repair on demand")
    for e in regressions:
        lines.append(f"REGRESSION {e.id}: {e.probe['file']} passed before and fails now: "
                     f"{behavioral['results'][e.id].detail}")
    if violations or gate_bad:
        lines.append(f"fractal: {len(violations) + len(gate_bad)} invariant(s) violated. Fix the code, or if the rule "
                     "itself changed, update the claim deliberately (`fractal rm` / `fractal put`).")
    if regressions:
        lines.append(f"fractal: {len(regressions)} behavioral regression(s). Fix the code, or update the test "
                     "if the behavior change is intended.")
    code = 1 if violations or gate_bad or own or regressions or (strict and needs_repair) else 0
    if code == 0 and behavioral["results"]:    # this state is about to be committed: new reference
        from .behavior import promote_baseline
        cache = ClaimCache(root)
        try:
            promote_baseline(cache, behavioral["results"])
        finally:
            cache.close()
    return code, "\n".join(lines)

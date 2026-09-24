---
name: fractal-onboard
description: Onboard onto this codebase by building a verified claim cache with the fractal CLI. Run when the user asks to onboard, map, or learn this repo.
disable-model-invocation: true
allowed-tools: Read, Grep, Glob, Bash(fractal:*)
---

# Onboard with the claim cache

You are building a map of this codebase out of **claims**: short, checkable statements tied
to the files they depend on. Verified claims matching a future prompt are injected into
that session automatically, so every claim you record saves future exploration, and every
wrong claim costs it. Claims are re-checked automatically when their files change.

Optional argument: a path or topic to focus on (e.g. `/fractal-onboard src/billing`).
Without one, map the whole repo at coarse zoom.

## 1. Check what is already known

Run `fractal query "<topic keywords>"` (and `fractal query --path <dir>` for the focus
path). Skip anything already `verified`; treat `trusted` claims as hints to confirm. Run
`fractal stats` once so you know the cache's size.

## 2. Map at coarse zoom first

Record at most ~15 claims before going deeper:

- **What the repo is** and its main entry points.
- **Top-level modules**: one claim per module: what it is responsible for, and what it
  guarantees to the rest of the code.
- **Workflows**: how to install, build, test, lint, run. Use `--kind workflow --pre "<goal>"`,
  with a command probe (`--run "<cmd>"`) only when the command is fast (< ~60s) and has no
  side effects.
- **Cross-cutting invariants**: auth, error handling, config loading, data access
  conventions: anything a new contributor would break by not knowing.
- **Flows people ask about**: how a request, event, or record moves through several files.
  These are the questions that cost the most exploration without claims.

## 3. Zoom only where it pays

Go one level deeper into a module only if it is central, non-obvious, or in the focus.
For each zoomed module, record its public interface and invariants with
`--dep <coarse claim id> --parent <coarse claim id>`.

**Stop zooming** when a claim can be checked directly by a probe, or when the next level
would just restate the code.

## Recording a claim

```sh
fractal put "<claim>" --read <file> [--read <file> ...] --probe '<json>'
```

- **One fact per claim**, stated as a contract a reader can rely on without opening the
  file: "All HTTP handlers in `api/` are registered through `router.add()` in
  `api/routes.py`", not "api/routes.py handles routing".
- **`--read` lists every file the claim depends on.** Editing any of them invalidates the
  claim, so too few reads means it can silently go wrong; too many means it goes stale
  needlessly. Globs (`'app/lib/**/*.dart'`) work for repo-wide rules and also catch added or
  removed files.
- **Attach a probe whenever the claim is mechanically checkable.** With a probe the claim
  is `verified`; without one it is only `trusted`, and trusted claims are never injected.
  - `{"type":"grep","pattern":"router\\.add\\(","paths":["api/**/*.py"],"expect":{"min":1}}`
  - `{"type":"grep","pattern":"\\beval\\(","paths":["src/**/*.py"],"expect":"absent","exclude":"^\\s*#"}`
    (grep is line-based; `exclude` skips matching lines, e.g. comments)
  - `{"type":"all","probes":[ ... ]}` when a claim has several checkable parts.
  - `--run "npm test -- --silent"` for a command that must exit 0.
- **Every checkable statement in the claim must be covered by the probe.** `verified`
  means "the probe passed", so a claim that says more than its probe checks overstates
  what is known. Split it: the probed part as one claim, the rest separately (trusted) or
  not at all.
- **The probe must fail if the claim becomes false.** A probe that passes regardless (a
  word that appears everywhere) is worse than none.
- **Make probes sturdy against harmless edits**: anchor on identifiers rather than exact
  formatting (`GPS_FILTER_M\s*=\s*5\b`, not `^const GPS_FILTER_M = 5;$`), and prefer
  `{"min": n}` over an exact count unless the count is the claim.
- Probes must never read huge generated files (data dumps, build output, vendored deps);
  keep `paths` globs to source. Command probes must be read-only and fast.
- Do not record what file names already say, or anything you did not confirm in the code.
- Do not modify any files.

## 4. Finish

Run `fractal stats` and report: how many claims were recorded, how many are `verified`
vs `trusted`, any that `failed` (and why), and which areas you deliberately left at
coarse zoom.

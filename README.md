# fractal-harness

A contract-chunked agent harness. Work is a DAG of edges `{pre} ⟹ {post}`; each
edge is tied to the source files it reads, checked by a deterministic probe, and
cached. When a source file changes, the edges that read it go stale, and so do the
edges downstream of them. Stale edges are re-checked the next time someone
queries them.

This first layer is the **claim cache**: a store of verified claims about a codebase
that invalidates itself. It works against any repo and is exposed to agent harnesses
over MCP.

Design notes live in `context/`, which is gitignored.

## Quick start: onboard a repo

```sh
# once per machine: puts `fractal` on your PATH (editable, so harness edits apply immediately)
uv tool install --editable /path/to/fractal-harness

# once per target repo
cd /path/to/target-repo
fractal init      # store, .gitignore, .mcp.json, /fractal-onboard skill, settings, CLAUDE.md block
fractal doctor    # verify the setup
claude            # approve the fractal-claims server when prompted (once per repo)
> /fractal-onboard            # map the whole repo at coarse zoom
> /fractal-onboard src/api    # or zoom into one area
```

`fractal init` is idempotent. It merges into an existing `.mcp.json`, `.claude/settings.json`, `.gitignore`
and `CLAUDE.md` instead of overwriting them, and it only manages its own marked block in `CLAUDE.md`.
Re-run it after upgrading the harness to refresh the skill. `--no-claude-md` and `--no-settings` skip
those two files.

What it writes into the target repo:

| File | Purpose |
|---|---|
| `.fractal/edges.db` | The claim store (gitignored; claims are per-machine for now) |
| `.mcp.json` | Registers the `fractal-claims` MCP server (`fractal mcp`) |
| `.claude/skills/fractal-onboard/SKILL.md` | The `/fractal-onboard` procedure |
| `.claude/settings.json` | Enables the server, allows its tools, and installs the prompt hook |
| `CLAUDE.md` | Tells Claude to query claims before exploring and record them after tasks |

Claude Code still asks you to approve a project's MCP server the first time. Project settings
can't approve their own servers, which is intentional.

## Develop

```sh
uv sync
```

## Claims

A claim is text plus the files it reads plus an optional probe:

| Status | Meaning |
|---|---|
| `verified` | Probe passed against the current source, and every dependency is verified |
| `trusted` | No probe, or depends on a trusted claim. Treat as a hint. |
| `stale` | Sources or upstream changed since the verdict. It is re-checked on query. |
| `failed` | Probe did not pass |
| `pending` | Planned but not generated yet |

Probes:

```json
{"type": "grep", "pattern": "auth_middleware", "paths": ["src/**/*.py"], "expect": "present"}
{"type": "command", "run": "pytest -q tests/test_auth.py", "expect_exit": 0}
```

A `grep` probe's `expect` is `"present"`, `"absent"`, `{"count": n}`, `{"min": n}` or `{"max": n}`.

A trusted claim whose sources change stays `stale` until someone re-asserts it with
`put`. Re-checking it automatically would launder the change.

## Claim injection (hook)

Waiting for the agent to query the cache didn't pay off in practice. The agent often
skipped it, loading the MCP tools cost a turn, and keyword queries missed. So `fractal init`
also installs a `UserPromptSubmit` hook, `fractal hook prompt`. It ranks verified and trusted
claims against the prompt (BM25 over claim text and read paths) and injects the top 6 as
context before the agent's first turn. In repos without a store, and on any error, it
does nothing.

Search is ranked: any query term can match, identifiers like `_gpsDistanceFilterM` are
tokenized, and stopword-only queries return nothing.

## Learning from sessions (`fractal record`)

The `Stop` hook only appends the session's transcript path to `.fractal/queue.jsonl`. It
makes no model call and adds no context. `fractal record` processes the queue later, in one
headless session that verifies facts in the source and records them as claims with probes.
That cost counts as investment, not as part of any task.

Recording only pays off for sessions where the cache fell short, so `record` triages first:

| Session | Recorded? |
|---|---|
| Explored ≥2 files no injected claim covered | yes, pointed at the uncovered files |
| Made ≥6 exploration calls (even inside covered files) | yes: the injected claims fell short |
| Hit: the cache covered what it explored, little searching | skipped |
| Question ≥0.6 similar to one already recorded | skipped (`--force` overrides) |

If nothing qualifies, no model call is made. On round-3 benchmark data, triage keeps 3 of 15
sessions: the three most expensive questions.

```sh
fractal record              # process the queue
fractal record --dry-run    # show the extraction prompt and what would be skipped
```

## Keeping claims current (`fractal update`, `fractal repair`)

Updates borrow from video coding. When a probe passes, the lines it matched (plus context)
are stored as the claim's anchors, its keyframe. A per-line snapshot of each file it reads
is stored too. After edits, `fractal update` classifies every affected claim locally, with
no model call and no git:

| Class | Meaning | Cost |
|---|---|---|
| `clean` / `moved` | probe passes; anchors unchanged or just moved (anchors rebased) | $0 |
| `fresh` | probe passes; nothing to anchor (absent/command probes) | $0 |
| `suspect` | probe passes but the anchored code changed substantially | delta repair |
| `delta` | probe fails; anchors found with a small residual | delta repair |
| `rewrite` | probe passes but ≥25% (and ≥20 lines) of a file the claim reads was rewritten | keyframe |
| `scene_cut` | probe fails; anchors gone or heavily rewritten | keyframe |
| `reassert` | claim has no probe and its files changed | keyframe |

`fractal repair` fixes broken claims in one batched headless session. A delta repair sees
only the claim, its probe and the residual. A keyframe repair re-verifies from source, and
so does every claim after 3 delta repairs in a row, so patches can't drift. By default only
**demanded** claims are repaired: ones the prompt hook wanted to inject. Claims nobody asks
about stay broken for free. A failing invariant is reported as a violation and left alone:
the code may be what's wrong.

## Auditing probes (`fractal audit`)

A claim is only as good as its probe. `fractal audit` mutation-tests every grep probe *in
memory*, without writing files or calling a model, in about a second for 40 claims:

| Check | Mutation | Expected | Finding if not |
|---|---|---|---|
| removal | delete the lines the probe matched | probe fails | **weak**: the probe doesn't depend on its matches |
| injection | (absent probes) add a line violating the rule | probe fails | **weak**: the rule can never fail (e.g. an over-broad `exclude`) |
| values | change each number the claim states | probe fails | **partial**: the claim states a value the probe doesn't check |
| identifiers | rename each identifier the claim mentions | probe fails | **partial**: the claim mentions something the probe doesn't check |

File names and identifiers absent from the probed files are ignored. Exit code 1 if any
probe is weak.

## Invariants: the tent-pole of agent alignment

An agent is aligned with a codebase when it knows the rules before it acts, is checked
against them as it acts, and can't commit a violation. Invariants are human-owned:

```
proposed (agent) ──human accepts──→ enforced ──code breaks it──→ violated
                                        ↑                            │
                                        └── fix the code, or change the rule deliberately
```

```sh
fractal propose "src never imports legacy" --read src/app.py \
  --probe '{"type":"grep","pattern":"^import legacy\\b","paths":["src/**/*.py"],"expect":"absent"}'
fractal invariants --audit       # proposed / enforced / rejected, with probe audit verdicts
fractal accept <id>              # human only: needs an interactive terminal
fractal reject <id>
```

- `put --kind invariant` and `propose` always create a **proposal**. Re-asserting a rule
  never changes the state a human gave it.
- `fractal init` denies agents `fractal accept`/`reject` (`permissions.deny`, which beats
  the allow on the rest of the CLI).
- **While editing:** a `PostToolUse` hook (`fractal hook edit`) re-checks the enforced
  rules whose probes scan the edited file, in milliseconds. On a violation it exits 2, so
  Claude sees the rule and the offending lines and fixes them, or asks you if the request
  conflicts with the rule. The edit itself isn't undone; the commit gate is the backstop.
- **At commit:** `fractal check` fails on any violated enforced rule. Proposed rules are
  reported but never block.
- Repair never rewrites an enforced rule.

## Region tree

Claims are placed on a capacity-bounded tree over the code's own address space
(directories → files): each claim sits at the smallest region containing everything it
depends on, and a rule placed at a region governs everything beneath it. `affects(deps, file)`
decides which rules an edit re-checks. Glob matching follows `Path.glob` semantics
(`**/` matches zero or more directories). The tree underlies the coming coverage map and
rule-first injection.

## Invariants and the commit gate (`fractal check`)

Record rules the code must follow with `--kind invariant`. `fractal check` re-checks every
claim affected by the working tree (locally, no model call) and exits 1 if an invariant is
violated. Other broken claims are reported as needing repair (they fail the check only
with `--strict`). `fractal init --git-hook` installs it as a pre-commit hook. It never
overwrites an existing hook; it tells you to add `fractal check` yourself instead.

Every path a probe scans counts as a dependency, alongside the declared `--read` files.
A rule probed over `app/lib/**/*.dart` is re-checked when *any* of those files changes.

## Probe-first pruning

A step is redundant if its outcome already holds before any work is done (`P ⟹ Q`), like
sorting an already-sorted list. `needed` / `claims_needed` takes the step's outcome and a
probe that passes only once the outcome holds. It reports the step as redundant if the
outcome is already a verified claim or the probe passes now. Otherwise the step is needed.
Steps marked `deliberate` (e.g. re-validation at a trust boundary) are never pruned.
`fractal stats` reports the **redundancy rate**. A weak probe can wrongly prune a step,
so write probes that fail until the work is done.

## CLI

The target repo is `--root`, then `$FRACTAL_ROOT`, then the enclosing git repo.
`fractal mcp` also checks `$CLAUDE_PROJECT_DIR` before the git repo.
The store lives at `<root>/.fractal/edges.db`.

```sh
fractal put "every handler is wrapped in auth_middleware" \
  --read src/app.py --probe '{"type":"grep","pattern":"auth_middleware","paths":["src/*.py"]}'
fractal put "tests pass" --read src/app.py --run "pytest -q"
fractal query auth                 # re-verifies stale matches
fractal query --path src/ --all    # include stale/failed
fractal refresh                    # mark stale after edits (no probes run)
fractal needed "config loader exists" --read src/config.py \
  --probe '{"type":"grep","pattern":"def load_config","paths":["src/*.py"]}'
                                   # REDUNDANT (exit 3) if the outcome already holds
fractal verify                     # re-run every probe
fractal stats
```

Run any of these as `uv run fractal ...` from this repo, or install the package elsewhere.

## MCP

`fractal init` registers the server. To register it by hand instead:

```sh
claude mcp add fractal-claims -- fractal mcp
```

The server resolves the target repo from `$CLAUDE_PROJECT_DIR`, which Claude Code sets.

Tools: `claims_query`, `claims_put`, `claims_needed`, `claims_verify`, `claims_stats`.

Command probes run shell commands in the target repo. Only point the server at
repos whose claims you trust.

## Tests

```sh
uv run pytest
```

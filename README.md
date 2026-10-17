# fractal-harness

**Invariants as the tent-pole of agent alignment.** A coding agent is aligned with a codebase
when it knows the rules that govern the code before it acts, is checked against them as it
acts, and cannot commit a violation. fractal keeps those rules, and the verified facts agents
need to work within them, in front of the right agent at the right moment, and keeps them
true as the code changes.

- **Rules (invariants)** are human-owned. Agents propose them; a human accepts them.
- **Facts** (knowledge, workflows, interfaces) are machine-maintained: learned from sessions,
  re-checked when their files change, repaired cheaply.
- Every claim is tied to the files it depends on and checked by a deterministic **probe**.

Design notes and the full experiment log live in `context/` (gitignored).

## Quick start

```sh
# once per machine (editable: harness changes apply immediately)
uv tool install --editable /path/to/fractal-harness

# once per repo
cd /path/to/repo
fractal init --git-hook     # hooks, skill, permissions, pre-commit gate
fractal doctor
claude
> /fractal-onboard          # proposes rules and records facts
```

Then review what the agent proposed and enforce the rules you agree with, in your own terminal:

```sh
fractal invariants --audit  # proposed / enforced / rejected, with probe audit verdicts
fractal accept <id>         # human only
```

## The alignment loop

| When | What happens | Hook |
|---|---|---|
| **Before acting** | The prompt gets the enforced **RULES** governing the code the task likely touches (repo-wide rules, rules over files named in the prompt or read by matching facts), then matching **FACTS**, each with evidence (`✓ app/lib/main.dart:3607: …`). With no enforced rules and no matching facts, nothing is injected: the session is identical to one without fractal. | `UserPromptSubmit` → `fractal hook prompt` |
| **While acting** | Every Edit/Write/MultiEdit is applied **in memory** and checked against the enforced rules that scan that file. A violating edit is blocked before it is written; the agent sees the rule and the offending lines. | `PreToolUse` → `fractal hook pre-edit` |
| | After any edit, the same rules are re-checked on disk (a backstop that also runs command probes). | `PostToolUse` → `fractal hook edit` |
| **At commit** | `fractal check` fails on any violated enforced rule. Proposed rules are reported, never blocking. | git pre-commit |
| **End of turn** | The behavioral claims (tests) the uncommitted changes can break run in one batch; a regression blocks finishing. | `Stop` → `fractal hook stop` |
| **After the session** | The session is queued; `fractal record` later learns facts and proposes rules from it (skipping sessions the cache already served). | `Stop` → `fractal hook stop` |

## Behavioral contracts: the codebase as a graph

Every test file is a behavioral claim about exactly the code it exercises (its transitive
imports, plus files it reads by path), placed on the region tree:

```sh
fractal import-tests        # one claim per test file (flutter, dart or pytest); no tests run
fractal behavior --all      # baseline: one batched run records every verdict
fractal behavior            # run only the claims the uncommitted changes can break
```

At the end of every agent turn, the `Stop` hook runs the behavioral claims the uncommitted
changes affect (batched per runner) and blocks on a **regression** (passed at the baseline,
fails now) or a violated enforced invariant, so the agent fixes it before handing back.
Already-failing tests never block; after two blocks in a session it reports instead. The
commit gate runs the same set and promotes passing results to the new baseline. Tests that
exercise no repo code are reported as vacuous.

## The invariant lifecycle

```
proposed (agent) ──human accepts──→ enforced ──code breaks it──→ violated
                                        ↑                            │
                                        └── fix the code, or change the rule deliberately
```

```sh
fractal propose "core never imports from cmds" --read core/io.py \
  --probe '{"type":"grep","pattern":"^from cmds","paths":["core/**/*.py"],"expect":"absent"}'
fractal accept <id>     # needs an interactive terminal; agents are denied it by init
fractal reject <id>
```

- Re-asserting a rule never changes the state a human gave it.
- Repair never rewrites an enforced rule: a violation means the code or the rule is wrong,
  and a human decides which.
- A rule is only as strong as its probe: `fractal invariants --audit` mutation-tests each one.

## Claims and probes

```sh
fractal put "All handlers are registered via router.add() in api/routes.py" \
  --read api/routes.py --probe '{"type":"grep","pattern":"router\\.add\\(","paths":["api/**/*.py"],"expect":{"min":1}}'
fractal query auth                 # ranked search; stale matches are re-verified
fractal manifest app/lib           # an owner's context for a region: rules first, then facts
```

| Status | Meaning |
|---|---|
| `verified` | the probe passed against the current source (and every dependency is verified) |
| `trusted` | no probe; never injected, never a guarantee |
| `stale` | sources or upstream changed since the verdict; re-checked on demand |
| `failed` | the probe did not pass |

Probes: `{"type":"grep","pattern":…,"paths":[globs],"expect":"present"|"absent"|{"count":n}|{"min":n}|{"max":n},"exclude":…}`
(line-based), `{"type":"command","run":…}`, or `{"type":"all","probes":[…]}`. Every path a
probe scans is a dependency. Globs follow `Path.glob` semantics.

## Keeping claims true

After edits, `fractal update` classifies every affected claim locally (no model, no git),
borrowing from video coding: a passing probe's matched lines are the claim's keyframe.

| Class | Meaning | Cost |
|---|---|---|
| `clean` / `moved` / `fresh` | probe passes; anchors unchanged, moved, or nothing to anchor | $0 |
| `suspect` / `delta` | anchored code changed substantially / probe fails with a small residual | cheap repair |
| `rewrite` / `scene_cut` / `reassert` | file largely rewritten / anchors gone / unprobed claim's files changed | full re-verify |

`fractal repair` fixes **descriptive** claims in one batched session (by default only those a
prompt wanted). On a 20-commit replay of a real repo, ~89% of re-checks were free.

`fractal audit` mutation-tests probes in memory (delete what they matched, inject violations,
change stated values, rename mentioned identifiers): `weak` probes don't depend on their
code; `partial` claims say more than their probes check.

## Node specs and the mutant ledger

A node spec starts with front matter declaring what it rests on and what it offers (`fractal ledger node
<name> --spec <file>` registers it; `fractal verify-plan <name>` says which verdicts still hold):

```
---
parents: stats          # nodes whose contracts this one uses; their changes stale it
after: bench            # sequencing only: must come first, contract not used (never stales)
implements: D18         # decisions reproduced in full
see: naive              # non-binding peer examples
guarantee: ...          # what a child may rely on
condition: ...          # when the guarantee holds
exports: src/x.rs::T, src/x.rs::T::m
planned: src/x.rs::T::m     # exports whose code does not exist yet (spec first): children may depend on them
refines: NearestK=Nearest   # a new export for an already-owned decision extends it on purpose
distinct: Other=Nearest     # ...or is a separate concept (silences the ownership lint either way)
oracle: equality        # tests compare two implementations: verify-plan requires the
                        # "every output entry is NONE/empty" mutant to be recorded killed
---
```

- An export that does not exist yet is an error unless the spec lists it under `planned:`. A planned export has no
  signature, so a child sees it as "planned"; when the code appears the child's verdicts go stale once, with the reason
  "is now defined", and `ledger node --fix` drops it from `planned:`. `graph-check` does not call it stale.
- Under a "Design" heading, mark each list item `[binding]` (reproduce it) or `[advisory]` (override it with
  evidence, and say so). `ledger node` warns about unmarked ones.
- `ledger node` also warns when a node exports a new symbol for a decision another node's symbol already
  implements, but only for decisions a concept claim names (`fractal only-in ... --decision S5`, or an ID in the
  concept's name): ownership belongs to a concept, and a cross-cutting decision many nodes implement is not checked.
  It also warns when code uses a module that is only declared `after:`.
- `fractal update` lists `unchecked` decisions: registered, but no `Check:` line, citing claim or test names them.
- When one parent export changes, `verify-plan` suggests starting with the mutants whose code names it.
  Every verdict still counts as stale.

## Running mutants

`fractal mutate <node> --json mutants.json --cmd 'pytest -q {tests}' --build-cmd '<compile step>'` runs a verifier's
mutants and records the verdicts in the ledger. The verifier chooses the mutants (small patterns: `file`, `span`,
`tests`, and `find`+`replace` or `replacement`); fractal applies each in its own throwaway git worktree of the node's
commit, so your checkout is never touched and nothing is left applied if a run dies. The same command must pass on the
unmutated commit first, or nothing is recorded. A failing test is a kill, passing tests a survivor (recorded
`survived`: arguing equivalence is still the verifier's, via `ledger record`), and a mutant that does not build is
reported invalid. Outcomes are memoized by the commit's tree, the edits and the commands, so a repeat costs nothing.
`--env CARGO_TARGET_DIR=/tmp/t{job}` gives parallel runs their own build directories. The executor underneath
(`executor.py`) is the primitive other judges, such as reclamation, reuse.

## Reclamation

`fractal reclaim` shrinks the repo to the smallest state that keeps every verdict: mark, sweep, gate, verify.
`graph-check` marks what nothing reaches (anything under `public:` or `.fractal-public` is exempt; tests, rules and
probes are never candidates). The sweep proposes three kinds: stale spec `exports:` (metadata), code nothing reaches
(the item, its decorators/docs, and for Rust the `impl` blocks of that type), and a node whose spec is gone (deregistered).
Recorded verdicts are evidence: they are never proposed for deletion, and those of unregistered nodes are only listed. A code candidate that another live worktree
mentions is blocked. `--verify --cmd '<tests>'` applies each deletion in a scratch worktree of HEAD (all the passing
ones together as a last check); `--apply` writes only what verified to your working tree and the audit log, and commits
nothing. It needs a clean tree and tests that pass first. Passing is required, not proof: reference scanning is a word
match and tests can be weak. Deleting an export stales its node's children; the plan says so.

## Contract changes across worktrees

`fractal impact <node>` compares the node's exports with the merge-base against the default branch (your working tree
included). For each export whose definition changed or vanished, or all of them when the `guarantee:`/`condition:`
changed, it asks the reference index which *other* live worktrees (`--branches` adds branch tips with no worktree)
mention the item in files this checkout has no mention in: callers added elsewhere that this node's own tests cannot
see. `verify-plan` prints the same section when it finds any. It is a warning: a mention is a word match, so confirm it
is a real use.

## Single ownership ("only here")

Some invariants say a thing exists in exactly one place: one ranking expression, one kind encoding, one nearest
order. `fractal only-in "<concept>" --pattern '<regex>' --in <owner file> --paths '<glob>'` proposes that as a rule
(or `--kind struct --name 'Nearest\w*'` for definitions of a symbol kind). The `only_in` probe fails when the
pattern appears anywhere else, and when the owner no longer has it (moved or renamed). It also works in the
pre-edit hook, so a second copy is blocked before it is written. `fractal concepts [--write CONCEPTS.md]` prints the
registry generated from these claims: concept, owner, and whether it still holds.

## Region tree

Claims live on a capacity-bounded tree over the code's own address space (directories →
files): each claim sits at the smallest region containing everything it depends on, and a
rule placed at a region governs everything beneath it. The tree answers "which rules govern
this file?" for the hooks and places claims in manifests.

## Real-use metrics

`fractal stats`: claims by status, prompts seen, hit rate, claims injected, sessions and
claims recorded with cost, repairs with cost. These are the numbers that decide whether the
cache pays for itself: hit rate × savings − maintenance − learning cost.

## Develop

```sh
uv sync
uv run pytest
```

`experiments/` holds the benchmarks (claim injection A/B, goodput analysis, history replay)
and the parked parallel/recursive planner (`experiments/fractal_planner/`), with their tests.

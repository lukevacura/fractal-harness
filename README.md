# fractal-harness

A prototype harness based on [Explorers and Judges writing](https://lukevacura.com/blog/explorers_judges/).

**Invariants as the tent-pole of agent alignment.** A coding agent is aligned with a codebase
when it knows the rules that govern the code before it acts, is checked against them as it
acts, and cannot commit a violation. fractal keeps those rules, and the verified facts agents
need to work within them, in front of the right agent at the right moment, and keeps them
true as the code changes.

- **Rules (invariants)** are layered. Agents propose them; a node's agent may accept a rule
  inside its own subtree's files (if `fractal.toml` allows it); a parent layer can override it
  only when no information is lost; the human is the base case at the root and the arbiter when
  agents disagree or a change would lose information.
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
fractal invariants --audit  # proposed / enforced / rejected / overridden, with probe audit verdicts
fractal accept <id>         # you, at the root: it asks you to confirm on your terminal
fractal pin                 # once, and after you edit [policy]: pins older rules' revisions and the policy
```

Everything beyond the basics (named roots, accept policy, jobs, briefs) is optional and configured
in `fractal.toml` at the repo root; with no file, behavior is the single-repo default.

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
proposed ──accept──→ enforced ──code breaks it──→ violated
                        │  ↑                          │
              override  │  └─ --undo                  └─ fix the code, or change the rule deliberately
                        ↓
                    overridden (kept, with who and why)
```

```sh
fractal propose "core never imports from cmds" --read core/io.py \
  --probe '{"type":"grep","pattern":"^from cmds","paths":["core/**/*.py"],"expect":"absent"}'
fractal accept <id>                   # the human at the root
fractal accept <id> --as <node>       # a node's agent, for a rule inside that node's files or its descendants'
fractal override <id> --reason "..."  # a higher layer demotes a lower layer's rule (--by <node>|root, --undo)
fractal change <id> --probe '{...}'|--drop --reason "..."   # any change to an enforced rule (--by <node>|root)
fractal reject <id>
fractal queue                         # everything awaiting you, as what each decision would change
```

`fractal queue` lists every decision waiting for a human (proposals, arbitration requests and conflicts held at root
or on a `human_nodes` node, rules changed outside the CLI, unpinned policy edits) with the net change in information
each would make, re-checked when shown:

```
[1] CONTRADICTION  proposal 4b1e…  no legacy imports anywhere
      ! contradicts enforced rule [a90c…] "mid imports legacy" (@root): …
[2] adds information  proposal 800f…  CapacityError and friends are defined only in capacity.rs
      + newly enforces: no lines matching /(struct CapacityError|…)/ in crates/**/* (except crates/core/src/capacity.rs)
      + newly enforces: at least 4 lines matching /(struct CapacityError|…)/ in crates/core/src/capacity.rs
[3] adds nothing  proposal 2928…  Core numeric code never calls mul_add
      = already enforced by [b0f0…]: no lines matching /mul_add|fmadd|vfma/ in crates/core/src/**/*.rs
```

Contradictions and global losses come first, then additions, then rules that cannot be compared (command probes:
"unknown"), then rules that add nothing. "Adds nothing" is claimed only when every part of the rule is proven already
enforced; "newly enforces" can overstate (a part only a command rule covers is listed). Command and test probes are
re-run with `--slow`. Two rules that conflict through different patterns are not detected.

Every change to the rule set is judged by one quantity, its **delta**: what it newly forbids and what it newly
allows, comparing the rules in effect before and after (precedence included). A part of a removed rule that other
rules still forbid is **absorbed**; one that nothing still forbids, or that can't be proven (commands and tests can't
be compared), is a **global loss**. The queue shows the delta; reclamation proposes changes whose delta is empty.
Rules are compared on their typed parts (grep, `only_in`, `paths`, commit gates), paths as glob patterns, never as
today's files. Contradiction uses the same comparison: two rules contradict when no state satisfies both.

That works because every probe reads the same files: the repo's files, meaning tracked files plus untracked ones git
doesn't ignore. Nothing under a git-ignored directory is read (even by `build/**`: such a rule checks nothing, and
`check` says so), and nothing under a symlinked directory. In `paths` a literal names one file; in skips (`!`,
`exclude_paths`), `only_in` files and commit gates it names that file or the directory under it. Paths are
repo-relative, with no leading or trailing `/`.

One law decides who may make a change (`accept --as`, `override --by`, `retire`, `narrow`, `ratchet`, `change --by`):

- **No loss.** An agent never lets through what was forbidden. A loss is the human's: `fractal conflict raise` files it.
- **Standing.** To drop or reshape a rule, the agent's node holds it or ranks above it, and `accept_policy` allows
  agents (`human_only` is the default; `local`, `local_with_audit`). Never a root rule. A ratchet needs no standing:
  it only moves a debt rule's bounds toward the target the human already ruled.
- **Region.** A rule a node holds depends only on files that node (or its descendants) owns.

Everything else is the root's: you, in your terminal, shown the same delta. Identity is not authenticated: `--as`
and `--by` are a convention the policy gates, not a credential.

- **The human is the base case.** `--as <node>` also needs a probe that runs, passes and checks something, and
  one no higher layer's rule contradicts.
- **Local rules can be overridden by parents.** A rule that survives unchallenged may simply not need challenging yet.
  Root rules are paramount: precedence applies in the hooks, `check` and `manifest`.
- Re-asserting a rule never changes the state it was given.
- Repair never rewrites an enforced rule: a violation means the code or the rule is wrong, and the layer
  above decides which.
- A rule is only as strong as its probe: `fractal invariants --audit` mutation-tests each one.

### Conflicts: contradiction as the signal

When a local rule clashes with a higher layer, or keeps blocking work, that is information, not noise. Blocks are
logged; a rule blocked `friction_threshold` times within `friction_window_days` raises a `conflict` event (a
heuristic signal that only raises events). A contradiction with a higher layer's rule is caught at accept time:
sound when the two rules' file patterns nest, a warning when they only overlap in today's files.

```sh
fractal conflict list               # awaiting a decision
fractal conflict show <id>          # why, block counts, history
fractal conflict raise ...          # the premise behind a claim is wrong: raise it on the parent, never weaken the probe
fractal conflict resolve <id> keep|narrow|override   # the parent's agent decides
fractal conflict escalate <id>      # one layer up; at the root it becomes an arbitration request for a human
fractal conflict arbitrate <id>     # human only: settles a request
```

An agent resolves a conflict by default. Only a conflict that reaches the root, or one on a node listed in
`human_nodes`, goes to the human. If the agent disagrees with a human rule it must explain why in a written
arbitration request; it never overrides a root rule itself.

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

Paths in a grep probe may start with `!` to exclude (`"paths":["src/**","!src/legacy.rs"]`; at least one must not).
Probes: `{"type":"grep","pattern":…,"paths":[globs],"expect":"present"|"absent"|{"count":n}|{"min":n}|{"max":n},"exclude":…}`
(line-based), `{"type":"command","run":…}`, or `{"type":"all","probes":[…]}`. Every path a
probe scans is a dependency. Globs follow `Path.glob` semantics.

Prefer a typed probe to a shell command: the checker can reason about typed probes (whether a change to a rule loses
information), and a command it cannot, so every change to a command rule needs you.

```json
{"type":"only_in","pattern":"UnsafeCell","paths":["src/**/*.rs"],"files":["src/sync.rs"]}
{"type":"paths","paths":["**/proptest-regressions/**"],"expect":"absent"}
{"type":"paths","paths":["db/migrations/*"],"unless_name":"[0-9]{3}_[a-z0-9_]+\\.sql","expect":"absent"}
{"type":"paths","paths":["db/migrations/*.sql"],"unique":"^[0-9]{3}"}
{"type":"staged_paths","frozen":["db/migrations/*.sql"]}
```

`paths` counts the repo's files (a tracked file counts even if `.gitignore` matches it; a file an edit is about to
create counts too): files that must not
exist, a naming convention (`unless_name` counts the files whose whole name does not match), or a key no two files
share (`unique`). `frozen` is checked at commit: files there may be added, never modified, deleted or renamed.

```sh
fractal lift <id> --probe '<typed json>' --reason "..."   # (human) replace a rule's probe; shows both results
```

**Debt.** A rule worth having that the code already breaks is kept, not dropped:

```sh
fractal propose "no legacy imports" --probe '{"type":"grep",...,"expect":"absent"}' --debt
fractal ratchet <id>      # lowers each bound to today's count; restores "absent" at zero
```

`--debt` stores each violated absent/max bound as `{"max": <today's count>}` with the original expectation as its
target. The rule forbids anything worse than today. Lowering a bound only forbids more, so any layer may ratchet;
raising one is a loss and needs you. `fractal propose` and `fractal invariants` also say when a proposal is
violated now or contradicts another rule.

Every rule and fact shows an **evidence class**, what a pass establishes. Only `[exact]` is a guarantee the agent
need not re-check; the others say what remains to verify:

| Class | Probe | What a pass means |
|---|---|---|
| `exact` | `only_in`, `paths`, commit gates, or a grep with `"evidence":"exact"` (human-declared) | the probe decides the property |
| `proxy` | any other grep | a pattern approximates it |
| `test` | `test`, `command` | a test passes; `fractal invariants` adds `mutation-checked by <node>` when a still-valid killed verdict names the test |
| `doc-pinned` | `section`, `symbol` | the text is unchanged; the code is not checked |

An `all` probe is as weak as its weakest member. `--behavioral` (or `"behavioral":true`) marks a rule about what the
code does; `fractal invariants` reports it as a `gap` while only a pattern backs it, and reports probe-less and
doc-pinned rules too.

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
joins: engine_a, engine_b   # a join node: tests that these agree; each must be a parent (contract watched)
---
```

- `implements:` holds decision IDs and `exports:`/`planned:` hold `path::item` refs or file paths. Prose there is not
  split into words: it is ignored, with a note (put prose in parentheses or the spec body).
- Rust `pub use` counts as a definition: `pub use a::X;`, `as` aliases and single-line `{A, B as C}` groups define
  the names they expose, and cfg-gated alternatives of one name form a single interface (editing any branch changes
  the contract). Private `use` and globs name nothing. A facade module can be exported as `path::*` (the whole file
  is the interface, so any edit is a contract change; prefer naming items).
- A node registers against its own branch (`refs/heads/<branch>`), and its parents' exports are read at that commit.
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
- `fractal activations` lists the nodes due an agent and why: `spec` or `contract` changed, a failing `probe` (rejected
  rules don't count; a grep is blamed on the files with matching lines), or an unresolved `survivor` mutant. A parent's
  implementation change that keeps its contract raises nothing. Two things are listed once, not per node: proposals
  awaiting a human, and survivors that carry an equivalence argument but were never recorded `equivalent`.
- When a decision heading is renamed (`S4 (proposed)` to `S4`), `fractal update` offers the relink; `fractal put
  --section INVARIANTS.md::S4` replaces the dead claim, then re-run `ledger node` for the specs that cite it. A section
  over 2,000 characters is injected cut (marked, with its ref) but its digest covers all of it; `put` warns at 90%.
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

The rule set is reclaimed too, and first (`--class rules`): an enforced rule is **redundant** when everything it
forbids is still forbidden by the other rules in effect (the same proof as absorption, so typed probes only; a command
rule is never a candidate). Candidates are judged together, so of two identical rules only one goes; node rules go
before root rules and newer before older, so the older ruling is kept. `--apply` retires redundant node rules (demoted
with history, `fractal override --undo` restores them); a redundant root rule is listed with the `fractal override`
you run in your terminal, whose prompt says it loses no information. A **vacuous** rule (its globs match no file
today) is reported but not proposed: it still forbids files added later.

Code reclamation keeps every enforced rule passing, not only the tests: a deletion that a rule needs (a presence or
`only_in` rule on the item) is blocked, each deletion alone and all of them together before `--apply`.

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

## Roots, derived state and briefs

`fractal.toml` (all optional):

```toml
[roots]                      # specs and decisions may live in another repo; refs are <root>:<path>
docs = "../project-docs"

[policy]
accept_policy = "human_only" # human_only | local | local_with_audit
friction_threshold = 5
friction_window_days = 14
human_nodes = ["core"]       # conflicts on these nodes are only ever decided by a human
root_signal = "tty"          # how a root decision is confirmed: tty (your terminal; default) | flag (--yes, scripts)
                             # once pinned, edits to accept_policy, human_nodes, root_signal apply after `fractal pin`

[jobs]
backend = "local"            # or "ssh", with [jobs.ssh] host / dir / prefix

[brief]
report_dir = "docs/reports"  # where `fractal report file` copies scratch reports
```

Done is derived, never marked:

```sh
fractal status [node]        # unregistered | pending | due | stale | done, from verdicts and digests
fractal dag [--format json]  # the node graph generated from specs and the ledger, one line per node
fractal repair --all         # re-verify every broken claim without a model; add --agent to hand the rest to one
```

Handing a node to an agent:

```sh
fractal brief <node>         # deterministic prompt: repo root rules first, why the node is due (incl. its branch
                             # and worktrees), task, spec, only that node's premises, reporting
fractal report file <path>   # file a scratch report into the docs with a provenance header; `report list`
fractal run --node <n> <cmd> # run a command at the node's commit on the [jobs] backend, memoized by tree hash
fractal run --detach <cmd>   # start in the background; `run status <id>`, `run log <id> --tail N`
```

There are no job templates: a job is just a command. The ssh backend is implemented but has not been run
against a real host.

Cleaning up after agents. Agent worktrees pile up; `fractal worktrees` lists the ones whose work is already in the
base (default: the main checkout's HEAD; `--into REV` to change it) and, with `--apply`, removes them:

```sh
fractal worktrees                    # dry run: what is removable, and why each other one is kept
fractal worktrees --apply            # `git worktree remove` (no --force) on the removable ones
fractal worktrees --apply --branches # also `git branch -d` the merged branch, unless a node registers it
fractal worktrees --squashed         # also count squash/rebase merges, matched by patch id (HEURISTIC)
```

A worktree is removable only if its tip is an ancestor of the base (a git fact), it has work of its own (one
sitting exactly at the base may be an agent that just started), it is not locked or your current checkout, and
nothing would be lost: either it is clean, or every uncommitted change is already in the base byte for byte.
Anything else is kept and the reason is printed. A branch a node registers is never deleted, since the ledger
resolves that node's commit from it. Nothing is committed.

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

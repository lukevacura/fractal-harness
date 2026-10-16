---
name: fractal-verify
description: Verify a node independently by mutation testing, recording each verdict in fractal's mutant ledger so later re-verification re-runs only what a change can have affected.
disable-model-invocation: true
allowed-tools: Read, Grep, Glob, Edit, Write, MultiEdit, Bash
---

# Verify a node and record the verdicts

You are the verifier for the node in `$ARGUMENTS` (a node name, plus its spec path). Judge the
implementation independently: recompute results yourself and re-derive any argument you rely on.
Do not rely on the implementer's reasoning, and do not read it if it was left in a report.

1. **Plan.** Run `fractal verify-plan <node>`.
   - No recorded verdicts: verify the node in full (steps 2 and 3 for every mutant you can think of).
   - Verdicts exist: re-run **only** the mutants under "Re-run these mutants" and the "Survived"
     ones, using the diff it prints. Leave "Settled" mutants alone unless the diff touches them.
     "Everything is stale" means the spec or a premise changed: verify in full.
2. **Mutate.** For each mutant, change the code in a scratch copy or revert it afterwards, run the
   tests, and note whether any test fails.
   - A failing test **kills** it. Note which tests fail.
   - No test fails: it **survived**. Decide whether it is really equivalent (same behavior on every
     input): if so, write the argument. If not, the tests are weak: report that as a finding.
3. **Record every verdict at the commit you verified**, so the next run can reuse it:

   ```
   fractal ledger record <node> --json - <<'JSON'
   [{"file": "src/x.rs", "span": "40-42", "mutant": "flip < to <=", "result": "killed", "tests": ["test_edge"]},
    {"file": "src/x.rs", "span": "57", "mutant": "drop clamp", "result": "equivalent",
     "argument": "the value is already clamped by the caller"},
    {"file": "src/x.rs", "span": "63-70", "mutant": "skip retry", "result": "survived"}]
   JSON
   ```
   - `span` is 1-based lines in the verified commit, covering exactly the code you changed.
   - A killed mutant needs `tests` (the names of the tests that fail); an equivalent one needs `argument`.
   - Verify a committed state (the default `commit` is `HEAD`); commit or stash first.
   - Re-recording a mutant replaces its old verdict; do that for every one you re-ran.
4. First time for a node, or after its spec changed: register what it rests on with
   `fractal ledger node <node> --spec <spec.md>`, then record. The spec's front matter declares
   the node's premises (`parents:` nodes, `implements:` decisions; register parents first). Verdicts
   go stale only when the spec, an implemented decision, or a direct parent's contract (its
   `guarantee:`, `condition:` and `exports:` signatures) changes; if the header is missing, ask the
   orchestrator to add it.
5. Report PASS only if no mutant survived unresolved and every verdict is recorded. List the
   survivors and any weak tests otherwise.

Never run `fractal accept` or `fractal reject`.

---
name: fractal-work
description: Do a task by working from the repo's behavioral graph: map the task onto regions and their contracts, change contracts first, work region by region (subagents for independent regions), done when contracts pass.
disable-model-invocation: true
allowed-tools: Read, Grep, Glob, Edit, Write, MultiEdit, Agent, Bash(fractal:*)
---

# Work from the behavioral graph

This repo's tests form a graph of **behavioral contracts**: each test file covers exactly the
code it imports, grouped into regions. Use it as your framework for the task in `$ARGUMENTS`.

1. **Map it.** Run `fractal map "<the task>"`. You get the regions the task touches, each
   region's contracts (its definition of done), what it relies on upstream, the downstream
   contracts that must not break, enforced rules, and weak spots (regions with no tests).
2. **Plan along the cuts.** Decide which regions change and whether any *behavior* changes.
   If it does, change or add that region's contract (the test) first, so the test states the
   new behavior before the code does. Weak spots get a new test for what you change.
3. **Work region by region.** For each region, load its context with
   `fractal manifest <region>` and work only within it, relying on the upstream contracts as
   stated. If several regions are independent, dispatch a subagent per region with its
   manifest, its contracts and the upstream contracts it may assume.
4. **Done = contracts hold.** A region is done when its contracts pass. Then run
   `fractal behavior`: it runs exactly the contracts your changes can affect. Fix any that
   regress unless the task intends the behavior change (then update that test and say so).
5. **Never** edit or work around enforced rules; if the task requires breaking one, stop and
   ask the user.

Finish with: the regions you changed, the contracts you added or changed and why, and the
result of `fractal behavior`.

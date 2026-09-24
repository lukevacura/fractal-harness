---
name: fractal-onboard
description: Onboard onto this codebase: propose the rules its code must follow and record verified facts with the fractal CLI. Run when the user asks to onboard, map, or learn this repo.
disable-model-invocation: true
allowed-tools: Read, Grep, Glob, Bash(fractal:*)
---

# Onboard: propose rules, record facts

This repo uses **invariants** as the backbone of agent alignment: rules the code must follow,
owned by humans. Enforced rules are shown to every agent before it acts, checked on every
edit, and checked at commit. You help in two ways:

1. **Propose rules** the code already follows and should keep following. You only
   propose; the user decides which become binding.
2. **Record facts**: short, verified statements about how the code works, so future
   sessions don't have to re-read it. Facts matching a prompt are injected automatically,
   and every claim is re-checked when its files change.

Optional argument: a path or topic to focus on (e.g. `/fractal-onboard src/billing`).
Without one, cover the whole repo at coarse zoom.

## 1. Check what is already known

Run `fractal invariants` (existing and proposed rules), `fractal query "<topic keywords>"`,
and `fractal manifest <dir>` for the focus path. Skip what is already `verified`; treat
`trusted` claims as hints to confirm.

## 2. Propose rules (the priority)

Look for rules a new contributor could break without noticing:

- **Boundaries**: "`core/` never imports from `cmds/`", "only `db.py` talks to SQLite".
- **Forbidden things**: deleted APIs that must not come back, banned calls (`eval`,
  `print` in library code), direct access that must go through a wrapper.
- **Required things**: "every command module defines `NAME` and `run`", "every migration
  has a down step", "the quest constructor asserts at least one waypoint".
- **Conventions the code relies on**: ID formats, where config is loaded, how errors are
  raised.

```sh
fractal propose "<the rule, stated so a violation is unambiguous>" --read <file> --probe '<json>'
```

- A rule's probe must **fail when the rule is violated**. Rules are usually `absent`
  probes (the forbidden thing never appears) or presence probes over every file the rule
  covers (`"paths": ["src/commands/*.py"], "expect": {"min": 1}` per required element).
- Propose only rules the code **already follows**: the probe must pass now.
- A rule describes what must stay true, not how the code currently happens to work. If
  breaking it would be fine, it is a fact, not a rule.
- **Never run `fractal accept` or `fractal reject`.** Those are the user's decisions.
- After proposing, run `fractal invariants --audit`: fix or drop any proposal whose audit
  verdict is `weak`, and tighten `partial` ones (the rule mentions something its probe
  doesn't check).

## 3. Record facts at coarse zoom

At most ~15 facts before going deeper:

- **What the repo is** and its main entry points.
- **Top-level modules**: what each is responsible for and what it guarantees to the rest.
- **Workflows**: how to install, build, test, lint, run (`--kind workflow --pre "<goal>"`,
  with `--run "<cmd>"` only if the command is fast and has no side effects).
- **Interfaces** other modules call (`--kind interface`).
- **Flows people ask about**: how a request, event, or record moves through several files.

Go one level deeper only where a module is central, non-obvious, or in the focus. Stop when
a claim can be checked directly by a probe, or when the next level would restate the code.

```sh
fractal put "<fact>" --read <file> [--read <file> ...] --probe '<json>'
```

## Writing any claim (rules and facts)

- **One statement per claim**, usable without opening the file: "All HTTP handlers in
  `api/` are registered through `router.add()` in `api/routes.py`", not "api/routes.py
  handles routing".
- **`--read` lists every file the claim depends on** (globs like `'app/lib/**/*.dart'`
  work and also catch added or removed files). Every path the probe scans counts too.
- **Probes** (grep is line-based; `exclude` skips matching lines, e.g. comments):
  - `{"type":"grep","pattern":"router\\.add\\(","paths":["api/**/*.py"],"expect":{"min":1}}`
  - `{"type":"grep","pattern":"\\beval\\(","paths":["src/**/*.py"],"expect":"absent","exclude":"^\\s*#"}`
  - `{"type":"all","probes":[ ... ]}` when a claim has several checkable parts.
  - `--run "npm test -- --silent"` for a command that must exit 0 (read-only and fast only).
- **Every checkable statement in the claim must be covered by the probe**; a claim that
  says more than its probe checks overstates what is known. Split it, or leave the
  unprobed part out.
- **Sturdy probes**: anchor on identifiers, not exact formatting (`GPS_FILTER_M\s*=\s*5\b`,
  not `^const GPS_FILTER_M = 5;$`); prefer `{"min": n}` over exact counts.
- Never probe huge generated files (data dumps, build output, vendored deps).
- Record only what you confirmed in the code. Do not modify any files.

## 4. Finish

Report to the user:
- the rules you **proposed** (with their audit verdicts), and ask them to review with
  `fractal invariants --audit` and enforce the ones they agree with using
  `fractal accept <id>` in their own terminal;
- how many facts you recorded, `verified` vs `trusted`, any that `failed` and why;
- which areas you left at coarse zoom.

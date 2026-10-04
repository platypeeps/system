---
title: local-health-check sweeps every status verb instead of naming three
created: 2026-09-11
status: done
item: sd:460
---

# PRD — system-local-health-check-does-not-sweep-status

## Problem

CLAUDE.md convention 6 (item 6 under its Conventions heading) says `local-health-check` "runs
the `status` of every tool that has one and reports only `1`". It did not.
Before PR 1 (at `4c9ebff`, lines 232-234 of `health-check.sh`) the stage was
a list of three paths —
`mezmo-pipeline`, `local-fluentbit`, `local-opentelemetry-collector` — and the
runner was a fourth check on its own (`health-check.sh` lines 159-163 at
`b4264ad`, deleted by PR 3), gated on its LaunchAgent being loaded. Nothing else is reached. The convention
describes a sweep; the script is an inventory, and it has already drifted:
`local-sd-plan/sd-plan.sh` implements the 0/3/1 contract exactly (it exits 3
on this machine: `local-sd-plan: SKIP — no repos.personal.conf`) and no nightly
job runs it, because adding it meant editing a list nobody remembered to edit.
Found 2026-09-11 while landing `local-sd-plan`.

The same convention closed with its own inventory — "`mezmo-pipeline`,
`local-fluentbit` and `local-opentelemetry-collector` implement it"
(`CLAUDE.md` line 147 at `4c9ebff`) — which was one tool short for the same
reason.

A second, smaller thing is tangled into the list. The stage the three sit in
is Mezmo's: its heading is "mezmo shippers and control plane", every finding
is prefixed `mezmo:`, and the remediation line says "a 401 means the service
key was rotated, a 404 means the pipeline or source id moved". That text is
right for `pipeline.sh` and wrong for anything else, so a non-Mezmo tool added
to this loop would raise a finding whose suggested fix is nonsense for it. The
tool is the thing that knows what its own failure means — `pipeline.sh` already
prints `FAIL — $MEZMO_API_BASE unreachable or rejecting (HTTP …)` on that path
(`mezmo-pipeline/pipeline.sh:177-189`) — and the health check is the wrong
place to keep a second copy of that knowledge.

### What a naive sweep would do — measured, not guessed

The row that filed this (`sd:442`) sketches the fix as "glob the sibling
folders for an entrypoint, run `status`, report only 1". Run against this
checkout on 2026-09-11 that sketch is wrong in two ways, and both are
requirements below:

- **Only 4 of 52 entrypoints implement the contract.** 18 answer a `status`
  verb at all; of those, 4 have an `exit 3` path (`mezmo-pipeline`,
  `local-fluentbit`, `local-opentelemetry-collector`, `local-sd-plan`). Three
  more use a `0 ok / 1 degraded / 2 not loaded` scheme documented in their
  own help (`local-cswap/cswap.sh:176`, `local-task-actions/task-actions.sh:400`,
  `local-n8n/n8n.sh:251-252`) — the same meaning with a different number for
  "nothing to check". `local-sd-runner` is a two-state verdict, 0 or 1 with
  no "not configured" — which is why the health check wraps it in
  `launchctl print` and does the third state itself. The remaining ten
  (`agent-prompt`, `ai-apps`, `bin-links`, `claude`, `cron-jobs`,
  `machine-setup`, `mock-mcp`, `msgsnap`, `opentelemetry-demo`, `sd-db`)
  have a `status` that is a report: it exits 0 whatever it saw, and says
  nothing a nightly finding could act on (`mock-mcp.sh status` exits 0 with
  `container: not running`; `machine-setup.sh status` exits 0 on drift unless
  `--fail-on-drift`).
- **The other 34 entrypoints have no `status` verb**, so convention 1 prints
  usage and exits 1, which is the finding code (32 of them; `prism` exits 2,
  `notify` exits 0 and prints nothing). A sweep that runs `status` on every
  entrypoint raises 32 findings on a healthy machine. One of them,
  `local-gito/gito.sh status`, did not return at all: it started downloading
  a package and was killed at 20 seconds. A sweep that runs arbitrary
  entrypoints runs arbitrary code, so it needs a bound.
- **`help` is a usable but imperfect signal of the verb.** Grepping each
  tool's `help` output for a `status` line found 16 of the 18 with a false
  positive (`notify`, where `status` is a `-k` kind) — which is why detection
  is a design question below and not a requirement here.

So "every tool that has one" has to mean *every tool that says it has one*,
and the saying has to be something the sweep can read from the tool, not a
list in the health check.

## Requirements

1. `health-check.sh` derives the set of tools it asks for `status` from the
   tools themselves, not from a path list in its own source. Adding a tool
   that implements convention 6 makes it checked without touching
   `health-check.sh`.
2. A tool that does not implement convention 6 — no `status` verb, or a
   `status` that is a report and not a verdict — is never run by the sweep
   and never raises a finding. The 34 entrypoints without the verb and the
   ten reports above stay silent.
3. Exit 0 and exit 3 are silent; exit 1 is a finding. Any other exit
   (2, 124, 127) is not silently folded into either — it is reported as what
   it is, "`<tool> status` exited N", because a contract violation is a bug
   worth one line and a hang is worth more than one.
4. Each `status` call is bounded in time. A tool that does not answer inside
   the bound is a finding naming the tool and the bound.
5. The remediation line of a finding is specific to the tool that raised it.
   The text about rotated service keys and moved pipeline ids lives with
   `mezmo-pipeline`, or is dropped in favour of what `pipeline.sh status`
   already prints. No tool inherits another tool's remedy.
6. The runner check (`health-check.sh` lines 159-163 at `b4264ad`) either
   joins the sweep on the same terms — which means `runner.sh status` learns to
   exit 3 when its LaunchAgent is not loaded, so the `launchctl print` gate
   moves into the tool — or keeps its own stage for a reason written next to
   it. Two mechanisms asking the same question of one tool is the duplication
   this item removes.
7. `CLAUDE.md` convention 6 says what the script does. It stops naming which
   tools implement the contract and says how to find out, the way the ports
   gotcha already says "ask the machine". `health-check.sh`'s own help text
   (`local-health-check/health-check.sh:48-55`) says the same thing.

## Assumptions

Assumptions, not requirements; each is checkable and the design may overturn
one:

- Convention 1's `help` output is a reliable enough signal of which verbs a
  tool has that detection can read it. Every entrypoint here is required to
  document its subcommands there, and the four contract tools and the three
  0/1/2 tools all do; the measured false positive is one tool. The
  alternative — a marker the tool prints or a declaration file — is also a
  per-tool signal and satisfies requirement 1; which one is a design
  decision, and the one thing it may not be is a list.
- The three 0/1/2 tools (`cswap`, `task-actions`, `n8n`) are in scope only
  if migrating them to exit 3 is a two-line change each. If it is more, they
  stay out and the PRD says so. Under "report only 1" their `2` is already
  silent, so leaving them out costs nothing tonight.
- `local-sd-plan status` exiting 3 for "no `repos.personal.conf`" in a
  worktree of this repository is that tool's business, not this item's.

## Acceptance criteria

- [x] `grep -c 'mezmo-pipeline/pipeline.sh\|local-fluentbit/fluentbit.sh\|local-opentelemetry-collector' local-health-check/health-check.sh`
      prints `0` — no tool path is written in the health check.
- [x] `sh local-health-check/health-check.sh check` on this machine reports
      `no findings — system healthy` (or only findings that were present
      before the change), and the run finishes in under 60 seconds. Any
      `usage:` text or a `gito` line in the findings is a fail.
- [ ] With `local-sd-plan/sd-plan.sh` made to exit 1 (e.g. `SD_PLAN_STATUS_FORCE=1`
      or an equivalent test seam the design names), `health-check.sh check`
      prints a finding naming `local-sd-plan`, and the finding's remedy does
      not mention a service key or a pipeline id. (No seam forces
      `sd-plan` itself to exit 1; the shape is proven by the suite's
      `local-declares-broken` fixture, and the remedy is now the tool's own
      first line by construction, so no sweep finding can carry another
      tool's text. `sd-plan.sh status` exits 3 on this machine. Unticked
      2026-10-03, sd:1238: not evidenced, because no exit-1 seam ran.)
- [x] With `mezmo-pipeline/pipeline.sh` made to exit 1, the finding's remedy
      is the Mezmo-specific one, and no other tool's finding carries it.
- [x] A scratch sibling folder whose entrypoint declares the contract and
      answers `status` with `sleep 600` produces one finding naming the tool
      and the bound, and the check still finishes inside the bound plus a
      margin. A scratch folder whose entrypoint has no `status` verb produces
      no finding. These two run as `sh local-health-check/health-check.sh
      test`, against a fixture tree, and that suite is a `run_suite` line in
      `.github/workflows/system-native.yml` — `OK`, no skips.
- [x] `CLAUDE.md` convention 6 no longer contains the phrase
      "implement it" followed by tool names, and its description of what
      `local-health-check` does is one that `grep` on `health-check.sh` can
      confirm — the PR that lands this quotes the lines side by side.
- [x] `local-health-check/health-check.sh help` no longer says `mezmo:` as a
      stage name; it describes the sweep.
- [x] `sd-docs-lint` from the repository root exits 0 after the change (rule
      7: every `path:line` above still points where it says; annotated
      2026-10-03, sd:1238: rule 6 checks `path:line` into Markdown only, so
      citations into code are not verified by this lint).

## References

- `sd:442` — "system: local-health-check does not sweep status verbs, it
  names three", `planning`, last moved 2026-09-11. The filing row; this item
  is its planning artifact; the task row stays open until this ships.
- `CLAUDE.md`, item 6 under Conventions — the sentence this makes true.
- `local-health-check/health-check.sh:266-339` — the sweep, where the
  three-path stage was (lines 223-243 at `4c9ebff`).
- `local-machine-setup/machine-setup.sh candidates service` — the precedent
  for "ask the machine, not the list", cited in the CLAUDE.md ports gotcha.
- `platypeeps/system#248`–`#252` — the `local-sd-plan` landing that found
  this.

## Review

`sd-review --scope planning` — **not run**. From this checkout it answers
`sd-review: error: no planning or in_progress work item under docs/work`.
The checkout is the runner's clone under `/Volumes/sd-work/worktrees/442/`,
and the pack keys items by `git rev-parse --show-toplevel` (its
`repo_root`), which is the clone's path, while the row's `repo` is
`~/repos/system` — the resolution `sd-db.sh work register` does
since #250 and the pack's readers do not yet. `sd-status` from the same
clone reports every item in this folder `status-unreadable` for the same
reason, the two siblings included. No approval is claimed from this lane;
run it from the primary checkout once the branch is there, or from any
checkout once the pack resolves a clone to its repository.

The measurements in the problem statement were taken in this clone on
2026-09-11 and are reproducible with the commands they describe; they are
the reviewer's first thing to re-run.

## Log

- 2026-09-11 created from `sd:442`; measured every entrypoint's `status` exit
  code on this machine before writing the requirements. `design.md` and
  `implement.md` written in the same run because `sd-plan.sh item` refuses
  a run without them (`local-sd-plan/sd_plan.py:28-29`). Registered as
  `sd:460`. Planning review lane not run — see `## Review`.
- 2026-09-11 delivered in three pull requests: `platypeeps/system#263` (the
  sweep, the remedy, the convention), `#268` (the fixture suite, run by
  `health-check.sh test` and a `run_suite` line), `#267` (runner `status`
  exits 3 when its agent is not loaded, the heartbeat stage deleted, and a
  sweep fallback for the two entrypoints that do not follow convention 1).
  Acceptance criteria re-run on merged `main` before this commit: `status
  sweep: 5 tool(s) declared, 5 checked` in 30 s, no `usage:` or `gito` line,
  suite `OK`, lint clean. Left to a person: `launchctl bootout` of
  `local.system-tools.sd-runner` and seeing `runner.sh status` exit 3. Closed by
  `#269`, whose `Delivers:` trailer this commit repeats in a block git
  parses -- the first one sat in its own paragraph and did not count.

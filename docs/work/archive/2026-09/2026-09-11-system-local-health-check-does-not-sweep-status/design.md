# Design — system-local-health-check-does-not-sweep-status

The PRD leaves one real question open — how the sweep knows which tools
implement convention 6 — and four smaller ones that follow from it. Each
choice below names the alternative it rejected and why.

## 1. Detection: the tool declares itself in `help`, and already does

The sweep runs `<entrypoint> help` for every sibling folder's entrypoint and
treats a tool as implementing the contract when its help output mentions
`local-health-check`. That is the whole mechanism.

It is not a new convention. Measured on 2026-09-11, all four tools that
implement the contract already say so in their help — `local-health-check
reads those` (`local-fluentbit/fluentbit.sh`,
`local-opentelemetry-collector/opentelemetry-collector.sh`), `local-health-check
reads those codes` (`mezmo-pipeline/pipeline.sh`), `local-health-check reads
these` (`local-sd-plan/sd-plan.sh`) — and no other entrypoint's help does,
except `health-check.sh` itself, which the sweep skips by name. Each author
wrote that sentence unprompted because convention 6 says the health check
reads the codes; making it the detection signal turns a courtesy into the
contract, and the cost of joining the sweep is the sentence the author was
going to write anyway.

Convention 6's text becomes: *say so in `help` — "local-health-check reads
these codes" — and the nightly sweep finds you; leave it out and it does not.*
Convention 1 already requires every entrypoint to answer `help` with its
subcommands and exit 0, so the sweep's probe is a call every tool here is
obliged to answer cheaply.

**Rejected: a path list.** That is the bug.

**Rejected: run `status` on everything and classify the result.** 34
entrypoints have no `status` verb and 32 of them exit 1 with usage — the
finding code. Distinguishing "usage, exit 1" from "broken, exit 1" by
sniffing stdout for `usage:` is a text heuristic on 52 tools' error messages,
and it runs 52 tools nightly to learn which 4 matter. One of them,
`local-gito/gito.sh status`, started a package download when probed. A
health check that runs arbitrary entrypoints with an argument they do not
recognise is the thing convention 1's "print usage and exit 1" exists to make
harmless, and this design does not lean on that harmlessness.

**Rejected: parse `help` for a `status` subcommand line.** Measured: 16 of 18
found, one false positive (`local-notify/notify.sh`, where `status` is a
`-k` kind), and it selects the ten tools whose `status` is a report that
exits 0 regardless — harmless tonight, but it means the sweep runs
`machine-setup.sh status` (several seconds, a full drift report) every night
for nothing. A verb's existence is not a declaration that its exit code means
anything.

**Rejected: parse `help` for the `status` line and the digit `3`.** This also
selects exactly the four, today, with no tool edits. It lost to the phrase on
one point: a digit in a description is a coincidence waiting to happen
(`status [lines]` with "default 3" would match), and a sentence naming the
reader is not.

**Rejected: a declaration file or a fixed comment in the source.**
`machine-setup.sh candidates` reads ports out of source, so the precedent
exists. But a comment is invisible to the person running `help`, and the
phrase in `help` is read by both the sweep and the human — one declaration,
two readers, and it cannot drift from the documentation because it *is* the
documentation.

## 2. The bound is plain sh, not `timeout`

Each `help` probe and each `status` call runs under a bound implemented in
POSIX sh — the call in the background, a sleeping watchdog, `kill` on
expiry, exit 124 reported. `timeout` on this machine is Homebrew coreutils at
`/opt/homebrew/bin/timeout`, and the nightly job runs under launchd's bare
PATH, the same PATH that gave `local-sd-plan` Xcode's Python 3.9
(`local-sd-plan/sd-plan.sh:7-9`). A dependency that resolves in a terminal and
not under launchd is the kind of bug that stays green for months.

Bounds: 5 seconds for `help` (a heredoc; a tool that takes longer is doing
work in the wrong place, and the sweep says so), 30 seconds for `status`
(`pipeline.sh status` makes one HTTPS call; `sd-plan.sh status` asks the
runner for its heartbeat). Both are constants at the top of the stage, not
options: nothing tunes them per tool, because a tool that needs longer than
30 seconds to say whether it is healthy has a different problem.

A tool killed at the bound is a finding — "`<tool> status` did not answer in
30s" — not a skip. Exit 2, 127 and anything else outside {0, 1, 3} is a
finding worded as the number, per PRD requirement 3. A tool whose `help`
matched but whose `status` exits 2 has declared a contract it does not keep,
and one line naming that is worth more than folding it into silence.

## 3. The remedy line comes from the tool

The finding becomes two lines with nothing Mezmo-shaped in the health check:

    - <folder>: <first line of the tool's status output>
      run <path> status by hand for the full response

`pipeline.sh status` already prints `FAIL — <base> unreachable or rejecting
(HTTP <code>)` (`mezmo-pipeline/pipeline.sh:177-189`). The two hints the
health check carries today — 401 means the service key was rotated, 404 means
the pipeline or source id moved — move into that line, keyed on the code
`pipeline.sh` already has in `$MZ_CODE`. The tool knew the code; it should say
what the code means. Nothing else in the sweep ever carries another tool's
remedy again.

**Rejected: a per-tool remedy table in the health check.** A second list,
keyed by the first.

## 4. The runner joins the sweep, and its `3` moves into the tool

`runner.sh status` exited 0 or 1 (`cli.py` line 132 at `b4264ad`) and had
no "nothing to check", which is why the health check wrapped it in
`launchctl print gui/$(id -u)/local.system-tools.sd-runner` (`health-check.sh` lines
159-163 at `b4264ad`). That gate was the third state, implemented outside
the tool it describes. PR 3 moved it: `status` exits 3 when the LaunchAgent
is not loaded — the same `launchctl print`, one folder over
(`local-sd-runner/sd_runner/cli.py:36-50` asks, and
`local-sd-runner/sd_runner/cli.py:137-149` answers 3 with the JSON body
unchanged) — its help gains the sentence, and the separate stage is deleted.
One mechanism.

The sweep then had to *find* `runner.sh`. It derives each folder's
entrypoint by convention 1, and `local-sd-runner/runner.sh` is not
`sd-runner.sh` — nor is `local-project-dashboard/dashboard.sh`
`project-dashboard.sh`. Renaming either means editing and reloading a
launchd plist and, for the dashboard, the pack's `sd-plugin.json`
(CLAUDE.md, Gotchas), which was rejected for this item. The operator chose a
fallback in the sweep instead
(`local-health-check/health-check.sh:298-309`): when `<stem>.sh` is absent
and the folder has exactly one `*.sh` at its root, that one is the
entrypoint; any other shape is skipped as before. Measured on 2026-09-11,
those two folders are the only ones the fallback reaches.

The one caller that reads the exit code, `local-sd-plan/sd-plan.sh:114`,
treats any non-zero as "the runner is not dispatching" and is unchanged by
a 3: on a machine with a participation list and no runner, sd-plan is
configured and broken, and still says 1.

**Rejected: leave the stage with a comment.** Then convention 6 reads "every
tool that declares it, plus one that does not", and the sentence this item
exists to make true is false by one.

## 5. The 0/1/2 tools stay out

`local-cswap`, `local-task-actions` and `local-n8n` exit `2` for "not
loaded", which is convention 6's `3` under another number. Under "report
only 1" they would sweep cleanly today, but they do not declare the contract
and this item does not make them. Three tools, three help texts, three
`exit 2` → `exit 3` edits, and `local-cswap/cswap.sh:9` documents its codes in a header
comment that would have to move too: it is small, it is unrelated to the
sweep, and it is the kind of tidy-up that turns a 150-line change into a
400-line one with three more reviewers' worth of context. If someone wants
them swept, the price is the sentence in `help` and a `3`, and nothing in the
health check changes to admit them — which is the test that section 1 is
right.

## 6. A fixture-driven test suite, because the acceptance criteria are tests

The PRD's scratch-folder criteria — a declaring tool that exits 1 raises one
finding, a declaring tool that sleeps raises one finding naming the bound, an
entrypoint with no `status` raises nothing — are a test suite written as
prose. `health-check.sh` grows a `test` verb and a `HEALTH_CHECK_TOOLS_ROOT`
seam (default `$DIR/..`, the same shape as `HEALTH_CHECK_STATE`), and the
suite points the sweep at a fixture tree of four scratch entrypoints. It is
Python `unittest`, not sh, for the reason CLAUDE.md gives for
`local-repo-sync` and `local-sd-plan`: the CI wrapper asserts a unittest
summary and refuses skips. It joins `.github/workflows/system-native.yml` as
one more `run_suite` line.

The rest of the health check — SMART, launchd, log faults — is not under
test and this does not put it there. The seam isolates the sweep stage; the
suite runs `check` against a fixture root and asserts on the findings block
only.

## Decisions

- **Detection is the phrase `local-health-check` in `help` output.** Decided
  by the planning run, 2026-09-11, on the measurement in section 1. Reversed
  if a tool's help acquires the phrase for another reason — the fix is then a
  stricter phrase, still in `help`, never a list.
- **The bound is sh, 5s/30s, not tunable.** Decided 2026-09-11. Reversed by a
  real tool needing longer, which would be a reason to look at that tool.
- **The runner migrates in this item.** Decided 2026-09-11, section 4. If
  PR 3 turns out to touch more of `sd_runner` than `cli.py` and its help, it
  is dropped from this item, the heartbeat stage stays with a comment, and the
  PRD's requirement 6 is satisfied by the second branch of its own sentence.
- **The 0/1/2 tools are out.** Decided 2026-09-11, section 5.
- **Off-convention entrypoints are found by a one-`*.sh` fallback, not
  renamed.** Decided by the operator 2026-09-11 for PR 3, section 4: the
  rename's cost is a plist and a plugin manifest on every machine. Reversed
  if a third folder acquires a lone `*.sh` that is not its entrypoint — the
  fix is then a rename after all, or a name the sweep can read.

## Risks

- **A tool declares the phrase and does not keep the contract.** Then the
  sweep reports its 2 or its usage-1 as a finding with the number in it,
  which is the tool's bug surfacing in the right place. Accepted.
- **`help` on some entrypoint is slow or has side effects.** All 52 answered
  `help` inside the probe on 2026-09-11, but nothing prevents a future
  entrypoint from doing work before its `case`. The 5s bound turns that into
  a finding rather than a hang. Accepted, and the finding is the right
  outcome.
- **The phrase is prose and a rewrite of a help text drops it.** Then that
  tool silently leaves the sweep. The test suite cannot catch this — it runs
  against fixtures, not the real folders. Mitigation: the `check` output
  gains one `notes:` line, "status sweep: N tools declared", so a nightly
  report that says 3 where it said 4 is readable by a human. Not a finding,
  because a count is not a fact about health; but visible.
- **One sweep of `help` per night across 52 tools** is ~52 process spawns
  more than today. Measured wall time for all 52 `help` calls on
  2026-09-11: 5.0 seconds, the slowest single call 0.34 s
  (`local-mac-utils/mac-utils.sh`). Accepted.

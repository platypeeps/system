# Implement — system-local-health-check-does-not-sweep-status

Three pull requests. PR 1 is the item; PR 2 makes PR 1's acceptance criteria
run in CI instead of by hand; PR 3 removes the last hand-written stage. PR 2
and PR 3 are independent of each other and both depend on PR 1.

## PR 1 — the sweep, the remedy, and the convention

`local-health-check/health-check.sh` replaces its Mezmo stage (lines
223-243 at `4c9ebff`) with a sweep — now
`local-health-check/health-check.sh:266-339` — `pipeline.sh` takes back its
own remedy text, and CLAUDE.md says what the script does.

- [x] A `bounded SECS cmd...` helper in POSIX sh: background the command,
      watchdog with `sleep`, `kill` on expiry, exit 124. No `timeout`.
      (`local-health-check/health-check.sh:83-113`; callers redirect to
      files, not a pipe, so a child the killed command leaves behind cannot
      hold the bound open, and the watchdog kills those children too.)
- [x] `HEALTH_CHECK_TOOLS_ROOT` (default `$DIR/..`); the sweep globs
      `$HEALTH_CHECK_TOOLS_ROOT/*/` and derives each folder's entrypoint by
      convention 1 (`<folder minus local-/mezmo->.sh`), skipping folders with
      none and skipping `local-health-check` itself.
- [x] For each entrypoint: `bounded 5 sh "$e" help`; a tool joins the sweep
      when that output contains `local-health-check`.
- [x] For each declared tool: `bounded 30 sh "$e" status`; 0 and 3 silent; 1
      a finding of the tool's first output line plus `run <path> status by
      hand for the full response`; 124 a finding naming the bound; any other
      code a finding naming the code.
- [x] One `info` line: `status sweep: N tool(s) declared, M checked` (the
      design's section 6 risk). `M` counts the tools whose `status` answered
      inside the bound, so a hang reads as `4 declared, 3 checked`.
- [x] `mezmo-pipeline/pipeline.sh` `status_cmd`: the FAIL line says what a
      401 and a 404 mean, keyed on `$MZ_CODE`. The health check's copy of
      that sentence is deleted. Two cases in the offline suite pin the text.
- [x] `health-check.sh` help text (`local-health-check/health-check.sh:48-55`)
      describes the sweep and the declaration phrase; no `mezmo:` stage name.
- [x] `local-health-check/README.md` says the same.
- [x] `CLAUDE.md` convention 6: replaces the three-name inventory
      (`CLAUDE.md` line 147 at `4c9ebff`; now item 6 of its Conventions) with the
      declaration rule and how to enumerate
      (`grep -l 'local-health-check' */*.sh` — or, once PR 1 lands, the
      sweep's own `notes:` line).
- [x] `grep -cE 'pipeline\.sh|fluentbit\.sh|opentelemetry-collector\.sh'
      local-health-check/health-check.sh` prints `0`.
- [x] `sh local-health-check/health-check.sh check` on this machine: no new
      findings, `status sweep: 4 tool(s) declared` in notes, under 60 seconds.
      (2026-09-11: findings block identical to the capture taken before the
      change; `status sweep: 4 tool(s) declared, 4 checked`; 30.6 s.)
- [x] `sd-docs-lint` from the root exits 0 (the line citations in this
      folder move when the stage is rewritten — update them in the same PR).

## PR 2 — the test suite, wired into CI

- [ ] `local-health-check/tests/test_sweep.py`, `unittest`, run by
      `health-check.sh test` (the verb joins `run|check`; help documents it).
- [ ] A fixture root built in `setUp` with four scratch folders, each holding
      a `<name>.sh` that answers `help` and `status`:
      `local-declares-broken` (help has the phrase, `status` exits 1),
      `local-declares-skip` (phrase, exits 3), `local-declares-hangs`
      (phrase, `status` sleeps 600), `local-silent` (no phrase, no `status`
      verb, usage exit 1).
- [ ] Runs `health-check.sh check` with `HEALTH_CHECK_TOOLS_ROOT` at the
      fixture and `HEALTH_CHECK_STATE` in a temp dir, asserting: exactly two
      findings; one names `local-declares-broken` and carries its first
      output line; one names `local-declares-hangs` and the bound; nothing
      names `local-declares-skip` or `local-silent`; the run finishes inside
      the bound plus a margin.
- [ ] The sweep-hang bound is overridable for the test only
      (`HEALTH_CHECK_STATUS_BOUND`, undocumented in help, so the hang case
      takes 2 seconds and not 30). This is the one tunable, and it exists for
      the suite, not for tools.
- [ ] `.github/workflows/system-native.yml` gains
      `run_suite health-check sh local-health-check/health-check.sh test -v`
      next to the others. CLAUDE.md's opening paragraph does not count suites
      and is not edited for this.
- [ ] `sh local-health-check/health-check.sh test -v` prints a unittest
      summary with `OK` and no skips.

## PR 3 — the runner joins the sweep

- [x] `local-sd-runner/sd_runner/cli.py`: `status` exits 3 when
      `launchctl print gui/<uid>/local.system-tools.sd-runner` fails (the gate moved in
      from `health-check.sh` lines 159-163 at `b4264ad`;
      `local-sd-runner/sd_runner/cli.py:36-50` and
      `local-sd-runner/sd_runner/cli.py:137-149`), 0/1 otherwise as before.
      The JSON body is unchanged so the dashboard and
      `local-sd-plan/sd-plan.sh:114` read what they read.
- [x] `runner.sh` help: the `status` line ends with "local-health-check reads
      these codes" (`local-sd-runner/runner.sh:9`).
- [x] `local-sd-runner/runner.sh test -v` passes with a new case for the
      not-loaded exit (`local-sd-runner/tests/test_status.py`; 2026-09-11:
      `Ran 133 tests`, `OK (skipped=3)` — the same three
      `test_ship_lifecycle` skips, "cross-repository sd-ship checkout is
      required", as untouched `main`).
- [x] The stage at `health-check.sh` lines 159-163 at `b4264ad` is deleted;
      the sweep's note reads `5 tool(s) declared` on a machine with the
      runner installed (2026-09-11: `status sweep: 5 tool(s) declared, 5
      checked`, 28 s, no finding naming the runner).
- [x] The sweep finds `runner.sh` without a rename: when `<stem>.sh` is
      absent and the folder holds exactly one `*.sh`, that one is the
      entrypoint (`local-health-check/health-check.sh:298-309`; design
      section 4). Reaches `local-sd-runner` and `local-project-dashboard`
      and nothing else on this checkout.
- [x] If this touches more than `cli.py`, its test and `runner.sh`'s help,
      stop and take the design's fallback: the stage stays with a comment
      saying why the runner's third state lives here. (Not taken: the change
      is `cli.py`, `tests/test_status.py`, the help line, and the two
      READMEs; the sweep fallback above is the health check's own.)

## Verification

Named before the work:

- **PR 1:** `grep -cE 'pipeline\.sh|fluentbit\.sh|opentelemetry-collector\.sh'
  local-health-check/health-check.sh` prints `0`, and
  `sh local-health-check/health-check.sh check` on this machine prints the
  same findings block as before the change (captured first) plus one
  `status sweep:` note. A `usage:` line or a `gito` line anywhere in the
  output is a fail. Then, by hand once: `MEZMO_PIPELINE_SERVICE_KEY=bogus
  sh mezmo-pipeline/pipeline.sh status` exits 1 and its FAIL line names the
  401 remedy — that is the tool's test, and PR 1 relies on it.
- **PR 2:** `sh local-health-check/health-check.sh test -v` ends in `OK`
  with the four fixture cases listed; the CI job's `run_suite health-check`
  line is green on the PR. A skip is a fail (the wrapper enforces it).
- **PR 3:** `sh local-sd-runner/runner.sh test -v` ends in `OK`; on this
  machine `sh local-sd-runner/runner.sh status; echo $?` prints `0` with the
  agent loaded and `3` after `launchctl bootout gui/$(id -u)/local.system-tools.sd-runner`
  (and `0` again after bootstrap). The bootout is the one step here that
  touches a live service and is run by a person, not CI.
- **All three:** `sd-docs-lint` from the repository root exits 0 — the
  citations in this folder point into the stage being rewritten and will
  need moving in PR 1.

What cannot be verified here: that the phrase survives future help-text
rewrites in the four tools. The `notes:` count is the only guard, and it is
read by a person.

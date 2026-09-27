# local-health-check

Nightly computer health review with emailed findings and suggested fixes.
Complements local-maintenance (upkeep: updates, mounts, cert, disk space,
log rotation) — this one asks "is the machine sick?", not "is it tidy?".

## Usage

```sh
./health-check.sh check   # print the report, no email
./health-check.sh run     # email findings via local-notify (cron entrypoint)
./health-check.sh test    # the status sweep's unittest suite (-v forwarded)
```

No email when everything is clean. `run` exits 1 only when the email could
not be delivered, so the cron failure push covers a lost report.

## Checks

- app crash reports (`.ips`, last day) and kernel panics (last week) from
  both DiagnosticReports folders
- every `$SYSTEM_TOOLS_LABEL_PREFIX.*` (default `local.system-tools.*`) /
  `com.platypeeps.*` LaunchAgent: loaded, and last exit code clean
- disk0 SMART status
- memory pressure level, swap usage, 5-minute load vs core count
- unified-log fault volume — flagged only when it more than doubles the
  stored baseline (absolute counts are six-figure noise on a healthy Mac)
- new entries in /Library/LaunchDaemons, /Library/LaunchAgents and
  ~/Library/LaunchAgents since the baseline: persistence added behind your
  back gets flagged once, then joins the baseline
- default gateway ping and DNS resolution
- the `status` verb of every sibling tool that declares convention 6 (see
  below): exit 0 and 3 are silent, 1 is a finding carrying the tool's own
  first output line, any other code or no answer inside 30 seconds is a
  finding naming the code or the bound

State (fault + persistence baselines) lives in `~/.config/health-check`;
first run seeds it and says so instead of alerting.

Unless that stage is switched off, `local-jev` orders those findings so the
loudest line in the mail is the one that matters — see **Ordering with Jev**.

## The status sweep

No tool is named in `health-check.sh`. It asks every sibling folder's
convention-1 entrypoint (`<folder minus local->.sh`) for `help`,
bounded at 5 seconds, and a tool is in the sweep when that output contains
the phrase `local-health-check` — the sentence its author writes anyway,
"local-health-check reads these codes", next to the `status` line. Those
tools are then asked `status`, bounded at 30 seconds. A tool that does not
say the sentence is never run and never raises a finding, which is what
keeps the 30-odd entrypoints without a `status` verb — and the ones whose
`status` is a report that exits 0 regardless — out of the nightly mail.
When a folder has no `<stem>.sh` but exactly one `*.sh` at its root, that
one is taken as the entrypoint — for `local-sd-runner/runner.sh` and
`local-project-dashboard/dashboard.sh`, whose names a launchd plist and the
pack's `sd-plugin.json` hold, so a rename was rejected; any other shape is
skipped.

To join: implement `status` as 0 healthy / 3 nothing to check / 1 broken,
and add the sentence to `help`. Nothing here changes. To see who is in, read
the notes line `./health-check.sh check` prints:
"status sweep: N tool(s) declared, M checked". The probe is the only count:
a grep of the sources for the phrase also finds this folder's own script,
which the sweep skips, and any script that merely mentions the sweep.

The remedy line of a finding is the tool's own first output line, so a tool
whose `status` knows what its failure means should say so there — for
example, naming a rotated API key on a 401. The health check carries no tool's remedy.

The bound is plain `sh` (a backgrounded command and a `sleep` watchdog),
not `timeout`: that is Homebrew coreutils here, and the nightly job runs
under launchd's bare PATH. On expiry it sends TERM to the command and every
descendant, then KILL a second later to whatever is left, so a `status`
that ignores TERM still ends. A hang is known by the bound's own flag, not
by exit code 124, so a `status` that exits 124 is reported as that code. `HEALTH_CHECK_TOOLS_ROOT` points the sweep at a
different folder of tools; it exists for a fixture tree, not for machines.

## Ordering with Jev

`local-jev` answers one narrow question about some state. Here it answers
"does this finding need a human tonight?", once per distinct shareable form,
and the findings are printed highest-probability first with the number
appended. Findings that share a form, such as two unloaded jobs, are one
question and carry its one answer, so they tie and keep the stages' order:

```
- launchd: local.system-tools.repo-sync last exited 1  [jev: needs a human tonight 0.91]
  fix: check its log — cron jobs: local-cron-jobs/logs/, ...
```

**It orders and labels. That is all it does.** No finding is suppressed,
dropped or reworded, the count in the subject line does not move, and no exit
code changes: `run` still exits 0 with findings and 1 only when the email
could not be delivered. The marker words a stage prints are the stage's, and
this stage never touches them.

It runs when Jev can answer here and this stage has not been switched off,
which is one question and so one call:

```sh
jev enabled JEV_HEALTH_CHECK    # 0: keyed, switched on, and this stage is on
JEV_HEALTH_CHECK=off ./health-check.sh run   # today's report, for one run
```

**`JEV_HEALTH_CHECK` only switches this stage off; unset means on.** It used
to be an opt-in testing `=1`, and a per-caller switch that defaults to off
makes every integration added after it silently never run — a keyed machine,
the fleet switch on, and no ordering, because nobody exported anything. The
words it reads are the switch's own, `0`, `off`, `false`, `no` and `disabled`
in any case; every other value leaves the stage on, the `1` this used to
require included. It only ever subtracts: `jev enabled` reads the fleet
switch, the key, a key still reading `change-me` and an unparsable
`JEV_TIMEOUT` first, so this variable can take the stage out and can never put
it back in on a machine that cannot answer.

With this stage switched off, or with Jev switched off, unkeyed or failing, the
report is byte for byte the report it has always been. `jev enabled` costs
nothing and calls nothing, so the check is free on a machine that never had a
key. Jev's entrypoint is resolved from this folder
(`../local-jev/jev.sh`, overridable with `HEALTH_CHECK_JEV` for a fixture) and
never from `PATH`: cron does not have one.

Every failure past that point degrades to today's order and today's text, and
says so in the report's notes rather than silently — a failed request, an
answer set that does not cover every finding, a probability outside 0..1.

### One request, and never about itself

All the findings go out in a single `jev ask`: questions in one request run in
parallel, so the nightly cost is one round trip however long the report is. A
report with fewer than two findings asks nothing at all.

The sweep above probes `local-jev status`, which is itself a real request. So
when that probe is what raised the finding, the ordering does not run: a judge
that just said it is broken cannot rank its own failure, and asking anyway
would spend a second request to learn nothing. That is why the stage sits
after the sweep. Nothing recurses — `jev ask` runs no tool in this repository
— the risk was the doubled bill, not a loop.

### What leaves the machine

Every Jev call leaves the machine, so the payload is built for that and not
copied from the report:

- **sent**: one shareable form per finding, which each finding site writes
  beside its headline. It holds the stage's own words, numbers, and this
  repository's tool folder names, and nothing the machine named:
  - a launchd label becomes `<cron job>` (`<prefix>.cron.*`) or `<agent>`:
    `launchd: <cron job> last exited 1`;
  - crashing apps, kernel panic files, the top faulting processes and new
    launch daemons become counts;
  - a status-sweep finding keeps the tool's folder name and drops the tool's
    verdict line: `local-cron-jobs: status reports broken (exit 1)`, because
    that line names the failed jobs;
  - the gateway address is dropped.

  The old redaction still runs over it as a second net: `/Users/...` and
  `$HOME` paths become `<path>`, addresses `<ip>`, and this machine's host
  name, any `*.ts.net` and any `*.local` name `<host>`. Quotes and
  backslashes are replaced rather than escaped, so a finding can never
  reshape the JSON.
- **never sent**: the finding headline (it names private routines such as
  `<prefix>.cron.<job>`, apps and installed software), the `fix:` lines, the
  notes block, the report header, the host name, anything from a `.env`, and
  any finding at all when this stage is switched off.

Two findings with the same shareable form, such as two cron jobs that both
failed, are one question to Jev. They share its one answer and keep the
stages' order between them, so Jev never ranks names it did not see.

A finding site that writes no shareable form stops the ordering for the whole
report, with `jev ordering skipped: N finding(s) have no shareable form` in
the notes. A new finding that forgets it fails closed; it cannot leak its
headline (sd:1362).

The redaction over-reaches on purpose — a clock time can read as an address.
Losing a timestamp costs the judgment nothing; a leaked tailnet name cannot be
taken back.

## Tests

`./health-check.sh test` runs `tests/test_sweep.py` (`unittest`; arguments
are forwarded, so `-v` lists the cases). It builds a root of scratch
tools — one that declares the contract and exits 1, one that exits 3, one
that exits 124, three that hang (a plain `sleep`, a `sleep` in a grandchild,
and one that ignores TERM), one that says nothing and has no `status` —
points one `check` run at it with `HEALTH_CHECK_TOOLS_ROOT` and a temp
`HEALTH_CHECK_STATE`, and asserts on the lines that name a fixture: the
right findings and first lines, silence for the others, the
`declared`/`checked` count, the bound enforced, and nothing left running
afterwards. The other stages still
run against the real machine, which is where the suite's wall time goes
(`log show --last 24h` is most of it). The `system-native` CI job runs it
as one `run_suite` line; a skip fails there.

`tests/test_jev_order.py` covers the Jev ordering with the call stubbed —
nothing in the suite reaches the network. It runs `check` once per case against
a fixture tools root, a stub `jev.sh` (`HEALTH_CHECK_JEV`) and a stub `PATH`
for the machine stages, and asserts the contract: this stage switched off and
Jev disabled each reproduce today's output byte for byte, an answer reorders and
labels, a failed `ask`, a short answer set and answers under ids nobody asked
leave the findings and the exit code alone, the set of findings is identical
in every case, nothing private reaches the payload (a path with a space in it
included), and a finding raised by `local-jev` itself stops the
triage before the request.

`local-jev` declares convention 6 in its own help, so the nightly sweep probes
it, and one case is there for the other exit: a `local-jev` that answers `3` —
switched off, or never keyed — stays silent in the sweep and stops nothing.
A `3` is not a finding, this stage never made it one, and `jev enabled` is
what decides whether the ordering runs.

The example cron job `local-cron-jobs/examples/health-check-nightly.job` runs
`run` at 04:15; copy it into `<config>/cron-jobs/jobs/` to schedule it.

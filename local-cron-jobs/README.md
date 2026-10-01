# local-cron-jobs

Headless Claude Code jobs on a schedule, run by macOS launchd — no Claude
Desktop, no open terminal, no cloud routine required. Each job is a headless
`claude -p "<prompt>"` invocation using the CLI's stored credentials.

## Usage

```sh
./cron-jobs.sh list                    # jobs, schedules, installed state, FROM folder
./cron-jobs.sh install <job>|--all     # generate plist + load into launchd
./cron-jobs.sh verify <job>|--all      # installed plist still matches the generator
./cron-jobs.sh uninstall <job>|--all   # unload + remove plist
./cron-jobs.sh run <job>               # run once now, foreground (also writes the job log)
./cron-jobs.sh status [job]            # launchd state, last exit, log tail, recent failures
                                       #   `runs` counts what launchd spawned; a hand-run `run`/`exec` is not counted
./cron-jobs.sh logs <job> [lines]      # tail a job's log
./cron-jobs.sh test                    # unittest suite in tests/ (CI runs it too)
```

`--all` means every job this machine runs: the shared `<config>/cron-jobs/jobs/`,
this host's `<config>/cron-jobs/jobs/<host>/`, and any `CRON_JOBS_EXTRA_DIRS`.
Other hosts' folders are never read, so one config directory can serve several
machines (see [Per-machine jobs](#per-machine-jobs)).

`verify` renders the plist the generator would write now into a scratch
directory and compares it byte-for-byte with the installed one; it never
touches launchd. A job whose plist has gone stale — the generator changed, the
installed file did not — reports `STALE` and exits 1. A setup script can run
this per job and reinstall on a mismatch, so a plist can no longer report
healthy purely because the file exists.

## Configuration

The repository ships no job definitions. Everything per-machine lives in one
directory outside the checkout:

```
<config> = ${SYSTEM_TOOLS_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/system}

<config>/cron-jobs/jobs/*.job         shared jobs: every machine runs them
<config>/cron-jobs/jobs/<host>/*.job  this machine's own jobs
<config>/cron-jobs/.env           the variables below (copy .env.example)
<config>/cron-jobs/notify.conf    NTFY_TOPIC for phone pushes (copy notify.conf.example)
```

The script reads `.env` on every call, including the runs launchd starts with
only `PATH` and `HOME`, so put values there rather than only in a login shell.
An exported value wins over `.env`. All variables are optional.
Every value in `.env` is exported to the jobs, so a job that calls another tool
sees the same `SYSTEM_TOOLS_LABEL_PREFIX` as the script.

| Variable | Meaning |
| --- | --- |
| `SYSTEM_TOOLS_CONFIG` | The `<config>` root above. Export it (not in `.env`) to move every tool's config at once. |
| `CRON_JOBS_EXTRA_DIRS` | Colon-separated extra job directories, searched before `<config>/cron-jobs/jobs/`. A job there overrides a same-named job in the config directory. Keep a schedule in another repository this way. |
| `CRON_JOBS_HOST` | The host folder's name, default `hostname -s`, lower-cased. Set it in `.env` when the machine's name changes with the network. |
| `SYSTEM_TOOLS_LABEL_PREFIX` | launchd label prefix, default `local.system-tools`. Labels are `<prefix>.cron.<job>`. |

## Per-machine jobs

A job in `<config>/cron-jobs/jobs/` runs on every machine that reads this
config directory. A job in `<config>/cron-jobs/jobs/<host>/` runs only on the
machine whose lower-cased `hostname -s` (or `CRON_JOBS_HOST`) is `<host>`.

- A host job overrides a same-named shared job on that host only.
- Other hosts' folders are ignored: not listed, installed, or run.
- No host folder is fine: the machine runs the shared jobs.
- `list` prints a `FROM` column: `jobs`, `jobs/<host>`, or an extra directory.

The lookup order is: each `CRON_JOBS_EXTRA_DIRS` entry, then `jobs/<host>/`,
then `jobs/`. The first folder with `<job>.job` defines the job. The dashboard
Toolbox and `sd-db` read job files in the same order
(`cron_job_dirs` in `lib/system_tools_config.py` and in `sd_db/config.py`).

Move a job to one machine by moving its file, then reinstall there:

```sh
host="$(hostname -s | tr '[:upper:]' '[:lower:]')"
mkdir -p ~/.config/system/cron-jobs/jobs/"$host"
mv ~/.config/system/cron-jobs/jobs/<job>.job ~/.config/system/cron-jobs/jobs/"$host"/
./cron-jobs.sh list    # FROM now reads jobs/<host> for that job
```

The plist does not change, so `verify` stays `ok`. On the other machines,
`uninstall <job>` before the file leaves the shared folder.

## Examples

`examples/` holds sample jobs that run tools in this repository. They are
documentation: nothing installs them. Copy the ones you want:

```sh
mkdir -p ~/.config/system/cron-jobs/jobs
cp examples/health-check-nightly.job ~/.config/system/cron-jobs/jobs/
./cron-jobs.sh install health-check-nightly
```

Job files are sourced by `cron-jobs.sh` wherever they live, so `$ROOT` (this
folder) is available to them: `"$ROOT/../local-<tool>/<tool>.sh"` reaches a
sibling tool from any checkout.

## Adding a job

Drop `<config>/cron-jobs/jobs/<name>.job` (or `jobs/<host>/<name>.job` for this machine only, or a file in a `CRON_JOBS_EXTRA_DIRS` directory; shell vars) and `./cron-jobs.sh install <name>`:

```sh
JOB_SCHEDULE="0 7 * * 1"     # 5-field cron, LOCAL time; supports * N a,b,c */N
JOB_DIR=""                   # optional working directory (default $HOME)
JOB_PROMPT="/some-skill or any prompt"
JOB_MODEL=""                 # optional
JOB_CLAUDE_ARGS=""           # optional extra claude CLI flags
JOB_RESULT_OK=""             # optional ERE the reply's last `RESULT: ` line must match
```

`claude -p` exits 0 whatever the prompt concluded. A prompt that ends its reply
with a `RESULT: ...` line can set `JOB_RESULT_OK` to an extended regex. The run
then fails (exit 1) when the reply has no `RESULT: ` line, or when its last one
does not match. The reply is printed after the agent exits, so the log of such
a job fills at the end rather than live. Example:
`JOB_RESULT_OK="^RESULT: (ok|skipped, window closed)$"`.

Jobs that don't need Claude set `JOB_COMMAND` instead of `JOB_PROMPT` — a
plain shell command run via `bash -c` (exactly one of the two, never both).
Example: `examples/secret-scan-weekly.job` runs
`local-scan-for-secrets/scan-for-secrets.sh critical` every Monday 07:00;
the scanner's exit 2 on findings counts as a failure on purpose, so leaks
trigger the failure notifications below.

`install` translates the cron expression into launchd `StartCalendarInterval`
entries and loads `<prefix>.cron.<name>` into `gui/$UID` (prefix from
`SYSTEM_TOOLS_LABEL_PREFIX`, default `local.system-tools`). Re-run `install`
after editing a job to apply changes.

Every plist runs `/bin/bash <dir>/local-cron-jobs/cron-jobs.sh exec <name>`.
The `local-machine-setup` cron stage reads that command to tell its own agents
from another installer's agent under the same label prefix. It uninstalls only
its own. Change the command in `write_plist` together with `cron_plist_ours` in
`local-machine-setup/machine-setup.sh`; a test there renders a plist with this
script and fails when the two disagree.

## Failure reporting

On non-zero exit the wrapper:
1. posts a macOS Notification Center alert,
2. appends to `logs/failures.log`,
3. pushes to [ntfy.sh](https://ntfy.sh) if `NTFY_TOPIC` is set in
   `<config>/cron-jobs/notify.conf` (copy `notify.conf.example`). That is the best
   channel for reaching your phone with zero infrastructure; the macOS
   notification only helps while you're at this machine.

`status` lists only failures that are still outstanding. A logged failure is
dropped once the job's own log records a later `[<job>] <ts> done` line, so a
job that failed at 02:16 and succeeded at 02:16 the next night stops being
reported. When every logged failure has been followed by a success, `status`
says so instead of reprinting a list nobody can act on. The comparison is a
string compare of the `%Y-%m-%dT%H:%M:%S%z` stamps — correct while the offset
is stable, off by a day across a DST change.

`status` also answers with an exit code, and `local-health-check` reads it: 0
healthy, 3 when no job it was asked about is installed on this machine, 1 when
the evidence says a job's last run failed and nothing has recorded a success
since. `health-check.sh` drops launchd's counter for a `<prefix>.cron.*` label
only when `status` exits 0 for that job; it still reports a plist that is not
loaded, which only launchd knows.

**launchd's failure stands, and only a recorded success retires it.** That is
the inversion the ninth round of the #486 review asked for, and it is the one
thing to understand about everything below. Every earlier round asked when
launchd's non-zero exit could be *suppressed*, and hardened the evidence that
fed the suppression; each hardening closed the hole in front of it and opened
the next one, because absence of recovery evidence was still being read as
recovery. A failure is now retired only by positive, self-contained evidence
that a later run succeeded. No record, a record in an earlier format, a record
from another lifetime, a fresh lifetime, a label launchd does not hold: each
of those is silence. Silence suppresses no failure and invents none.

**The run writes its own outcome down.** `logs/.<job>.runs` is written by a
completed run and by nothing else. `exit=<code>` is that run's own exit code
and is always written. `runs=<n>`/`lifetime=<coalition id>`/`boot=<kern.boottime
sec>` name the run and are written when launchd can supply them, all three or
none. The run writes the record to a temp file and renames it into place, so a
reader sees the old record or the new one, never part of either. The run
removes `logs/.<job>.attempt` only after the rename succeeds: a run that could
not record its outcome keeps the marker, which withdraws the earlier success.
Every completed run has an outcome; only some have a launchd identity —
launchd holds no coalition for a label it has bootstrapped and not yet spawned,
which is every label after a reboot, so a run by hand in that window has no
identity to write. Aborting the write there left the previous lifetime's
record answering for the newer run, in both directions: a manual failure after
a recorded success read `exec_exit=7 status_exit=0`, and a manual success after
a recorded failure read `exec_exit=0 status_exit=1`, so a hand-run recovery
could not clear a finding until the next scheduled run. Until it was there, the record carried the run's identity and `status`
re-derived the outcome by matching an anchored pattern against the log — two
independent readings of one run, which disagree the moment the job's own
output has no trailing newline: `printf error; exit 7` writes
`error[demo] … FAILED rc=7`, the anchor does not match it, the identity fields
advance as usual, and an earlier `done` passes every freshness check. Wherever
launchd holds the label, which is every installed job, nothing parses log text
to decide pass or fail any more. The log's `done`/`FAILED` line still decides
in one case: the label is not loaded, and no run has recorded an outcome, so
nothing else holds a verdict (step 5 of `job_is_broken`'s ladder).

**The other three fields say which run the record is**, and that is all they
decide: whether the record may supersede launchd's verdict. They are checked
against launchd, and all three must match, or the record supersedes nothing —
it is still the latest known outcome, and it still outranks the log.

The two halves of that rule are asymmetric, and the asymmetry is the point. A
recorded FAILURE reports on its own: the record holds the last completed run
there was, a later completed run would have replaced the file, and reporting is
the safe direction anyway. A recorded SUCCESS may retire launchd's failure only
with the identity, because "later than launchd's last spawn" is what those
three fields prove and nothing else does. Without them a success waits behind
launchd's verdict and answers only where launchd has none.
`runs` is launchd's own spawn counter, incremented at spawn — measured, not
assumed: a probe agent kickstarted three times read `runs = 1, 2, 3` from
inside its own three runs. The test is equality: a counter ahead of the record
is a run that recorded nothing, and a counter behind it is a record from a
lifetime the tokens failed to separate. The coalition id is the label's
resource coalition, constant across the runs of one lifetime (three kickstarts
read `runs = 1, 3, 5` under the same id) and changed by every bootstrap,
including a `bootout` plus `bootstrap` inside one boot that boot time cannot
see. The boot token is `kern.boottime`, which separates the boots across which
that id repeats — it is a boot-local counter, so a post-reboot label can be
handed the id a pre-reboot record already names. Each covers the other's blind
spot.

A hand run is what makes this work in sd:1201's own case. `run` and `exec`
from a shell never reach launchd, so they leave `runs` untouched and record
launchd's current count with their own exit code. The re-run that fixed a job
is therefore a completed success at launchd's newest run number, and it is the
one thing that retires launchd's failure.

**Only a completed run writes the record.** A start is not a completion: while
the number was also written at spawn, a run killed by SIGKILL afterwards left
the count level beside the previous `done` and read as healthy. Nothing keeps
a job from reporting itself through its own slot either, because a job launchd
reports as `state = running` is named rather than judged — launchd counted that
run at spawn and it has recorded no outcome yet. That covers a `status` read
from inside a running job, which is what `local-health-check` is.

**A run that started and recorded nothing withdraws a recorded success.**
launchd's identity cannot see a hand run at all, so two successive hand runs
carry the identical triple and the first one's `exit=0` kept proving it was
the newest run after a second hand run died on SIGKILL. `logs/.<job>.attempt`
is written before the command runs and removed when an outcome is recorded,
which every path that finishes does, including the trap that catches a
failure. What survives it is a run no trap could see, and its marker takes the
recorded success out of the ladder. That is not a failure of its own: it
leaves launchd's evidence to decide, so it reports only where launchd already
held a failure. The marker carries the boot it was written in, so one left by
a power cut is debris after the next reboot — by which point the record's own
identity has stopped matching anyway.

**No record means no opinion, not a failure.** An installation that predates
this carries a green log and nothing beside it, and a record in any earlier
format — the bare number this began as, the `runs=`/`boot=` pair, the
`runs=`/`lifetime=` pair, or the three-field record without `exit=` — carries
no outcome and reads the same way. A record with `exit=` and no identity is
none of those: it is the current format with fields absent, it carries an
outcome, and it is read as one. None of that is evidence of a failure,
so none of it produces one: launchd's own last exit decides, and a clean exit
there leaves the job unreported. Only when launchd holds no verdict either,
because the label is not loaded, does the log's last `done`/`FAILED` line
decide. `install` writes no record for the same
reason. It bootstraps the label, so launchd sits at `runs = 0` and
`last exit code = (never exited)` and holds no verdict; a record written there
would have to claim an exit code for a run that has not happened.

The bound on that is one run per job. A job whose launchd exit is non-zero and
whose recovery was a hand run — sd:1201's own jobs — is reported again until
its next completed run writes a record, because the `done` in its log is text
that F3 proved can lie. Two of this machine's jobs were in that state when the
change landed.

**A job launchd has not run yet is pending, not broken.** An installed job
with no record, no log and no launchd verdict is unknown, and this file
reports unknown — a 0 with nothing behind it retires launchd's counter in
`health-check.sh`. The one exception is launchd saying, itself, that it has
not run the job: a label it holds and has not spawned prints `runs = 0` and
`last exit code = (never exited)`, and then there is no outcome *because*
there has been no run. Reported as a failure, every newly installed job was a
nightly finding until its first slot — weeks for a monthly job.

Both readings are required, and that keeps the exception away from the case it
must not touch. A label launchd does not hold prints neither field, so an
installed plist that is not loaded stays reported; `health-check.sh` also
reports that one separately, and only launchd knows it. A recorded failure is
untouched either way: pending is about the absence of an outcome, never about
overriding one.

**Zero runs in a lifetime is no evidence, not a missing one.** launchd prints
`runs = 0` for a label it has bootstrapped and not yet spawned, with no
coalition block, because no process has been placed in one, and
`last exit code = (never exited)` — all three measured on this machine, before
and after a `bootout`/`bootstrap` of a label that had already exited 7. That
is the state of every label for a while after every reboot. There is no run
here for the record to be about and no verdict from launchd either, so the
last recorded outcome stands. Reading that zero as an unaccounted run made a
normal reboot a finding against every green job until its next slot — days for
a weekly job, weeks for a monthly one — with no failed invocation anywhere.

A hand-run counts. `run` and the LaunchAgent are the same code path, and both
write the job's log: launchd through the plist's `StandardOutPath`, `run`
through a tee (skipped when stdout already is that file, so launchd runs are
not doubled). Before that, only the failure half of a hand-run was recorded —
`logs/failures.log` was appended on any run, while the `done` line that clears
it landed only under launchd. So running a job by hand could raise a failure
that nothing but the next scheduled firing could clear, and left the job
looking stale to `watchdog`, which reads log age.

That tee adds time to a hand-run, and most of it is the one sleep after tee has
already exited. Closing the last writer is what hands tee its EOF and on macOS
that wakeup is sometimes lost, so the run's teardown reopens the FIFO to deliver
it, up to fifty times, sleeping 0.1 s between tries (sd:773). In the ordinary
case it takes one try, so the added time is roughly the real wall time of one
`sleep 0.1`, and that overshoots by an amount that varies with timer behaviour
and load. Measured at sd:808 on one Darwin 25.6.0 arm64 machine at load 5.5-8.6,
30 runs of a three-line job per variant, three repeats: every run with the loop
did exactly one iteration, and the loop added 0.165 s a run (0.043 s without it,
0.208 s with it). The same count held with a 0.01 s sleep, so tee is gone well
inside the first 0.1 s. A 0.01 s sleep cut the added time to 0.040 s, about 75%
less; 0.02 s for the first five tries cut it to 0.077 s, about 53% less. A probe
of the same shape on the same machine at the verification of system #357, load
3.9-5.1, measured +0.237 s with cuts of 70% and 36%, because `sleep 0.1` took a
median 0.23 s there. These are two sessions' figures, not fixed costs. The 0.1 s
sleep and the bound of 50 are kept by owner decision (sd:808, 2026-09-14). The
measured cuts saved 0.085-0.165 s a hand-run in those two sessions (sd:808:
0.125 s with a 0.01 s sleep, 0.088 s with the 0.02 s form; the #357
verification: 0.165 s and 0.085 s), on hand-runs only. The alternative, 0.02 s
for the first five tries with the bound raised to 54, keeps the five-second
window by arithmetic only and was not timed against a stuck tee. A scheduled job
pays none of this — launchd already has the job's stdout on the log, so no tee
is started and the teardown returns at its first line.

The loop bounds the spinning, not the hang. Fifty reopens over five seconds
that fail to release tee end at the same `wait` as before and the run blocks for
ever, which is the failure sd:773 is about. The reopen makes that much less
likely and not impossible: measured at sd:773 on one machine, 4 hangs in 3000
runs without it and 0 in 3000 with it. Killing tee once the bound is spent would
trade the residual hang for a truncated log, and the end of the log is the half
worth reading, so it is not done.

A prompt job's `claude -p` also writes its own trace, per run, to
`logs/<job>.debug.<run>.log`, where `<run>` is the run id the report carries
(`YYYYMMDDTHHMMSSZ-<pid>`), through `--debug-file` on the agent call. Only a
failure keeps one: a run that exits 0 removes its file before it ends, and
after every prompt-job run the runner keeps the newest five per job and
removes the rest, so a job that fails every night does not fill the log
directory. The job log never quotes the trace. Read it after an exit the
runner cannot explain, such as `error: An unknown error occurred (Unexpected)`
(sd:972): that text is the runtime's, printed before any session exists, and
the debug file is the only record of what the binary was doing. The log's
second line, `[<job>] <ts> agent: <path> -> <target>`, names the binary
`claude_binary()` resolved and what it links to, so a version flip between
two runs is readable from the log. A `JOB_COMMAND` job gets neither.

## External dependency: google-workspace-mcp

Prompt jobs that read or send Gmail can go through a host
**google-workspace-mcp** server (`mcp__workspace-mcp__*` tools, HTTP on
`localhost:8083`). It is not part of this repo; a LaunchAgent keeps it alive
(`RunAtLoad` + `KeepAlive`). If it's down, such jobs should skip the mail step
and say so; check `launchctl print gui/$UID/<its label>`.

`KeepAlive` restarts only a process that exits. A server that hangs while its
process lives (2026-09-25) is caught by `workspace-mcp-watch` instead: every
10 minutes it runs `local-workspace-mcp/workspace-mcp.sh watch`, which probes
with an MCP initialize and restarts the agent once when that goes unanswered.

## Gotchas

- Jobs run with `--dangerously-skip-permissions` — headless runs can't answer
  permission prompts. Only add jobs whose prompts you trust end to end.
- The agent binary is resolved when a prompt job runs, in this order: an
  exported `CLAUDE_BIN`, `claude` on `PATH`, then `~/.local/bin/claude` (the
  install location). None of the three means the job fails with one sentence
  naming `CLAUDE_BIN` — not a "no such file" from exec, which is what a bare
  name fallback gave any caller that reached this script without a login
  shell's `PATH`. `list`, `verify` and `help` never resolve it.
- A prompt job's `claude -p` runs with `SD_HANDOFF_RESTORE=0` in its
  environment, set on that one call and not exported for the run. The pack's
  SessionStart hook (`bin/sd-handoff-restore`) otherwise restores a pending
  handoff packet into any session that starts in the repository, and an
  unattended 3 a.m. job is not the session the packet was written for — it
  would consume it. The pack's `sd-handoff` SKILL.md has said this script
  exports the variable since the hook shipped; until 2026-09-11 nothing here
  did. `JOB_COMMAND` jobs are untouched. `./cron-jobs.sh test` is the check:
  a fake `CLAUDE_BIN` records the environment it was started with.
- Auth: uses the `claude` CLI's stored account. **cswap-auto rotates accounts**
  (see `local-cswap`) — a job that fires right after a switch runs on whatever
  account is active. Skills pinned to one account's connectors (Notion, Drive)
  may fail on the wrong account; that surfaces as a job failure notification.
- claude.ai-hosted connectors that need interactive re-auth will fail
  headlessly — re-auth in an interactive session, the cron run picks it up.
- launchd runs missed `StartCalendarInterval` jobs once when the Mac wakes,
  but not if it was powered off — a skipped week stays skipped.
- Overlap protection: a job still running when its next slot fires is skipped
  (a kernel `flock` on `logs/.<job>.flock`, taken through perl). The job's
  processes inherit it, so it stays held while any of them runs, even with the
  runner killed. The kernel releases it when the last one exits, so a SIGKILL
  leaves no stale lock behind.
- `status` names a run that started, recorded no outcome and whose process is
  gone as `abandoned` in its verdict line. That is not a failure: the exit code
  stays 0, and the next completed run clears it.
- LaunchAgents run only while you are logged in (not logged out / FileVault
  pre-login).

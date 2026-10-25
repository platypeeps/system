---
title: The scheduled jobs
eyebrow: What runs unattended, and what it leaves behind
stand: A job is a schedule plus one verb. launchd fires it, a lock keeps it single, and every run leaves a log line, a database row, and — on failure or a declared finding — a report that Today shows.
---

## The shape of a job

A job is one file: `<config>/cron-jobs/jobs/<name>.job`, where `<config>` is
`$SYSTEM_TOOLS_CONFIG` (default `~/.config/system`). It sets `JOB_SCHEDULE`
and **exactly one** of two verbs.

```sh
JOB_SCHEDULE="45 4 * * *"
JOB_COMMAND="\"$HOME/repos/platypeeps/sd-ai-command-pack/bin/sd-db\" backup"
```

- **`JOB_COMMAND`** runs a shell command under `bash -c`.
- **`JOB_PROMPT`** runs a prompt through `claude -p`, with
  `--dangerously-skip-permissions` and a debug file. `JOB_MODEL` and
  `JOB_CLAUDE_ARGS` apply to this form only.

Setting neither is an error. Setting **both** is also an error, caught when the
job file is sourced: *"pick one"*.

The choice is a real design decision, not a style preference: a deterministic
step belongs in `JOB_COMMAND`, and only work that needs an agent belongs in
`JOB_PROMPT`.

@diagram jobs-anatomy

## Which machine runs which job

The repository ships no installed jobs. `local-cron-jobs/examples/` is a
*catalogue*: copy the jobs every machine should run into
`<config>/cron-jobs/jobs/`, and the jobs one machine runs into
`<config>/cron-jobs/jobs/<host>/`. `<host>` is `hostname -s` lower-cased, or
`CRON_JOBS_HOST`. `CRON_JOBS_EXTRA_DIRS` may add more folders.

A host job overrides a same-named shared one, and other hosts' folders are
never read. `install --all` installs the shared jobs plus this host's; no
profile filter sits between the folders and the install. So on a machine where
`local-machine-setup` records a profile, `install --all` and `verify --all`
refuse, and its cron stage installs the jobs that profile names (sd:3186).

## What the jobs are

@jobs

## What one run does

`launchd` invokes `cron-jobs.sh exec <job>`, and that function does six things
before the job's own work begins.

1. **Takes a lock** — a `mkdir` on `logs/.<job>.lock`. A second run while the
   first is active exits 0 with *"previous run still active"*. The lock is
   released by an `EXIT` trap whose path is expanded when the trap is set, not
   when it fires — a function-local variable out of scope at trap time aborted
   the trap under `set -u`, which left the lock forever and made every later
   run skip itself.
2. **Raises the file-descriptor limit** to 65536. launchd hands a job 256 open
   files where a login shell gets 1048576, and the agent needs more than 256 —
   so every prompt-driven job failed overnight while the same job passed by
   hand. The hard limit is already unlimited, so this needs no privilege.
3. **Mirrors output into the job log**, through a FIFO rather than a pipeline.
   A pipeline would put `cmd_exec` in a subshell, where its `EXIT` trap does
   not fire in the parent — and that trap is what releases the lock.
4. **Records the log offset** before starting, so the run's own output can be
   located inside an appended log.
5. **Runs the verb**, in `JOB_DIR` or `$HOME`.
6. **Reports**, always — see below.

For a prompt job, the agent binary is resolved in a fixed order: an explicit
`CLAUDE_BIN`, then `PATH`, then `/opt/homebrew/bin/claude` (the
`claude-code@latest` cask), then `~/.local/bin/claude` (the native installer,
kept for a machine without the cask yet). If none exists, that is
*this job's* failure — it reaches the log, the failure log and the report
like any other non-zero exit, rather than killing the script before the lock
and the log mirror are in place.

The log also records what the binary resolved to, because the cask's `claude`
is a symlink into a versioned Caskroom folder that each upgrade moves. A
version flip between two runs is then readable from the log rather than
inferred. macOS attributes a privacy grant to that versioned binary, so after
the move to the cask, and after a cask upgrade, the operator may need to
re-grant Full Disk Access to the new binary (`.claude/rules/macos-tcc.md`).

Prompt jobs run with `SD_HANDOFF_RESTORE=0` on that one command. A session-start
hook restores a pending handoff packet into whatever session starts in that
repository — and an unattended 3 a.m. run is not the session the packet was
written for. Set on the command and not exported, because a `JOB_COMMAND` job
runs whatever it names, and what that inherits is its own business.

## What every run leaves

**A log line.** `logs/<job>.log`, appended, with a start line carrying the
working directory and an end line reading `done` or `FAILED rc=N`.

**A database row.** `sd reports ingest` records the run id, start, end, exit
code and log offset. If the report cannot be written, the job says so on
stderr and names where the source still is — the run is not lost because the
database was unavailable.

**On failure only:** a line in `failures.log`. A failed run, or one that
prints `SD_REPORT_ATTENTION: <reason>`, files a report that needs attention;
Today lists it until it is acknowledged. Nothing is mailed (sd:3454). A
failure that puts data at risk also pushes to the phone, once until a run is
not at risk (sd:5997). A run proves its data safe with the line
`SD_REPORT_ATTENTION: data safe, one step failed: <step>`; any other failure
is at risk.

A failing prompt job keeps its `claude -p` debug trace; a passing one deletes
its own. The five newest surviving traces are kept, so a job failing every
night cannot fill the log directory.

## Two checks that catch silent breakage

**`cron-jobs.sh verify`** renders the plist a job *would* get into a scratch
directory and compares it byte for byte with the installed one, reporting
`STALE` on a difference. It touches launchd not at all.

This exists because installation could only ever create a plist that was
*absent*. A plist whose content had gone stale reported healthy forever — every
one of nine jobs read fine while carrying a `PATH` that a fix had already
corrected.

**`watchdog-daily`** measures the time of each installed job's last finished
run, its last `done` or `FAILED rc=N` line, against a window derived from its
own schedule: about 26 hours for a daily job, 8 days for a weekly one, 32 days
for a monthly one. Not the log's mtime: a run skipped on the lock still writes
a line, and kept a wedged job green. A job with no finished run is measured
from its install time instead, reported as *"never finished a run,
installed"*, so a job that has silently never fired is visible, not merely
absent from the report.

`STALE` and `MISSING` are spelled in a drift vocabulary that a machine
status check can grep for. A stage that prints only a human-readable
remediation line makes such a check lie: the gap is on screen and
the check still exits 0.

## The 02:xx slot needs a scheduled wake

A laptop in deep idle answers a 02:xx slot inside a DarkWake maintenance
window, where networking is maintenance-only. `repo-sync-nightly` at 02:45
runs, finishes, and fails every repository — split between connection failures
and `Permission denied (publickey)`. The later slots are unaffected, so this is
a slot problem rather than a night problem.

The on-disk key cannot cover for it: `~/.ssh/identity_work` is
passphrase-protected, so every unattended git call depends on `SSH_AUTH_SOCK`
in the launchd domain.

The remedy is a per-machine system setting, and **no stage sets it**:

```sh
sudo pmset repeat wakeorpoweron MTWRFSU 02:40:00
```

Check `pmset -g sched` for a `Repeating power events:` block before trusting a
green night. A machine that happened to be awake passes this slot on its own —
exactly the kind of green that hides the bug.

`machine-setup.sh status` does that check: with no repeating wake it prints
`wake    : MISSING` and counts one drift item.

## Status

**Verified in this build.** The two verbs and the refusal of both or neither;
the install set read from the config jobs folder; the six steps of one
run and the reasons recorded for the lock trap, the descriptor limit and the
FIFO; the binary resolution order; the three records a run leaves and that only
failure or a finding opens a report; the debug-trace retention; what `verify` compares and what
`watchdog-daily` measures. The job table is generated from the example job files
at build time.

**Not verified here.** Whether this machine has the 02:40 wake configured, and
which jobs are currently installed — both are per-machine. Run `pmset -g sched`
and `cron-jobs.sh status`.

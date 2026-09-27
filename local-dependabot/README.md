# local-dependabot

The contract for `dependabot-daily`, the scheduled job that resolves Dependabot
pull requests across the repo fleet every morning, unattended.

The daily sweep is prompt-driven — `cron-jobs.sh` runs
`claude -p --dangerously-skip-permissions` and points it at
[`ROUTINE.md`](ROUTINE.md), which is the whole product. That is deliberate: the
work needs judgement a shell script cannot supply. Classifying a grouped bump
by its widest member means reading Dependabot's prose; deciding whether a
repository's CI actually ran means understanding what a skipped job is; and
three of four holds on the first manual sweep came from repo-specific
tripwires an agent had to read and understand, which a naive
`if major then skip` script would have merged straight through.

The one script here is the other verb's mechanical half: `dependabot.sh
holds` is the held-bump watcher's pass — discovery through the same two
gates, the three probes, the lift sequence and supersession, exactly as the
second half of `ROUTINE.md` states them — and `dependabot.sh test` is the
suite that pins it against a GitHub double and a registry double, because the
acceptance criteria assert edit order and idempotence, which a prompt cannot
be asserted for. Writing a hold in the first place stays the agent's
judgement.

The grant is broad, so the instructions are narrow. `ROUTINE.md` is written as
a contract rather than a description for exactly that reason — nobody is
watching this run.

## Usage

```sh
../local-cron-jobs/cron-jobs.sh install dependabot-daily   # load into launchd
../local-cron-jobs/cron-jobs.sh run dependabot-daily       # run once, now
tail -f ../local-cron-jobs/logs/dependabot-daily.log       # watch it work
./dependabot.sh holds --dry-run --repo platypeeps/example-site  # the watcher, reading only
./dependabot.sh test -v                                    # its suite
../local-cron-jobs/cron-jobs.sh install dependabot-holds-weekly  # the watcher, weekly
../local-cron-jobs/cron-jobs.sh run dependabot-holds-weekly      # one pass over the fleet, now
```

Schedule and prompt live in `../local-cron-jobs/jobs/dependabot-daily.job`.
Runs 06:20 daily — before the working day, clear of `maintenance-nightly`
(03:45) and `repo-sync-nightly` (02:45), which fast-forwards the checkouts the
job enumerates from.

The watcher is the second job, `dependabot-holds-weekly`, Monday 05:45 —
before that morning's daily sweep, so a rebase it asks for is swept the same
day. It is a `JOB_COMMAND` job and not a prompt: it runs `dependabot.sh
holds` over the fleet, whose output is already the log the contract
specifies, one line per record in the words a person searches for (`HELD`,
`LIFTED`, `LIFTED-EARLIER`, `CARRIED`, `UNKNOWN`, `MALFORMED`, `IGNORED`)
and a `TOTAL`. Nothing sits between the script and the log to paraphrase it.
It exits 0 on a week that lifts nothing, for the same reason the daily sweep
does; the failure banner is for a run that could not happen at all. The first
records it watched were three hand-written holds on a website repository,
evaluated `HELD` the same day.

## What it will and will not do

Merges patch and minor bumps whose checks are proven to have actually run and
which GitHub reports `CLEAN`. Never merges a major, a Docker image digest, or
anything in a repository with no CI configured — "no checks reported" is not
"checks passed".

Two gates decide which repositories it touches at all, and they are different
questions. Write access says a merge would *succeed*; the owner gate says one
is *wanted*. Both must pass. The owner gate is two written lists, not one —
repositories named as never-touch are checked before the ones named as allowed,
so a deny survives anything that would otherwise re-admit them. See
`ROUTINE.md` for both lists, and for why they are written out when everything
else here is enumerated at runtime.

It exits 0 whether or not anything merged. Finding pull requests it could not
merge is the job working correctly; failing on that would fire the failure
banner every morning until the signal meant nothing.

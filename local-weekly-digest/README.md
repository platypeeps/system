# local-weekly-digest

One Sunday report summarizing what the automation fleet did all week, so
the nightly jobs can stay quiet unless something needs a human.

## Usage

```sh
./weekly-digest.sh check   # print the summary
./weekly-digest.sh run     # print it for the run's report (cron entrypoint)
./weekly-digest.sh test    # the unittest suite in tests/ (-v forwarded)
```

Contents: per-job run/failure counts for the last 7 days (parsed from
local-cron-jobs logs), the week's failure log entries, machine-setup drift
count (only when a `local-machine-setup` sibling or `WEEKLY_DIGEST_MSETUP`
exists), pending brew/mas updates, root disk usage, and Jev against the
fallback (below).

Unlike the other jobs this one **always needs attention**: `run` prints an
`SD_REPORT_ATTENTION:` line with the date, so each week's digest is its own
report on Reports and Today until it is acknowledged. The finding jobs end
their line in a checksum of their findings instead; a summary is new each
week, so it has nothing to fold into. Nothing is mailed
(sd:3454).
The example cron job `local-cron-jobs/examples/weekly-digest.job` runs it
Sundays 07:00; copy it into `<config>/cron-jobs/jobs/` to schedule it.

## Failure order

The `failures this week` block lists the most frequent failures first. It
groups the lines by job, then orders the jobs by:

1. its failures this week;
2. its newest failure, compared in UTC so a daylight-saving change cannot
   reorder it;
3. its name, so a tie never shuffles the digest from one week to the next.

Inside a job, the newest line comes first. Each line carries the weekly
count, also when the week has only one failure:

```
failures this week:
  2026-09-19T02:45:11-0600 repo-sync-nightly FAILED rc=1 (log: logs/repo-sync-nightly.log)  [3 this week]
```

**It orders and labels. That is all it does.** No line is dropped or
reworded, and no exit code changes. The order comes from the failure log
alone, and nothing leaves the machine.

An earlier version ranked by the current failure streak, read from each
job's own log. Log rotation keeps only a tail, so that streak had no
reliable value, and the ranking was removed (sd:1478).

Only the shape that `notify_failure` in `local-cron-jobs` writes is ordered.
When any line has another shape, the block keeps the log's order, and a
`notes:` line counts the lines that stopped it.

This replaced an order that Jev computed (sd:1478). Once job names stopped
leaving the machine (sd:1474), Jev saw only each line's time, exit code and
weekly count. Those are the numbers this order reads, without a request.

## Jev against the fallback

The operator keeps Jev, measures it, then decides (sd:3583). The digest gives
the measurement, one line per Jev stage for the last 7 days:

```
jev against the fallback, last 7 days (per stage):
  JEV_EXAMPLE_A                4 call(s), differed from the fallback in 1 of 4 (25%)
  JEV_EXAMPLE_B                2 call(s), no fallback answer recorded
```

The lines come from `sd-db judgments compare --since <7 days ago> --json`,
in the command pack named by `SD_PACK_ROOT`. Every stage belongs to one
caller. A call counts as compared when the caller recorded its own answer
for the same decision (`--baseline` or `--shadow`). "Not
comparable" means that answer was not a number, such as a `--fallback`
marker. The comparison arms (Kev, Haiku) and local-only stages, which only
Kev answers, stay in that report.

It is a read: the digest asks Jev nothing. A read that fails or times out
(120 s) prints `unknown`, never a quiet week. Without the pack's `bin/sd-db`
the block is left out, like the drift line.

## Tests

`./weekly-digest.sh test` runs `tests/test_failure_order.py` (`unittest`;
arguments are forwarded, so `-v` lists the cases). It points `check` at a
fixture cron-log tree (`WEEKLY_DIGEST_CRON_LOGS`) and a fixture
machine-setup (`WEEKLY_DIGEST_MSETUP`). It stubs `brew`, `mas` and
`hostname` on `PATH`, so two runs produce the same bytes. The cases are the
contract:

- the order: count, then recency, then name;
- recency across a daylight-saving offset change;
- the label on each line, a single failure included;
- no dropped or reworded line;
- an unshaped line keeping the log's order with a note, a single line and a
  four-field line without the `(log: ...)` tail included.

`tests/test_jev_divergence.py` writes a ledger with the pinned pack's
`sd_db` under a temporary HOME and reads it back through the digest. It also
covers a failed read, an absent pack, and the bound. A `jev` stub on `PATH`
fails any test that calls it. `tests/test_jev_contract.py` lists this folder
in `NOT_CALLERS`, because it names Jev and asks it nothing.

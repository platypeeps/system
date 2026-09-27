# local-weekly-digest

One Sunday email summarizing what the automation fleet did all week, so
the nightly jobs can stay quiet unless something needs a human.

## Usage

```sh
./weekly-digest.sh check   # print the summary
./weekly-digest.sh run     # email it via local-notify (cron entrypoint)
./weekly-digest.sh test    # the unittest suite in tests/ (-v forwarded)
```

Contents: per-job run/failure counts for the last 7 days (parsed from
local-cron-jobs logs), the week's failure log entries, machine-setup drift
count (only when a `local-machine-setup` sibling or `WEEKLY_DIGEST_MSETUP`
exists), pending brew/mas updates, root disk usage.

Unlike the other jobs this one **always emails** — the digest is the
report, and its weekly arrival doubles as a heartbeat that the automation
layer is alive. `run` exits 1 only when the email could not be delivered.
The `weekly-digest` cron job runs Sundays 07:00.

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

`tests/test_jev_contract.py` at the root fails if this folder calls Jev again.

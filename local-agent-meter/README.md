# local-agent-meter

Appends one agent-usage reading to a JSONL ledger every four hours: `codexbar usage`
per provider (claude, codex), each cswap account's seven-day use, and `rtk gain` for
token-proxy savings.

The ledger is, in order: `--out`, else `AGENT_METER_FILE` (exported, or set in
`<config>/agent-meter/.env`; copy `.env.example`), else
`<config>/agent-meter/meter.jsonl`. `<config>` is
`${SYSTEM_TOOLS_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/system}`. The default
ledger's directory is created on first use; a ledger named by `AGENT_METER_FILE` or
`--out` must already have its directory, so a moved target fails loudly instead of
starting a new series nobody reads.

Since sd:234 every reading is written twice: the JSONL line, and one `meter` cost
row per provider per window in the sd database (`~/.local/share/sd/sd.db`), through
`sd_db.sample`. A `meter` row carries the window and the percentage and no money,
and the later Usage-screen slice (8d) reads those rows for its gauges. The rows go
in first, in one
transaction; the JSONL line then records `sd_db.rows`, or `errors.sd_db` when the
library could not be imported or the database could not be opened, or
`errors.sd_db.<provider>.<window>` when one sample was refused. Nothing about the
rows changes the exit, which stays 0.

## What the ledger keeps from codexbar

Since sd:7325 each provider entry is cut to a list of fields (`CODEXBAR_KEEP`):
the provider, source, the three rate windows, `updatedAt`, the extra windows and
the pace numbers. codexbar also reports the account's email, id, login method and
organization; none of them reaches the ledger. An error's message goes to `errors`
with any email in it replaced by `<email>`.

From `rtk gain` the ledger keeps `summary.total_saved` and `summary.avg_savings_pct`
(sd:7344), each only when it is a finite number.

## Claude accounts

Since sd:7322 every reading also carries `accounts`, one entry per Claude account
that `cswap list --json` manages: its slot `number`, `active`, the seven-day `pct`,
`resetsAt` and `expectedPct`, the `scoped` per-model pcts by name, `status` and
`fetchedAt`. The script copies those fields only; the email, organization name and
UUID never reach the ledger. A missing or failing cswap is `accounts: null` and
`errors.cswap`, without cswap's output, and the exit stays 0. `CSWAP_BIN` names the
binary, as in `local-cswap/cswap-auto.conf`; else PATH, then `~/.local/bin`.

    ./agent-meter.py accounts              # per account: pace now, use at each reset

`accounts` prints, per account, the current window's pct against its expected pace,
and per past window the pct of the last reading before its reset. A reading every
four hours can miss up to four hours before a reset, so that number is a lower
bound, printed with how long before the reset it was read. The key is the slot
number: `cswap swap` and `cswap move` change it.

There are no per-account `meter` rows: `sd_db.sample` refuses a provider its
registry does not name, so a name such as `claude#1` needs a pack change first.

## Run

    ./agent-meter.py                       # append one reading, write the rows
    ./agent-meter.py --out /tmp/probe.jsonl --no-db              # the JSONL line only
    PYTHONPATH=$SD_PACK_ROOT/lib python3 tests/test_agent_meter.py   # the collector's tests

A probe store somewhere harmless: `--db` opens an existing database and never
creates one, and the sample reads `providers.yaml` beside it for each provider's
bill, so make both first (`sd_db` is the pack's `lib/`):

    mkdir -p /tmp/probe && cp ~/.local/share/sd/providers.yaml /tmp/probe/
    PYTHONPATH=~/repos/platypeeps/sd-workflow/lib python3 -c \
        "from sd_db.migrate import initialise; initialise('/tmp/probe/sd.db')"
    ./agent-meter.py --out /tmp/probe/meter.jsonl --db /tmp/probe/sd.db

The rows need `sd_db`, which the script loads from the command pack checkout's `lib/`
(`SD_PACK_ROOT`, default `~/repos/platypeeps/sd-workflow`) ahead of any copy
the interpreter has installed (sd:3279). With no `lib/sd_db` there, the script still
appends the JSONL line, with the missing import in `errors.sd_db`.

The tests are `tests/test_agent_meter.py`, a `run_suite` line of `tests/ci-native.sh`.

## Schedule

Installed through the job framework, not a hand-written plist:

    ../local-cron-jobs/cron-jobs.sh install agent-meter
    ../local-cron-jobs/cron-jobs.sh status agent-meter

Copy `../local-cron-jobs/examples/agent-meter.job` into `<config>/cron-jobs/jobs/`
first. It sets `0 */4 * * *` and runs the script under the pack's `.venv/bin/python`;
the script finds `sd_db` in the pack's `lib/`, not in that interpreter. After a change
to the job file, `cron-jobs.sh verify agent-meter` says STALE until
`cron-jobs.sh install agent-meter` rewrites the plist. The framework adds failure notification and `logs/failures.log`.
Keep this job as the one schedule for the collector: a second one appends duplicate
readings to the same ledger.

## Duplicate collectors

Every record carries `prior_gap_s`, the seconds since the reading before it, and a run
that lands under `DUP_GAP_S` (an hour, against a four-hour cadence) says so on stderr so
the job framework surfaces it. A second collector is otherwise invisible: it writes
well-formed readings to the right file, and the series just quietly runs at double rate.
The gap is on the record rather than only in a log so the question is answerable
backwards, from data already collected.

## Failure behavior

A meter that fails is recorded as an `errors` entry on the record and the run still exits
0 — a gap in the series is worse than a noisy one, and a collector must not page anyone at
04:00. The one hard failure is a missing directory for a ledger named by
`AGENT_METER_FILE` or `--out`, which means its location moved; that exits 1 so the
framework reports it.

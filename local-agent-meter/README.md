# local-agent-meter

Appends one agent-usage reading to a JSONL ledger every four hours: `codexbar usage`
per provider (claude, codex) and `rtk gain` for token-proxy savings.

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

## Run

    ./agent-meter.py                       # append one reading, write the rows
    ./agent-meter.py --out /tmp/probe.jsonl --no-db              # the JSONL line only
    sh ../local-sd-db/sd-db.sh test -p test_agent_meter.py       # the collector's tests

A probe store somewhere harmless: `--db` opens an existing database and never
creates one, and the sample reads `providers.yaml` beside it for each provider's
bill, so make both first (the pack's interpreter has `sd_db`):

    mkdir -p /tmp/probe && cp ~/.local/share/sd/providers.yaml /tmp/probe/
    ~/repos/platypeeps/sd-ai-command-pack/.venv/bin/python -c \
        "from sd_db.migrate import initialise; initialise('/tmp/probe/sd.db')"
    ./agent-meter.py --out /tmp/probe/meter.jsonl --db /tmp/probe/sd.db

The rows need `sd_db`, which the pack's virtualenv has and Homebrew's `python3` does
not; the job below runs the pack's interpreter. Under any other python3 the script
still appends the JSONL line, with the missing import in `errors.sd_db`.

The tests live in `local-sd-db/tests/test_agent_meter.py`, in the library's suite,
because the CI workflow fails every leg on a folder that grows `tests/` without a
`run_suite` line naming it; they move here when that line is added.

## Schedule

Installed through the job framework, not a hand-written plist:

    ../local-cron-jobs/cron-jobs.sh install agent-meter
    ../local-cron-jobs/cron-jobs.sh status agent-meter

Copy `../local-cron-jobs/examples/agent-meter.job` into `<config>/cron-jobs/jobs/`
first. It sets `0 */4 * * *` and runs the script under the pack's `.venv/bin/python`,
the interpreter that has `sd_db` installed (the same one
`local-project-dashboard/dashboard.sh` serves under). After a change to the job file,
`cron-jobs.sh verify agent-meter` says STALE until `cron-jobs.sh install agent-meter`
rewrites the plist. The framework adds failure notification and `logs/failures.log`.
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

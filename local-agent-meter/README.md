# local-agent-meter

Appends one agent-usage reading to a JSONL ledger every four hours: per provider its
usage (claude from `cswap list --json`, codex from `codex app-server`), each cswap
account's seven-day use, and `rtk gain` for token-proxy savings. Two live readings
feed the dashboard's Usage view and Health page; see "Live readings".

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
`errors.sd_db.<provider>.<window>` when one sample was refused. A row is stamped
with its reading's `read_at`; a reading already stored under that time is counted in
`sd_db.seen` and not written again. Nothing about the rows changes the exit, which
stays 0.

## What the ledger keeps

Since sd:9256 the ledger's `usage` holds one reading per provider (`--providers`,
default `claude,codex`), the shape the live rows have (below). Claude's window is
the active cswap account's seven days. Codex's come live from `codex app-server`
(`account/rateLimits/read`, the account's `codex` bucket), read at the run's time;
`CODEX_BIN` names the binary, else `PATH` and Homebrew. With no binary or no answer
in 20 s, they are the `rate_limits` of the newest `token_count` event in
`$CODEX_HOME/sessions` (default `~/.codex`), with that event's time as `read_at`, and
the live failure in `live_error` (sd:9655). Only the limits are copied: the event's
session text is not. The `meter` rows come from these windows. A window whose
reset is past is dropped from the reading and counted in its `stale`, on every
path. Lines from before sd:9256 hold `codexbar` instead; `accounts` reads neither.

cswap gives no five-hour number, so no `claude` 300-minute row is written after
sd:9256. A `meter` row has no `removed` status: the last codexbar-era row stays in
the table, and the Usage screen's month read stops showing it when the month turns.

From `rtk gain` the ledger keeps `summary.total_saved` and `summary.avg_savings_pct`
(sd:7344), each only when it is a finite number.

## Claude accounts

Since sd:7322 every reading also carries `accounts`, one entry per Claude account
that `cswap list --json` manages: its slot `number`, `active`, the seven-day `pct`,
`resetsAt` and `expectedPct`, the `scoped` per-model pcts by name, `status`,
`fetchedAt`, and `alias` when `cswap alias` gave the slot a short lower-case
name. The script copies those fields only; the email, organization name and
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
    PYTHONPATH=$SD_PACK_ROOT/lib python3 -m unittest discover -s tests -t .   # the tests

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

The tests are `tests/test_*.py`, the `agent-meter` `run_suite` line of `tests/ci-native.sh`.

## Live readings

    ./agent-meter.py usage                 # one row per 'usage providers' line
    ./agent-meter.py services              # one row per 'status services' line

Each reads a Settings list, a workflow database setting row, and writes one
`heartbeat` state row per entry in one transaction; the newest row per key is the
answer and the retention job keeps one per key. A database that never held the row
gets the starting list written once, so Settings shows what the job reads; a cleared
row reads nothing. A key the list no longer names, or every key of a cleared row, gets
one row with status `removed`, once; a reader skips a removed key. A refused line is
an `SD_REPORT_ATTENTION` line and the rest still run; a store that does not open exits 1. Neither writes the ledger.

- `usage` (sd:9256): setting `dashboard.usage_providers`, one provider per line, `#`
  comments; a line may add `cap=N`, N a positive decimal in the spend's unit: the
  spend's limit when the provider's answer has none (Baseten's budget has no read API); a malformed cap is refused and the row reads without it. Row `usage:<provider>`: `status` (`ok`, `unknown`, `no-source`, `removed`), `source`,
  `read_at`, `windows` (name, used percent, minutes, reset), `balance` and `spend`
  (amount and unit; `resets_at`, the cycle end: the next UTC month start for a
  month, OpenWhispr's period end; `limit`, OpenRouter's key limit when it resets monthly, its BYOK use then in the amount when the key counts it, else the cap; an OpenWhispr period that ended is `unknown`), `accounts` for claude, `error` when unknown. Keys come from the
  environment by name (`usage_sources.py` names each); no key, answer body or
  account field reaches a row. A failed read is `unknown`, never 0; a provider with
  no usage API is `no-source`.
  `github` (the month's bill after discounts) and `copilot` read `GH_TOKEN`, which
  needs the `user` scope. `copilot` holds the month's AI credits, in `credits`, and
  their cost; a plan still on premium requests reads `requests` instead. Add
  their lines to a list written before sd:9592. `anthropic-api` (the month's API
  cost from the Admin API cost report) reads `ANTHROPIC_ADMIN_KEY` (sd:9594).
  `huggingface` has no quota API: pass a ZeroGPU quota error,
  `agent-meter.py usage --huggingface-error "<text>"`, and its row holds the
  seconds left (`balance`, unit `seconds`) and `read_at`, `source` `error`.
  A live run leaves that row standing until the next one; a text with no
  `(<n>s requested vs. <n>s left)` writes nothing and exits 1 (sd:9586).
- `services` (sd:9259): setting `dashboard.status_services`, one
  `<name> <kind> <url> [provider[,provider]]` line each; kinds `statuspage`
  (summary.json), `slack` (slack-status.com v2.0.0), `google-incidents`
  (incidents.json) and `http` (the page answers). Row `service-status:<name>`:
  `status` (`ok`, `degraded`, `outage`, `unknown`, `removed`), `indicator`, open `incidents`
  (title, url, impact, start), non-operational `components`, `providers`, `read_at`
  and `error`. A failed read is `unknown`, never `ok`.
  A `statuspage` line may end in `groups=<g>[,<g>]` (each group's row
  and its components, name lower-cased with `-` for spaces, plus
  ungrouped ones) and
  `stale=<days>` (1 to 365; drop an open none, minor or maintenance
  incident with no update for longer; a major or critical one, or one
  with no `updated_at`, stays; alone it keeps the page's `status`);
  with `groups=`,
  `status` is the worst kept component or incident (sd:9722). An incident
  is dropped only when every component it names is listed outside the
  groups. Cloudflare keeps its core products in a group of their own:
  name `cloudflare-sites-and-services` with the region. A page whose
  indicator is worse than every component and incident it lists reads
  `unknown`: nothing proves the cause is outside the filter. A kept
  outage still reads `outage` on a critical page. Maintenance explains
  only a maintenance indicator, never a minor one.
  A list still equal to the earlier default moves to the new one on the
  next run. An edited list stays as it is: add
  `groups=north-america,cloudflare-sites-and-services stale=3` to its
  `cloudflare` line in Settings.

Run them on the hub only: the rows go to the hub's database.

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

The live readings have their own jobs, `agent-usage.job` (every 15 minutes) and
`service-status.job` (every 5 minutes); copy and install them the same way. They
write no ledger line.

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

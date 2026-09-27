# mezmo-pipeline

The Mezmo pipeline control plane: the operational-state variable and the
webhook source config, against `api.mezmo.com`'s v3 API.

Replaces `mezmo-pipeline-state/` and `mezmo-webhook-source/`. Those two shared
a service key, a pipeline id and a byte-identical placeholder guard across two
folders and two `.env` files, while disagreeing about which host to talk to.

## Usage

```sh
mkdir -p ~/.config/system/mezmo-pipeline
cp .env.example ~/.config/system/mezmo-pipeline/.env   # once, fill in the service key + the ids you need
./pipeline.sh state get
./pipeline.sh state normal|incident
./pipeline.sh state test-mode-on|test-mode-off
./pipeline.sh source webhook
./pipeline.sh status
./pipeline.sh test
```

`MEZMO_PIPELINE_SERVICE_KEY` and `MEZMO_PIPELINE_ID` are needed by everything;
`state` also needs `MEZMO_PIPELINE_STATE_ID`, `source` also needs
`MEZMO_SOURCE_ID` and `MEZMO_ACCOUNT_ID`. Only the subcommand's own variables
are required, so `state get` does not demand the source ids. All may come from
`~/.config/system/mezmo-pipeline/.env` (`$SYSTEM_TOOLS_CONFIG/mezmo-pipeline/` when that is set) or straight from the
environment — the file is optional when they are already exported. When both
are present, the file wins.

`state` and `source webhook` need `jq`.

## What changed when the two folders merged

- **Every response's HTTP status is checked.** The old scripts used `curl -i`
  and `curl -s` with no status check at all: a 401 or a 404 printed and exited
  0, `set -e` never fired, and a state flip that never happened looked exactly
  like one that did.
- **`source webhook` reads before it writes.** The old `webhook-source.sh`
  built a whole source object from scratch and PUT it with `"access_keys": []`
  and `"signing_keys": []` hardcoded — against an API its own README described
  as replace-not-patch. Any key configured on that source was destroyed every
  time the script ran. It now GETs the source, changes only
  `capture_metadata` and `auto_parse`, and PUTs back what it read; a failed
  read aborts instead of writing blind.
- **One host.** `api.logdna.com` is the legacy name for the same `/v3/pipeline`
  surface; everything now uses `api.mezmo.com`, overridable via
  `MEZMO_API_BASE`.
- **Every write is read back.** Both write paths re-GET afterwards and print
  what the API now holds, because these commands mutate a live account.

## Gotchas

- **`state get` sends `pipeline_id` in a body on a GET.** That is unusual — a
  server is entitled to ignore a GET body — but it is what this deployment
  answers today, so the merge preserved it verbatim rather than "fixing" it
  blind against a live work account. If `get` ever starts returning
  everything or nothing, try `?pipeline_id=` as a query parameter instead;
  that is the shape the API most likely wants.
- **The read-modify-write shape of `source webhook` follows REST convention,
  not a verified transcript.** It was not smoke-tested against a live account
  — there is no Mezmo pipeline on this machine to test against. The failure
  mode is safe by construction (a failed or unparseable read aborts before any
  write), but the first real run deserves watching. `pipeline.sh` prints the
  read-back so that run tells you what happened.
- `test-mode-*` also sets `operational_state: incident`, matching the original
  scripts.
- A value left at its `.env.example` placeholder is rejected the same as a
  missing one, naming the variable. `machine-setup.sh doctor` does not report
  this folder — it is in `PLACEHOLDER_EXPECTED`, because the ids name
  pipelines that exist only on the work account and the warning could never be
  actioned here. The guard covers it at the point of use instead.
- `status` exits **3** when the folder is not configured and **1** when it is
  configured and failing. `local-health-check` reads those codes so a machine
  with no Mezmo credentials does not raise a nightly finding it cannot act on.

## Tests

`./pipeline.sh test` runs an offline suite — 58 assertions, no network. `curl`
is shadowed on `PATH` by a recording stub (`tests/curl-stub`), so the tests
assert on the exact method, URL and body the tool would have sent. Both bugs
above have a regression test that fails if the fix is backed out.

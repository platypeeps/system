# local-fluentbit

Fluent Bit demo pipeline in docker: a `random` input, Lua processors, and two
HTTP outputs that ship the records as `json_lines` to two configured
destinations — **`start` sends data off-box**.

## Usage

```sh
mkdir -p "${SYSTEM_TOOLS_CONFIG:-$HOME/.config/system-tools}/fluentbit"
cp .env.example "${SYSTEM_TOOLS_CONFIG:-$HOME/.config/system-tools}/fluentbit/.env"  # once, then fill in
./fluentbit.sh start|stop|status|update
```

`status` reports whether the shipper is up without sending anything.
`status --probe` also POSTs one marker record at the first destination URL and reports the
HTTP response — that ships data off-box, which is why it is opt-in. Exit codes
are 0 healthy, 3 nothing to check (not configured, or stopped), 1 up but
failing or started and since gone; `local-health-check` reads them.

`start` writes `state/started` (gitignored) and `stop` removes it. The
container runs with `--rm`, so a crash removes it and leaves nothing to find;
the marker is how `status` tells a crashed shipper (exit 1) from one stopped on
purpose (exit 3). After a reboot or a Docker restart, a shipper that was
running reads as broken until `start` or `stop` runs again.

## Secrets and destinations

The two destination URLs (`FLUENTBIT_EXPORT_URL_1`, `FLUENTBIT_EXPORT_URL_2`)
and their `Authorization` header values (`FLUENTBIT_EXPORT_AUTH_1`,
`FLUENTBIT_EXPORT_AUTH_2`) live in `$SYSTEM_TOOLS_CONFIG/fluentbit/.env`
(see `.env.example`), or come from the environment. `start` splits each URL
into the output plugin's host, port, URI and TLS fields and renders the
gitignored `config.yaml` from the tracked `config.yaml.template`.

Any backend with an HTTP ingest endpoint for JSON lines works: its ingest URL
is the destination and its ingestion key is the `Authorization` value.

## Gotchas

- A value left at its `.env.example` placeholder is rejected the same as a
  missing one, so a half-filled `.env` fails naming the variable instead of
  shipping records at a placeholder URL.
- No destination value lives in a tracked file. Keep it that way: the
  template holds only `${...}` placeholders.

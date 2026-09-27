# local-opentelemetry-collector

OpenTelemetry Collector (contrib image) with `./config.yaml`. OTLP grpc on
`127.0.0.1:4317`, OTLP http on `:4318`, zpages on `:55679`.

Forwards everything it receives to an OTLP/HTTP backend of your choice.

## Usage

```sh
mkdir -p ~/.config/system/opentelemetry-collector
cp .env.example ~/.config/system/opentelemetry-collector/.env   # first run: fill in the export URL and auth header
./opentelemetry-collector.sh start|stop|status|update
```

`status` exits 0 when the container is up and zpages answers
`/debug/servicez` with HTTP 2xx, 3 when there is nothing to check (not
configured, or stopped), and 1 when it is up and failing or was started and
is gone; `local-health-check` reads those codes. `start` writes
`state/started` (gitignored) and `stop` removes it: the container runs with
`--rm`, so a crash leaves nothing to find, and the marker is how `status`
tells a crash from a deliberate stop. After a reboot or a Docker restart, a
collector that was running reads as broken until `start` or `stop` runs again.

`start` refuses to run without `OTLP_EXPORT_URL` (the backend's OTLP/HTTP
base URL) and `OTLP_EXPORT_AUTH` (the `Authorization` header value), from
`~/.config/system/opentelemetry-collector/.env` (`$SYSTEM_TOOLS_CONFIG/opentelemetry-collector/` when that is set) or already exported.

## Gotchas

- Keeps the standard OTLP `4317`/`4318`: receiving OTLP is this service's
  whole purpose. `local-jaeger` moved to `4327`/`4328`, so the usual
  app -> collector -> jaeger pipeline can run end to end.
- Use `local-opentelemetry-traces` to send test traces at it.
- A pasted URL containing `undefined` usually means a web UI's copy failed;
  the collector would start and then die on the DNS lookup. `start` rejects
  that string rather than letting it fail at runtime.
- The OTLP receiver sets `endpoint: 0.0.0.0:...` explicitly. The collector's
  own default is localhost, which inside a container is the container's
  loopback — the published ports reach nothing and `curl` to `:4318` returns
  no response at all. Host-side exposure stays loopback-only because the run
  script publishes on `127.0.0.1`.
- The export credential lives in the config `.env`, never in `config.yaml`.

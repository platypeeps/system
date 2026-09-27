# local-jaeger

Jaeger all-in-one in docker (UI `:16686`, OTLP `:4327`/`:4328`, Zipkin `:9411`)
plus the HotROD demo app (`:8080`, sends traces to jaeger via OTLP).

## Usage

```sh
./jaeger.sh start|stop|update           # jaeger itself
./jaeger.sh start|stop|update hotrod    # HotROD demo (needs jaeger running)
```

## Gotchas

- The UI (`16686`) is bound to `127.0.0.1`. The ingest ports
  (`4327`/`4328`, `9411`, `14250`, `14268`, `14269`) stay on all interfaces
  on purpose: accepting spans from another machine is what they are for,
  and they carry no credential.

- OTLP is on `4327`/`4328`, not the standard `4317`/`4318`:
  `local-opentelemetry-collector` keeps those, since receiving OTLP is its
  entire job, and the usual app -> collector -> jaeger pipeline needs both
  running. Point a direct exporter at `4327`/`4328`, or override with
  `JAEGER_OTLP_GRPC_PORT` / `JAEGER_OTLP_HTTP_PORT`.
- HotROD uses `--link jaeger`, so start jaeger first.
- Image pinned to `1.76.0`.

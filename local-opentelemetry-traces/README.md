# local-opentelemetry-traces

Generate test traces with `telemetrygen` (from `$GOBIN`) against a local OTLP
endpoint — pairs with `local-opentelemetry-collector` or `local-jaeger`.

## Usage

```sh
./opentelemetry-traces.sh
```

Sends 3 traces, insecure OTLP, output filtered to start/traces/stop lines.

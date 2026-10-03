# local-opentelemetry-demo

The OpenTelemetry demo on a local kind cluster (`otel-demo` / context
`kind-otel-demo`), with the collector exporting to an OTLP/HTTP backend and
optional MCP port-forwards. Everything runs through one entrypoint.

## Usage

```sh
./opentelemetry-demo.sh help    # full subcommand list
```

Chart overrides: `collector-values.yaml` (memory sizing, the events receiver,
and the OTLP/HTTP export pipeline). It holds no destination or credential.

## Configuration and secrets

Values live in `$SYSTEM_TOOLS_CONFIG/opentelemetry-demo/.env` (copy
`.env.example`) or come from the environment. The file is optional when they
are exported, and wins when both are present. A missing value fails naming the
variable and both remedies.

| Variable | Needed by | Meaning |
| --- | --- | --- |
| `OTLP_EXPORT_URL` | `start`, `upgrade` | OTLP/HTTP base URL of the export backend |
| `OTLP_EXPORT_AUTH` | `start`, `upgrade` | auth header value |
| `OTLP_EXPORT_AUTH_HEADER` | optional | auth header name, default `Authorization` |
| `AURA_CHART_DIR`, `AURA_VALUES_FILE` | `mcp-install` | local aura chart and values file |
| `OTEL_DEMO_REPO` | `repo-start`, `repo-stop` | upstream opentelemetry-demo checkout |

The script turns the export values into the `otlp-export` k8s secret (keys
`url`, `auth`, `auth-header`) on every install/upgrade; the collector reads it
through `extraEnvs`. The collector does not expand `${env:...}` in map keys, so
the script also passes the header name to helm with `--set-string`.

Any OTLP/HTTP backend works: its OTel ingest URL is `OTLP_EXPORT_URL` and its
ingestion key is `OTLP_EXPORT_AUTH`. A backend that reads the key from an
`apikey` header needs `OTLP_EXPORT_AUTH_HEADER=apikey`.

## Gotchas

- All subcommands pin the kind context — nothing acts on the ambient kubectl context.
- The aura chart additionally needs `OPENAI_API_KEY` exported.
- `OLD/` holds pre-consolidation scripts and is gitignored.

# local-genai-traces

Always-on trace collection for Gen-AI experiments on this machine. An
OpenTelemetry Collector receives OTLP and sends every span to two places:

- **Phoenix** (UI on `127.0.0.1:6016`), which files spans by
  `openinference.project.name`;
- **a raw OTLP JSON file**, `storage/raw/traces.jsonl`, for analysis
  outside Phoenix. It rotates at 100 MB and keeps 7 old files.

```
experiment --OTLP grpc :4337 / http :4338--> collector --> Phoenix :6016
                                                    \--> storage/raw/traces.jsonl
```

## Usage

```sh
./genai-traces.sh start      # once; the containers then come back with Docker
./genai-traces.sh status
./genai-traces.sh endpoint   # the variables an experiment exports (grpc|http|docker)
./genai-traces.sh tail       # follow the raw file
```

`status` exits 0 when both containers run, the collector health check
answers and Phoenix `/healthz` answers with HTTP 2xx. It exits 3 when the
service was never started here, and 1 when it was started and is now gone,
partly running or failing. `local-health-check` reads those codes.

## What exports here

- `local-aura`: `aura.sh server`, `server-repo` and `experiment` set
  `OTEL_EXPORTER_OTLP_ENDPOINT` to this service unless one is already exported.
- `local-jev`: with `JEV_TRACES_URL=http://127.0.0.1:4338/v1/traces` in
  `<config>/jev/.env`, each call sends one metadata-only span.
- Anything else: export the lines `endpoint` prints: the standard
  `OTEL_EXPORTER_OTLP_ENDPOINT` and `OTEL_EXPORTER_OTLP_PROTOCOL` pair.
  `endpoint http` gives the OTLP/HTTP port, `endpoint docker` the address a
  container uses. Set
  `openinference.project.name` as a resource attribute to get a Phoenix
  project of its own; without it, spans land in `default`.

## Gotchas

- **Always on means Docker is on.** The containers use
  `restart: unless-stopped` instead of `--rm`, so Docker brings them back
  after a restart. Docker Desktop must start at login for that to happen.
  `status` reports 1 while the marker says started and the containers are missing.
- **Ports.** 4317/4318 belong to `local-opentelemetry-collector`, 4327/4328
  to `local-jaeger` and 6006 to `local-phoenix`. This service takes
  4337/4338, 6016 and 13137 (collector health), so all can run at once.
- **Containers reach it through the host.** A container exports to
  `http://host.docker.internal:4337`; `127.0.0.1` inside a container is the
  container itself.
- **Content may be recorded.** `aura.sh experiment` sets
  `OTEL_RECORD_CONTENT=true`, so its prompts and answers sit in Phoenix and
  the raw file. `aura.sh server` does not, unless that variable is exported.
  Both stores live under `storage/`, which git ignores. Do not point
  production traffic here.
- `update` pulls the latest images and recreates a started service on them.
  It deletes no image: `local-opentelemetry-collector` runs the same
  collector image. Phoenix data in `storage/phoenix` survives.
- **Retention.** Phoenix deletes traces older than
  `GENAI_TRACES_RETENTION_DAYS` (default 30; 0 keeps them forever), checked
  weekly. Phoenix reads the value only when it creates its default policy,
  on a new `storage/phoenix`; on an existing store, change the policy in
  the Phoenix UI under data retention. The raw file rotates on its own.

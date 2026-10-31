# local-genai-traces

Always-on trace collection for Gen-AI experiments on this machine. An
OpenTelemetry Collector receives OTLP and sends every span to two places:

- **Phoenix** (UI on `127.0.0.1:6016`), which files spans by
  `openinference.project.name`;
- **a raw OTLP JSON file**, `storage/raw/traces.jsonl`, for analysis
  outside Phoenix. It rotates at 100 MB and keeps every old file until
  `archive` moves it to the permanent record (below).

Metrics and log events, such as Claude Code's native OTel export, go to
`storage/raw/metrics.jsonl` and `storage/raw/logs.jsonl` with the same
rotation and archive rules. Phoenix takes traces only.

```
experiment --OTLP grpc :4337 / http :4338--> collector --> Phoenix :6016 (traces)
                                                    \--> storage/raw/{traces,metrics,logs}.jsonl
                                                    \--> second OTLP destination (optional)
```

## Usage

```sh
./genai-traces.sh start      # once; the containers then come back with Docker
./genai-traces.sh status
./genai-traces.sh endpoint   # the variables an experiment exports (grpc|http|docker|tailnet)
./genai-traces.sh tail       # follow the raw file
./genai-traces.sh archive    # move rotated raw files to the permanent archive
```

`status` exits 0 when both containers run, the collector health check
answers and Phoenix `/healthz` answers with HTTP 2xx. It exits 3 when the
service was never started here, and 1 when it was started and is now gone,
partly running or failing. `local-health-check` reads those codes.

## The permanent record

Phoenix keeps 30 days. The raw traces are kept for good in `GENAI_TRACES_ARCHIVE_DIR`, set in
`<config>/genai-traces/.env`; a NAS share suits it. Create the folder once:
`archive` never creates it.

`./genai-traces.sh archive` moves each rotated file,
`storage/raw/<signal>-<UTC time>-<reason>.jsonl` for traces, metrics and
logs, to `<archive>/<signal>-<UTC time>-<reason>.jsonl.gz`, so names sort
by time. It never moves a live `<signal>.jsonl`. It checks each gzip against its source
before removing the source, deletes nothing in the archive, and refuses to
write over a file with the same name. A rerun moves nothing twice; a run
stopped midway finishes on the next. Exit 3 means nothing to do: the
variable is unset, or the folder is missing (an unmounted share). Exit 1
means a file could not be archived; it stays in `storage/raw`.

`archive` owns deletion. `collector.yaml` sets `max_backups: 0`, so the
collector keeps every rotated file, and `archive` removes one only after
its checked copy is in place. A missed run, a burst or an offline share
loses nothing; the files wait on the system disk instead. Twenty waiting
(about 2 GB) is a fault: `archive` exits 1 when it cannot move them
(unset, or the folder missing), and `status` exits 1 so the nightly
health check sees a job that is not running. A machine with no archive
set grows too; `status` warns it the same way.

Run it hourly with the cron job `local-cron-jobs/examples/genai-traces-archive.job`,
copied to `<config>/cron-jobs/jobs/`. The job treats exit 3 as success, so
an unmounted share does not page every hour.

To load archived traces back into Phoenix, unpack them and replay them with
a one-off collector on this service's network. Spans already in Phoenix
are sent again:

```sh
mkdir -p restore
gunzip -c "<archive>/traces-<UTC time>-<reason>.jsonl.gz" > restore/traces.jsonl
docker run --rm --network local-genai-traces_default \
  -v "$PWD/restore:/in:ro" otel/opentelemetry-collector-contrib:latest \
  --config='yaml:receivers::otlpjsonfile::include: [/in/*.jsonl]' \
  --config='yaml:receivers::otlpjsonfile::start_at: beginning' \
  --config='yaml:exporters::otlp::endpoint: phoenix:4317' \
  --config='yaml:exporters::otlp::tls::insecure: true' \
  --config='yaml:service::pipelines::traces::receivers: [otlpjsonfile]' \
  --config='yaml:service::pipelines::traces::exporters: [otlp]'
```

The replay collector keeps watching the folder. Stop it with Ctrl-C once
the spans show in Phoenix.

## A second destination

Off by default: nothing leaves the machine. To also send every span to
another OTLP/HTTP endpoint, set two values in `<config>/genai-traces/.env`
(copy `.env.example`), then run `./genai-traces.sh start`:

- `TRACES_FORWARD_OTLP_ENDPOINT`: the `https://` base URL; the collector
  adds `/v1/traces`.
- `TRACES_FORWARD_OTLP_HEADERS`: `name=value` pairs, comma-separated, such
  as the destination's key.

`start` writes `state/forward.yaml`, a collector overlay readable by the
owner only, and adds `docker-compose.forward.yml`, which mounts it. The
header values stay in that file: not in the container's environment, its
command or the output. The new exporter has its own queue and retries, so
a slow or failing destination drops only its own copy; Phoenix and the raw
file go on. Clear the endpoint and run `start` to turn it off.

## What exports here

- `mezmo-aura`: `aura.sh server`, `server-repo` and `experiment` set
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
- **Ports.** 4317/4318 belong to `local-opentelemetry-collector`. This service
  takes 4337/4338, 6016 and 13137 (collector health), so both can run at once.
- **Satellites reach the hub's collector over the tailnet.** On the hub,
  `machine-setup.sh update sd --apply` adds a private `tailscale serve`
  route, HTTPS 4339 to OTLP/HTTP 4338; the collector stays on loopback.
  `endpoint tailnet` prints `https://<hub>:4339`, the hub named by the
  satellite's `~/.config/sd/hub.json`, and machine-setup points the
  satellite's agents there. A hub that is down drops the export.
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

#!/bin/sh
# Always-on Gen-AI trace collection: an OTel Collector that feeds Phoenix and
# a raw OTLP JSON file. Experiments on this machine export here.
# OTLP on 127.0.0.1:4337 (grpc) / :4338 (http), Phoenix UI on :6016.
# Optional values, such as a second OTLP destination, come from
# $SYSTEM_TOOLS_CONFIG/genai-traces/.env — see .env.example.
# Usage: genai-traces.sh start|stop|status|update|archive|endpoint|tail|test
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
. "$DIR/../lib/config.sh"
. "$DIR/../lib/bounded.sh"
st_source_env genai-traces

# 4317/4318 belong to local-opentelemetry-collector. This service takes the
# next free set so both can run at once.
GENAI_TRACES_GRPC_PORT="${GENAI_TRACES_GRPC_PORT:-4337}"
GENAI_TRACES_HTTP_PORT="${GENAI_TRACES_HTTP_PORT:-4338}"
GENAI_TRACES_UI_PORT="${GENAI_TRACES_UI_PORT:-6016}"
GENAI_TRACES_HEALTH_PORT="${GENAI_TRACES_HEALTH_PORT:-13137}"
# Phoenix deletes traces older than this many days (0 keeps them forever).
# It reads the value only when it creates its default policy, on a new store.
GENAI_TRACES_RETENTION_DAYS="${GENAI_TRACES_RETENTION_DAYS:-30}"
export GENAI_TRACES_GRPC_PORT GENAI_TRACES_HTTP_PORT GENAI_TRACES_UI_PORT GENAI_TRACES_HEALTH_PORT
export GENAI_TRACES_RETENTION_DAYS

COLLECTOR=local-genai-collector
PHOENIX=local-genai-phoenix

# `start` leaves this marker and `stop` removes it. The containers restart
# with Docker (`restart: unless-stopped`), so a missing container while the
# marker exists is a failure, not a service nobody started.
STARTED="$DIR/state/started"

# The collector overlay that adds the second OTLP destination. start writes
# it from TRACES_FORWARD_OTLP_ENDPOINT and _HEADERS; it holds the header
# values, so it is the owner's alone and nothing prints it.
FORWARD="$DIR/state/forward.yaml"

compose() {
  docker compose -f "$DIR/docker-compose.yml" "$@"
}

# compose_forward: compose with the override that mounts the overlay.
compose_forward() {
  docker compose -f "$DIR/docker-compose.yml" -f "$DIR/docker-compose.forward.yml" "$@"
}

# Same patterns fluentbit.sh refuses: a copied example forwards nothing.
is_placeholder() {
  case "$1" in
    *change-me*|*changeme*|*CHANGE_ME*|your-*|xxx*|XXX*|*placeholder*) return 0 ;;
  esac
  return 1
}

# plain_value <value>: 0 when the value fits a double-quoted YAML string the
# collector leaves alone: printable, no quote, backslash or `$` (the
# collector expands ${...}). The caller names the variable, never the value.
plain_value() {
  case "$1" in
    ''|*[![:print:]]*|*'"'*|*'\'*|*'$'*) return 1 ;;
  esac
  is_placeholder "$1" && return 1
  return 0
}

refuse() {
  echo "local-genai-traces: $1; fix it in $(st_config_file genai-traces .env) or the environment" >&2
  exit 1
}

# render_forward: check the values and write the overlay through a private
# temporary file, so a stopped run leaves the old overlay or the new one.
render_forward() {
  plain_value "$TRACES_FORWARD_OTLP_ENDPOINT" ||
    refuse "TRACES_FORWARD_OTLP_ENDPOINT is a placeholder or holds a quote, backslash, \$ or control character"
  # https://host[:port][/path] only: the collector does not start with an
  # endpoint its exporter cannot parse, and then Phoenix gets nothing too.
  url_shape="TRACES_FORWARD_OTLP_ENDPOINT must be https://host[:port][/path], a host name or IPv4 address"
  case "$TRACES_FORWARD_OTLP_ENDPOINT" in
    https://*) ;;
    *) refuse "$url_shape" ;;
  esac
  url_rest="${TRACES_FORWARD_OTLP_ENDPOINT#https://}"
  hostport="${url_rest%%/*}"
  host="${hostport%%:*}"
  case "$host" in
    ''|*[!A-Za-z0-9.-]*|[.-]*) refuse "$url_shape" ;;
  esac
  case "$hostport" in
    *:*)
      port="${hostport#*:}"
      case "$port" in
        ''|*[!0-9]*|0*) refuse "$url_shape" ;;
      esac
      [ "${#port}" -le 5 ] && [ "$port" -le 65535 ] || refuse "$url_shape"
      ;;
  esac
  case "${url_rest#"$hostport"}" in
    *[!A-Za-z0-9._~/%-]*) refuse "$url_shape" ;;
  esac
  headers=""
  rest="${TRACES_FORWARD_OTLP_HEADERS:-}"
  # name=value pairs separated by commas, as OTEL_EXPORTER_OTLP_HEADERS.
  while [ -n "$rest" ]; do
    pair="${rest%%,*}"
    case "$rest" in *,*) rest="${rest#*,}" ;; *) rest="" ;; esac
    name="${pair%%=*}"
    value="${pair#*=}"
    case "$pair" in
      *=*) ;;
      *) refuse "TRACES_FORWARD_OTLP_HEADERS needs name=value pairs separated by commas" ;;
    esac
    case "$name" in
      ''|*[!A-Za-z0-9-]*) refuse "a header name in TRACES_FORWARD_OTLP_HEADERS is not letters, digits and dashes" ;;
    esac
    plain_value "$value" ||
      refuse "a header value in TRACES_FORWARD_OTLP_HEADERS is empty, a placeholder, or holds a quote, backslash, \$ or control character"
    headers="$headers
      \"$name\": \"$value\""
  done
  [ -n "$headers" ] && headers="headers:$headers" || headers="headers: {}"
  mkdir -p "$DIR/state"
  # Docker makes a directory of a bind source that is missing.
  [ -d "$FORWARD" ] && rmdir "$FORWARD"
  rm -f "$FORWARD.tmp"
  (
    umask 077
    cat > "$FORWARD.tmp" <<EOF
# Written by genai-traces.sh start from TRACES_FORWARD_OTLP_*; do not edit.
# The second OTLP destination, next to Phoenix and the raw file.
exporters:
  otlphttp/forward:
    endpoint: "$TRACES_FORWARD_OTLP_ENDPOINT"
    $headers
    # Its own queue and retries: a slow or failing destination fills and
    # drops from this queue alone, and the other exporters go on.
    sending_queue:
      enabled: true
    retry_on_failure:
      enabled: true
service:
  pipelines:
    traces:
      exporters: [otlp/phoenix, file/raw, otlphttp/forward]
EOF
  )
  mv -f "$FORWARD.tmp" "$FORWARD"
}

# same <gz> <file>: 0 when <gz> is a sound gzip of exactly <file>. Bounded:
# the archive sits on a share that can stop answering.
same() {
  st_step 900 sh -c 'gzip -t -- "$1" && gzip -dc -- "$1" | cmp -s - "$2"' sh "$1" "$2"
}

# archive_one <file> <dest.gz>: gzip <file> to a dot-named partial file
# beside <dest.gz>, check it, rename it into place, then remove <file>. A
# stopped run leaves the source, and at most the partial file, which the next
# run writes over; a <dest.gz> that exists is never written or removed.
archive_one() {
  part="$(dirname "$2")/.$(basename "$2").partial"
  # Bounded, as every step on the share: one that stops answering would
  # otherwise hang the run. 1 is "no such file"; anything else is unknown.
  there=0
  st_bounded 30 test -e "$2" || there=$?
  case "$there" in
    0|1) ;;
    *)
      echo "local-genai-traces: FAIL — could not check $2 (exit $there); kept $1"
      return 1
      ;;
  esac
  if [ "$there" -eq 0 ]; then
    # A run stopped after the rename: finish it when the copy matches.
    if same "$2" "$1"; then
      rm -f "$1" || return 1
      return 0
    fi
    echo "local-genai-traces: FAIL — $2 exists and differs; kept $1"
    return 1
  fi
  st_step 900 sh -c 'gzip -c -- "$1" > "$2"' sh "$1" "$part" || {
    echo "local-genai-traces: FAIL — could not write $part"
    return 1
  }
  same "$part" "$1" || {
    echo "local-genai-traces: FAIL — $part does not match $1; kept $1"
    return 1
  }
  st_step 300 mv "$part" "$2" || return 1
  rm -f "$1" || return 1
}

# archive: move each rotated raw file (traces-<UTC time>-<reason>.jsonl,
# never the live traces.jsonl) into GENAI_TRACES_ARCHIVE_DIR as <name>.gz.
# Names are the collector's, so they sort by time. Exits 3 when there is
# nothing to do (unset, or the folder missing: an unmounted share), 1 when
# a file failed or the backlog is a fault (below).
#
# archive is the only deleter of rotated files: collector.yaml keeps every
# one (max_backups 0), so a file not yet archived is never lost, it waits
# on the system disk. ARCHIVE_ALARM files waiting (about 2 GB) is a fault
# archive and status report, whatever stops the archive.
ARCHIVE_ALARM=20

# backlog_fault: print why the backlog is a fault and return 0, else 1.
backlog_fault() {
  count=0
  for src in "$DIR"/storage/raw/traces-*.jsonl; do
    [ -f "$src" ] && count=$((count + 1))
  done
  if [ "$count" -ge "$ARCHIVE_ALARM" ]; then
    echo "$count rotated files (about $((count * 100)) MB) wait on the system disk; the collector keeps each until archive moves it"
    return 0
  fi
  return 1
}

archive() {
  if [ -z "${GENAI_TRACES_ARCHIVE_DIR:-}" ]; then
    if why=$(backlog_fault); then
      echo "local-genai-traces: FAIL — GENAI_TRACES_ARCHIVE_DIR is not set and $why"
      exit 1
    fi
    echo "local-genai-traces: SKIP — GENAI_TRACES_ARCHIVE_DIR is not set; set it in $(st_config_file genai-traces .env)"
    exit 3
  fi
  dest="${GENAI_TRACES_ARCHIVE_DIR%/}"
  # Never created here: under an unmounted share it would land on the
  # system disk. A probe that does not answer is a fault, not a skip.
  probe=0
  st_bounded 30 test -d "$dest" || probe=$?
  case "$probe" in
    0) ;;
    1)
      if why=$(backlog_fault); then
        echo "local-genai-traces: FAIL — archive folder $dest is missing and $why"
        exit 1
      fi
      echo "local-genai-traces: SKIP — archive folder $dest is missing (share not mounted?)"
      exit 3
      ;;
    *)
      echo "local-genai-traces: FAIL — could not check archive folder $dest (exit $probe)"
      exit 1
      ;;
  esac
  moved=0
  failed=0
  for src in "$DIR"/storage/raw/traces-*.jsonl; do
    [ -f "$src" ] || continue
    if archive_one "$src" "$dest/$(basename "$src").gz"; then
      moved=$((moved + 1))
    else
      failed=$((failed + 1))
    fi
  done
  if [ "$failed" -gt 0 ]; then
    echo "local-genai-traces: FAIL — archived $moved, $failed failed and stay in storage/raw"
    exit 1
  fi
  echo "local-genai-traces: OK — archived $moved rotated file(s) to $dest"
}

# up: create or update both containers, with the second destination when
# TRACES_FORWARD_OTLP_ENDPOINT is set and exactly as before when it is not.
# The collector is recreated every time: compose sees no change inside the
# bind-mounted collector.yaml or the overlay, and would keep the collector
# running on the old config.
up() {
  if [ -n "${TRACES_FORWARD_OTLP_ENDPOINT:-}" ]; then
    render_forward
    compose_forward up -d
    compose_forward up -d --force-recreate --no-deps collector
  else
    compose up -d
    compose up -d --force-recreate --no-deps collector
    # Removed only once the collector runs without it: a container still
    # mounting a missing file would get a directory from Docker on restart.
    rm -f "$FORWARD"
  fi
}

running() {
  [ -n "$(docker ps -q -f "name=^$1\$" 2>/dev/null)" ]
}

# probe NAME URL: 0 on a complete 2xx answer, else 1 with the reason on stdout.
probe() {
  rc=0
  code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 "$2" 2>/dev/null) || rc=$?
  case "$rc:$code" in
    0:2??) return 0 ;;
    *:|*:000) echo "$1 at $2 is not answering" ;;
    0:*) echo "$1 at $2 answered HTTP $code" ;;
    *) echo "$1 at $2 answered HTTP $code but did not complete (curl exit $rc)" ;;
  esac
  return 1
}

case "$1" in
  start)
    mkdir -p "$DIR/storage/raw" "$DIR/storage/phoenix" "$DIR/state"
    up
    date '+%Y-%m-%dT%H:%M:%S%z' > "$STARTED"
    ;;
  stop)
    # Keep the record when the containers may still be there: with
    # restart unless-stopped they come back with Docker, and a cleared
    # marker would let status call them "never started".
    if ! compose down; then
      echo "local-genai-traces: stop failed; the containers may still run (is Docker up?)" >&2
      exit 1
    fi
    rm -f "$STARTED"
    ;;
  status)
    # Exit 3 means nothing to check: never started here. 1 means started and
    # now gone or failing. local-health-check reads these codes.
    # First, whatever runs: the collector deletes no rotated file, and one
    # archive does not move (not set, the job not installed, or failing)
    # stays on the system disk after a stop too.
    if why=$(backlog_fault); then
      echo "local-genai-traces: FAIL — archive behind: $why"
      exit 1
    fi
    missing=""
    running "$COLLECTOR" || missing="$missing $COLLECTOR"
    running "$PHOENIX" || missing="$missing $PHOENIX"
    if [ -n "$missing" ]; then
      if [ -f "$STARTED" ]; then
        echo "local-genai-traces: FAIL — started $(cat "$STARTED") and not running:$missing"
        echo "  check Docker is up, then run start again, or stop to clear this"
        exit 1
      fi
      if [ "$missing" = " $COLLECTOR $PHOENIX" ]; then
        echo "local-genai-traces: SKIP — not started on this machine"
        exit 3
      fi
      echo "local-genai-traces: FAIL — partly running; not running:$missing"
      exit 1
    fi
    if ! why=$(probe collector "http://127.0.0.1:$GENAI_TRACES_HEALTH_PORT/"); then
      echo "local-genai-traces: FAIL — $why"
      exit 1
    fi
    if ! why=$(probe phoenix "http://127.0.0.1:$GENAI_TRACES_UI_PORT/healthz"); then
      echo "local-genai-traces: FAIL — $why"
      exit 1
    fi
    echo "local-genai-traces: OK — OTLP on $GENAI_TRACES_GRPC_PORT/$GENAI_TRACES_HTTP_PORT, Phoenix on $GENAI_TRACES_UI_PORT"
    ;;
  update)
    # Pull, not `docker rmi`: local-opentelemetry-collector runs the same
    # collector image, so deleting it by ID fails or breaks that service.
    # A started service is recreated on the new images; the record stays.
    if ! compose pull; then
      echo "local-genai-traces: update failed; pull did not complete (is Docker up?)" >&2
      exit 1
    fi
    if [ -f "$STARTED" ]; then
      up
    fi
    ;;
  archive)
    archive
    ;;
  endpoint)
    # The standard OTel SDK pair an experiment exports to send traces here:
    # an endpoint and the protocol it speaks. grpc by default; `http` for
    # OTLP/HTTP, `docker` for a container reaching the host.
    case "${2:-grpc}" in
      grpc) url="http://127.0.0.1:$GENAI_TRACES_GRPC_PORT"; proto=grpc ;;
      http) url="http://127.0.0.1:$GENAI_TRACES_HTTP_PORT"; proto=http/protobuf ;;
      docker) url="http://host.docker.internal:$GENAI_TRACES_GRPC_PORT"; proto=grpc ;;
      *)
        echo "usage: $(basename "$0") endpoint [grpc|http|docker]" >&2
        exit 1
        ;;
    esac
    echo "OTEL_EXPORTER_OTLP_ENDPOINT=$url"
    echo "OTEL_EXPORTER_OTLP_PROTOCOL=$proto"
    echo "PHOENIX_UI=http://127.0.0.1:$GENAI_TRACES_UI_PORT"
    ;;
  tail)
    exec tail -n "${2:-5}" -F "$DIR/storage/raw/traces.jsonl"
    ;;
  test)
    shift
    exec "${PYTHON:-python3}" -m unittest discover -s "$DIR/tests" -t "$DIR" "$@"
    ;;
  -h|--help|help)
    cat <<'HELPEOF'
usage: genai-traces.sh start|stop|status|update|archive|endpoint|tail|test

  start        run the collector and Phoenix (docker compose, restart
               unless-stopped, so they come back with Docker) and record
               that they were started (state/started); with
               TRACES_FORWARD_OTLP_ENDPOINT set, the collector also sends
               every span to that second OTLP destination
  stop         stop and remove both containers, and clear that record
  status       report whether both containers are up, the collector health
               check answers and Phoenix /healthz answers with HTTP 2xx.
               Exits 0 healthy, 3 when there is nothing to check (never
               started), 1 when started and since gone or failing, or
               when 20 rotated files wait unarchived on the system disk —
               local-health-check reads those codes.
  update       pull the latest images, and recreate the containers on them
               when the service was started; Phoenix data survives
               (start and update always recreate the collector, so it
               reads the current collector.yaml)
  archive      move each rotated raw file (storage/raw/traces-*.jsonl,
               never the live traces.jsonl) into GENAI_TRACES_ARCHIVE_DIR,
               gzipped and checked; the only deleter of rotated files, it
               deletes nothing there. Exits 0 done, 3 nothing to do (unset,
               or the folder missing: share not mounted), 1 when a file
               could not be archived, or when 20 wait with the folder
               unset or missing
  endpoint [grpc|http|docker]
               print OTEL_EXPORTER_OTLP_ENDPOINT and _PROTOCOL for an
               experiment (grpc by default), and the Phoenix URL
  tail [n]     follow the raw OTLP JSON file (storage/raw/traces.jsonl)
  test         run this folder's tests

environment: GENAI_TRACES_GRPC_PORT (4337), GENAI_TRACES_HTTP_PORT (4338),
             GENAI_TRACES_UI_PORT (6016), GENAI_TRACES_HEALTH_PORT (13137),
             GENAI_TRACES_RETENTION_DAYS (30; Phoenix reads it on a new store)

config:      <config>/genai-traces/.env, or the environment; see .env.example
  TRACES_FORWARD_OTLP_ENDPOINT  https:// base URL of a second OTLP/HTTP
                                destination; the collector adds /v1/traces.
                                Unset sends nothing anywhere but Phoenix.
  GENAI_TRACES_ARCHIVE_DIR      the permanent home of the raw traces, for
                                archive; it must exist, archive never makes it
  TRACES_FORWARD_OTLP_HEADERS   name=value pairs, comma-separated, sent with
                                each export (e.g. the destination's key)
                                start, or update, applies a change
HELPEOF
    exit 0
    ;;
  *)
    echo "usage: $(basename "$0") start|stop|status|update|archive|endpoint|tail|test" >&2
    exit 1
    ;;
esac

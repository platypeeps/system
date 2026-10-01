#!/bin/sh
# Always-on Gen-AI trace collection: an OTel Collector that feeds Phoenix and
# a raw OTLP JSON file. Experiments on this machine export here.
# OTLP on 127.0.0.1:4337 (grpc) / :4338 (http), Phoenix UI on :6016.
# Usage: genai-traces.sh start|stop|status|update|endpoint|tail|test
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"

# 4317/4318 belong to local-opentelemetry-collector and 4327/4328 to
# local-jaeger; 6006 is local-phoenix's. This service takes the next free set
# so all of them can run at once.
GENAI_TRACES_GRPC_PORT="${GENAI_TRACES_GRPC_PORT:-4337}"
GENAI_TRACES_HTTP_PORT="${GENAI_TRACES_HTTP_PORT:-4338}"
GENAI_TRACES_UI_PORT="${GENAI_TRACES_UI_PORT:-6016}"
GENAI_TRACES_HEALTH_PORT="${GENAI_TRACES_HEALTH_PORT:-13137}"
export GENAI_TRACES_GRPC_PORT GENAI_TRACES_HTTP_PORT GENAI_TRACES_UI_PORT GENAI_TRACES_HEALTH_PORT

COLLECTOR=local-genai-collector
PHOENIX=local-genai-phoenix

# `start` leaves this marker and `stop` removes it. The containers restart
# with Docker (`restart: unless-stopped`), so a missing container while the
# marker exists is a failure, not a service nobody started.
STARTED="$DIR/state/started"

compose() {
  docker compose -f "$DIR/docker-compose.yml" "$@"
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
    compose up -d
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
    compose down 2>/dev/null || true
    rm -f "$STARTED"
    docker images -a | grep -E "otel/opentelemetry-collector-contrib|arizephoenix/phoenix" \
      | awk '{print $3}' | xargs docker rmi
    ;;
  endpoint)
    # The lines an experiment exports to send its traces here.
    echo "OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:$GENAI_TRACES_GRPC_PORT"
    echo "OTEL_EXPORTER_OTLP_HTTP_ENDPOINT=http://127.0.0.1:$GENAI_TRACES_HTTP_PORT"
    echo "OTEL_EXPORTER_OTLP_DOCKER_ENDPOINT=http://host.docker.internal:$GENAI_TRACES_GRPC_PORT"
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
usage: genai-traces.sh start|stop|status|update|endpoint|tail|test

  start        run the collector and Phoenix (docker compose, restart
               unless-stopped, so they come back with Docker) and record
               that they were started (state/started)
  stop         stop and remove both containers, and clear that record
  status       report whether both containers are up, the collector health
               check answers and Phoenix /healthz answers with HTTP 2xx.
               Exits 0 healthy, 3 when there is nothing to check (never
               started), 1 when started and since gone or failing —
               local-health-check reads those codes.
  update       stop both containers and delete their local images by image ID
               (next start pulls the latest)
  endpoint     print the OTLP endpoints and the Phoenix URL for experiments
  tail [n]     follow the raw OTLP JSON file (storage/raw/traces.jsonl)
  test         run this folder's tests

environment: GENAI_TRACES_GRPC_PORT (4337), GENAI_TRACES_HTTP_PORT (4338),
             GENAI_TRACES_UI_PORT (6016), GENAI_TRACES_HEALTH_PORT (13137)
HELPEOF
    exit 0
    ;;
  *)
    echo "usage: $(basename "$0") start|stop|status|update|endpoint|tail|test" >&2
    exit 1
    ;;
esac

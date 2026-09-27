#!/bin/sh
# OpenTelemetry Collector (contrib) with ./config.yaml.
# OTLP on 127.0.0.1:4317 (grpc) / :4318 (http), zpages on :55679.
# Usage: opentelemetry-collector.sh start|stop|status|update|test
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"

# 4317/4318 are the standard OTLP pair and stay here: receiving OTLP is this
# service's whole purpose. local-jaeger moved to 4327/4328 so both can run.
GRPC_PORT="${OTELCOL_GRPC_PORT:-4317}"
HTTP_PORT="${OTELCOL_HTTP_PORT:-4318}"
ZPAGES_PORT="${OTELCOL_ZPAGES_PORT:-55679}"

# `start` leaves this marker and `stop` removes it. The container runs with
# --rm, so one that crashed leaves nothing for `docker ps -a` to find; without
# the marker a collector that died reads the same as one nobody started, and
# `status` answers "nothing to check" for a broken service (sd:1387).
STARTED="$DIR/state/started"

# config.yaml reads the OTLP/HTTP export destination out of the environment so
# no live credential sits in a tracked file; the values come from
# <config>/opentelemetry-collector/.env (lib/config.sh). A missing file is
# fine when the values are already exported.
# shellcheck source=../lib/config.sh
. "$DIR/../lib/config.sh"
st_source_env opentelemetry-collector

require_env() {
  missing=""
  for v in OTLP_EXPORT_URL OTLP_EXPORT_AUTH; do
    eval "val=\${$v:-}"
    [ -n "$val" ] || missing="$missing $v"
  done
  [ -z "$missing" ] || {
    for v in $missing; do st_missing "$v" opentelemetry-collector .env; done
    exit 1
  }
  case "$OTLP_EXPORT_URL" in
    *undefined*|*change-me*)
      echo "OTLP_EXPORT_URL is $OTLP_EXPORT_URL — that is a placeholder, not a host." >&2
      echo "A URL containing \"undefined\" usually means a failed copy out of a web UI; re-copy it." >&2
      exit 1
      ;;
  esac
}

case "$1" in
  start)
    require_env
    docker run -d --rm --name local-opentelemetry-collector \
      -v "$DIR/config.yaml:/etc/otelcol-contrib/config.yaml" \
      -e "OTLP_EXPORT_URL=$OTLP_EXPORT_URL" \
      -e "OTLP_EXPORT_AUTH=$OTLP_EXPORT_AUTH" \
      -p 127.0.0.1:"$GRPC_PORT":4317 \
      -p 127.0.0.1:"$HTTP_PORT":4318 \
      -p 127.0.0.1:"$ZPAGES_PORT":55679 \
      otel/opentelemetry-collector-contrib:latest
    mkdir -p "$DIR/state"
    date '+%Y-%m-%dT%H:%M:%S%z' > "$STARTED"
    ;;
  stop)
    docker stop local-opentelemetry-collector 2>/dev/null || true
    rm -f "$STARTED"
    ;;
  status)
    # Exit 3 means "nothing to check" — not configured, or simply not running.
    # local-health-check reads these codes, so 1 stays reserved for a collector
    # that is up and actually broken.
    missing=""
    for v in OTLP_EXPORT_URL OTLP_EXPORT_AUTH; do
      eval "val=\${$v:-}"
      [ -n "$val" ] || missing="$missing $v"
    done
    case "${OTLP_EXPORT_URL:-}" in *undefined*|*change-me*) missing="$missing OTLP_EXPORT_URL" ;; esac
    if [ -n "$missing" ]; then
      echo "local-opentelemetry-collector: SKIP — not configured on this machine"
      exit 3
    fi
    if [ -z "$(docker ps -q -f 'name=^local-opentelemetry-collector$' 2>/dev/null)" ]; then
      if [ -f "$STARTED" ]; then
        echo "local-opentelemetry-collector: FAIL — started $(cat "$STARTED") and the container is gone (crashed, or removed without stop)"
        echo "  --rm removed the dead container; run start again, or stop to clear this"
        exit 1
      fi
      echo "local-opentelemetry-collector: SKIP — container is not running"
      exit 3
    fi
    # zpages answering means the collector is alive and its pipelines built;
    # a config it rejected leaves the container dead rather than half-up.
    # Healthy needs both a complete transfer (curl exits 0) and a 2xx. curl's
    # exit alone asserted the TCP connect: it is 0 on a 404 or a 500 without
    # -f, and 0 on a 3xx even with it. The code alone misses a 200 whose body
    # then stalls or is cut short (sd:1387). A failed connect prints 000.
    rc=0
    code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 \
      "http://127.0.0.1:$ZPAGES_PORT/debug/servicez" 2>/dev/null) || rc=$?
    case "$rc:$code" in
      0:2??)
        echo "local-opentelemetry-collector: OK — up, zpages answering on $ZPAGES_PORT"
        ;;
      *:|*:000)
        echo "local-opentelemetry-collector: FAIL — container up but zpages on $ZPAGES_PORT is not answering"
        exit 1
        ;;
      0:*)
        echo "local-opentelemetry-collector: FAIL — container up but zpages on $ZPAGES_PORT answered HTTP $code"
        exit 1
        ;;
      *)
        echo "local-opentelemetry-collector: FAIL — zpages on $ZPAGES_PORT answered HTTP $code but the response did not complete (curl exit $rc)"
        exit 1
        ;;
    esac
    ;;
  update)
    docker stop local-opentelemetry-collector 2>/dev/null || true
    rm -f "$STARTED"
    docker images -a | grep "otel/opentelemetry-collector" | awk '{print $3}' | xargs docker rmi
    ;;
  test)
    shift
    exec "${PYTHON:-python3}" -m unittest discover -s "$DIR/tests" -t "$DIR" "$@"
    ;;
  -h|--help|help)
    cat <<'HELPEOF'
usage: opentelemetry-collector.sh start|stop|status|update|test

  start        run the container (docker, --rm, named) and record that it
               was started (state/started)
  stop         stop and remove the container, and clear that record
  status       report whether the collector is up and its zpages answering
               with HTTP 2xx. Exits 0 healthy, 3 when there is nothing to
               check (not configured, or stopped), 1 when up but
               failing or started and since gone —
               local-health-check reads those codes.
  update       stop the container and delete its local images by image ID
               (next start pulls the latest)
  test         run this folder's tests

environment: OTELCOL_GRPC_PORT (4317), OTELCOL_HTTP_PORT (4318),
             OTELCOL_ZPAGES_PORT (55679)
             OTLP_EXPORT_URL, OTLP_EXPORT_AUTH (required by start: the
             OTLP/HTTP endpoint to forward to and its Authorization header
             value; from <config>/opentelemetry-collector/.env, <config> being
             SYSTEM_TOOLS_CONFIG or ~/.config/system, or the
             environment — see .env.example)
HELPEOF
    exit 0
    ;;
  *)
    echo "usage: $(basename "$0") start|stop|status|update|test" >&2
    exit 1
    ;;
esac

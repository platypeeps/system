#!/bin/sh
# Fluent Bit demo pipeline: random input -> two HTTP (json_lines) outputs.
# NOTE: start SHIPS DATA OFF-BOX to the two configured destination URLs.
# Destinations and auth come from $SYSTEM_TOOLS_CONFIG/fluentbit/.env — see
# .env.example. config.yaml is generated from config.yaml.template at start
# and is gitignored.
# Usage: fluentbit.sh start|stop|status|update|test
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
. "$DIR/../lib/config.sh"
CONTAINER=fluent-bit

# `start` leaves this marker and `stop` removes it. The container runs with
# --rm, so one that crashed leaves nothing for `docker ps -a` to find; without
# the marker a shipper that died reads the same as one nobody started, and
# `status` answers "nothing to check" for a broken service (sd:1387).
STARTED="$DIR/state/started"

# Same patterns machine-setup's doctor reports on, so a half-filled .env fails
# here naming the variable instead of shipping records at a null pipeline id
# and leaving the operator to work out why nothing arrived.
is_placeholder() {
  case "$1" in
    *change-me*|*changeme*|*CHANGE_ME*|your-*|xxx*|XXX*|*placeholder*) return 0 ;;
    *00000000-0000-0000-0000-000000000000*) return 0 ;;
  esac
  return 1
}

load_env() {
  st_source_env fluentbit
}

# split_url <n>: set EXPORT_HOST_<n>, EXPORT_PORT_<n>, EXPORT_URI_<n> and
# EXPORT_TLS_<n> from FLUENTBIT_EXPORT_URL_<n>, the output plugin's fields.
split_url() {
  eval "url=\$FLUENTBIT_EXPORT_URL_$1"
  case "$url" in
    https://*) tls=on;  port=443; rest="${url#https://}" ;;
    http://*)  tls=off; port=80;  rest="${url#http://}" ;;
    *) echo "fluentbit.sh: FLUENTBIT_EXPORT_URL_$1 must start with http:// or https://" >&2; exit 1 ;;
  esac
  hostport="${rest%%/*}"
  uri="${rest#"$hostport"}"
  [ -n "$uri" ] || uri=/
  host="${hostport%%:*}"
  [ "$host" = "$hostport" ] || port="${hostport#*:}"
  eval "EXPORT_HOST_$1=\$host EXPORT_PORT_$1=\$port EXPORT_URI_$1=\$uri EXPORT_TLS_$1=\$tls"
}

# Returns 1 (and, unless quiet, explains) when the destinations are not usable.
check_config() {
  missing=""
  placeholder=""
  for v in FLUENTBIT_EXPORT_URL_1 FLUENTBIT_EXPORT_AUTH_1 \
           FLUENTBIT_EXPORT_URL_2 FLUENTBIT_EXPORT_AUTH_2; do
    eval "val=\${$v:-}"
    if [ -z "$val" ]; then
      missing="$missing $v"
    elif is_placeholder "$val"; then
      placeholder="$placeholder $v"
    fi
  done
  [ -z "$missing" ] && [ -z "$placeholder" ] && return 0
  if [ "${1:-}" != "quiet" ]; then
    [ -n "$missing" ] && echo "fluentbit.sh: missing$missing" >&2
    [ -n "$placeholder" ] && echo "fluentbit.sh: still at placeholder(s):$placeholder" >&2
    echo "export them, or copy local-fluentbit/.env.example to $(st_config_dir fluentbit)/.env and fill it in" >&2
  fi
  return 1
}

running() {
  [ -n "$(docker ps -q -f "name=^${CONTAINER}$" 2>/dev/null)" ]
}

case "${1:-}" in
  start)
    load_env
    check_config
    split_url 1
    split_url 2
    sed -e "s|\${FLUENTBIT_EXPORT_AUTH_1}|$FLUENTBIT_EXPORT_AUTH_1|" \
        -e "s|\${FLUENTBIT_EXPORT_AUTH_2}|$FLUENTBIT_EXPORT_AUTH_2|" \
        -e "s|\${EXPORT_HOST_1}|$EXPORT_HOST_1|" -e "s|\${EXPORT_HOST_2}|$EXPORT_HOST_2|" \
        -e "s|\${EXPORT_PORT_1}|$EXPORT_PORT_1|" -e "s|\${EXPORT_PORT_2}|$EXPORT_PORT_2|" \
        -e "s|\${EXPORT_URI_1}|$EXPORT_URI_1|" -e "s|\${EXPORT_URI_2}|$EXPORT_URI_2|" \
        -e "s|\${EXPORT_TLS_1}|$EXPORT_TLS_1|" -e "s|\${EXPORT_TLS_2}|$EXPORT_TLS_2|" \
        "$DIR/config.yaml.template" > "$DIR/config.yaml"
    docker run -d --rm --name "$CONTAINER" \
      -v "$DIR:/fluent-bit/etc" \
      cr.fluentbit.io/fluent/fluent-bit:latest \
      -c /fluent-bit/etc/config.yaml
    mkdir -p "$DIR/state"
    date '+%Y-%m-%dT%H:%M:%S%z' > "$STARTED"
    ;;
  stop)
    docker stop "$CONTAINER" 2>/dev/null || true
    rm -f "$STARTED"
    ;;
  status)
    # Exit 3 means "nothing to check" — not configured, or stopped with `stop`,
    # which for a demo shipper is the normal resting state and not a fault.
    # Started and since gone is 1: that is a crash, not a resting state.
    # local-health-check reads these codes, so 1 is reserved for a shipper that
    # is up and actually broken.
    load_env
    if ! check_config quiet; then
      echo "local-fluentbit: SKIP — not configured on this machine"
      exit 3
    fi
    if ! running; then
      if [ -f "$STARTED" ]; then
        echo "local-fluentbit: FAIL — started $(cat "$STARTED") and container '$CONTAINER' is gone (crashed, or removed without stop)"
        echo "  --rm removed the dead container; run start again, or stop to clear this"
        exit 1
      fi
      echo "local-fluentbit: SKIP — container '$CONTAINER' is not running"
      exit 3
    fi
    echo "local-fluentbit: container '$CONTAINER' is up"
    if [ "${2:-}" != "--probe" ]; then
      echo "  (pass --probe to also POST one marker record at the ingest URL)"
      exit 0
    fi
    # The probe SHIPS ONE RECORD OFF-BOX, which is why it is opt-in.
    url="$FLUENTBIT_EXPORT_URL_1"
    code=$(curl -sS -o /dev/null -w '%{http_code}' \
      --request POST --url "$url" \
      -H "Authorization: $FLUENTBIT_EXPORT_AUTH_1" \
      -H 'Content-Type: application/json' \
      --data '{"probe":"local-fluentbit status","hostname":"probe"}') || code=""
    if [ -z "$code" ]; then
      echo "local-fluentbit: FAIL — could not reach $url"
      exit 1
    fi
    case "$code" in
      2??) echo "local-fluentbit: OK — probe POST returned HTTP $code" ;;
      *)   echo "local-fluentbit: FAIL — probe POST returned HTTP $code"; exit 1 ;;
    esac
    ;;
  update)
    docker stop "$CONTAINER" 2>/dev/null || true
    rm -f "$STARTED"
    docker images -a | grep "fluent/fluent-bit" | awk '{print $3}' | xargs docker rmi
    ;;
  test)
    shift
    exec "${PYTHON:-python3}" -m unittest discover -s "$DIR/tests" -t "$DIR" "$@"
    ;;
  -h|--help|help)
    cat <<'HELPEOF'
usage: fluentbit.sh start|stop|status|update|test

  start        render config.yaml from config.yaml.template with the values
               from the config .env, then run the container (docker, --rm,
               named) and record that it was started (state/started).
               Ships random demo records to two HTTP destinations.
  stop         stop and remove the container (no-op when not running), and
               clear that record
  status       report whether the shipper is up. Exits 0 when it is, 3 when
               there is nothing to check (not configured, or stopped), 1 when
               it is up but failing or was started and is gone —
               local-health-check reads those.
               `status --probe` also POSTs one marker record at the first
               destination URL, which SHIPS DATA OFF-BOX.
  update       stop the container and delete its local images by image ID
               (next start pulls the latest)
  test         run this folder's tests

environment ($SYSTEM_TOOLS_CONFIG/fluentbit/.env or exported; see .env.example):
  FLUENTBIT_EXPORT_URL_1, FLUENTBIT_EXPORT_URL_2    destination URLs
                                                    (http[s]://host[:port]/path)
  FLUENTBIT_EXPORT_AUTH_1, FLUENTBIT_EXPORT_AUTH_2  Authorization header values
HELPEOF
    exit 0
    ;;
  *)
    echo "usage: $(basename "$0") start|stop|status|update|test" >&2
    exit 1
    ;;
esac

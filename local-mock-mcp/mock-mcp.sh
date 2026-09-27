#!/bin/sh
# Mock MCP server (a mock-mcp-service checkout named by MOCK_MCP_SRC):
# build the image, run it, switch which failure scenario it serves, and
# register it with Claude Code. Listens on :9992, endpoint /mcp.
# Usage: mock-mcp.sh build|start|stop|restart|status|scenarios|scenario|logs|register|unregister|update|test
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
. "$DIR/../lib/config.sh"
st_source_env mock-mcp

PORT="${MOCK_MCP_PORT:-9992}"
SRC="${MOCK_MCP_SRC:-}"
DEFAULT_SCENARIO="${MOCK_SCENARIO:-}"

IMAGE="${MOCK_MCP_IMAGE:-mock-mcp-service}"
NAME="${MOCK_MCP_NAME:-mock-mcp}"
SCEN_DIR="$SRC/mock-scenarios/scenarios"
URL="http://localhost:$PORT/mcp"

die() { echo "$(basename "$0"): $*" >&2; exit 1; }

need_src() {
  [ -n "$SRC" ] || { st_missing MOCK_MCP_SRC mock-mcp .env; exit 1; }
  [ -d "$SRC" ] || die "checkout not found: $SRC (set MOCK_MCP_SRC)"
}

# default_scenario: MOCK_SCENARIO, or fail naming it.
default_scenario() {
  [ -n "$DEFAULT_SCENARIO" ] || { st_missing MOCK_SCENARIO mock-mcp .env; exit 1; }
  echo "$DEFAULT_SCENARIO"
}

running() {
  docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$NAME"
}

# The container is the state: which scenario is being served is whatever
# MOCK_SCENARIO the running container was started with. No state file to
# drift out of sync with docker.
active_scenario() {
  docker inspect "$NAME" --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null |
    sed -n 's/^MOCK_SCENARIO=//p'
}

list_scenarios() {
  need_src
  [ -d "$SCEN_DIR" ] || die "no scenarios directory: $SCEN_DIR"
  for f in "$SCEN_DIR"/*.yaml; do
    [ -e "$f" ] || continue
    basename "$f" .yaml
  done | sort
}

start_container() {
  scen="$1"
  need_src
  docker image inspect "$IMAGE" >/dev/null 2>&1 ||
    die "image '$IMAGE' not built — run: $(basename "$0") build"
  [ -f "$SCEN_DIR/$scen.yaml" ] || {
    echo "$(basename "$0"): no such scenario '$scen'. Available:" >&2
    list_scenarios | sed 's/^/  /' >&2
    exit 1
  }

  docker rm -f "$NAME" >/dev/null 2>&1 || true

  # mock-scenarios is mounted from the checkout rather than used from the
  # image, so editing a scenario YAML needs a restart, not a rebuild.
  # MOCK_SCENARIOS_DIR is relative and resolves against the image's
  # WORKDIR (/opt/app), which is where the mount lands.
  docker run -d --rm --name "$NAME" -p 127.0.0.1:"$PORT":9992 \
    -e MOCK_SCENARIOS_DIR=mock-scenarios \
    -e MOCK_SCENARIO="$scen" \
    -v "$SRC/mock-scenarios:/opt/app/mock-scenarios:ro" \
    "$IMAGE" >/dev/null

  n=0
  while [ "$n" -lt 20 ]; do
    if curl -fsS -m 2 "http://localhost:$PORT/healthz" >/dev/null 2>&1; then
      echo "mock-mcp up on $URL serving '$scen'"
      return 0
    fi
    n=$((n + 1))
    sleep 0.5
  done

  echo "$(basename "$0"): container started but /healthz never answered; last logs:" >&2
  docker logs --tail 20 "$NAME" >&2 2>&1 || true
  exit 1
}

case "$1" in
  build)
    need_src
    docker build -t "$IMAGE" "$SRC"
    ;;
  start)
    running && die "already running ($(active_scenario)) — use restart or scenario"
    scen="${2:-}"
    [ -n "$scen" ] || scen="$(default_scenario)"
    start_container "$scen"
    ;;
  stop)
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    ;;
  restart)
    scen="${2:-$(active_scenario)}"
    [ -n "$scen" ] || scen="$(default_scenario)"
    start_container "$scen"
    ;;
  scenario)
    [ -n "$2" ] || die "scenario: needs a name — see: $(basename "$0") scenarios"
    start_container "$2"
    ;;
  scenarios)
    active="$(active_scenario)"
    list_scenarios | while read -r s; do
      if [ "$s" = "$active" ]; then echo "* $s"; else echo "  $s"; fi
    done
    ;;
  status)
    if running; then
      echo "container:  running as $NAME"
      echo "scenario:   $(active_scenario)"
    else
      echo "container:  not running"
      echo "scenario:   -"
    fi
    echo "endpoint:   $URL"
    if curl -fsS -m 2 "http://localhost:$PORT/healthz" >/dev/null 2>&1; then
      echo "health:     OK"
    else
      echo "health:     unreachable"
    fi
    if claude mcp get "$NAME" >/dev/null 2>&1; then
      echo "claude:     registered as '$NAME'"
    else
      echo "claude:     not registered — run: $(basename "$0") register"
    fi
    echo "source:     $SRC"
    ;;
  logs)
    shift
    docker logs -f --tail 50 "$NAME" "$@"
    ;;
  register)
    scope="${2:-user}"
    claude mcp get "$NAME" >/dev/null 2>&1 &&
      die "'$NAME' is already registered — run: $(basename "$0") unregister [scope]"
    claude mcp add --transport http --scope "$scope" "$NAME" "$URL"
    echo "reconnect with /mcp inside a running Claude Code session"
    ;;
  unregister)
    scope="${2:-user}"
    claude mcp remove "$NAME" -s "$scope"
    ;;
  update)
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    # By IMAGE ID ($3), not tag ($2) — locally built, so nothing to pull back.
    imgs="$(docker images -a | grep "^$IMAGE " | awk '{print $3}' || true)"
    [ -z "$imgs" ] || echo "$imgs" | xargs docker rmi -f >/dev/null
    need_src
    docker build -t "$IMAGE" "$SRC"
    ;;
  test)
    shift
    exec "${PYTHON:-python3}" -m unittest discover -s "$DIR/tests" -t "$DIR" "$@"
    ;;
  -h|--help|help)
    cat <<'HELPEOF'
usage: mock-mcp.sh build|start|stop|restart|status|scenarios|scenario <name>|logs|register|unregister|update|test

  build             docker build the image from the mock-mcp-service checkout
  start [scenario]  run the container (docker, --rm, named $MOCK_MCP_NAME);
                    the scenario defaults to $MOCK_SCENARIO
  stop              stop and remove the container
  restart [scen]    restart, keeping the scenario it is already serving
  scenario <name>   serve a different scenario (restarts the container)
  scenarios         list scenarios in the checkout; * marks the active one
  status            container, scenario, endpoint, health, Claude Code entry
  logs [args]       follow container logs (extra args go to docker logs)
  register [scope]  add the server to Claude Code (scope: user|local|project,
                    default user), then reconnect with /mcp
  unregister [scope]  remove it from Claude Code (default scope user)
  update            stop, delete the local image by image ID, rebuild
  test [args]       run this folder's tests (extra args go to unittest)

The scenarios directory is bind-mounted from the checkout, so editing a
scenario YAML needs a restart, not a rebuild.

Per-call scenario selection is also possible without restarting: the server
reads an X-Mock-Scenario header, and X-Mock-Session pins an independent
timeline per eval run. Claude Code sends only static headers, so switching
the container's scenario is the simpler route from a session.

environment, exported or set in $SYSTEM_TOOLS_CONFIG/mock-mcp/.env
(copy .env.example):
             MOCK_MCP_SRC   (required for build/start/scenarios: the
                             mock-mcp-service checkout)
             MOCK_SCENARIO  (default scenario for start/restart)
             MOCK_MCP_PORT  (default 9992)
             MOCK_MCP_NAME  (container and Claude Code server name,
                             default mock-mcp)
             MOCK_MCP_IMAGE (image tag, default mock-mcp-service)
HELPEOF
    exit 0
    ;;
  *)
    echo "usage: $(basename "$0") build|start|stop|restart|status|scenarios|scenario <name>|logs|register|unregister|update|test" >&2
    exit 1
    ;;
esac

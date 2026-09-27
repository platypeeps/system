#!/bin/sh
# ClickHouse server (:8123/:9000, data in ./storage, logs in ./logs) plus
# the ClickHouse MCP server (SSE on :8002). Password is a local dev value.
# Usage: clickhouse.sh start|stop|update|client
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"

# The MCP port is 8002, not 8001: 8001 is the redis-stack convention that
# RedisInsight in local-redis publishes, and the two used to be mutually
# exclusive.
HTTP_PORT="${CLICKHOUSE_HTTP_PORT:-8123}"
NATIVE_PORT="${CLICKHOUSE_NATIVE_PORT:-9000}"
MCP_PORT="${CLICKHOUSE_MCP_PORT:-8002}"

# ./.env provides defaults only — already-exported values win (same
# precedence rule as local-notify). The file is optional; without it the
# baked-in local dev values apply.
ENV_CLICKHOUSE_USER="${CLICKHOUSE_USER:-}"
ENV_CLICKHOUSE_PASSWORD="${CLICKHOUSE_PASSWORD:-}"
if [ -f "$DIR/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$DIR/.env"
  set +a
fi
if [ -n "$ENV_CLICKHOUSE_USER" ]; then CLICKHOUSE_USER="$ENV_CLICKHOUSE_USER"; fi
if [ -n "$ENV_CLICKHOUSE_PASSWORD" ]; then CLICKHOUSE_PASSWORD="$ENV_CLICKHOUSE_PASSWORD"; fi
CLICKHOUSE_USER="${CLICKHOUSE_USER:-default}"
CLICKHOUSE_PASSWORD="${CLICKHOUSE_PASSWORD:-verysecure}"

case "$1" in
  start)
    mkdir -p "$DIR/storage" "$DIR/logs"
    docker run -d --rm --name clickhouse-server \
      -e CLICKHOUSE_USER="$CLICKHOUSE_USER" \
      -e CLICKHOUSE_PASSWORD="$CLICKHOUSE_PASSWORD" \
      -v "$DIR/storage:/var/lib/clickhouse" \
      -v "$DIR/logs:/var/log/clickhouse-server" \
      --ulimit nofile=262144:262144 \
      -p 127.0.0.1:"$HTTP_PORT":8123 -p 127.0.0.1:"$NATIVE_PORT":9000 \
      clickhouse/clickhouse-server
    docker run -d --rm --name clickhouse-mcp \
      -e CLICKHOUSE_HOST=0.0.0.0 \
      -e CLICKHOUSE_USER="$CLICKHOUSE_USER" \
      -e CLICKHOUSE_PASSWORD="$CLICKHOUSE_PASSWORD" \
      -e CLICKHOUSE_PORT="$HTTP_PORT" \
      -e CLICKHOUSE_MCP_SERVER_TRANSPORT=sse \
      -e CLICKHOUSE_MCP_BIND_HOST=0.0.0.0 \
      -p 127.0.0.1:"$MCP_PORT":8000 mcp/clickhouse
    ;;
  stop)
    docker stop clickhouse-server 2>/dev/null || true
    docker stop clickhouse-mcp 2>/dev/null || true
    ;;
  update)
    "$0" stop
    docker images -a | grep "clickhouse-server" | awk '{print $3}' | xargs docker rmi
    docker images -a | grep "mcp/clickhouse" | awk '{print $3}' | xargs docker rmi
    ;;
  client)
    docker run \
      -e CLICKHOUSE_USER="$CLICKHOUSE_USER" \
      -e CLICKHOUSE_PASSWORD="$CLICKHOUSE_PASSWORD" -it --rm \
      --network=container:clickhouse-server \
      clickhouse/clickhouse-server clickhouse-client
    ;;
  -h|--help|help)
    cat <<'HELPEOF'
usage: clickhouse.sh start|stop|update|client

  start        run the container (docker, --rm, named)
  stop         stop and remove the container
  update       stop the container and delete its local images by image ID
               (next start pulls the latest)
  client       open clickhouse-client inside the running container

CLICKHOUSE_USER (default: default) and CLICKHOUSE_PASSWORD (default: a local
dev value) come from the environment or ./.env (see .env.example; exported
values win) and are applied to the server, the MCP server, and the client.

environment: CLICKHOUSE_HTTP_PORT (8123), CLICKHOUSE_NATIVE_PORT (9000),
             CLICKHOUSE_MCP_PORT (8002)
HELPEOF
    exit 0
    ;;
  *)
    echo "usage: $(basename "$0") start|stop|update|client" >&2
    exit 1
    ;;
esac

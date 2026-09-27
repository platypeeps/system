#!/bin/sh
# Graphiti knowledge-graph MCP server + FalkorDB backend via docker compose.
# API keys come from <config>/graphiti-mcp/.env (see lib/config.sh and
# .env.example); compose gets that path explicitly, never ./.env.
# Usage: graphiti-mcp.sh start|stop|update
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"
# shellcheck source=../lib/config.sh
. "$DIR/../lib/config.sh"
# docker-compose.yml reads GRAPHITI_ENV_FILE for env_file; --env-file makes
# compose interpolate from the same file instead of a ./.env beside it.
GRAPHITI_ENV_FILE="$(st_config_dir graphiti-mcp)/.env"
export GRAPHITI_ENV_FILE

compose() {
  docker compose --env-file "$GRAPHITI_ENV_FILE" "$@"
}

need_env() {
  if [ ! -f "$GRAPHITI_ENV_FILE" ]; then
    st_missing "$GRAPHITI_ENV_FILE" graphiti-mcp .env
    exit 1
  fi
}

case "$1" in
  start)
    need_env
    compose up -d
    ;;
  stop)
    need_env
    compose down
    ;;
  update)
    need_env
    compose down
    docker images -a | grep "knowledge-graph-mcp" | awk '{print $3}' | xargs docker rmi
    compose pull
    ;;
  -h|--help|help)
    cat <<'HELPEOF'
usage: graphiti-mcp.sh start|stop|update

  start        docker compose up -d (needs <config>/graphiti-mcp/.env, where
               <config> is $SYSTEM_TOOLS_CONFIG, default
               ~/.config/system; copy .env.example there)
  stop         docker compose down
  update       compose down, delete the knowledge-graph-mcp images by image
               ID, compose pull (next start runs the fresh image)
HELPEOF
    exit 0
    ;;
  *)
    echo "usage: $(basename "$0") start|stop|update" >&2
    exit 1
    ;;
esac

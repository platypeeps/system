#!/bin/sh
# Graphiti knowledge-graph MCP server + FalkorDB backend via docker compose.
# API keys come from ./.env (gitignored) — see .env.example.
# Usage: graphiti-mcp.sh start|stop|update
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

case "$1" in
  start)
    docker compose up -d
    ;;
  stop)
    docker compose down
    ;;
  update)
    docker compose down
    docker images -a | grep "knowledge-graph-mcp" | awk '{print $3}' | xargs docker rmi
    docker compose pull
    ;;
  -h|--help|help)
    cat <<'HELPEOF'
usage: graphiti-mcp.sh start|stop|update

  start        docker compose up -d (needs ./.env — see .env.example)
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

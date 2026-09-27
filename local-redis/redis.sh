#!/bin/sh
# Redis Stack (server + RedisInsight UI on :8001).
# Usage: redis.sh start|stop|update
set -e

# Redis keeps the registered 6379 and the redis-stack 8001. falkordb moved to
# 6380, graphiti's bundled falkordb to 6381 and the clickhouse MCP to 8002, so
# all of them can now run at once. Superseded: 6379 by falkordb and graphiti-mcp, 8001 by the
# clickhouse MCP. Only one of those may be listed in a profile at a time.
PORT="${REDIS_PORT:-6379}"
INSIGHT_PORT="${REDISINSIGHT_PORT:-8001}"

case "$1" in
  start)
    docker run -d --rm --name redis \
      -p 127.0.0.1:"$PORT":6379 \
      -p 127.0.0.1:"$INSIGHT_PORT":8001 \
      redis/redis-stack:latest
    ;;
  stop)
    docker stop redis 2>/dev/null || true
    ;;
  update)
    docker stop redis 2>/dev/null || true
    docker images -a | grep "redis-stack" | awk '{print $3}' | xargs docker rmi
    ;;
  -h|--help|help)
    cat <<'HELPEOF'
usage: redis.sh start|stop|update

  start        run the container (docker, --rm, named)
  stop         stop and remove the container
  update       stop the container and delete its local images by image ID
               (next start pulls the latest)

environment: REDIS_PORT (default 6379), REDISINSIGHT_PORT (default 8001)
HELPEOF
    exit 0
    ;;
  *)
    echo "usage: $(basename "$0") start|stop|update" >&2
    exit 1
    ;;
esac

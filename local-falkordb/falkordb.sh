#!/bin/sh
# FalkorDB graph database (server on :6380, browser UI on :3003).
# Auth: FALKORDB_USER / FALKORDB_PASSWORD (defaults below match local-postgres).
# Usage: falkordb.sh start|stop|update
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"

# 6380, not redis's 6379: FalkorDB speaks the Redis protocol, so the two used
# to fight over one port and only one could be listed per profile.
PORT="${FALKORDB_PORT:-6380}"
UI_PORT="${FALKORDB_UI_PORT:-3003}"

# ./.env provides defaults only — already-exported values win (same
# precedence rule as local-notify). The file is optional; without it the
# baked-in local dev values apply.
ENV_FALKORDB_USER="${FALKORDB_USER:-}"
ENV_FALKORDB_PASSWORD="${FALKORDB_PASSWORD:-}"
if [ -f "$DIR/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$DIR/.env"
  set +a
fi
if [ -n "$ENV_FALKORDB_USER" ]; then FALKORDB_USER="$ENV_FALKORDB_USER"; fi
if [ -n "$ENV_FALKORDB_PASSWORD" ]; then FALKORDB_PASSWORD="$ENV_FALKORDB_PASSWORD"; fi
FALKORDB_USER="${FALKORDB_USER:-default}"
FALKORDB_PASSWORD="${FALKORDB_PASSWORD:-verysecure}"

case "$1" in
  start)
    REDIS_ARGS="--requirepass $FALKORDB_PASSWORD"
    if [ "$FALKORDB_USER" != "default" ]; then
      REDIS_ARGS="$REDIS_ARGS --user $FALKORDB_USER on >$FALKORDB_PASSWORD ~* &* +@all"
    fi
    docker run -d --rm --name falkordb \
      -p 127.0.0.1:"$PORT":6379 -p 127.0.0.1:"$UI_PORT":3000 \
      -e REDIS_ARGS="$REDIS_ARGS" \
      falkordb/falkordb:latest
    ;;
  stop)
    docker stop falkordb 2>/dev/null || true
    ;;
  update)
    docker stop falkordb 2>/dev/null || true
    docker images -a | grep "falkordb/falkordb" | awk '{print $3}' | xargs docker rmi
    ;;
  -h|--help|help)
    cat <<'HELPEOF'
usage: falkordb.sh start|stop|update

  start        run the container (docker, --rm, named)
  stop         stop and remove the container
  update       stop the container and delete its local images by image ID
               (next start pulls the latest)

environment:
  FALKORDB_USER      extra ACL user to create at start (default: default —
                     no extra user, just password auth)
  FALKORDB_PASSWORD  password for that user and for "default" (default: verysecure)

Both come from the environment or ./.env (see .env.example; exported values
win).

environment: FALKORDB_PORT (default 6380), FALKORDB_UI_PORT (default 3003)
HELPEOF
    exit 0
    ;;
  *)
    echo "usage: $(basename "$0") start|stop|update" >&2
    exit 1
    ;;
esac

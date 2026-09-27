#!/bin/sh
# PostgreSQL, data persisted in ./storage. Local dev credentials only —
# override via POSTGRES_USER / POSTGRES_PASSWORD env vars.
# Usage: postgres.sh start|stop|update
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"

# <config>/postgres/.env (see lib/config.sh) provides defaults only —
# already-exported values win (same precedence rule as local-notify). The file is optional; without it the
# baked-in local dev values apply.
ENV_POSTGRES_USER="${POSTGRES_USER:-}"
ENV_POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-}"
# shellcheck source=../lib/config.sh
. "$DIR/../lib/config.sh"
st_source_env postgres
if [ -n "$ENV_POSTGRES_USER" ]; then POSTGRES_USER="$ENV_POSTGRES_USER"; fi
if [ -n "$ENV_POSTGRES_PASSWORD" ]; then POSTGRES_PASSWORD="$ENV_POSTGRES_PASSWORD"; fi
POSTGRES_USER="${POSTGRES_USER:-admin}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-verysecure}"

# 5434, not 5432: other projects commonly run their own postgres containers on
# 5432 and 5433, so the published port stays clear of both. Named
# _HOST_PORT because it is the published port only — the server inside the
# container still listens on 5432, and a POSTGRES_PORT in .env is far more
# likely to mean "the port my client should dial".
POSTGRES_HOST_PORT="${POSTGRES_HOST_PORT:-5434}"

case "$1" in
  start)
    mkdir -p "$DIR/storage"
    docker run -d --rm --name postgres \
      -e POSTGRES_USER="$POSTGRES_USER" \
      -e POSTGRES_PASSWORD="$POSTGRES_PASSWORD" \
      -v "$DIR/storage:/var/lib/postgresql" \
      -p 127.0.0.1:"$POSTGRES_HOST_PORT":5432 postgres
    ;;
  stop)
    docker stop postgres 2>/dev/null || true
    ;;
  update)
    docker stop postgres 2>/dev/null || true
    docker images -a | grep "^postgres " | awk '{print $3}' | xargs docker rmi
    ;;
  -h|--help|help)
    cat <<'HELPEOF'
usage: postgres.sh start|stop|update

  start        run the container (docker, --rm, named)
  stop         stop and remove the container
  update       stop the container and delete its local images by image ID
               (next start pulls the latest)

POSTGRES_USER (default: admin) and POSTGRES_PASSWORD (default: a local dev
value) come from the environment or
<config>/postgres/.env (copy .env.example there; <config> is
$SYSTEM_TOOLS_CONFIG, default ~/.config/system; exported
values win).

POSTGRES_HOST_PORT (default 5434) is the published host port; 5432 and 5433 are
taken by other projects' postgres containers on this machine.
HELPEOF
    exit 0
    ;;
  *)
    echo "usage: $(basename "$0") start|stop|update" >&2
    exit 1
    ;;
esac

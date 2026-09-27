#!/bin/sh
# Grafana Enterprise on :3000, data persisted in ./storage.
# Usage: grafana.sh start|stop|update
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"

PORT="${GRAFANA_PORT:-3000}"   # 3000 is also the worldmonitor dev server

case "$1" in
  start)
    mkdir -p "$DIR/storage"
    docker run -d --rm --name=grafana -p 127.0.0.1:"$PORT":3000 \
      --user "$(id -u)" \
      --volume "$DIR/storage:/var/lib/grafana" \
      grafana/grafana-enterprise
    ;;
  stop)
    docker stop grafana 2>/dev/null || true
    ;;
  update)
    docker stop grafana 2>/dev/null || true
    docker images -a | grep "grafana/grafana" | awk '{print $3}' | xargs docker rmi
    ;;
  -h|--help|help)
    cat <<'HELPEOF'
usage: grafana.sh start|stop|update

  start        run the container (docker, --rm, named)
  stop         stop and remove the container
  update       stop the container and delete its local images by image ID
               (next start pulls the latest)

environment: GRAFANA_PORT (default 3000)
HELPEOF
    exit 0
    ;;
  *)
    echo "usage: $(basename "$0") start|stop|update" >&2
    exit 1
    ;;
esac

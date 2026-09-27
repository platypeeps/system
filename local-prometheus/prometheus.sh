#!/bin/sh
# Prometheus on :9090, data persisted in ./storage.
# Usage: prometheus.sh start|stop|update
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"

PORT="${PROMETHEUS_PORT:-9090}"

case "$1" in
  start)
    mkdir -p "$DIR/storage"
    docker run -d --rm --name prometheus -p 127.0.0.1:"$PORT":9090 \
      -v "$DIR/storage:/prometheus" prom/prometheus
    ;;
  stop)
    docker stop prometheus 2>/dev/null || true
    ;;
  update)
    docker stop prometheus 2>/dev/null || true
    docker images -a | grep "prom/prometheus" | awk '{print $3}' | xargs docker rmi
    ;;
  -h|--help|help)
    cat <<'HELPEOF'
usage: prometheus.sh start|stop|update

  start        run the container (docker, --rm, named)
  stop         stop and remove the container
  update       stop the container and delete its local images by image ID
               (next start pulls the latest)

environment: PROMETHEUS_PORT (default 9090)
HELPEOF
    exit 0
    ;;
  *)
    echo "usage: $(basename "$0") start|stop|update" >&2
    exit 1
    ;;
esac

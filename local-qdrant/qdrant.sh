#!/bin/sh
# Qdrant vector database, data persisted in ./storage.
# Usage: qdrant.sh start|stop|update
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"

# 6337, not qdrant's usual 6333: two other apps ship their own qdrant and are
# running right now — OpenWhispr.app on 6333/6334 and Miyo on 6335/6336. Every
# port in qdrant's normal range is therefore already taken on this machine.
PORT="${QDRANT_PORT:-6337}"

case "$1" in
  start)
    mkdir -p "$DIR/storage"
    docker run -d --rm --name qdrant -p 127.0.0.1:"$PORT":6333 \
      -v "$DIR/storage:/qdrant/storage" \
      qdrant/qdrant
    ;;
  stop)
    docker stop qdrant 2>/dev/null || true
    ;;
  update)
    docker stop qdrant 2>/dev/null || true
    docker images -a | grep "qdrant/qdrant" | awk '{print $3}' | xargs docker rmi
    ;;
  -h|--help|help)
    cat <<'HELPEOF'
usage: qdrant.sh start|stop|update

  start        run the container (docker, --rm, named)
  stop         stop and remove the container
  update       stop the container and delete its local images by image ID
               (next start pulls the latest)

environment: QDRANT_PORT (default 6337; 6333-6336 are taken by the qdrant
             instances bundled with OpenWhispr and Miyo)
HELPEOF
    exit 0
    ;;
  *)
    echo "usage: $(basename "$0") start|stop|update" >&2
    exit 1
    ;;
esac

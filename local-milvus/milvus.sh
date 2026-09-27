#!/bin/sh
# Milvus standalone (wraps upstream standalone_embed.sh) plus the Attu web
# UI on :8000. Data lives in ./volumes.
# Usage: milvus.sh start|stop|update|install
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"

# Attu, the Milvus web UI. Milvus' own ports come from the vendored
# standalone_embed.sh, which must not be hand-edited (refresh with `install`).
ATTU_PORT="${MILVUS_UI_PORT:-8000}"
cd "$DIR"

# Upstream standalone_embed.sh prefixes every docker call with sudo — a
# Linux assumption. On macOS Docker Desktop the local user talks to the
# daemon directly and all bind-mounted files are user-owned, so a PATH
# shim turns sudo into a no-op instead of hand-editing the vendored file.
SHIM="$DIR/.sudo-shim"
mkdir -p "$SHIM"
printf '#!/bin/sh\nexec "$@"\n' > "$SHIM/sudo"
chmod +x "$SHIM/sudo"
run_embed() { PATH="$SHIM:$PATH" ./standalone_embed.sh "$@"; }

case "$1" in
  start)
    run_embed start
    # standalone_embed.sh is vendored and starts milvus-standalone on the
    # default bridge; connect it to a named network so Attu can reach it by
    # DNS name instead of the deprecated --link flag.
    docker network inspect milvus-net >/dev/null 2>&1 || docker network create milvus-net
    docker network connect milvus-net milvus-standalone 2>/dev/null || true
    docker run -d --rm --name zilliz --network milvus-net -p 127.0.0.1:"$ATTU_PORT":3000 zilliz/attu:latest
    echo "Attu UI on http://localhost:8000 — connect to milvus-standalone:19530"
    ;;
  stop)
    docker stop zilliz 2>/dev/null || true
    run_embed stop
    ;;
  update)
    docker stop zilliz 2>/dev/null || true
    docker images -a | grep "zilliz" | awk '{print $3}' | xargs docker rmi
    run_embed upgrade
    ;;
  install)
    curl -sfL https://raw.githubusercontent.com/milvus-io/milvus/master/scripts/standalone_embed.sh -o standalone_embed.sh
    chmod +x standalone_embed.sh
    ;;
  -h|--help|help)
    cat <<'HELPEOF'
usage: milvus.sh start|stop|update|install

  start        standalone_embed.sh start (no sudo — a PATH shim no-ops the
               vendored script's sudo calls), then the Attu UI container
  stop         stop Attu, then standalone_embed.sh stop
  update       stop and delete the Attu image by image ID, then
               standalone_embed.sh upgrade
  install      refresh the vendored standalone_embed.sh from upstream

environment: MILVUS_UI_PORT (default 8000, the Attu web UI)
HELPEOF
    exit 0
    ;;
  *)
    echo "usage: $(basename "$0") start|stop|update|install" >&2
    exit 1
    ;;
esac

#!/bin/sh
# Milvus standalone (wraps upstream standalone_embed.sh) plus the Attu web
# UI on :8000. Data lives in ./volumes.
# Usage: milvus.sh start|stop|update|install
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"

# Attu, the Milvus web UI. Milvus' own ports come from the vendored
# standalone_embed.sh, which must not be hand-edited (refresh with `install`);
# loopback_binds below is the one patch, re-applied on every run and after
# every download (install, update).
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

# Upstream publishes 19530, 9091 and 2379 on every interface. Prefix each
# bare `-p PORT:PORT` with 127.0.0.1 so nothing off this machine reaches them;
# a line that already has an address does not match, so a rerun is a no-op.
loopback_binds() {
  sed 's/-p \([0-9][0-9]*:[0-9]\)/-p 127.0.0.1:\1/' standalone_embed.sh > standalone_embed.sh.new
  chmod +x standalone_embed.sh.new
  mv standalone_embed.sh.new standalone_embed.sh
}
# Download the upstream launcher beside the vendored one, keep the old copy if
# the download fails, then patch it.
fetch_embed() {
  curl -sfL https://raw.githubusercontent.com/milvus-io/milvus/master/scripts/standalone_embed.sh -o standalone_embed.sh.new
  chmod +x standalone_embed.sh.new
  mv standalone_embed.sh.new standalone_embed.sh
  loopback_binds
}
run_embed() { loopback_binds; PATH="$SHIM:$PATH" ./standalone_embed.sh "$@"; }

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
    # Download first: a failed download leaves the running service alone.
    fetch_embed
    docker stop zilliz 2>/dev/null || true
    docker images -aq --filter "reference=zilliz/attu" | sort -u | xargs docker rmi
    # Not `standalone_embed.sh upgrade`: it downloads standalone_embed_latest.sh
    # and runs that copy, which binds every interface. Do its steps here instead.
    docker stop milvus-standalone 2>/dev/null || true
    docker rm milvus-standalone 2>/dev/null || true
    run_embed start
    ;;
  install)
    fetch_embed
    ;;
  -h|--help|help)
    cat <<'HELPEOF'
usage: milvus.sh start|stop|update|install

  start        standalone_embed.sh start (no sudo — a PATH shim no-ops the
               vendored script's sudo calls), then the Attu UI container
  stop         stop Attu, then standalone_embed.sh stop
  update       stop and delete the Attu image by image ID, remove the
               Milvus container, refresh standalone_embed.sh from upstream
               (patched to loopback), then start it
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

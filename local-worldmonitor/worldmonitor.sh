#!/bin/sh
# world-monitor helper — the checkout path comes from WORLDMONITOR_DIR, this
# folder only holds the wrapper. Vite dev server listens on :3000.
# Usage: worldmonitor.sh dev [variant]|build|preview|lint
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
. "$DIR/../lib/config.sh"

# app_dir: resolve the checkout, or fail naming WORLDMONITOR_DIR.
app_dir() {
  st_source_env worldmonitor
  if [ -z "${WORLDMONITOR_DIR:-}" ]; then
    st_missing WORLDMONITOR_DIR worldmonitor .env
    exit 1
  fi
  [ -d "$WORLDMONITOR_DIR" ] || {
    echo "worldmonitor.sh: WORLDMONITOR_DIR is not a directory: $WORLDMONITOR_DIR" >&2
    exit 1
  }
  APP_DIR="$WORLDMONITOR_DIR"
}

case "$1" in
  dev)
    app_dir
    cd "$APP_DIR"
    case "${2:-full}" in
      full)                                 exec npm run dev ;;
      tech|finance|happy|commodity|energy)   exec npm run "dev:$2" ;;
      *)
        echo "unknown variant '$2' — one of: full tech finance happy commodity energy" >&2
        exit 1
        ;;
    esac
    ;;
  build)
    app_dir
    cd "$APP_DIR"
    exec npm run build
    ;;
  preview)
    app_dir
    cd "$APP_DIR"
    exec npm run preview
    ;;
  lint)
    app_dir
    cd "$APP_DIR"
    exec npm run lint
    ;;
  -h|--help|help)
    cat <<'HELPEOF'
usage: worldmonitor.sh dev [variant]|build|preview|lint

  dev [variant]  run the Vite dev server on :3000 (DEV_PORT overrides).
                 variant is one of: full (default) tech finance happy
                 commodity energy
  build          production build (tsc + vite build, plus the blog and
                 corpus prebuild steps)
  preview        serve the last production build
  lint           biome lint + the safe-html check

All subcommands run against the checkout in WORLDMONITOR_DIR, exported or set
in $SYSTEM_TOOLS_CONFIG/worldmonitor/.env (see .env.example).
HELPEOF
    exit 0
    ;;
  *)
    echo "usage: $(basename "$0") dev [variant]|build|preview|lint" >&2
    exit 1
    ;;
esac

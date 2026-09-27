#!/bin/sh
# Mezmo aura: install, run the web server (brew or repo build), CLI, and
# quick API smoke tests. Server listens on :3033 by default.
# Usage: aura.sh install|server|server-repo|cli|health|models|prompt [text]
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
. "$DIR/../lib/config.sh"

# <config>/aura/.env provides the API keys config.toml templates in and the
# checkout `server-repo` builds; values already exported win.
ENV_OPENAI_API_KEY="${OPENAI_API_KEY:-}"
ENV_MEZMO_API_KEY="${MEZMO_API_KEY:-}"
ENV_AURA_REPO="${AURA_REPO:-}"
st_source_env aura
[ -n "$ENV_OPENAI_API_KEY" ] && OPENAI_API_KEY="$ENV_OPENAI_API_KEY"
[ -n "$ENV_MEZMO_API_KEY" ] && MEZMO_API_KEY="$ENV_MEZMO_API_KEY"
[ -n "$ENV_AURA_REPO" ] && AURA_REPO="$ENV_AURA_REPO"
[ -n "${OPENAI_API_KEY:-}" ] && export OPENAI_API_KEY
[ -n "${MEZMO_API_KEY:-}" ] && export MEZMO_API_KEY

# A config.toml in <config>/aura/ replaces the one beside this script.
if [ -f "$(st_config_dir aura)/config.toml" ]; then
  DEFAULT_CONFIG="$(st_config_dir aura)/config.toml"
else
  DEFAULT_CONFIG="$DIR/config.toml"
fi

# The server cannot start without either key, so say which is missing first.
need_keys() {
  for var in OPENAI_API_KEY MEZMO_API_KEY; do
    eval "value=\${$var:-}"
    if [ -z "$value" ]; then
      st_missing "$var" aura .env
      exit 1
    fi
  done
}

PORT="${PORT:-3033}"

case "$1" in
  install)
    brew install mezmo/tap/aura
    brew install mezmo/tap/aura-web-server
    ;;
  server)
    need_keys
    HOST="${HOST:-0.0.0.0}"
    CONFIG_PATH="${CONFIG_PATH:-$DEFAULT_CONFIG}"
    exec aura-web-server --config "$CONFIG_PATH" --host "$HOST" --port "$PORT"
    ;;
  server-repo)
    need_keys
    if [ -z "${AURA_REPO:-}" ]; then
      st_missing AURA_REPO aura .env
      exit 1
    fi
    export HOST=0.0.0.0
    export PORT="$PORT"
    export AURA_CUSTOM_EVENTS=true
    export AURA_EMIT_REASONING=true
    cd "$AURA_REPO"
    cargo run --bin aura-web-server -- \
      --config "${CONFIG_PATH:-$DEFAULT_CONFIG}" \
      2>&1 | tee "$DIR/aura-output.txt"
    ;;
  cli)
    HOST="${HOST:-127.0.0.1}"
    API_URL="${AURA_API_URL:-http://${HOST}:${PORT}}"
    shift
    exec aura --api-url "$API_URL" "$@"
    ;;
  health)
    curl "http://localhost:$PORT/health"
    ;;
  models)
    curl "http://localhost:$PORT/v1/models"
    ;;
  prompt)
    TEXT="${2:-Hello}"
    # JSON-encode via python3 so quotes/backslashes/newlines in TEXT survive.
    BODY=$(printf '%s' "$TEXT" | python3 -c 'import json,sys; print(json.dumps({"messages":[{"role":"user","content":sys.stdin.read()}]}))')
    curl -X POST "http://localhost:$PORT/v1/chat/completions" \
      -H "Content-Type: application/json" \
      -d "$BODY"
    ;;
  -h|--help|help)
    cat <<'HELPEOF'
usage: aura.sh install|server|server-repo|cli|health|models|prompt [text]

  install      brew install aura + aura-web-server from mezmo/tap
  server       run aura-web-server on :3033 with config.toml
  server-repo  build and run the server from the $AURA_REPO checkout
               (cargo), output tee'd to aura-output.txt
  cli          interactive aura CLI against the local server
  health       GET /health on the running server
  models       GET /v1/models
  prompt       send one chat completion ("Hello" if no text given)

config:
  <config>/aura/.env         OPENAI_API_KEY, MEZMO_API_KEY, AURA_REPO; copy
                             .env.example. Exported values win.
  <config>/aura/config.toml  replaces config.toml beside this script.
  <config> is $SYSTEM_TOOLS_CONFIG, default ~/.config/system.
  CONFIG_PATH                one server config for this run only.
HELPEOF
    exit 0
    ;;
  *)
    echo "usage: $(basename "$0") install|server|server-repo|cli|health|models|prompt [text]" >&2
    exit 1
    ;;
esac

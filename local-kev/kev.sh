#!/bin/sh
# Kev: a small open-weights System One model, served on this Mac by Kev's own
# server (MLX on Apple Silicon). The checkout lives in kev/ beside this
# script by default, ignored by git; this folder holds the wrapper and the
# LaunchAgent template. The server binds 127.0.0.1:8009.
# Usage: kev.sh install|serve|agent-install|agent-uninstall|status|test|help
#
# status exits 0 healthy, 3 not installed or not running, 1 up and broken.
# local-health-check reads these codes.
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
. "$DIR/../lib/config.sh"

# <config>/kev/.env provides defaults only: a value already in the environment
# wins. Read it before anything below uses a value it may set. Set-ness is
# kept apart from the value, so an exported empty value wins too: KEV_API_KEY=
# switches auth off and HF_HOME= clears a cache path the file names.
for var in KEV_MODEL KEV_PORT KEV_DIR KEV_REPO_URL KEV_API_KEY HF_HOME \
           SYSTEM_TOOLS_LABEL_PREFIX; do
  eval "SET_$var=\${$var+set}; ENV_$var=\"\${$var:-}\""
done
st_source_env kev
for var in KEV_MODEL KEV_PORT KEV_DIR KEV_REPO_URL KEV_API_KEY HF_HOME \
           SYSTEM_TOOLS_LABEL_PREFIX; do
  eval "was=\"\${SET_$var:-}\"; saved=\"\${ENV_$var:-}\""
  [ "$was" != set ] || eval "$var=\"\$saved\""
done

KEV_MODEL="${KEV_MODEL:-jaredpalmer/kev-4b@v1.0}"
KEV_PORT="${KEV_PORT:-8009}"
KEV_DIR="${KEV_DIR:-$DIR/kev}"
KEV_REPO_URL="${KEV_REPO_URL:-https://github.com/jaredpalmer/kev.git}"
case "${KEV_API_KEY:-}" in
  change-me|changeme) KEV_API_KEY="" ;;
esac

# On a machine with the /Volumes/models disk, weights live there and not on
# the boot volume: the rule local-llama-cpp follows. A value already set wins,
# even an empty one, which leaves Hugging Face its own default.
if [ -z "${HF_HOME+set}" ] && [ -d /Volumes/models/huggingface ]; then
  HF_HOME=/Volumes/models/huggingface
fi
if [ -n "${HF_HOME:-}" ]; then export HF_HOME; else unset HF_HOME; fi

LABEL_PREFIX="${SYSTEM_TOOLS_LABEL_PREFIX:-local.system-tools}"
LABEL="$LABEL_PREFIX.kev"
SRC_PLIST="$DIR/kev.plist.template"
AGENT_DIR="$HOME/Library/LaunchAgents"
DST_PLIST="$AGENT_DIR/$LABEL.plist"
DOMAIN="gui/$(id -u)"
SERVICE="$DOMAIN/$LABEL"
URL="http://127.0.0.1:$KEV_PORT"

is_loaded() { launchctl print "$SERVICE" >/dev/null 2>&1; }

wait_until_unloaded() {
  i=0
  while [ "$i" -lt 50 ]; do
    is_loaded || return 0
    sleep 0.2
    i=$((i + 1))
  done
  return 1
}

usage() {
  echo "usage: kev.sh install|serve|agent-install|agent-uninstall|status|test|help" >&2
}

require_checkout() {
  [ -f "$KEV_DIR/pyproject.toml" ] || {
    echo "kev: no checkout at $KEV_DIR; run kev.sh install" >&2
    exit 3
  }
}

cmd_install() {
  command -v uv >/dev/null 2>&1 || { echo "kev: uv is not on PATH" >&2; exit 1; }
  if [ -d "$KEV_DIR/.git" ]; then
    git -C "$KEV_DIR" pull --ff-only
  else
    mkdir -p "$(dirname "$KEV_DIR")"
    git clone "$KEV_REPO_URL" "$KEV_DIR"
  fi
  # Kev pins Python 3.13 in its .python-version: torch has no 3.14 wheels.
  (cd "$KEV_DIR" && uv sync --extra serve)
  echo "kev: installed at $KEV_DIR; the first serve downloads $KEV_MODEL"
}

cmd_serve() {
  require_checkout
  cd "$KEV_DIR"
  if [ -n "${KEV_API_KEY:-}" ]; then export KEV_API_KEY; else unset KEV_API_KEY; fi
  exec uv run --extra serve python -m kev.serve --run "$KEV_MODEL" \
    --host 127.0.0.1 --port "$KEV_PORT"
}

cmd_agent_install() {
  require_checkout
  mkdir -p "$AGENT_DIR" "$DIR/logs"
  # launchd passes the agent only the environment the plist names, so the
  # config root this install read goes in it, or `serve` would read another.
  sed -e "s|@LABEL@|$LABEL|g" -e "s|@DIR@|$DIR|g" -e "s|@HOME@|$HOME|g" \
    -e "s|@CONFIG@|$SYSTEM_TOOLS_CONFIG|g" \
    "$SRC_PLIST" > "$DST_PLIST"
  if is_loaded; then
    launchctl bootout "$SERVICE" 2>/dev/null || true
    wait_until_unloaded || { echo "kev: $LABEL is still loaded after bootout" >&2; exit 1; }
  fi
  attempt=1
  while :; do
    if launchctl bootstrap "$DOMAIN" "$DST_PLIST" 2>/dev/null; then break; fi
    if [ "$attempt" -ge 5 ]; then
      echo "kev: bootstrap failed for $DST_PLIST" >&2
      launchctl bootstrap "$DOMAIN" "$DST_PLIST"
      exit 1
    fi
    attempt=$((attempt + 1))
    sleep 0.5
  done
  launchctl enable "$SERVICE"
  launchctl kickstart "$SERVICE" >/dev/null
  echo "kev: started $LABEL ($URL, $KEV_MODEL)"
  echo "  plist: $DST_PLIST"
  echo "  log:   $DIR/logs/kev.err.log"
}

cmd_agent_uninstall() {
  if is_loaded; then
    launchctl bootout "$SERVICE" || true
    wait_until_unloaded || { echo "kev: $LABEL is still loaded" >&2; exit 1; }
  fi
  rm -f "$DST_PLIST"
  echo "kev: removed $LABEL"
}

cmd_status() {
  if [ ! -f "$KEV_DIR/pyproject.toml" ]; then
    echo "kev: not installed (no checkout at $KEV_DIR)"
    exit 3
  fi
  if [ -n "${KEV_API_KEY:-}" ]; then
    body=$(curl -s --max-time 5 -H "Authorization: Bearer $KEV_API_KEY" \
      -w '\n%{http_code}' "$URL/v1/models" 2>/dev/null) || code=$?
  else
    body=$(curl -s --max-time 5 -w '\n%{http_code}' "$URL/v1/models" 2>/dev/null) || code=$?
  fi
  case "${code:-0}" in
    0) ;;
    7)
      # Connection refused: nothing listens, which is nothing to check.
      echo "kev: not running on $URL"
      exit 3
      ;;
    28)
      # Something accepted the connection and never answered: a hung server
      # is a broken one, and local-health-check must hear about it.
      echo "kev: $URL did not answer within 5 seconds"
      exit 1
      ;;
    *)
      echo "kev: $URL failed (curl exit $code)"
      exit 1
      ;;
  esac
  status=$(printf '%s\n' "$body" | tail -n 1)
  if [ "$status" = 200 ] && printf '%s\n' "$body" | grep -q '"models"'; then
    echo "kev: ok  $URL  model=$KEV_MODEL"
    exit 0
  fi
  echo "kev: $URL answered HTTP $status without a model list"
  exit 1
}

case "${1:-}" in
  install)          cmd_install ;;
  serve)            cmd_serve ;;
  agent-install)    cmd_agent_install ;;
  agent-uninstall)  cmd_agent_uninstall ;;
  status)           cmd_status ;;
  test)
    shift
    exec "${PYTHON:-python3}" -m unittest discover -s "$DIR/tests" -t "$DIR" "$@"
    ;;
  -h|--help|help)
    cat <<HELPEOF
usage: kev.sh install|serve|agent-install|agent-uninstall|status|test|help

Kev is a small open-weights model with TypeSafe's System One API. This runs
Kev's own server on 127.0.0.1:$KEV_PORT, from the git-ignored checkout in $KEV_DIR.

  install          clone or fast-forward $KEV_DIR, then uv sync --extra serve
  serve            run the server in the foreground (what the agent runs);
                   the first run downloads $KEV_MODEL
  agent-install    install and start the LaunchAgent $LABEL
                   (RunAtLoad and KeepAlive: it starts at login)
  agent-uninstall  stop and remove the LaunchAgent
  status           GET /v1/models: exit 0 healthy, 3 not installed or not
                   running, 1 up and broken.
                   local-health-check reads these codes
  test             run the unittest suite in tests/

Defaults come from \$SYSTEM_TOOLS_CONFIG/kev/.env (copy .env.example there);
an exported value wins: KEV_MODEL, KEV_PORT, KEV_DIR, KEV_REPO_URL,
KEV_API_KEY (optional bearer token), HF_HOME, SYSTEM_TOOLS_LABEL_PREFIX.
HELPEOF
    exit 0
    ;;
  "")
    usage
    exit 1
    ;;
  *)
    usage
    exit 1
    ;;
esac

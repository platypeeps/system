#!/bin/sh
# Mezmo aura: install, run the web server (brew or repo build), CLI, and
# quick API smoke tests, and trace experiments. Server listens on :3033.
# Every server and experiment exports its traces to local-genai-traces.
# Usage: aura.sh install|server|server-repo|cli|health|models|prompt [text]
#        aura.sh image | experiment start|stop|run|inspect|ps
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

# Every Aura run on this machine exports its traces to local-genai-traces,
# unless the caller set its own endpoint or AURA_TRACES=0.
GENAI_TRACES_GRPC_PORT="${GENAI_TRACES_GRPC_PORT:-4337}"
traces_env() {
  case "${AURA_TRACES:-1}" in 0|off|false|no|disabled) return 0 ;; esac
  # Content recording follows the local default only. A caller's own
  # endpoint may leave the machine, so its recording stays the caller's call
  # (Aura's default is off).
  if [ -z "${OTEL_EXPORTER_OTLP_ENDPOINT:-}" ]; then
    OTEL_EXPORTER_OTLP_ENDPOINT="http://127.0.0.1:$GENAI_TRACES_GRPC_PORT"
    OTEL_RECORD_CONTENT="${OTEL_RECORD_CONTENT:-true}"
    export OTEL_RECORD_CONTENT
  fi
  OTEL_SERVICE_NAME="${OTEL_SERVICE_NAME:-aura}"
  export OTEL_EXPORTER_OTLP_ENDPOINT OTEL_SERVICE_NAME
}

# The experiment runs only this locally built image, never the published one,
# so every experiment measures the same instrumented build. `image` builds it.
AURA_IMAGE="${AURA_IMAGE:-aura-local:instrumented}"
EXP="$DIR/experiment"
EXAMPLE=examples/quickstart-orchestration-math

need_repo() {
  if [ -z "${AURA_REPO:-}" ]; then
    st_missing AURA_REPO aura .env
    exit 1
  fi
  if [ ! -d "$AURA_REPO/$EXAMPLE" ]; then
    echo "aura.sh: $AURA_REPO/$EXAMPLE not found; AURA_REPO must be an aura checkout" >&2
    exit 1
  fi
}

# The experiment config reads its model through {{ env.LLM_* }} templates.
need_llm() {
  for var in LLM_PROVIDER LLM_MODEL LLM_API_KEY; do
    eval "value=\${$var:-}"
    if [ -z "$value" ]; then
      st_missing "$var" aura .env
      exit 1
    fi
  done
}

# Writes the two server configs from the checkout's math example: the model
# from the environment, and orchestration off for the single-agent server.
render_configs() {
  mkdir -p "$EXP/state"
  sed -e 's/^provider = .*/provider = "{{ env.LLM_PROVIDER }}"/' \
      -e 's/^api_key = .*/api_key = "{{ env.LLM_API_KEY }}"/' \
      -e 's/^model = .*/model = "{{ env.LLM_MODEL }}"/' \
      "$AURA_REPO/$EXAMPLE/config.toml" > "$EXP/state/config-orch.toml"
  sed -e '/^\[orchestration\]/,/^enabled/ s/^enabled = true/enabled = false/' \
      "$EXP/state/config-orch.toml" > "$EXP/state/config-single.toml"
  # An upstream change to the example would leave a config that silently
  # runs another model or mode; refuse it instead.
  grep -q '^api_key = "{{ env.LLM_API_KEY }}"' "$EXP/state/config-orch.toml" &&
    grep -q '^enabled = false' "$EXP/state/config-single.toml" || {
    echo "aura.sh: $AURA_REPO/$EXAMPLE/config.toml changed shape; update render_configs" >&2
    exit 1
  }
}

experiment_env() {
  export AURA_REPO AURA_IMAGE
  export AURA_ENV_FILE="$(st_config_dir aura)/.env"
  export AURA_EXPERIMENT_STATE="$EXP/state"
  export AURA_ORCH_PORT="${AURA_ORCH_PORT:-3101}"
  export AURA_SINGLE_PORT="${AURA_SINGLE_PORT:-3102}"
  export AURA_OTLP_ENDPOINT="${AURA_OTLP_ENDPOINT:-http://host.docker.internal:$GENAI_TRACES_GRPC_PORT}"
}

experiment_compose() {
  docker compose -f "$EXP/docker-compose.yml" "$@"
}

case "$1" in
  install)
    brew install mezmo/tap/aura
    brew install mezmo/tap/aura-web-server
    ;;
  server)
    need_keys
    traces_env
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
    traces_env
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
  image)
    need_repo
    rev="$(git -C "$AURA_REPO" rev-parse --abbrev-ref HEAD)@$(git -C "$AURA_REPO" rev-parse --short HEAD)"
    if [ -n "$(git -C "$AURA_REPO" status --porcelain --untracked-files=no)" ]; then
      rev="$rev+dirty"
    fi
    docker build --target release -t "$AURA_IMAGE" --label "aura.source=$rev" "$AURA_REPO"
    ;;
  experiment)
    case "${2:-}" in
      start)
        need_repo
        need_llm
        if ! docker image inspect "$AURA_IMAGE" >/dev/null 2>&1; then
          echo "aura.sh: image $AURA_IMAGE not found; build it with: aura.sh image" >&2
          exit 1
        fi
        if ! sh "$DIR/../local-genai-traces/genai-traces.sh" status >/dev/null 2>&1; then
          echo "aura.sh: local-genai-traces is not healthy; run local-genai-traces/genai-traces.sh start" >&2
          exit 1
        fi
        render_configs
        experiment_env
        experiment_compose up -d --build --wait
        echo "image $AURA_IMAGE ($(docker image inspect -f '{{ index .Config.Labels "aura.source" }}' "$AURA_IMAGE"))"
        ;;
      stop)
        experiment_env
        experiment_compose down
        ;;
      run)
        experiment_env
        exec sh "$EXP/scenarios.sh"
        ;;
      inspect)
        exec python3 "$EXP/inspect.py" "${3:-summary}" "$DIR/../local-genai-traces/storage/raw"
        ;;
      ps)
        experiment_env
        experiment_compose ps
        ;;
      *)
        echo "usage: $(basename "$0") experiment start|stop|run|inspect [summary|tree]|ps" >&2
        exit 1
        ;;
    esac
    ;;
  test)
    shift
    exec "${PYTHON:-python3}" -m unittest discover -s "$DIR/tests" -t "$DIR" "$@"
    ;;
  -h|--help|help)
    cat <<'HELPEOF'
usage: aura.sh install|server|server-repo|cli|health|models|prompt [text]
       aura.sh image | experiment start|stop|run|inspect|ps

  install      brew install aura + aura-web-server from mezmo/tap
  server       run aura-web-server on :3033 with config.toml
  server-repo  build and run the server from the $AURA_REPO checkout
               (cargo), output tee'd to aura-output.txt
  cli          interactive aura CLI against the local server
  health       GET /health on the running server
  models       GET /v1/models
  prompt       send one chat completion ("Hello" if no text given)
  image        build $AURA_IMAGE (default aura-local:instrumented) from the
               $AURA_REPO checkout, labelled with its branch and commit
  experiment start   run the math MCP server and two Aura servers
                     (orchestration on 127.0.0.1:3101, single agent on :3102)
                     from $AURA_IMAGE; refuses without the image or a healthy
                     local-genai-traces
  experiment run     send the fixed scenario requests
  experiment inspect [summary|tree]  summarise the aura spans in
                     local-genai-traces' raw file
  experiment stop|ps stop the experiment, or list its containers
  test               run this folder's tests

traces: server, server-repo and the experiment export OTLP to
  local-genai-traces (127.0.0.1:4337) with content recorded. An exported
  OTEL_EXPORTER_OTLP_ENDPOINT wins and leaves recording to the caller;
  AURA_TRACES=0 turns the default off.

config:
  <config>/aura/.env         OPENAI_API_KEY, MEZMO_API_KEY, AURA_REPO, and
                             LLM_PROVIDER, LLM_MODEL, LLM_API_KEY for the
                             experiment; copy .env.example. Exported values win.
  <config>/aura/config.toml  replaces config.toml beside this script.
  <config> is $SYSTEM_TOOLS_CONFIG, default ~/.config/system.
  CONFIG_PATH                one server config for this run only.
HELPEOF
    exit 0
    ;;
  *)
    echo "usage: $(basename "$0") install|server|server-repo|cli|health|models|prompt [text]|image|experiment|test" >&2
    exit 1
    ;;
esac

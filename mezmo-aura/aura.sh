#!/bin/sh
# Mezmo aura: build and install from a checkout, run the web server
# (`aura webserver`), CLI, quick API smoke tests, and trace experiments.
# Server listens on :3033.
# Every server and experiment exports its traces to local-genai-traces.
# Usage: aura.sh install|server|server-repo|cli|health|models|prompt [text]
#        aura.sh image | experiment start|stop|run|inspect|ps
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
. "$DIR/../lib/config.sh"

# The config folder is <config>/mezmo-aura/: convention 3 keeps a mezmo-*
# folder's full name. Until sd:2539 this tool was local-aura and read
# <config>/aura/. Nothing here moves the operator's files: a machine with only
# the old folder is told the command that does, and help and test still answer.
TOOL=mezmo-aura
CONF="$(st_config_dir "$TOOL")"
OLD_CONF="$(st_config_dir aura)"
case "${1:-}" in
  -h|--help|help|test|'') ;;
  *)
    if [ -d "$OLD_CONF" ] && [ ! -d "$CONF" ]; then
      echo "aura.sh: the config folder moved from $OLD_CONF to $CONF (sd:2539), and the old one is not read." >&2
      echo "  Move it: mv $OLD_CONF $CONF" >&2
      exit 1
    elif [ -d "$OLD_CONF" ]; then
      echo "aura.sh: $OLD_CONF is no longer read; $CONF is. Remove the old folder once nothing in it is needed." >&2
    fi ;;
esac

# <config>/mezmo-aura/.env provides the API keys config.toml templates in, the
# checkout `server-repo` and `image` build, and the experiment's settings;
# values already exported win over every one of them.
st_source_env "$TOOL"
[ -n "${OPENAI_API_KEY:-}" ] && export OPENAI_API_KEY
[ -n "${MEZMO_SERVICE_KEY:-}" ] && export MEZMO_SERVICE_KEY

# A config.toml in <config>/mezmo-aura/ replaces the one beside this script.
if [ -f "$CONF/config.toml" ]; then
  DEFAULT_CONFIG="$CONF/config.toml"
else
  DEFAULT_CONFIG="$DIR/config.toml"
fi

# The server cannot start without either key, so say which is missing first.
need_keys() {
  for var in OPENAI_API_KEY MEZMO_SERVICE_KEY; do
    eval "value=\${$var:-}"
    if [ -z "$value" ]; then
      st_missing "$var" "$TOOL" .env "$TOOL"
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
  # Spans only. Prompts and answers stay out unless the caller exports
  # OTEL_RECORD_CONTENT=true (Aura's default is off): a server may carry real
  # work, and the collector may forward. The experiment turns it on itself.
  OTEL_EXPORTER_OTLP_ENDPOINT="${OTEL_EXPORTER_OTLP_ENDPOINT:-http://127.0.0.1:$GENAI_TRACES_GRPC_PORT}"
  OTEL_SERVICE_NAME="${OTEL_SERVICE_NAME:-aura}"
  export OTEL_EXPORTER_OTLP_ENDPOINT OTEL_SERVICE_NAME
}

# The experiment runs only this locally built image, never the published one,
# so every experiment measures the same instrumented build. `image` builds it.
AURA_IMAGE="${AURA_IMAGE:-aura-local:instrumented}"
EXP="$DIR/experiment"
EXAMPLE=examples/quickstart-orchestration-math

# install and server-repo build from the checkout; they need no example.
need_checkout() {
  if [ -z "${AURA_REPO:-}" ]; then
    st_missing AURA_REPO "$TOOL" .env "$TOOL"
    exit 1
  fi
  if [ ! -f "$AURA_REPO/Cargo.toml" ]; then
    echo "aura.sh: $AURA_REPO/Cargo.toml not found; AURA_REPO must be an aura checkout" >&2
    exit 1
  fi
}

need_repo() {
  if [ -z "${AURA_REPO:-}" ]; then
    st_missing AURA_REPO "$TOOL" .env "$TOOL"
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
      st_missing "$var" "$TOOL" .env "$TOOL"
      exit 1
    fi
  done
}

# Writes the two server configs from the checkout's math example: the model
# from the environment, the MCP servers of an --overlay file, and
# orchestration off for the single-agent server.
render_configs() {
  mkdir -p "$EXP/state"
  sed -e 's/^provider = .*/provider = "{{ env.LLM_PROVIDER }}"/' \
      -e 's/^api_key = .*/api_key = "{{ env.LLM_API_KEY }}"/' \
      -e 's/^model = .*/model = "{{ env.LLM_MODEL }}"/' \
      "$AURA_REPO/$EXAMPLE/config.toml" > "$EXP/state/config-orch.toml"
  [ -z "$OVERLAY" ] || apply_overlay "$OVERLAY" "$EXP/state/config-orch.toml"
  sed -e '/^\[orchestration\]/,/^enabled/ s/^enabled = true/enabled = false/' \
      "$EXP/state/config-orch.toml" > "$EXP/state/config-single.toml"
  # An upstream change to the example would leave a config that silently
  # runs another model or mode; refuse it instead.
  grep -q '^provider = "{{ env.LLM_PROVIDER }}"' "$EXP/state/config-orch.toml" &&
    grep -q '^api_key = "{{ env.LLM_API_KEY }}"' "$EXP/state/config-orch.toml" &&
    grep -q '^model = "{{ env.LLM_MODEL }}"' "$EXP/state/config-orch.toml" &&
    grep -q '^enabled = false' "$EXP/state/config-single.toml" || {
    echo "aura.sh: $AURA_REPO/$EXAMPLE/config.toml changed shape; update render_configs" >&2
    exit 1
  }
}

# apply_overlay <overlay.toml> <config>: each [mcp.servers.<name>] and
# [orchestration.worker.<name>] table in the overlay replaces the config's
# table of that name, sub-tables included, or adds it. The overlay may hold
# nothing else, and the merged config must parse to exactly that, or the
# config is left as it was and start refuses.
# A key belongs in the overlay as {{ env.MEZMO_SERVICE_KEY }}: its value would
# land in experiment/state/, so an overlay holding it is refused.
apply_overlay() {
  python3 - "$1" "$2" <<'PY_OVERLAY'
import os, re, sys, tomllib

overlay_path, config_path = sys.argv[1:3]

def refuse(message):
    print(f"aura.sh: overlay {overlay_path}: {message}", file=sys.stderr)
    sys.exit(1)

try:
    with open(overlay_path, encoding="utf-8") as f:
        text = f.read()
    overlay = tomllib.loads(text)
except (OSError, tomllib.TOMLDecodeError) as e:
    refuse(str(e))
# Workers carry their own mcp_filter, so a server reaches the orchestration
# server's workers only through a worker table (sd:5077).
servers = overlay.get("mcp", {}).get("servers", {})
workers = overlay.get("orchestration", {}).get("worker", {})
shape = {"mcp": {"servers"}, "orchestration": {"worker"}}
if (not set(overlay) <= set(shape)
        or any(not isinstance(overlay[k], dict) or not set(overlay[k]) <= shape[k] for k in overlay)
        or not isinstance(servers, dict) or not isinstance(workers, dict)
        or not all(isinstance(v, dict) for v in (*servers.values(), *workers.values()))
        or not (servers or workers)):
    refuse("may hold only [mcp.servers.<name>] and [orchestration.worker.<name>] tables")
key = os.environ.get("MEZMO_SERVICE_KEY")
if key and key in text:
    refuse("holds the value of MEZMO_SERVICE_KEY; write {{ env.MEZMO_SERVICE_KEY }} instead")

with open(config_path, encoding="utf-8") as f:
    base_text = f.read()
base = tomllib.loads(base_text)
kept, dropping = [], False
for line in base_text.splitlines(keepends=True):
    header = re.match(r"\s*\[+\s*([^\]]*)", line)
    if header:
        parts = header.group(1).strip().split(".")
        dropping = len(parts) > 2 and (parts[:2] == ["mcp", "servers"] and parts[2] in servers
                                       or parts[:2] == ["orchestration", "worker"] and parts[2] in workers)
    if not dropping:
        kept.append(line)
merged_text = "".join(kept).rstrip("\n") + "\n\n# From the experiment start overlay.\n" + text
expected = dict(base)
for table, sub, adds in (("mcp", "servers", servers), ("orchestration", "worker", workers)):
    if adds:
        expected[table] = dict(base.get(table, {}))
        expected[table][sub] = {**expected[table].get(sub, {}), **adds}
try:
    merged = tomllib.loads(merged_text)
except tomllib.TOMLDecodeError as e:
    refuse(f"the merged config does not parse: {e}")
if merged != expected:
    refuse("the merged config is not the example plus these tables; update apply_overlay")
with open(config_path, "w", encoding="utf-8") as f:
    f.write(merged_text)
PY_OVERLAY
}

experiment_env() {
  # Compose passes these into the servers by name, so an exported-only setup
  # with no .env works, and no other key in .env reaches the containers.
  export AURA_REPO AURA_IMAGE LLM_PROVIDER LLM_MODEL LLM_API_KEY
  export AURA_EXPERIMENT_STATE="$EXP/state"
  export AURA_ORCH_PORT="${AURA_ORCH_PORT:-3101}"
  export AURA_SINGLE_PORT="${AURA_SINGLE_PORT:-3102}"
  export AURA_OTLP_ENDPOINT="${AURA_OTLP_ENDPOINT:-http://host.docker.internal:$GENAI_TRACES_GRPC_PORT}"
}

# experiment_option <--flag> experiment <verb> [options]: sets OVERLAY for
# --overlay, AURA_PROMPTS for --prompts, to the file's absolute path;
# TARGET_ARG for --target <name>; ALLOW_PROD for --allow-prod. Anything else,
# or a file that is not there, refuses before any work starts.
experiment_option() {
  _flag=$1
  shift 3
  TARGET_ARG="" TARGET_SET=0 ALLOW_PROD=0
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --allow-prod) ALLOW_PROD=1; shift; continue ;;
      --target|"$_flag")
        if [ "$#" -lt 2 ]; then
          echo "aura.sh: experiment: $1 needs a value" >&2
          exit 1
        fi ;;
      *)
        echo "aura.sh: experiment: unknown argument: $1; the options here are $_flag <file>, --target <name> and --allow-prod" >&2
        exit 1 ;;
    esac
    if [ "$1" = --target ]; then
      TARGET_ARG=$2 TARGET_SET=1
      shift 2
      continue
    fi
    if [ ! -f "$2" ] || [ ! -r "$2" ]; then
      echo "aura.sh: $_flag $2: no readable file there" >&2
      exit 1
    fi
    _file="$(cd "$(dirname "$2")" && pwd)/$(basename "$2")"
    case "$_flag" in
      --overlay) OVERLAY=$_file ;;
      --prompts) AURA_PROMPTS=$_file ;;
    esac
    shift 2
  done
}

# The Mezmo target the experiment runs against (sd:5073): --target, else
# AURA_MEZMO_TARGET (from .env or exported), else dev. It is never the
# shell's MEZMO_TARGET, whose default may be production: every MEZMO_*
# variable the caller exported is dropped, and the target's own values from
# AURA_MEZMO_<NAME>_SERVICE_KEY and AURA_MEZMO_<NAME>_MCP_URL (the name upper
# case, - as _) are exported as MEZMO_SERVICE_KEY and MEZMO_MCP_URL, for
# Compose to pass on by name. A name holding "prod" needs --allow-prod.
experiment_target() {
  if [ "$TARGET_SET" -eq 1 ]; then
    _target=$TARGET_ARG
  else
    _target=${AURA_MEZMO_TARGET:-dev}
  fi
  case "$_target" in
    ''|[!A-Za-z0-9]*|*[!A-Za-z0-9_-]*)
      echo "aura.sh: experiment: target '$_target' is not a name (letters, digits, - and _); set --target or AURA_MEZMO_TARGET" >&2
      exit 1 ;;
  esac
  case "$(printf '%s' "$_target" | tr '[:upper:]' '[:lower:]')" in
    *prod*)
      if [ "$ALLOW_PROD" -ne 1 ]; then
        echo "aura.sh: experiment: target $_target looks like production; add --allow-prod to run against it" >&2
        exit 1
      fi ;;
  esac
  for _var in $(env | sed -n 's/^\(MEZMO_[A-Za-z0-9_]*\)=.*/\1/p'); do
    unset "$_var"
  done
  TARGET_VARS="AURA_MEZMO_$(printf '%s' "$_target" | tr '[:lower:]-' '[:upper:]_')"
  eval "MEZMO_SERVICE_KEY=\${${TARGET_VARS}_SERVICE_KEY:-}"
  eval "MEZMO_MCP_URL=\${${TARGET_VARS}_MCP_URL:-}"
  MEZMO_TARGET=$_target
  export MEZMO_TARGET
  [ -z "$MEZMO_SERVICE_KEY" ] || export MEZMO_SERVICE_KEY
  [ -z "$MEZMO_MCP_URL" ] || export MEZMO_MCP_URL
}

# An overlay that reads a target value the target does not have would start
# servers whose config cannot load; name the variable instead.
need_overlay_values() {
  [ -n "$OVERLAY" ] || return 0
  for _var in SERVICE_KEY MCP_URL; do
    eval "_value=\${MEZMO_$_var:-}"
    if [ -z "$_value" ] && grep -Eq "\\{\\{ *env\\.MEZMO_$_var *(\\||\\}\\})" "$OVERLAY"; then
      st_missing "${TARGET_VARS}_$_var" "$TOOL" .env "$TOOL"
      exit 1
    fi
  done
}

experiment_compose() {
  docker compose -f "$EXP/docker-compose.yml" "$@"
}

case "$1" in
  install)
    # Built from the checkout, not Homebrew: the tap lags the nightlies.
    # Each version keeps its own folder, so the symlink can go back.
    need_checkout
    version="$(git -C "$AURA_REPO" describe --tags --always --dirty)"
    commit="$(git -C "$AURA_REPO" rev-parse HEAD)"
    (cd "$AURA_REPO" && cargo build --release --bin aura)
    dest="$HOME/.local/opt/aura/$version"
    mkdir -p "$dest" "$HOME/.local/bin"
    # Copy then rename, so a running aura keeps its old file.
    cp "$AURA_REPO/target/release/aura" "$dest/aura.new"
    chmod 755 "$dest/aura.new"
    mv -f "$dest/aura.new" "$dest/aura"
    printf '%s %s\n' "$version" "$commit" > "$dest/SOURCE"
    ln -sfn "$dest/aura" "$HOME/.local/bin/aura"
    echo "installed aura $version ($commit) at $dest; ~/.local/bin/aura links to it"
    ;;
  server)
    need_keys
    traces_env
    HOST="${HOST:-0.0.0.0}"
    CONFIG_PATH="${CONFIG_PATH:-$DEFAULT_CONFIG}"
    exec aura webserver --config "$CONFIG_PATH" --host "$HOST" --port "$PORT"
    ;;
  server-repo)
    need_keys
    need_checkout
    traces_env
    export HOST=0.0.0.0
    export PORT="$PORT"
    export AURA_CUSTOM_EVENTS=true
    export AURA_EMIT_REASONING=true
    cd "$AURA_REPO"
    cargo run --bin aura -- webserver \
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
        OVERLAY=""
        experiment_option --overlay "$@"
        experiment_target
        need_overlay_values
        need_repo
        need_llm
        if ! source_label="$(docker image inspect -f '{{ index .Config.Labels "aura.source" }}' "$AURA_IMAGE" 2>/dev/null)"; then
          echo "aura.sh: image $AURA_IMAGE not found; build it with: aura.sh image" >&2
          exit 1
        fi
        # A tag alone may name any image; only `image` writes this label.
        case "$source_label" in
          ''|'<no value>')
            echo "aura.sh: image $AURA_IMAGE has no aura.source label, so it was not built by aura.sh image; rebuild it with: aura.sh image" >&2
            exit 1
            ;;
        esac
        if ! sh "$DIR/../local-genai-traces/genai-traces.sh" status >/dev/null 2>&1; then
          echo "aura.sh: local-genai-traces is not healthy; run local-genai-traces/genai-traces.sh start" >&2
          exit 1
        fi
        render_configs
        # What `run` checks its target against.
        printf '%s\n' "$MEZMO_TARGET" > "$EXP/state/target"
        experiment_env
        experiment_compose up -d --build --wait
        echo "image $AURA_IMAGE ($source_label)"
        ;;
      stop)
        experiment_env
        experiment_compose down
        ;;
      run)
        AURA_PROMPTS=""
        experiment_option --prompts "$@"
        # The servers run against the target start recorded; a run names it,
        # and refuses another one or none.
        if ! started=$(cat "$EXP/state/target" 2>/dev/null) || [ -z "$started" ]; then
          echo "aura.sh: experiment run: no target recorded; start the servers with: aura.sh experiment start" >&2
          exit 1
        fi
        [ "$TARGET_SET" -eq 1 ] || { TARGET_ARG=$started; TARGET_SET=1; }
        experiment_target
        if [ "$MEZMO_TARGET" != "$started" ]; then
          echo "aura.sh: experiment run: the servers run against $started, not $MEZMO_TARGET; restart with: aura.sh experiment start --target $MEZMO_TARGET" >&2
          exit 1
        fi
        experiment_env
        export AURA_PROMPTS
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
        echo "usage: $(basename "$0") experiment start [--overlay <file>] [--target <name>] [--allow-prod]|stop|run [--prompts <file>] [--target <name>] [--allow-prod]|inspect [summary|tree]|ps" >&2
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

  install      build aura from the $AURA_REPO checkout (cargo --release),
               copy it to ~/.local/opt/aura/<git describe>/ with a SOURCE
               file naming the commit, and link ~/.local/bin/aura to it
  server       run `aura webserver` on :3033 with config.toml
  server-repo  build and run `aura webserver` from the $AURA_REPO checkout
               (cargo run), output tee'd to aura-output.txt
  cli          interactive aura CLI against the local server
  health       GET /health on the running server
  models       GET /v1/models
  prompt       send one chat completion ("Hello" if no text given)
  image        build $AURA_IMAGE (default aura-local:instrumented) from the
               $AURA_REPO checkout, labelled with its branch and commit
  experiment start [--overlay <file.toml>] [--target <name>] [--allow-prod]
                     run the math MCP server and two Aura servers
                     (orchestration on 127.0.0.1:3101, single agent on :3102)
                     from $AURA_IMAGE; refuses without the image or a healthy
                     local-genai-traces. Each [mcp.servers.<name>] and
                     [orchestration.worker.<name>] table of the overlay
                     replaces that server or worker or adds it (a worker's
                     mcp_filter decides which tools it sees); the overlay may
                     hold nothing else, and names the key as
                     {{ env.MEZMO_SERVICE_KEY }}, never its value.
                     The Mezmo target is --target, else AURA_MEZMO_TARGET,
                     else dev, never the shell's MEZMO_TARGET; a name holding
                     "prod" needs --allow-prod
  experiment run [--prompts <file>] [--target <name>] [--allow-prod]
                     send the six math requests, or each line of the file to
                     both servers (blank lines and # lines skipped); refuses a
                     target other than the one start recorded. Exits 1 when a
                     request fails or a reply is a provider error, naming the
                     case
  experiment inspect [summary|tree]  summarise the aura spans in
                     local-genai-traces' raw file
  experiment stop|ps stop the experiment, or list its containers
  test               run this folder's tests

traces: server, server-repo and the experiment export OTLP to
  local-genai-traces (127.0.0.1:4337); an exported OTEL_EXPORTER_OTLP_ENDPOINT
  wins and AURA_TRACES=0 turns the default off. Only the experiment records
  prompts and answers; for a server, export OTEL_RECORD_CONTENT=true.

config:
  <config>/mezmo-aura/.env         OPENAI_API_KEY, MEZMO_SERVICE_KEY, AURA_REPO, and
                                   LLM_PROVIDER, LLM_MODEL, LLM_API_KEY for the
                                   experiment; copy .env.example. Exported
                                   values win.
                                   AURA_MEZMO_TARGET names the experiment's
                                   Mezmo target; AURA_MEZMO_<NAME>_SERVICE_KEY
                                   and AURA_MEZMO_<NAME>_MCP_URL (the name upper
                                   case, - as _) hold its values. The
                                   experiment drops every exported MEZMO_*
                                   value and passes the target's to its
                                   servers by name only, as MEZMO_TARGET,
                                   MEZMO_SERVICE_KEY and MEZMO_MCP_URL.
  <config>/mezmo-aura/config.toml  replaces config.toml beside this script.
  <config> is $SYSTEM_TOOLS_CONFIG, default ~/.config/system.
  <config>/aura/ is the folder before sd:2539: with only it present, every
  verb but help and test stops and names the mv that moves it.
  CONFIG_PATH                one server config for this run only.
HELPEOF
    exit 0
    ;;
  *)
    echo "usage: $(basename "$0") install|server|server-repo|cli|health|models|prompt [text]|image|experiment|test" >&2
    exit 1
    ;;
esac

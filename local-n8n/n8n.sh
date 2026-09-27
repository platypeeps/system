#!/bin/sh
# n8n (npx, sqlite backend) plus external task-runner container, run as a
# macOS LaunchAgent that starts at login, restarts if it dies, and logs to
# ./logs — same pattern as local-cswap.
# Secrets (encryption key, runner token) come from <config>/n8n/.env
# (lib/config.sh; see .env.example) or from the environment — the file is optional
# when the values are already exported. Needs N8N_PUBLIC_HOST for its URLs.
# Usage: n8n.sh run|start|stop|status|update
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=../lib/config.sh
. "$DIR/../lib/config.sh"

# launchd label prefix shared by every agent from this repository; override per machine.
LABEL_PREFIX="${SYSTEM_TOOLS_LABEL_PREFIX:-local.system-tools}"
LABEL="$LABEL_PREFIX.n8n"

RUNNER_PORT="${N8N_RUNNER_PORT:-5680}"
PLIST_TEMPLATE="$DIR/n8n.plist.template"
AGENT_DIR="$HOME/Library/LaunchAgents"
DST_PLIST="$AGENT_DIR/$LABEL.plist"
DOMAIN="gui/$(id -u)"
SERVICE="$DOMAIN/$LABEL"
OUT_LOG="$DIR/logs/n8n.log"
ERR_LOG="$DIR/logs/n8n.err.log"

is_loaded() { launchctl print "$SERVICE" >/dev/null 2>&1; }

# `launchctl bootout` returns before launchd has finished tearing the job
# down, and bootstrapping during that window fails with "Input/output error".
wait_until_unloaded() {
  i=0
  while [ "$i" -lt 50 ]; do
    is_loaded || return 0
    sleep 0.2
    i=$((i + 1))
  done
  return 1
}

cmd_run() {
  st_source_env n8n
  MISSING=""
  for v in N8N_ENCRYPTION_KEY N8N_RUNNERS_AUTH_TOKEN; do
    eval "val=\${$v:-}"
    [ -n "$val" ] || MISSING="$MISSING $v"
  done
  if [ -n "$MISSING" ]; then
    for v in $MISSING; do st_missing "$v" n8n .env; done
    exit 78 # EX_CONFIG - do not hot-loop on a broken install
  fi

  export N8N_HOST=localhost
  export N8N_PORT=5678
  export N8N_PROTOCOL=https
  export N8N_PAYLOAD_SIZE_MAX=511

  # No N8N_BASIC_AUTH_*: those were removed in n8n 1.x and are ignored by
  # the version we run (grep the install tree: 0 references, vs 4 for
  # N8N_ENCRYPTION_KEY). Access control is n8n's own owner account.

  export N8N_SSL_CERT=~/.ssh/ssl/n8n_self_signed.pem
  export N8N_SSL_KEY=~/.ssh/ssl/n8n_self_signed.key

  export N8N_ENFORCE_SETTINGS_FILE_PERMISSIONS=true
  export N8N_GIT_NODE_DISABLE_BARE_REPOS=true
  export N8N_BLOCK_ENV_ACCESS_IN_NODE=true

  # Hardening for a publicly-funnelled instance. All four are live in the
  # pinned build (grep the install tree for refs; N8N_BASIC_AUTH_* is 0).
  # MFA matters most: without it the owner password is the only gate.
  export N8N_PUBLIC_API_DISABLED=true
  export N8N_SECURE_COOKIE=true
  export N8N_MFA_ENABLED=true

  # Webhook path prefix. Default `webhook` keeps existing workflow URLs
  # working; set N8N_ENDPOINT_WEBHOOK to something unguessable in the config .env to
  # make path-guessing useless -- that changes every webhook URL, so any
  # already registered with a third party has to be re-registered.
  export N8N_ENDPOINT_WEBHOOK="${N8N_ENDPOINT_WEBHOOK:-webhook}"

  export N8N_RUNNERS_MODE=external
  export N8N_RUNNERS_BROKER_LISTEN_ADDRESS=0.0.0.0

  # Public host, for webhook URLs only. Only the webhook path is funnelled
  # to the internet; the editor and the REST API stay on the tailnet, so
  # the two base URLs are deliberately different. Pointing the editor at
  # the public host would break the UI, since that host serves no /rest.
  if [ -z "${N8N_PUBLIC_HOST:-}" ]; then
    st_missing N8N_PUBLIC_HOST n8n .env
    echo "  (public host[:port] for webhook URLs — see README)" >&2
    exit 78
  fi
  export WEBHOOK_URL="https://$N8N_PUBLIC_HOST/"

  # Editor/REST base. Tailnet or loopback -- never the public funnel host.
  export N8N_EDITOR_BASE_URL="https://${N8N_EDITOR_HOST:-localhost:5678}/"

  export NODE_EXTRA_CA_CERTS=~/.ssh/ssl/n8n_self_signed.pem

  export DB_TYPE=sqlite
  export DB_SQLITE_LOCATION=~/.n8n/database.sqlite
  export DB_SQLITE_POOL_SIZE=4

  # A leftover container from a crashed run would make the named `docker run`
  # fail forever under KeepAlive, so clear it first; stop the runner again on
  # the way out so n8n and its runner live and die together.
  docker rm -f n8n-task-runner >/dev/null 2>&1 || true
  # Loopback: the runner only ever talks to n8n on this same host.
  docker run -d --rm --name n8n-task-runner -p 127.0.0.1:"$RUNNER_PORT":5680 n8nio/runners:latest
  trap 'docker stop n8n-task-runner >/dev/null 2>&1 || true' EXIT INT TERM

  # fnm needs its env eval'd in non-interactive sh; version comes from the
  # tracked .node-version instead of being duplicated here.
  cd "$DIR"
  eval "$(fnm env)"
  fnm use
  # Pinned: 2.15.1 is the version that wrote ~/.n8n/database.sqlite -- it
  # already contains the DB's newest migration, so a run migrates nothing.
  # Unpinned `npx n8n` pulls latest and would migrate every workflow and
  # credential in place. Upgrading is a deliberate step: back the DB
  # up first, then bump this pin.
  npx n8n@2.15.1
}

# Escape a value for the replacement side of a sed s|||.
sed_escape() {
  printf '%s' "$1" | sed -e 's/[&|\\]/\\&/g'
}

# Render the committed template with this machine's label, folder and home.
render_plist() {
  sed -e "s|@LABEL@|$(sed_escape "$LABEL")|g" \
      -e "s|@DIR@|$(sed_escape "$DIR")|g" \
      -e "s|@HOME@|$(sed_escape "$HOME")|g" \
      -e "s|@CONFIG@|$(sed_escape "$SYSTEM_TOOLS_CONFIG")|g" \
      "$PLIST_TEMPLATE"
}

cmd_start() {
  mkdir -p "$AGENT_DIR"
  render_plist >"$DST_PLIST"

  # Replace any previous registration so an edited plist actually takes effect.
  if is_loaded; then
    launchctl bootout "$SERVICE" 2>/dev/null || true
    wait_until_unloaded || { echo "ERROR: $LABEL is still loaded after bootout" >&2; exit 1; }
  fi

  # Retry: launchd can still report the old job as in-flight for a moment.
  attempt=1
  while :; do
    if launchctl bootstrap "$DOMAIN" "$DST_PLIST" 2>/dev/null; then
      break
    fi
    if [ "$attempt" -ge 5 ]; then
      echo "ERROR: bootstrap failed for $DST_PLIST" >&2
      launchctl bootstrap "$DOMAIN" "$DST_PLIST" # re-run to surface the error
      exit 1
    fi
    attempt=$((attempt + 1))
    sleep 0.5
  done
  launchctl enable "$SERVICE"
  launchctl kickstart "$SERVICE" >/dev/null

  echo "started: $LABEL"
  echo "  plist: $DST_PLIST"
  echo "  log:   $OUT_LOG"
}

cmd_stop() {
  # The trap in cmd_run stops the runner container when n8n exits, but cover
  # a manually started or orphaned one too.
  if is_loaded; then
    launchctl bootout "$SERVICE" || true
    wait_until_unloaded || { echo "ERROR: $LABEL is still loaded" >&2; exit 1; }
    rm -f "$DST_PLIST"
    echo "stopped and unloaded: $LABEL"
  else
    echo "not loaded: $LABEL"
  fi
  docker stop n8n-task-runner 2>/dev/null || true
}

cmd_status() {
  tail_lines="${1:-15}"

  echo "label:     $LABEL"
  if [ -f "$DST_PLIST" ]; then
    echo "plist:     $DST_PLIST (installed)"
  else
    echo "plist:     $DST_PLIST (NOT installed)"
  fi

  if ! is_loaded; then
    echo "state:     not loaded"
    echo "at login:  no"
    exit 2
  fi

  # Right after a kickstart the state is briefly "xpcproxy"/"spawn scheduled"
  # before it settles on "running". Give it a moment so status is not a false
  # negative.
  info=""; state=""
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    info="$(launchctl print "$SERVICE" 2>/dev/null || true)"
    state="$(echo "$info" | awk -F'= ' '/^\tstate = /{print $2; exit}')"
    [ "$state" = "running" ] && break
    sleep 0.2
  done

  if [ -z "$info" ]; then
    echo "state:     not loaded (disappeared while querying)"
    exit 2
  fi
  pid="$(echo "$info" | awk -F'= ' '/^\tpid = /{print $2; exit}')"
  last="$(echo "$info" | awk -F'= ' '/last exit code = /{print $2; exit}')"

  echo "state:     ${state:-unknown}"
  echo "pid:       ${pid:-none}"
  echo "last exit: ${last:-n/a}"
  echo "at login:  yes (RunAtLoad)"

  echo
  echo "--- $OUT_LOG (last $tail_lines) ---"
  if [ -s "$OUT_LOG" ]; then tail -n "$tail_lines" "$OUT_LOG"; else echo "(empty)"; fi

  if [ -s "$ERR_LOG" ]; then
    echo
    echo "--- $ERR_LOG (last $tail_lines) ---"
    tail -n "$tail_lines" "$ERR_LOG"
  fi

  # A pid means the job is spawned, even if the state label has not settled.
  if [ "$state" = "running" ] || [ -n "$pid" ]; then exit 0; else exit 1; fi
}

cmd_update() {
  if is_loaded; then
    echo "agent is loaded — run '$(basename "$0") stop' first, update, then start" >&2
    exit 1
  fi
  docker stop n8n-task-runner 2>/dev/null || true
  docker images -a | grep "n8nio/runners" | awk '{print $3}' | xargs docker rmi
}

case "${1:-}" in
  run)    cmd_run ;;
  start)  cmd_start ;;
  stop)   cmd_stop ;;
  status) shift; cmd_status "$@" ;;
  update) cmd_update ;;
  -h|--help|help)
    cat <<'HELPEOF'
usage: n8n.sh run|start|stop|status [lines]|update

  run          run n8n in the foreground: task-runner container + npx n8n
               (what the LaunchAgent calls; also usable for a manual run)
  start        render n8n.plist.template, install + load the LaunchAgent
               (<prefix>.n8n): starts at login, restarts on exit, logs to
               ./logs/n8n.log
  stop         unload the LaunchAgent and stop the task-runner container
  status       agent state + recent log lines (default 15); exits
               0 running / 1 degraded / 2 not loaded
  update       delete the local task-runner images by image ID (next run
               pulls the latest); refuses while the agent is loaded

N8N_ENCRYPTION_KEY, N8N_RUNNERS_AUTH_TOKEN and N8N_PUBLIC_HOST come from
<config>/n8n/.env (copy .env.example there; <config> is SYSTEM_TOOLS_CONFIG,
default ~/.config/system, fixed into the agent at start) or from the
environment; the file is optional when
they are already exported. N8N_PUBLIC_HOST is host[:port] with no scheme --
the tunnel fronting :5678 -- and `run` exits 78 when it is unset.

There is no basic auth: N8N_BASIC_AUTH_* was removed in n8n 1.x. Anything
this port is exposed to is gated only by n8n's own owner account.

environment: N8N_RUNNER_PORT (default 5680, the external task-runner container)
             SYSTEM_TOOLS_LABEL_PREFIX (launchd label prefix, default
             local.system-tools)
HELPEOF
    exit 0
    ;;
  *)
    echo "usage: $(basename "$0") run|start|stop|status [lines]|update" >&2
    exit 1
    ;;
esac

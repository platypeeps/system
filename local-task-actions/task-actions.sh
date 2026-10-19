#!/bin/sh
# task-actions — tiny localhost HTTP service that served the Review Queue
# digest's signed decision links, which flipped a status in a vault note. The
# decision moved to the dashboard's Tasks page (sd:5134): the server now
# answers an old link with a page that says so and changes nothing. It binds
# 127.0.0.1 only. LaunchAgent pattern as in local-cswap.
# Usage: task-actions.sh run|start|stop|status|test
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
. "$DIR/../lib/config.sh"

# <config>/task-actions/.env (outside the checkout; <config> is $SYSTEM_TOOLS_CONFIG,
# default ~/.config/system; see .env.example) provides defaults only; an exported
# SYSTEM_TOOLS_LABEL_PREFIX or TASK_ACTIONS_PORT wins; the prefix is kept once,
# in <config>/shared.env. Read it before anything below uses a value it may set.
st_source_env task-actions

LABEL_PREFIX="$(st_label_prefix)"
LABEL="$LABEL_PREFIX.task-actions"
SRC_PLIST="$DIR/task-actions.plist.template"
AGENT_DIR="$HOME/Library/LaunchAgents"
DST_PLIST="$AGENT_DIR/$LABEL.plist"
DOMAIN="gui/$(id -u)"
SERVICE="$DOMAIN/$LABEL"
OUT_LOG="$DIR/logs/task-actions.log"

PORT="${TASK_ACTIONS_PORT:-8766}"

is_loaded() { launchctl print "$SERVICE" >/dev/null 2>&1; }

# Public https URL from Tailscale Funnel, but only when the funnel actually
# proxies our port (bare "tailscale funnel status" header lines are ignored).
# `status` asks it, to say whether the public side still answers.
# `tailscale funnel status` talks to tailscaled over a local socket, and a
# daemon that is restarting or reconnecting answers slowly or not at all. It is
# bounded so a stalled daemon costs a few seconds, never an unbounded block in
# whatever called us.
#
# The timeout gets its own exit code, and that is the whole point of the
# sentinel file below. "The daemon did not answer" and "no funnel proxies this
# port" are different answers: the second is a configured machine with no
# funnel, where a localhost URL is correct, and the first is a machine that
# probably does have one and could not be asked. Flattening both into an empty
# string is what made this expensive -- every signing call paid the timeout
# again and got a localhost link that works nowhere but this machine.
#
#   0  prints the https base URL
#   1  no funnel proxies this port (or tailscale answered nothing useful)
#   2  the daemon did not answer within FUNNEL_TIMEOUT
FUNNEL_TIMEOUT="${TASK_ACTIONS_FUNNEL_TIMEOUT:-3}"

funnel_url() {
  # A full template, not `-t`: GNU mktemp wants X's in a -t prefix.
  tmp="$(mktemp "${TMPDIR:-/tmp}/task-actions-funnel.XXXXXX")"
  killed="$tmp.killed"
  tailscale funnel status >"$tmp" 2>/dev/null & pid=$!
  # The watchdog records that it fired, and only when the kill lands: a daemon
  # that answers in the same instant the timer expires is not a timeout.
  # Its stdout goes to /dev/null: killing the subshell leaves its `sleep`
  # running, and a `sleep` holding the `$(funnel_url)` pipe made every probe
  # wait out the whole timeout (sd:1770).
  ( sleep "$FUNNEL_TIMEOUT"; kill "$pid" 2>/dev/null && : >"$killed" ) >/dev/null 2>&1 &
  watchdog=$!
  wait "$pid" 2>/dev/null || true
  kill "$watchdog" 2>/dev/null || true
  wait "$watchdog" 2>/dev/null || true
  if [ -e "$killed" ]; then
    rm -f "$tmp" "$killed"
    return 2
  fi
  url="$(awk -v p=":$PORT" \
    '/^https:\/\// { url=$1 } $0 ~ p && url { print url; exit }' "$tmp")"
  rm -f "$tmp" "$killed"
  [ -n "$url" ] || return 1
  printf '%s\n' "$url"
}

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
  exec /usr/bin/env PORT="$PORT" FOLDER="$DIR" \
    DASH_STATIC="$DIR/../local-project-dashboard/sd_dashboard/v2/static" python3 - <<'PY'
import html, os, pathlib, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ["PORT"])

# The paths the digest's signed links used (`/task` to decide, `/open` to open
# the note). A link from a brief filed before sd:5134 still lands here for up
# to seven days: it gets this answer, and nothing reads or writes a note.
MOVED = ("/task", "/open")

# Every page is one panel on the foundation: tokens, theme control, no inline
# style. A state is a glyph and words, and the lamp edge repeats it.
GLYPHS = {"warning": "■", "unknown": "▨"}
THEME = ('<div class="theme" role="group" aria-label="Theme">'
         '<button type="button" data-theme-choice="dark">Dark</button>'
         '<button type="button" data-theme-choice="light">Light</button>'
         '<button type="button" data-theme-choice="system">System</button></div>')
PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<script src="/static/theme.js"></script>
<link rel="stylesheet" href="/static/tokens.css">
<link rel="stylesheet" href="/static/fonts.css">
<link rel="stylesheet" href="/static/confirm.css">
</head><body>
<main class="panel" data-state="{state}"><h1>{title}</h1>{detail}</main>
{theme}
</body></html>"""

# The one policy: this page runs its own files and nothing else.
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; font-src 'self'; "
       "form-action 'self'; base-uri 'none'; frame-ancestors 'none'")

# What /static/<name> serves: the foundation copies the dashboard already
# carries, and this page's stylesheet. Nothing else is reachable.
DASH = pathlib.Path(os.environ["DASH_STATIC"])
ASSETS = {
    "tokens.css": (DASH, "text/css"),
    "theme.js": (DASH, "text/javascript"),
    "fonts.css": (DASH, "text/css"),
    "fonts/ibm-plex-sans-var.woff2": (DASH, "font/woff2"),
    "confirm.css": (pathlib.Path(os.environ["FOLDER"]), "text/css"),
}

class H(BaseHTTPRequestHandler):
    def log_message(self, fmt, *a):
        print("[task-actions]", self.address_string(), fmt % a, flush=True)

    def reply(self, code, state, title, detail):
        line = f'<p><span class="g" aria-hidden="true">{GLYPHS[state]}</span>{html.escape(detail)}</p>'
        b = PAGE.format(state=state, title=html.escape(title), detail=line, theme=THEME).encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def asset(self, name):
        # Only the names listed in ASSETS; a name is a key, never a path.
        where, kind = ASSETS.get(name, (None, None))
        try:
            body = (where / name).read_bytes() if where else None
        except OSError:
            body = None
        if body is None:
            return self.reply(404, "unknown", "Not found", "Nothing is served here.")
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def moved(self):
        self.reply(410, "warning", "Decide on the dashboard",
                   "Nothing changed. Open the Review Queue brief on Today and decide on Tasks.")

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in MOVED:
            return self.moved()
        if path.startswith("/static/"):
            return self.asset(path[len("/static/"):])
        self.reply(404, "unknown", "Not found", "Nothing is served here.")

    def do_POST(self):
        if urllib.parse.urlparse(self.path).path in MOVED:
            return self.moved()
        self.reply(404, "unknown", "Not found", "Nothing is served here.")

print(f"[task-actions] listening on 127.0.0.1:{PORT}", flush=True)
srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
srv.daemon_threads = True
srv.serve_forever()
PY
}

cmd_start() {
  mkdir -p "$AGENT_DIR"
  # launchd passes the agent only the environment the plist names, so the
  # config root this start read goes in it, or `run` would read another.
  sed -e "s|@LABEL@|$LABEL|g" -e "s|@DIR@|$DIR|g" -e "s|@HOME@|$HOME|g" \
    -e "s|@CONFIG@|$SYSTEM_TOOLS_CONFIG|g" \
    "$SRC_PLIST" > "$DST_PLIST"
  if is_loaded; then
    launchctl bootout "$SERVICE" 2>/dev/null || true
    wait_until_unloaded || { echo "ERROR: $LABEL is still loaded after bootout" >&2; exit 1; }
  fi
  attempt=1
  while :; do
    if launchctl bootstrap "$DOMAIN" "$DST_PLIST" 2>/dev/null; then break; fi
    if [ "$attempt" -ge 5 ]; then
      echo "ERROR: bootstrap failed for $DST_PLIST" >&2
      launchctl bootstrap "$DOMAIN" "$DST_PLIST"
      exit 1
    fi
    attempt=$((attempt + 1))
    sleep 0.5
  done
  launchctl enable "$SERVICE"
  launchctl kickstart "$SERVICE" >/dev/null
  echo "started: $LABEL (127.0.0.1:$PORT)"
  echo "  plist: $DST_PLIST"
  echo "  log:   $OUT_LOG"
}

cmd_stop() {
  if is_loaded; then
    launchctl bootout "$SERVICE" || true
    wait_until_unloaded || { echo "ERROR: $LABEL is still loaded" >&2; exit 1; }
    rm -f "$DST_PLIST"
    echo "stopped and unloaded: $LABEL"
  else
    echo "not loaded: $LABEL"
  fi
}

cmd_status() {
  echo "label:  $LABEL"
  if [ -f "$DST_PLIST" ]; then echo "plist:  installed"; else echo "plist:  NOT installed"; fi
  if ! is_loaded; then
    echo "state:  not loaded"
    exit 3
  fi
  info="$(launchctl print "$SERVICE" 2>/dev/null || true)"
  echo "state:  $(echo "$info" | awk -F'= ' '/^\tstate = /{print $2; exit}')"
  if curl -s --max-time 2 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/x" | grep -q 404; then
    echo "http:   answering on 127.0.0.1:$PORT"
  else
    echo "http:   NOT answering on 127.0.0.1:$PORT"
    exit 1
  fi
  if command -v tailscale >/dev/null 2>&1; then
    pub=""
    code=0
    pub="$(funnel_url)" || code=$?
    if [ "$code" -eq 2 ]; then
      echo "public: UNKNOWN (tailscaled did not answer within ${FUNNEL_TIMEOUT}s)"
      exit 1
    fi
    if [ -n "$pub" ]; then
      if curl -s --max-time 8 "$pub/x" | grep -q '<h1>Not found</h1>'; then
        echo "public: up ($pub)"
      else
        echo "public: NOT healthy ($pub configured but our 404 page not seen)"
        exit 1
      fi
    else
      echo "public: not configured (run: tailscale funnel --bg $PORT)"
    fi
  fi
}

case "${1:-}" in
  run)    cmd_run ;;
  start)  cmd_start ;;
  stop)   cmd_stop ;;
  status) cmd_status ;;
  test)   shift
          exec "${PYTHON:-python3}" -m unittest discover -s "$DIR/tests" -t "$DIR" "$@" ;;
  -h|--help|help)
    cat <<'HELPEOF'
usage: task-actions.sh run|start|stop|status|test

  run     the HTTP server itself (what the LaunchAgent calls): binds
          127.0.0.1:8766. The Review Queue digest's signed decision links
          (/task, /open) went with sd:5134: an old link gets a page that
          sends you to the dashboard's Tasks page, and nothing changes
  start   install + load the LaunchAgent ($SYSTEM_TOOLS_LABEL_PREFIX.task-actions,
          prefix default local.system-tools), rendered from
          task-actions.plist.template
  stop    unload the LaunchAgent
  status  agent + HTTP state; exits 0 ok / 1 degraded / 3 not loaded (nothing
          to check) — local-health-check reads these codes
  test    run the unittest suite in tests/

The server binds localhost; Tailscale Funnel fronts it for other devices
(tailscale funnel --bg 8766), and `status` probes that public side too.

environment (exported, or in <config>/task-actions/.env, where <config> is
$SYSTEM_TOOLS_CONFIG, default ~/.config/system; see .env.example): TASK_ACTIONS_PORT
(8766), TASK_ACTIONS_FUNNEL_TIMEOUT (3), SYSTEM_TOOLS_LABEL_PREFIX
HELPEOF
    exit 0
    ;;
  *)
    echo "usage: $(basename "$0") run|start|stop|status|test" >&2
    exit 1
    ;;
esac

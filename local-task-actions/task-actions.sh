#!/bin/sh
# task-actions — tiny localhost HTTP service that lets the Obsidian task
# digest email carry "mark done" / "postpone" buttons. Links are HMAC-signed
# and expire; the server binds 127.0.0.1 only and edits TaskNote frontmatter
# in the vault. LaunchAgent pattern as in local-cswap.
# Usage: task-actions.sh run|start|stop|status|base-url|url <file-stem> <action> [days]|test
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
. "$DIR/../lib/config.sh"

# <config>/task-actions/.env (outside the checkout; <config> is $SYSTEM_TOOLS_CONFIG,
# default ~/.config/system; see .env.example) provides defaults only; an exported
# OBSIDIAN_VAULT, SYSTEM_TOOLS_LABEL_PREFIX or TASK_ACTIONS_PORT wins. Read it
# before anything below uses a value it may set.
ENV_OBSIDIAN_VAULT="${OBSIDIAN_VAULT:-}"
ENV_LABEL_PREFIX="${SYSTEM_TOOLS_LABEL_PREFIX:-}"
ENV_PORT="${TASK_ACTIONS_PORT:-}"
st_source_env task-actions
[ -n "$ENV_OBSIDIAN_VAULT" ] && OBSIDIAN_VAULT="$ENV_OBSIDIAN_VAULT"
[ -n "$ENV_LABEL_PREFIX" ] && SYSTEM_TOOLS_LABEL_PREFIX="$ENV_LABEL_PREFIX"
[ -n "$ENV_PORT" ] && TASK_ACTIONS_PORT="$ENV_PORT"

LABEL_PREFIX="${SYSTEM_TOOLS_LABEL_PREFIX:-local.system-tools}"
LABEL="$LABEL_PREFIX.task-actions"
SRC_PLIST="$DIR/task-actions.plist.template"
AGENT_DIR="$HOME/Library/LaunchAgents"
DST_PLIST="$AGENT_DIR/$LABEL.plist"
DOMAIN="gui/$(id -u)"
SERVICE="$DOMAIN/$LABEL"
OUT_LOG="$DIR/logs/task-actions.log"

PORT="${TASK_ACTIONS_PORT:-8766}"
VAULT="${OBSIDIAN_VAULT:-$HOME/Documents/Obsidian Vault}"
TASKS_SUBDIR="${OBSIDIAN_TASKS_SUBDIR:-TaskNotes/Tasks}"
SECRET_FILE="${TASK_ACTIONS_SECRET_FILE:-$HOME/.config/task-actions/secret}"

ensure_secret() {
  if [ ! -f "$SECRET_FILE" ]; then
    mkdir -p "$(dirname "$SECRET_FILE")"
    umask 077
    openssl rand -hex 32 > "$SECRET_FILE"
  fi
}

is_loaded() { launchctl print "$SERVICE" >/dev/null 2>&1; }

# Public https URL from Tailscale Funnel, but only when the funnel actually
# proxies our port (bare "tailscale funnel status" header lines are ignored).
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
# string is what made this expensive -- every caller paid the timeout again and
# got a localhost link that works nowhere but this machine.
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
  ( sleep "$FUNNEL_TIMEOUT"; kill "$pid" 2>/dev/null && : >"$killed" ) 2>/dev/null &
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

# The base URL every signed link is built on, resolved once. `url` calls this
# per invocation; a caller that signs many links in a row -- the Obsidian
# review digest signs three or four per card -- asks for it once with
# `base-url` and passes the answer back in TASK_ACTIONS_BASE_URL, so the
# daemon is probed once per digest instead of once per button.
#
#   0  prints the base URL, public or localhost
#   2  discovery could not answer; the caller decides what an unsigned run does
resolve_base() {
  if [ -n "${TASK_ACTIONS_BASE_URL:-}" ]; then
    printf '%s\n' "$TASK_ACTIONS_BASE_URL"
    return 0
  fi
  if command -v tailscale >/dev/null 2>&1; then
    code=0
    url="$(funnel_url)" || code=$?
    if [ "$code" -eq 2 ]; then
      echo "task-actions: tailscaled did not answer within ${FUNNEL_TIMEOUT}s" >&2
      return 2
    fi
    if [ -n "$url" ]; then
      printf '%s\n' "$url"
      return 0
    fi
  fi
  printf '%s\n' "http://127.0.0.1:$PORT"
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
  ensure_secret
  exec /usr/bin/env PORT="$PORT" VAULT="$VAULT" TASKS_SUBDIR="$TASKS_SUBDIR" \
    SECRET_FILE="$SECRET_FILE" python3 - <<'PY'
import datetime, hashlib, hmac, html, os, pathlib, re, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ["PORT"])
VAULT = pathlib.Path(os.environ["VAULT"])
TASKS = VAULT / os.environ["TASKS_SUBDIR"]
SECRET = open(os.environ["SECRET_FILE"]).read().strip().encode()

# Decision databases (local-obsidian-review digest): key -> (folder,
# action -> new status). The write is exactly the frontmatter `status`
# flip that the accept routines (blog-idea-accept, tips-accept,
# market-watch) key on; Meta Bind dropdowns in the note bodies bind to the
# same field, so no body edit is needed.
#
# The `skill` row below is the exception since 2026-08-31: `skill-proposal-accept`
# was retired that day (sd-ai-command-pack's `sd-skill-adopt` replaces the whole
# six-stage intake path it belonged to), so a Skill Proposal set to `accepted`
# records your decision and nothing carries it further. The row stays because
# recording the decision is still worth doing and `declined` was always terminal
# on its own; what is gone is the filing behind it.
DBS = {
    "blog":  ("System/Databases/Blog Ideas",      {"accept": "accepted", "decline": "declined"}),
    "skill": ("System/Databases/Skill Proposals", {"accept": "accepted", "decline": "declined"}),
    "tip":   ("System/Databases/Tips and Tricks", {"accept": "accepted", "decline": "declined"}),
    "watch": ("System/Databases/Market Watch",    {"track": "active", "pause": "paused", "decline": "declined"}),
    "topic": ("System/Databases/Topics",          {"adopt": "active", "park": "parked"}),
}

def sign(f, a, d, exp, db=""):
    # db joined the payload later; db-less links keep the original format
    # so task URLs already in mailboxes stay valid.
    msg = (f"{db}|{f}|{a}|{d}|{exp}" if db else f"{f}|{a}|{d}|{exp}").encode()
    return hmac.new(SECRET, msg, hashlib.sha256).hexdigest()

def set_field(fm, name, value):
    # Replace a scalar frontmatter field, or append it if missing.
    pat = rf"^{name}:.*$"
    if re.search(pat, fm, re.M):
        return re.sub(pat, f"{name}: {value}", fm, count=1, flags=re.M)
    return fm + f"\n{name}: {value}"

def next_occurrence(rec, cur):
    # Supports the four shapes TaskNotes uses here: DAILY/WEEKLY/MONTHLY/
    # YEARLY with optional INTERVAL. Falls back to +1 day.
    freq = (re.search(r"FREQ=(\w+)", rec) or [None, "DAILY"])[1]
    iv = int((re.search(r"INTERVAL=(\d+)", rec) or [None, "1"])[1])
    if freq == "DAILY":
        return cur + datetime.timedelta(days=iv)
    if freq == "WEEKLY":
        return cur + datetime.timedelta(weeks=iv)
    if freq == "MONTHLY":
        m = cur.month - 1 + iv
        return cur.replace(year=cur.year + m // 12, month=m % 12 + 1)
    if freq == "YEARLY":
        return cur.replace(year=cur.year + iv)
    return cur + datetime.timedelta(days=1)

def apply(f, action, days):
    path = TASKS / f"{f}.md"
    if not path.is_file() or ".." in f or "/" in f:
        return None, "task file not found"
    text = path.read_text()
    m = re.match(r"(?s)^---\n(.*?)\n---(.*)$", text)
    if not m:
        return None, "no frontmatter"
    fm, rest = m.group(1), m.group(2)
    now = datetime.datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S.000%z")
    now = now[:-2] + ":" + now[-2:]
    sched = (re.search(r"^scheduled:\s*(\S+)", fm, re.M) or [None, ""])[1][:10]
    rec = (re.search(r"^recurrence:\s*(\S+)", fm, re.M) or [None, ""])[1]

    if action == "postpone":
        base = datetime.date.fromisoformat(sched) if sched else datetime.date.today()
        target = max(base, datetime.date.today()) + datetime.timedelta(days=days)
        fm = set_field(fm, "scheduled", target.isoformat())
        outcome = f"postponed to {target.isoformat()}"
    elif action == "done":
        if rec:
            # Recurring: record this occurrence and advance the schedule.
            cur = datetime.date.fromisoformat(sched) if sched else datetime.date.today()
            if re.search(r"^complete_instances:\s*\[", fm, re.M):
                fm = re.sub(r"^complete_instances:\s*\[",
                            f"complete_instances: ['{cur.isoformat()}', ",
                            fm, count=1, flags=re.M) if "[]" not in \
                    re.search(r"^complete_instances:.*$", fm, re.M).group(0) else \
                    re.sub(r"^complete_instances:\s*\[\]",
                           f"complete_instances: ['{cur.isoformat()}']",
                           fm, count=1, flags=re.M)
            else:
                fm += f"\ncomplete_instances: ['{cur.isoformat()}']"
            nxt = next_occurrence(rec, cur)
            fm = set_field(fm, "scheduled", nxt.isoformat())
            outcome = f"occurrence {cur.isoformat()} done, next {nxt.isoformat()}"
        else:
            fm = set_field(fm, "status", "done")
            outcome = "marked done"
    else:
        return None, "unknown action"

    fm = set_field(fm, "dateModified", now)
    path.write_text(f"---\n{fm}\n---{rest}")
    return outcome, None

def apply_note(db, f, action):
    # Decision databases: the whole action is one status flip.
    subdir, actions = DBS[db]
    path = VAULT / subdir / f"{f}.md"
    if not path.is_file() or ".." in f or "/" in f:
        return None, "note not found"
    new = actions.get(action)
    if not new:
        return None, "unknown action"
    text = path.read_text()
    m = re.match(r"(?s)^---\n(.*?)\n---(.*)$", text)
    if not m:
        return None, "no frontmatter"
    fm, rest = m.group(1), m.group(2)
    cur = (re.search(r"^status:\s*(\S+)", fm, re.M) or [None, ""])[1]
    if cur == new:
        return f"already {new}", None
    fm = set_field(fm, "status", new)
    path.write_text(f"---\n{fm}\n---{rest}")
    return f"status: {cur or '(none)'} → {new}", None

PAGE = """<!doctype html><meta name=viewport content="width=device-width">
<body style="font-family:-apple-system,sans-serif;margin:3em auto;max-width:28em;text-align:center">
<div style="font-size:3em">{icon}</div><h2>{title}</h2><p>{detail}</p>{extra}</body>"""

# GET /task shows this instead of acting, so a mail scanner prefetching the
# link cannot mutate a task; the human clicks the button, which POSTs.
CONFIRM_BTN = """<form method="POST" action="/task">
{inputs}<button type="submit" style="padding:10px 22px;border:0;border-radius:8px;
font-size:15px;font-weight:600;color:#fff;background:{bg};cursor:pointer">{label}</button>
</form><p style="color:#999;font-size:12px">nothing happens until you press the button</p>"""

def obsidian_uri(f, db=""):
    subdir = DBS[db][0] if db else os.environ["TASKS_SUBDIR"]
    return "obsidian://open?vault=%s&file=%s" % (
        urllib.parse.quote(VAULT.name), urllib.parse.quote(f"{subdir}/{f}"))

# action -> (button label, button color, page icon); postpone label is
# built dynamically from the day count.
LABELS = {
    "done":    ("✓ Mark done", "#2c7a2c", "✓"),
    "postpone": (None,         "#8a6d1a", "→"),
    "accept":  ("✓ Accept",    "#2c7a2c", "✓"),
    "decline": ("✗ Decline",   "#8a3030", "✗"),
    "track":   ("▶ Track",     "#2c7a2c", "▶"),
    "pause":   ("⏸ Pause",     "#8a6d1a", "⏸"),
    "adopt":   ("✓ Adopt",     "#2c7a2c", "✓"),
    "park":    ("⏸ Park",      "#666666", "⏸"),
}

class H(BaseHTTPRequestHandler):
    def log_message(self, fmt, *a):
        print("[task-actions]", self.address_string(), fmt % a, flush=True)

    def reply(self, code, icon, title, detail="", extra="", headers=None):
        b = PAGE.format(icon=icon, title=html.escape(title),
                        detail=html.escape(detail), extra=extra).encode()
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def params(self, query):
        q = urllib.parse.parse_qs(query)
        return (q.get("f", [""])[0], q.get("a", [""])[0],
                q.get("d", ["3"])[0], q.get("exp", [""])[0],
                q.get("t", [""])[0], q.get("db", [""])[0])

    def checked(self, f, a, d, exp, t, db=""):
        if db and db not in DBS:
            self.reply(403, "✗", "unknown database")
            return False
        if not hmac.compare_digest(sign(f, a, d, exp, db), t):
            self.reply(403, "✗", "bad signature")
            return False
        if exp < datetime.date.today().isoformat():
            self.reply(403, "✗", "link expired",
                       "run the digest again for fresh links")
            return False
        return True

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        f, a, d, exp, t, db = self.params(u.query)
        if u.path == "/open":
            if not self.checked(f, a, d, exp, t, db) or a != "open":
                return
            self.send_response(302)
            self.send_header("Location", obsidian_uri(f, db))
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if u.path != "/task":
            return self.reply(404, "?", "not found")
        if not self.checked(f, a, d, exp, t, db):
            return
        if a not in LABELS or (db and a not in DBS[db][1]) \
                or (not db and a not in ("done", "postpone")):
            return self.reply(400, "✗", "unknown action")
        inputs = "".join(
            f'<input type="hidden" name="{k}" value="{html.escape(v, quote=True)}">'
            for k, v in (("f", f), ("a", a), ("d", d), ("exp", exp),
                         ("t", t), ("db", db)) if v)
        label, bg, icon = LABELS[a]
        label = label or f"Postpone {d} day(s)"
        self.reply(200, icon, f, "confirm the action below",
                   CONFIRM_BTN.format(inputs=inputs, label=label, bg=bg))

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        if u.path != "/task":
            return self.reply(404, "?", "not found")
        n = int(self.headers.get("Content-Length", "0") or "0")
        f, a, d, exp, t, db = self.params(self.rfile.read(min(n, 65536)).decode())
        if not self.checked(f, a, d, exp, t, db):
            return
        outcome, err = apply_note(db, f, a) if db else apply(f, a, int(d))
        if err:
            return self.reply(400, "✗", err)
        icon = LABELS.get(a, ("", "", "✓"))[2]
        self.reply(200, icon, f, outcome)

print(f"[task-actions] listening on 127.0.0.1:{PORT}", flush=True)
srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
srv.daemon_threads = True
srv.serve_forever()
PY
}

# Prints a signed action URL for the digest email. Links carry a 7-day
# expiry; the secret never appears in the URL, only the HMAC. The base URL
# is the Tailscale Funnel URL when one proxies our port (so buttons work
# from any device), else localhost; TASK_ACTIONS_BASE_URL overrides both.
cmd_url() { # [-b db] file-stem action [days]
  DB=""
  if [ "${1:-}" = "-b" ]; then DB="${2:?-b needs a database key}"; shift 2; fi
  [ $# -ge 2 ] || { echo "usage: $(basename "$0") url [-b db] <file-stem> <action> [days]" >&2; exit 1; }
  ensure_secret
  # Exit 2 rather than a localhost link the recipient cannot open: a link
  # signed against 127.0.0.1 works on this machine and nowhere the digest is
  # read. The caller sees the code and drops to Open-only.
  code=0
  base="$(resolve_base)" || code=$?
  [ "$code" -eq 0 ] || exit "$code"
  F="$1" A="$2" D="${3:-3}" DB="$DB" BASE="$base" SECRET_FILE="$SECRET_FILE" python3 - <<'PY'
import datetime, hashlib, hmac, os, urllib.parse
f, a, d, db = os.environ["F"], os.environ["A"], os.environ["D"], os.environ["DB"]
exp = (datetime.date.today() + datetime.timedelta(days=7)).isoformat()
secret = open(os.environ["SECRET_FILE"]).read().strip().encode()
msg = f"{db}|{f}|{a}|{d}|{exp}" if db else f"{f}|{a}|{d}|{exp}"
t = hmac.new(secret, msg.encode(), hashlib.sha256).hexdigest()
path = "/open" if a == "open" else "/task"
q = {"f": f, "a": a, "d": d, "exp": exp, "t": t}
if db:
    q["db"] = db
print(os.environ["BASE"] + path + "?" + urllib.parse.urlencode(q))
PY
}

cmd_start() {
  ensure_secret
  mkdir -p "$AGENT_DIR"
  sed -e "s|@LABEL@|$LABEL|g" -e "s|@DIR@|$DIR|g" -e "s|@HOME@|$HOME|g" \
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
    exit 2
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
      if curl -s --max-time 8 "$pub/x" | grep -q '<h2>not found</h2>'; then
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
  url)    shift; cmd_url "$@" ;;
  base-url) resolve_base ;;
  test)   shift
          exec "${PYTHON:-python3}" -m unittest discover -s "$DIR/tests" -t "$DIR" "$@" ;;
  -h|--help|help)
    cat <<'HELPEOF'
usage: task-actions.sh run|start|stop|status|base-url|url <file-stem> <done|postpone> [days]|test

  run     the HTTP server itself (what the LaunchAgent calls): binds
          127.0.0.1:8766 and applies signed done/postpone actions to
          TaskNote frontmatter in the vault
  start   install + load the LaunchAgent ($SYSTEM_TOOLS_LABEL_PREFIX.task-actions,
          prefix default local.system-tools), rendered from
          task-actions.plist.template
  stop    unload the LaunchAgent
  status  agent + HTTP state; exits 0 ok / 1 degraded / 2 not loaded
  url     print a signed action URL (used by local-obsidian-tasks and
          local-obsidian-review to build digest email buttons); links
          expire after 7 days. `url [-b db] <stem> <action> [days]`.
          Base URL: TASK_ACTIONS_BASE_URL, else the Tailscale Funnel
          URL when one proxies this port, else http://127.0.0.1:8766.
  test    run the unittest suite in tests/

actions: open — HTTPS wrapper that 302-redirects to the obsidian:// URI
(mail clients strip custom-scheme links, https survives). done —
non-recurring tasks get status: done; recurring tasks get the occurrence
recorded in complete_instances and scheduled advanced per the recurrence
rule. postpone — scheduled moves N days out (default 3). GET on /task only
shows a confirmation page; the actual edit happens on POST, so mail-scanner
link prefetches cannot mutate anything.

decision databases (-b): one frontmatter status flip per action —
  blog   System/Databases/Blog Ideas       accept|decline
  skill  System/Databases/Skill Proposals  accept|decline
  tip    System/Databases/Tips and Tricks  accept|decline
  watch  System/Databases/Market Watch     track|pause|decline
  topic  System/Databases/Topics           adopt|park
The nightly accept routines pick up the flipped statuses from there.

The HMAC secret lives in ~/.config/task-actions/secret (auto-generated,
0600, never leaves the machine). The server binds localhost; Tailscale
Funnel fronts it for other devices (tailscale funnel --bg 8766 — the
serve config persists across restarts, no extra LaunchAgent needed).

environment (exported, or in <config>/task-actions/.env, where <config> is
$SYSTEM_TOOLS_CONFIG, default ~/.config/system; see .env.example): TASK_ACTIONS_PORT
(8766), OBSIDIAN_VAULT (~/Documents/Obsidian Vault), OBSIDIAN_TASKS_SUBDIR,
TASK_ACTIONS_SECRET_FILE, TASK_ACTIONS_BASE_URL, SYSTEM_TOOLS_LABEL_PREFIX
HELPEOF
    exit 0
    ;;
  *)
    echo "usage: $(basename "$0") run|start|stop|status|base-url|url <stem> <action> [days]|test" >&2
    exit 1
    ;;
esac

#!/bin/sh
# General-purpose notification: local macOS banner + ntfy push to phone.
# ntfy settings come from ./.env (gitignored) — see .env.example.
# Replaces the old ~/bin/common/sendalert.sh (iMessage-only, hardcoded number).
set -e

# Resolve symlinks with relative targets against the link's own directory —
# a bare readlink walk breaks for the relative links bin-links installs.
SELF="$0"
while [ -L "$SELF" ]; do
  target="$(readlink "$SELF")"
  case "$target" in
    /*) SELF="$target" ;;
    *)  SELF="$(dirname "$SELF")/$target" ;;
  esac
done
DIR="$(cd "$(dirname "$SELF")" && pwd)"

# .env provides defaults only — values already in the environment win, so
# `NTFY_TOPIC=other notify.sh ...` targets a different topic as expected.
ENV_NTFY_TOPIC="${NTFY_TOPIC:-}"
ENV_NTFY_SERVER="${NTFY_SERVER:-}"
ENV_NTFY_TOKEN="${NTFY_TOKEN:-}"
ENV_IMESSAGE_TO="${IMESSAGE_TO:-}"
ENV_EMAIL_TO="${EMAIL_TO:-}"
ENV_NOTIFY_EMAIL_TO="${NOTIFY_EMAIL_TO:-}"
ENV_EMAIL_FROM="${EMAIL_FROM:-}"
ENV_WORKSPACE_MCP_URL="${WORKSPACE_MCP_URL:-}"
# JEV_NOTIFY joins them because it is this tool's kill switch: a caller that
# writes `JEV_NOTIFY=0 notify.sh ...` to keep one message off the wire must
# win over whatever .env says, exactly as an explicit NTFY_TOPIC does.
ENV_JEV_NOTIFY="${JEV_NOTIFY:-}"
[ -f "$DIR/.env" ] && . "$DIR/.env"
[ -n "$ENV_JEV_NOTIFY" ] && JEV_NOTIFY="$ENV_JEV_NOTIFY"
# Exported, not just set: `jev enabled JEV_NOTIFY` is a subprocess and reads
# the environment. Sourcing .env above sets a shell variable only, so without
# this a `JEV_NOTIFY=0` line in .env would be read by nothing and the stage
# would keep running -- a kill switch that looks set and is not.
[ -z "${JEV_NOTIFY:-}" ] || export JEV_NOTIFY
[ -n "$ENV_NTFY_TOPIC" ]  && NTFY_TOPIC="$ENV_NTFY_TOPIC"
[ -n "$ENV_NTFY_SERVER" ] && NTFY_SERVER="$ENV_NTFY_SERVER"
[ -n "$ENV_NTFY_TOKEN" ]  && NTFY_TOKEN="$ENV_NTFY_TOKEN"
[ -n "$ENV_IMESSAGE_TO" ] && IMESSAGE_TO="$ENV_IMESSAGE_TO"
[ -n "$ENV_EMAIL_TO" ]   && EMAIL_TO="$ENV_EMAIL_TO"
[ -n "$ENV_NOTIFY_EMAIL_TO" ] && NOTIFY_EMAIL_TO="$ENV_NOTIFY_EMAIL_TO"
[ -n "$ENV_EMAIL_FROM" ] && EMAIL_FROM="$ENV_EMAIL_FROM"
[ -n "$ENV_WORKSPACE_MCP_URL" ] && WORKSPACE_MCP_URL="$ENV_WORKSPACE_MCP_URL"

TITLE="Alert"
PRIORITY="default"
CHANNELS="local,ntfy"
FORMAT="plain"
BEST_EFFORT=0
KIND=""
FOLLOWUP=0
# Did the caller decide these itself? Only "no" lets the optional Jev routing
# below touch them: an explicit flag is an instruction, not a suggestion.
CHANNELS_SET=0
PRIORITY_SET=0
# Looked up by NAME at send time, not stored as an id. The modify API takes
# ids, but an id is per-mailbox: hardcoding one worked until the mail turned
# out to be landing in a different account than the label lived in, and the
# call failed with "labelId not found" on a label that plainly exists. A name
# survives that, and survives the label being rebuilt.
FOLLOWUP_LABEL="${FOLLOWUP_LABEL:-!Followup}"

usage() {
  cat <<'HELPEOF'
usage: notify.sh [-t title] [-k kind] [-p priority] [-c channels] [-f format]
       [-b] [-F] message...

  -t title      notification title (default: Alert)
  -k kind       brief|status — prefixes the EMAIL subject with "Brief: " or
                "Status: " so every scheduled mail sorts under one of two
                headings. Other channels keep the bare title. Omit for a
                one-off notification that is neither.
  -F            this status mail reports something needing action: label it
                !Followup and mark it Important in Gmail. Ignored without a
                message id back from the send, and never fails the send —
                the mail is already delivered by then, and reporting a
                delivery failure over a labelling one is how a job ends up
                exiting 1 with the report sitting in the inbox.
  -p priority   ntfy priority: min|low|default|high|urgent (default: default)
  -c channels   comma list: local,ntfy,imessage,email (default: local,ntfy)
  -b            best effort: succeed if ANY requested channel delivered.
                Without it one dead channel fails the whole call, which is
                why every nightly job exited 1 while its ntfy push landed.
                Failed channels are still named on stderr either way.
  -f format     message body format: plain|html (default: plain).
                Only the email channel renders html; the other channels
                receive the body as-is, so pair -f html with -c email.

channels:
  local         macOS notification banner (osascript)
  ntfy          push to phone via ntfy — needs NTFY_TOPIC, optional
                NTFY_SERVER (default https://ntfy.sh) and NTFY_TOKEN
  imessage      iMessage via Messages.app — needs IMESSAGE_TO
  email         Gmail via the local google_workspace_mcp server — needs
                NOTIFY_EMAIL_TO (the one allowed recipient; EMAIL_TO, if
                set, must equal it) and EMAIL_FROM (the authorized Google
                account);
                WORKSPACE_MCP_URL overrides http://127.0.0.1:8083/mcp.
                Title becomes the subject, the message the body.

configuration: export the variables or copy .env.example to .env.

Jev routing (experimental, on unless JEV_NOTIFY switches it off):
  JEV_NOTIFY=0  do not ask the sibling local-jev/jev.sh to pick the channel set and
                the priority from the title and the message -- but only for
                the ones the caller left to the defaults. An explicit -c or
                -p wins and skips the call entirely. Jev never adds imessage
                (reaching someone's phone messages is a human's decision) and
                never chooses "no channel". Anything that fails -- the switch
                off, no key on this machine, an error, or no answer within
                JEV_TIMEOUT seconds (default 5) -- falls back to the defaults
                above and says why on stderr. The notification still goes out.
                Only the title and the message leave the machine, and they
                are redacted on the way out: credential shapes and machine
                identifiers become <redacted> or <path>. The delivery keeps
                the raw text. The ntfy topic, the ntfy token, the iMessage
                recipient and everything else in .env never leave at all.
                Redaction catches shapes, not meaning, so it is the second
                line: every Jev call is a network call to a third party, and
                a caller handling secret material should pass -c/-p or set
                JEV_NOTIFY=0.
HELPEOF
}

case "$1" in -h|--help|help) usage; exit 0;; esac

while getopts "t:k:p:c:f:bF" opt; do
  case "$opt" in
    t) TITLE="$OPTARG" ;;
    k) KIND="$OPTARG" ;;
    F) FOLLOWUP=1 ;;
    p) PRIORITY="$OPTARG"; PRIORITY_SET=1 ;;
    c) CHANNELS="$OPTARG"; CHANNELS_SET=1 ;;
    f) FORMAT="$OPTARG" ;;
    b) BEST_EFFORT=1 ;;
    *) usage >&2; exit 1 ;;
  esac
done
shift $((OPTIND - 1))

if [ $# -eq 0 ]; then
  usage >&2
  exit 1
fi
MESSAGE="$*"

case "$PRIORITY" in
  min|low|default|high|urgent) ;;
  *) echo "notify.sh: bad priority '$PRIORITY' (min|low|default|high|urgent)" >&2; exit 1 ;;
esac

case "$FORMAT" in
  plain|html) ;;
  *) echo "notify.sh: bad format '$FORMAT' (plain|html)" >&2; exit 1 ;;
esac

# SUBJECT is the email-only view of TITLE. Kept separate rather than rewriting
# TITLE so the ntfy push and the macOS banner keep reading the way they always
# have — the prefix exists to sort a mailbox, and a phone notification is not
# a mailbox.
case "$KIND" in
  brief)  SUBJECT="Brief: $TITLE" ;;
  status) SUBJECT="Status: $TITLE" ;;
  "")     SUBJECT="$TITLE" ;;
  *) echo "notify.sh: bad kind '$KIND' (brief|status)" >&2; exit 1 ;;
esac

# -F is about a problem worth opening, which a brief never reports. Rejected
# rather than ignored: a caller that passes both has its categories confused,
# and silently dropping the flag would hide a real alert.
if [ "$FOLLOWUP" = 1 ] && [ "$KIND" = brief ]; then
  echo "notify.sh: -F is for status mail, not briefs" >&2
  exit 1
fi

# --- optional Jev routing ---------------------------------------------------
# Experimental, additive, and on unless switched off. Nothing below may change what
# this script does unless JEV_NOTIFY switches the stage off, or Jev cannot
# answer on this machine;
# every other path leaves CHANNELS and PRIORITY exactly as they were set
# above. `jev enabled` costs nothing and calls nothing, so the check itself is
# safe in a hot path -- and a machine with no TYPESAFE_API_KEY answers it the
# same way as one with the switch off.
#
# Resolved from DIR, not from PATH: notify.sh is linked into ~/bin/common by
# local-bin-links, and a `jev` on PATH is the same link farm one `install` out
# of date. DIR is already walked through $0's symlinks at the top of the file.
JEV_SH="$DIR/../local-jev/jev.sh"

# One question, with a hard wall-clock cap. An alert must never wait on an
# experimental judgment, and jev's own JEV_TIMEOUT is per attempt and retried,
# so the cap lives here too. Prints the answer; any failure returns nonzero and
# the caller keeps its default. The poll is 1s-grained, which is under the
# round trip it is racing.
# The payload is redacted before it leaves, and this tool needs it more than
# its siblings do. local-health-check and local-weekly-digest each carry their
# own copy of a program like this over their own findings; notify.sh carries
# the alert text of every other tool in the repository, including tools nobody
# has written yet, so what turns up in $MESSAGE is not enumerable in advance.
#
# Two classes. Credential shapes come from local-scan-for-secrets' PATTERNS --
# copied, not sourced, for the reason convention 2's symlink loop is copied
# into every script that needs one: a sibling reaching into another tool's
# internals is the coupling this repo avoids, and that tool must never become
# a Jev caller. Machine identifiers are health-check's set.
#
# A path runs to the end of its line: a folder name can hold a space, and
# nothing in a line says where such a path ends, so stopping at the space sent
# the words after it (sd:1608). Whatever followed the path is lost with it:
# a routing needs none of it, and a leaked folder name cannot be taken back.
#
# An IPv6 address needs a `::` or all eight groups, so a clock time such as
# 12:34:56 stays; a `::` in other text (a Rust path) goes, which is the same
# over-reach health-check accepts.
# An IPv6 address ending in a dotted quad (::ffff:192.0.2.1) has its own
# rules ahead of the IPv4 one; after it, the hex groups would stay behind.
#
# This is a second line and not the first. The first is that a caller handling
# secret material passes -c/-p, which skips the call, or sets JEV_NOTIFY=0.
jev_write_redactions() {
  cat > "$1" <<'SEDEOF'
s/sk-proj-[A-Za-z0-9_-]{20,}/<redacted>/g
s/sk-ant-[A-Za-z0-9_-]{20,}/<redacted>/g
s/sk-[A-Za-z0-9]{40,}/<redacted>/g
s/github_pat_[A-Za-z0-9_]{20,}/<redacted>/g
s/gh[pousr]_[A-Za-z0-9]{36,}/<redacted>/g
s/glpat-[A-Za-z0-9_-]{20,}/<redacted>/g
s/(AKIA|ASIA)[0-9A-Z]{16}/<redacted>/g
s/AIza[0-9A-Za-z_-]{35}/<redacted>/g
s/ya29\.[0-9A-Za-z_-]{30,}/<redacted>/g
s/xox[baprs]-[0-9A-Za-z-]{10,}/<redacted>/g
s/xapp-[0-9]-[0-9A-Za-z-]{10,}/<redacted>/g
s/hf_[A-Za-z0-9]{30,}/<redacted>/g
s/npm_[A-Za-z0-9]{36}/<redacted>/g
s/apikey_[A-Za-z0-9]{16,}/<redacted>/g
s/r8_[A-Za-z0-9]{30,}/<redacted>/g
s/(pk|sk)_live_[0-9A-Za-z]{20,}/<redacted>/g
s/SG\.[0-9A-Za-z_-]{20,}\.[0-9A-Za-z_-]{20,}/<redacted>/g
s/eyJ[A-Za-z0-9_-]{20,}\.eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}/<redacted>/g
s/-----BEGIN ([A-Z]+ )?PRIVATE KEY( BLOCK)?-----/<redacted>/g
s/[Aa]uthorization[^A-Za-z0-9]{1,4}([Bb]earer[^A-Za-z0-9]{1,3})?[A-Za-z0-9+/]{24,}={0,2}/<redacted>/g
s/[A-Za-z0-9_]*(KEY|TOKEN|SECRET|PASS|PAT|CRED)[A-Za-z0-9_]*=[^[:space:]]+/<redacted>/g
s|[a-z][a-z0-9+.-]*://[^/[:space:]:@"]{3,}:[^/[:space:]:@"]{8,}@|<redacted>@|g
s|/Users/.*|<path>|
s|/private/.*|<path>|
s/([0-9a-fA-F]{1,4}:){6}[0-9]{1,3}(\.[0-9]{1,3}){3}/<ip>/g
s/[0-9a-fA-F:]*::([0-9a-fA-F]{1,4}:)*[0-9]{1,3}(\.[0-9]{1,3}){3}/<ip>/g
s/[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}/<ip>/g
s/([0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}/<ip>/g
s/[0-9a-fA-F:]*::[0-9a-fA-F:]*/<ip>/g
s/[A-Za-z0-9._-]*\.ts\.net/<host>/g
s/[A-Za-z0-9._-]*\.local/<host>/g
SEDEOF
  printf 's|%s.*|<path>|\n' "$HOME" >> "$1"
  for _h in "$(hostname -s 2>/dev/null || true)" "$(hostname 2>/dev/null || true)"; do
    [ -z "$_h" ] || printf 's|%s|<host>|g\n' "$_h" >> "$1"
  done
}

# Bookkeeping only: one row saying the defaults -- the control arm -- are
# what routed this notification, under the same stage key the switch reads.
# `jev record` sends nothing, needs no key, prints nothing and always exits 0.
#
# `--outcome ok` always: the old path completed, and that this row exists at
# all is the fact nothing else carries.
#
# $1 is the cause, and it is passed only when jev never wrote its own row --
# here, only when this script killed it at the deadline. jev flushes its
# measurement after the answer is printed and installs no signal handler, so
# a SIGTERM leaves nothing behind and this row is the only record there will
# be. When jev did answer unusably it has already written the cause, and
# repeating it counts one decision as two: DECLINES groups by stage and cause
# across both arms with no deduplication.
jev_record_baseline() {
  [ -x "$JEV_SH" ] || return 0
  if [ -n "${1:-}" ]; then
    "$JEV_SH" record --caller local-notify --stage JEV_NOTIFY \
      --arm baseline --outcome ok --decline "$1" >/dev/null 2>&1 || true
  else
    "$JEV_SH" record --caller local-notify --stage JEV_NOTIFY \
      --arm baseline --outcome ok >/dev/null 2>&1 || true
  fi
  return 0
}

jev_ask() {
  jev_limit="${JEV_TIMEOUT:-5}"
  case "$jev_limit" in ''|*[!0-9]*) jev_limit=5 ;; esac
  jev_out=$(mktemp) || return 1
  # Only the title and the message. Nothing sourced from .env is in scope
  # here, and nothing else may be added to this state without saying so in
  # the README: every call leaves the machine.
  printf 'Title: %s\nMessage: %s\n' "$TITLE" "$MESSAGE" \
    | sed -E -f "$JEV_REDACT" \
    | "$JEV_SH" "$@" --state - --caller local-notify --stage JEV_NOTIFY \
        >"$jev_out" 2>/dev/null &
  jev_pid=$!
  jev_waited=0
  while kill -0 "$jev_pid" 2>/dev/null; do
    if [ "$jev_waited" -ge "$jev_limit" ]; then
      kill -TERM "$jev_pid" 2>/dev/null || :
      wait "$jev_pid" 2>/dev/null || :
      rm -f "$jev_out"
      echo "notify.sh: jev did not answer in ${jev_limit}s; using the defaults" >&2
      # The SIGTERM above is why the cause is passed: jev was killed before
      # it could flush, so nothing else will record this timeout.
      jev_record_baseline timeout
      return 1
    fi
    sleep 1
    jev_waited=$((jev_waited + 1))
  done
  wait "$jev_pid" 2>/dev/null && jev_rc=0 || jev_rc=$?
  jev_answer=$(cat "$jev_out")
  rm -f "$jev_out"
  if [ "$jev_rc" != 0 ] || [ -z "$jev_answer" ]; then
    echo "notify.sh: jev gave no usable answer; using the defaults" >&2
    jev_record_baseline
    return 1
  fi
  printf '%s\n' "$jev_answer"
}

# Returns 0 only when it actually asked. A non-zero return means the stage
# declined -- no jev.sh, Jev off or unkeyed, or JEV_NOTIFY switched it off --
# and the caller must not then announce a routing that never happened.
jev_route() {
  [ -x "$JEV_SH" ] || return 1
  # One call asks both halves: can Jev answer on this machine, and has
  # JEV_NOTIFY been used to switch this stage off? Unset means on -- a
  # per-caller switch that defaults to off makes every integration added
  # after it silently never run.
  # `--record` so the decline is counted: most notifications end here, and
  # the defaults routing them is the only fact the control arm has.
  if ! "$JEV_SH" enabled JEV_NOTIFY --record --caller local-notify \
       >/dev/null 2>&1; then
    # No second row here. The gate above wrote one in the process this call
    # already started, and `judgment.py` counts gate events on their own line;
    # a `jev record` subprocess per decline would double the cost of the path
    # that declines on every run, which is the one path that must stay cheap.
    return 1
  fi

  # Fail closed. A routing nobody got is a notification delivered on the
  # defaults; a payload sent because its redaction could not be built is a
  # secret on somebody else's server, and those are not the same mistake.
  JEV_REDACT=$(mktemp) && jev_write_redactions "$JEV_REDACT" || {
    rm -f "${JEV_REDACT:-}"
    echo "notify.sh: could not build the redaction; not asking jev" >&2
    return 1
  }

  if [ "$CHANNELS_SET" = 0 ]; then
    # Two routes, and imessage is in neither. Escalating to someone's phone
    # messages is a decision a human makes; an ordinary push is the ceiling.
    # Neither route is empty either -- Jev chooses among channels, it never
    # chooses none. Descriptions carry no commas: --criteria splits on them.
    jev_pick=$(jev_ask choice 'Which route fits this notification?' \
      --id notify-route \
      --criteria 'desk=routine or informational; a banner on the screen the person is already at is enough,phone=needs attention away from the desk; banner plus a push to the phone' \
      --unsure-below 0.7) || jev_pick=""
    case "$jev_pick" in
      desk)  CHANNELS="local" ;;
      phone) CHANNELS="local,ntfy" ;;
      # `unsure`, empty, or a name outside the two above: today's default. An
      # unexpected answer is never pasted into CHANNELS, which is the other
      # half of why imessage cannot arrive this way.
      *) ;;
    esac
  fi

  if [ "$PRIORITY_SET" = 0 ]; then
    # `score` prints a position on the levels, 0-based, as a float. Round,
    # clamp, and map back through the same list -- so the value handed to ntfy
    # is always one of the five names, never whatever came back.
    jev_raw=$(jev_ask score 'How urgently does this need a person?' \
      --id notify-priority \
      --levels 'min,low,default,high,urgent') || jev_raw=""
    case "$jev_raw" in
      ''|*[!0-9.]*) jev_idx="" ;;
      *) jev_idx=$(awk -v s="$jev_raw" 'BEGIN{i=int(s+0.5); if(i<0)i=0; if(i>4)i=4; print i}') ;;
    esac
    case "$jev_idx" in
      0) PRIORITY="min" ;;
      1) PRIORITY="low" ;;
      2) PRIORITY="default" ;;
      3) PRIORITY="high" ;;
      4) PRIORITY="urgent" ;;
      *) ;;
    esac
  fi
  rm -f "$JEV_REDACT"
  return 0
}

# Both, not either: an explicit -c or -p means the caller has already made
# the routing decision, and a half-decided route is a worse answer than the
# one it asked for.
if [ "$CHANNELS_SET" = 0 ] && [ "$PRIORITY_SET" = 0 ]; then
  if jev_route; then
    echo "notify.sh: jev routing on: channels=$CHANNELS priority=$PRIORITY" >&2
  fi
fi

FAILED=""
DELIVERED=""
SKIPPED=""

send_local() {
  osascript \
    -e 'on run argv' \
    -e 'display notification (item 2 of argv) with title (item 1 of argv)' \
    -e 'end run' \
    "$TITLE" "$MESSAGE"
}

send_ntfy() {
  if [ -z "${NTFY_TOPIC:-}" ]; then
    echo "notify.sh: NTFY_TOPIC not set (export it, or copy .env.example to .env)" >&2
    return 2
  fi
  server="${NTFY_SERVER:-https://ntfy.sh}"
  if [ -n "${NTFY_TOKEN:-}" ]; then
    curl -fsS -o /dev/null \
      -H "Authorization: Bearer $NTFY_TOKEN" \
      -H "Title: $TITLE" -H "Priority: $PRIORITY" \
      -d "$MESSAGE" "$server/$NTFY_TOPIC"
  else
    curl -fsS -o /dev/null \
      -H "Title: $TITLE" -H "Priority: $PRIORITY" \
      -d "$MESSAGE" "$server/$NTFY_TOPIC"
  fi
}

# One MCP session with workspace-mcp: initialize (the session id rides a
# response header), then notifications/initialized. Prints the session id.
mcp_open_session() {
  hdr=$(mktemp) || return 1
  curl -fsS -X POST "$1" -D "$hdr" -o /dev/null --max-time 10 \
    -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
    -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"notify-sh","version":"1.0"}}}' \
    || { rm -f "$hdr"; return 1; }
  msid=$(tr -d '\r' < "$hdr" | awk -F': ' 'tolower($1)=="mcp-session-id"{print $2}')
  rm -f "$hdr"
  [ -n "$msid" ] || { echo "notify.sh: workspace-mcp gave no session id" >&2; return 1; }
  curl -fsS -X POST "$1" -o /dev/null --max-time 10 \
    -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
    -H "mcp-session-id: $msid" \
    -d '{"jsonrpc":"2.0","method":"notifications/initialized"}' || return 1
  printf '%s\n' "$msid"
}

# Gmail through the google_workspace_mcp HTTP server already running on this
# machine (launchd, port 8083) — Mail.app is not configured here and headless
# osascript automation hangs on TCC, so the MCP server is the reliable path.
send_email() {
  # The one address these notifications are allowed to reach. Not a default to
  # be overridden — a rule. Every job in this repo reports on one person's
  # machine, and a stray EMAIL_TO (a copied .env, an exported var left over
  # from a shell experiment) would quietly redirect machine inventories, repo
  # lists and health findings to whatever it happened to hold. EMAIL_FROM stays
  # free: that is which Google account does the sending, a separate question.
  # The pinned address is NOTIFY_EMAIL_TO, set once per machine in .env; to
  # retarget deliberately, change it there.
  if [ -z "${NOTIFY_EMAIL_TO:-}" ]; then
    echo "notify.sh: NOTIFY_EMAIL_TO not set: the one address the email channel may mail (export it, or copy .env.example to .env)" >&2
    return 2
  fi
  EMAIL_TO_ONLY="$NOTIFY_EMAIL_TO"
  if [ -z "${EMAIL_TO:-}" ]; then
    EMAIL_TO="$EMAIL_TO_ONLY"
  elif [ "$EMAIL_TO" != "$EMAIL_TO_ONLY" ]; then
    echo "notify.sh: EMAIL_TO is '$EMAIL_TO'; this repo only mails $EMAIL_TO_ONLY (NOTIFY_EMAIL_TO)" >&2
    # 1, not 2: rc=2 means "no credentials here, skip the channel", which -b
    # forgives and which would let a redirected EMAIL_TO drop mail in silence.
    # A wrong address is a misconfiguration to fix, so it fails the call.
    return 1
  fi
  if [ -z "${EMAIL_FROM:-}" ]; then
    echo "notify.sh: EMAIL_FROM not set (export it, or copy .env.example to .env)" >&2
    return 2
  fi
  url="${WORKSPACE_MCP_URL:-http://127.0.0.1:8083/mcp}"
  # workspace-mcp sometimes accepts initialize and then never answers the
  # notifications/initialized that follows (two lost mails on 2026-09-27). A
  # fresh session a few seconds later works, so the session setup is tried
  # three times. The send below is tried once: it may reach Gmail and still
  # fail here, and a retry would deliver the mail twice.
  tries=0
  while :; do
    tries=$((tries + 1))
    sid=$(mcp_open_session "$url") && break
    [ "$tries" -ge 3 ] && return 1
    echo "notify.sh: workspace-mcp session setup failed (try $tries of 3); retrying" >&2
    sleep "${NOTIFY_MCP_RETRY_WAIT:-3}"
  done
  payload=$(EMAIL_FROM="$EMAIL_FROM" EMAIL_TO="$EMAIL_TO" SUBJ="$SUBJECT" BODY="$MESSAGE" FMT="$FORMAT" python3 -c '
import json, os
print(json.dumps({"jsonrpc":"2.0","id":2,"method":"tools/call","params":{
  "name":"send_gmail_message",
  "arguments":{"user_google_email":os.environ["EMAIL_FROM"],
               "to":os.environ["EMAIL_TO"],
               "subject":os.environ["SUBJ"],
               "body":os.environ["BODY"],
               "body_format":os.environ["FMT"]}}}))') || return 1
  resp=$(curl -fsS -X POST "$url" --max-time 30 \
    -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
    -H "mcp-session-id: $sid" \
    -d "$payload") || return 1
  case "$resp" in
    *'"isError":false'*) ;;
    *) echo "notify.sh: workspace-mcp send failed: $(printf '%s' "$resp" | tail -c 300)" >&2; return 1 ;;
  esac

  # Delivered. Everything below is decoration on a mail that already landed,
  # so every failure path here warns and returns 0 — see -F in the usage.
  [ "$FOLLOWUP" = 1 ] || return 0
  # The server reports success as "Email sent! Message ID: <id>"; the id is the
  # only handle on the message, and there is no other way to get it without
  # searching the mailbox for a subject that may not be unique.
  mid=$(printf '%s' "$resp" | sed -n 's/.*Email sent! Message ID: \([0-9a-f][0-9a-f]*\).*/\1/p' | head -1)
  if [ -z "$mid" ]; then
    echo "notify.sh: sent, but no message id in the reply — not labelled !Followup" >&2
    return 0
  fi
  # Resolve the label name to an id in whichever mailbox this credential
  # actually opens. Important is a system label and needs no lookup, so a
  # missing !Followup still leaves the mail flagged rather than untouched.
  # INBOX is re-added, not left alone. A Gmail filter archives every Status:
  # mail on arrival so routine receipts never reach the inbox; this call runs
  # after delivery, so putting INBOX back here is what makes -F mail visible.
  # Deliberately not solved in the filter: the filter cannot test for
  # !Followup, because that label is applied by this very call, after the
  # message has already been delivered and filtered. Racing it would be
  # unreliable; re-adding after the fact cannot lose.
  labels='"IMPORTANT", "INBOX"'
  lid=""
  if [ -n "$FOLLOWUP_LABEL" ]; then
    lresp=$(curl -fsS -X POST "$url" --max-time 30 \
      -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
      -H "mcp-session-id: $sid" \
      -d "{\"jsonrpc\":\"2.0\",\"id\":3,\"method\":\"tools/call\",\"params\":{\"name\":\"list_gmail_labels\",\"arguments\":{\"user_google_email\":\"$EMAIL_FROM\"}}}" 2>/dev/null) || lresp=""
    lid=$(printf '%s' "$lresp" | tr ',' '\n' \
      | sed -n "s/.*$(printf '%s' "$FOLLOWUP_LABEL" | sed 's/[][\.*^$/]/\\&/g') (ID: \(Label_[0-9A-Za-z_]*\)).*/\1/p" | head -1)
  fi
  if [ -n "$lid" ]; then
    labels="\"$lid\", $labels"
  else
    echo "notify.sh: label '$FOLLOWUP_LABEL' not found in this mailbox — marked Important only" >&2
  fi
  mresp=$(curl -fsS -X POST "$url" --max-time 30 \
    -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
    -H "mcp-session-id: $sid" \
    -d "{\"jsonrpc\":\"2.0\",\"id\":4,\"method\":\"tools/call\",\"params\":{\"name\":\"modify_gmail_message_labels\",\"arguments\":{\"user_google_email\":\"$EMAIL_FROM\",\"message_id\":\"$mid\",\"add_label_ids\":[$labels]}}}" 2>/dev/null) || {
    echo "notify.sh: sent, but the !Followup labelling call failed" >&2
    return 0
  }
  case "$mresp" in
    *'"isError":false'*) ;;
    *) echo "notify.sh: sent, but labelling was refused: $(printf '%s' "$mresp" | tail -c 200)" >&2 ;;
  esac
  return 0
}

send_imessage() {
  if [ -z "${IMESSAGE_TO:-}" ]; then
    echo "notify.sh: IMESSAGE_TO not set (export it, or copy .env.example to .env)" >&2
    return 2
  fi
  osascript \
    -e 'on run {target, msg}' \
    -e 'tell application "Messages"' \
    -e 'set targetService to 1st service whose service type = iMessage' \
    -e 'send msg to buddy target of targetService' \
    -e 'end tell' \
    -e 'end run' \
    "$IMESSAGE_TO" "$TITLE: $MESSAGE"
}

# A channel this machine was never given credentials for is not a failure: on a
# work profile there is no google_workspace_mcp to mail through, and saying
# "failed" put a line in every cron log about a channel that profile will never
# have. Exit 2 from a send_* means not configured; 1 means it tried and could
# not deliver.
dispatch() { # $1 = channel name, $2 = sender function
  # rc captured, not tested via $? in an elif: the status there is the one the
  # if-condition left behind, which is easy to clobber and easy to misread.
  "$2" && rc=0 || rc=$?
  case "$rc" in
    0) DELIVERED="$DELIVERED $1" ;;
    2) SKIPPED="$SKIPPED $1" ;;
    *) FAILED="$FAILED $1" ;;
  esac
}

OLDIFS=$IFS; IFS=','
for ch in $CHANNELS; do
  IFS=$OLDIFS
  case "$ch" in
    local)    dispatch local    send_local ;;
    ntfy)     dispatch ntfy     send_ntfy ;;
    imessage) dispatch imessage send_imessage ;;
    email)    dispatch email    send_email ;;
    *) echo "notify.sh: unknown channel '$ch' (local|ntfy|imessage|email)" >&2; FAILED="$FAILED $ch" ;;
  esac
done
IFS=$OLDIFS

[ -n "$SKIPPED" ] && echo "notify.sh: not configured here, skipped:$SKIPPED" >&2

# Nothing delivered and nothing even tried: this machine cannot reach its owner
# at all. Always an error, -b or not — the whole point of -b is that ANOTHER
# channel carried the message, and here none did.
if [ -z "$DELIVERED" ] && [ -z "$FAILED" ]; then
  echo "notify.sh: no notification channel is configured on this machine" >&2
  exit 1
fi

if [ -n "$FAILED" ]; then
  echo "notify.sh: failed channels:$FAILED" >&2
  # -b treats the alert as delivered as long as one channel carried it. The
  # caller still sees which channel died on stderr; it just is not told the
  # notification failed when the user's phone already buzzed.
  if [ "$BEST_EFFORT" = 1 ] && [ -n "$DELIVERED" ]; then
    echo "notify.sh: delivered via:$DELIVERED (best effort)" >&2
    exit 0
  fi
  exit 1
fi

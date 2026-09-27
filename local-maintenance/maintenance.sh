#!/bin/sh
# Nightly machine maintenance: pending OS/App Store updates, docker prune
# (Sundays), backup freshness, certificate expiry, log rotation, disk space.
# Checks come from maintenance.conf plus maintenance.<profile>.conf in
# <config>/maintenance/ (outside the checkout);
# findings are emailed via local-notify.
# Usage: maintenance.sh run|check|uv-prune|uv-prune-plist|test
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
. "$DIR/../lib/config.sh"

CONF_DIR="$(st_config_dir maintenance)"
CONF="$CONF_DIR/maintenance.conf"
NOTIFY="$DIR/../local-notify/notify.sh"

# Some checks only make sense on one machine, so they live in
# maintenance.<profile>.conf, read after the common file. The profile comes from
# the same state file machine-setup records, so there is one answer per machine
# and nothing new to keep in sync. Both conf files live in <config>/maintenance/;
# the committed maintenance.conf.example is the starting point.
STATE_DIR="${MACHINE_SETUP_STATE:-$HOME/.config/machine-setup}"
PROFILE="${MAINTENANCE_PROFILE:-}"
if [ -z "$PROFILE" ] && [ -f "$STATE_DIR/profile" ]; then
  PROFILE=$(head -1 "$STATE_DIR/profile")
fi
PROFILE_CONF=""
if [ -n "$PROFILE" ] && [ -f "$CONF_DIR/maintenance.$PROFILE.conf" ]; then
  PROFILE_CONF="$CONF_DIR/maintenance.$PROFILE.conf"
fi

usage() { echo "usage: $(basename "$0") run|check|uv-prune|uv-prune-plist|test" >&2; exit 1; }

LABEL_PREFIX="${SYSTEM_TOOLS_LABEL_PREFIX:-local.system-tools}"
UV_PRUNE_LABEL="$LABEL_PREFIX.uv-cache-prune"

# uv-prune: run at login by the uv-cache-prune LaunchAgent ($UV_PRUNE_LABEL,
# rendered from uv-cache-prune.plist.template by `uv-prune-plist`).
# `uv cache prune` needs uv's cache lock to itself, and a long-lived uv server
# holds it while it runs, so a prune taken later in the day waits and gives up.
# At login the only such server is the Google Workspace MCP, a KeepAlive
# LaunchAgent: pause it, prune only when no other uv process is left, then
# start it again whatever the prune did. Never --force: uv's lock is what
# keeps a concurrent uv safe, so a prune that cannot take it does nothing.
#
# An unloaded agent is an outage KeepAlive cannot end, because launchd no
# longer knows the agent. So each paused label is written to UV_PRUNE_STATE
# before its bootout and removed only once the agent is loaded again. A run
# that finds a record restores it first and prunes nothing. The exit status
# is nonzero only while an agent stays down, and the plist's KeepAlive
# (SuccessfulExit false) makes launchd run this again until it is restored.
UV_PRUNE_PAUSE="${UV_PRUNE_PAUSE-$LABEL_PREFIX.google-workspace-mcp}"
UV_PRUNE_STATE="${UV_PRUNE_STATE:-$HOME/.local/state/uv-prune/paused}"
uv_prune() {
  uid=$(id -u)
  paused=""
  if [ -s "$UV_PRUNE_STATE" ]; then
    paused=$(cat "$UV_PRUNE_STATE")
    echo "uv-prune: restoring agents a previous run left down:" $paused
    uv_prune_resume || return 1
    return 0
  fi
  if ! command -v uv >/dev/null 2>&1; then
    echo "uv-prune: uv is not installed; nothing to prune"; return 0
  fi
  cache=$(uv cache dir 2>/dev/null) || cache="$HOME/.cache/uv"
  if [ ! -d "$cache" ]; then
    echo "uv-prune: no cache at $cache; nothing to prune"; return 0
  fi
  trap 'uv_prune_resume' EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  # `set -e` does not reach inside a function called from `||`, so every
  # write is checked by hand: no agent is unloaded unless its label is on
  # disk first, because without the record a failed restart is never retried.
  for label in $UV_PRUNE_PAUSE; do
    launchctl print "gui/$uid/$label" >/dev/null 2>&1 || continue
    if ! { mkdir -p "$(dirname "$UV_PRUNE_STATE")" && echo "$label" >> "$UV_PRUNE_STATE"; } 2>/dev/null; then
      echo "uv-prune: cannot record $label in $UV_PRUNE_STATE; skipped" >&2
      uv_prune_resume || return 1
      return 0
    fi
    paused="$paused $label"
    launchctl bootout "gui/$uid/$label" 2>/dev/null || true
  done
  # Give the paused agents' uv and uvx processes time to exit.
  waited=0
  while { pgrep -x uv || pgrep -x uvx; } >/dev/null 2>&1 && [ "$waited" -lt "${UV_PRUNE_WAIT:-20}" ]; do
    sleep 1; waited=$((waited + 1))
  done
  if pgrep -x uv >/dev/null 2>&1 || pgrep -x uvx >/dev/null 2>&1; then
    echo "uv-prune: another uv process holds the cache; skipped"
    uv_prune_resume || return 1
    return 0
  fi
  before=$(du -sk "$cache" | cut -f1)
  prune_rc=0
  UV_LOCK_TIMEOUT="${UV_PRUNE_LOCK_TIMEOUT:-60}" uv cache prune || prune_rc=$?
  after=$(du -sk "$cache" | cut -f1)
  # A failed prune is logged, not returned: a nonzero exit makes launchd run
  # this again, which would pause the MCP again in a loop.
  echo "uv-prune: $cache $((before / 1024)) MB -> $((after / 1024)) MB (prune exit $prune_rc)"
  uv_prune_resume || return 1
  return 0
}
# Start every paused agent again, retrying a failed bootstrap. An agent
# already loaded counts as restored. What stays down stays in the record and
# fails the run, naming the command that restores it by hand.
uv_prune_resume() {
  # Nothing paused: leave any record alone (the EXIT trap lands here too).
  [ -n "$paused" ] || return 0
  failed=""
  for label in $paused; do
    plist="$HOME/Library/LaunchAgents/$label.plist"
    tries=0
    until launchctl print "gui/$uid/$label" >/dev/null 2>&1 \
        || launchctl bootstrap "gui/$uid" "$plist" 2>/dev/null; do
      tries=$((tries + 1))
      if [ "$tries" -ge 3 ]; then
        echo "uv-prune: $label did not start again; run: launchctl bootstrap gui/$uid $plist" >&2
        failed="$failed $label"
        break
      fi
      sleep "${UV_PRUNE_RESUME_DELAY:-5}"
    done
  done
  paused=""
  if [ -n "$failed" ]; then
    # Replace the record only through a complete new copy, so a failed
    # write keeps the old one, which names every agent this one does.
    { printf '%s\n' $failed > "$UV_PRUNE_STATE.new" && mv "$UV_PRUNE_STATE.new" "$UV_PRUNE_STATE"; } 2>/dev/null \
      || echo "uv-prune: kept the old record in $UV_PRUNE_STATE; could not update it" >&2
    return 1
  fi
  rm -f "$UV_PRUNE_STATE"
}

case "${1:-}" in
  run)   APPLY=1 ;;
  check) APPLY=0 ;;
  uv-prune)
    rc=0; uv_prune || rc=$?
    exit "$rc"
    ;;
  uv-prune-plist)
    # Print the login agent's plist, filled for this checkout and this user.
    sed -e "s|@LABEL@|$UV_PRUNE_LABEL|g" -e "s|@DIR@|$DIR|g" \
        -e "s|@HOME@|$HOME|g" -e "s|@PAUSE@|$UV_PRUNE_PAUSE|g" \
        "$DIR/uv-cache-prune.plist.template"
    exit 0
    ;;
  test)
    shift
    exec "${PYTHON:-python3}" -m unittest discover -s "$DIR/tests" -t "$DIR" "$@"
    ;;
  -h|--help|help)
    cat <<'HELPEOF'
usage: maintenance.sh run|check|uv-prune|uv-prune-plist|test

  run    run all checks, apply the safe fixes (rotate oversized logs; docker
         system prune on Sundays), and email the findings that need a human
         via local-notify's email channel (what the maintenance-nightly cron
         job runs). Exits 1 only when that email could not be delivered.
  check  read-only: run all checks and print findings and would-be fixes
         without changing anything or emailing.
  uv-prune
         prune uv's cache (what the uv-cache-prune login agent runs).
         Pauses the LaunchAgents in UV_PRUNE_PAUSE (default
         $SYSTEM_TOOLS_LABEL_PREFIX.google-workspace-mcp, prefix default
         local.system-tools), skips when any other uv process is
         left, never passes --force, and starts the paused agents again.
         Exits 1 only while a paused agent stays down; its label stays in
         ~/.local/state/uv-prune/paused, and the next run restores it first.
  uv-prune-plist
         print the login agent's plist (label
         $SYSTEM_TOOLS_LABEL_PREFIX.uv-cache-prune), filled from
         uv-cache-prune.plist.template for this checkout and $HOME.
  test   run this tool's unit tests.

Checks are configured in <config>/maintenance/maintenance.conf (<config> is
$SYSTEM_TOOLS_CONFIG, default ~/.config/system; copy
local-maintenance/maintenance.conf.example there), plus
<config>/maintenance/maintenance.<profile>.conf for the ones only one machine wants (profile as
recorded by machine-setup; override with MAINTENANCE_PROFILE). Pipe-separated
directives, # comments allowed; a leading ~/ expands to $HOME:

  disk|<path>|<min-free-percent>      free space on the filesystem of <path>
  cert|<pem>|<warn-days>              x509 expiry within <warn-days>
  mount|<path>                        path is a mounted volume
  process|<name>|<label>              process is running (pgrep -x)
  age|<file>|<max-days>|<label>       file modified within <max-days>
  logs|<dir-or-file>|<max-mb>         rotate *.log files larger than <max-mb>
                                      (tail kept in <name>.1, file truncated
                                      in place so open writers keep going)

Beyond the conf: pending macOS updates (softwareupdate), pending App Store
updates (mas outdated), AI session logs past their retention (deleted via
scan-for-secrets.sh prune, which owns the retention and the directory list;
~/.claude/projects is not on it -- Claude Code deletes its own transcripts),
and on Sundays `docker system prune -f` (images and containers only — volumes
are never pruned).
HELPEOF
    exit 0
    ;;
  *) usage ;;
esac

if [ ! -f "$CONF" ]; then
  echo "maintenance.sh: missing $CONF (copy local-maintenance/maintenance.conf.example to $CONF)" >&2
  exit 1
fi

TMPD=$(mktemp -d)
trap 'rm -rf "$TMPD"' EXIT INT TERM
FINDINGS="$TMPD/findings"   # needs a human — emailed
ACTIONS="$TMPD/actions"     # fixed automatically — footer of the email
: > "$FINDINGS"; : > "$ACTIONS"

finding() { echo "- $1" >> "$FINDINGS"; }
action()  { echo "- $1" >> "$ACTIONS"; }

expand() {
  # shellcheck disable=SC2088  # matching a literal leading ~/ on purpose
  case "$1" in
    "~/"*) echo "$HOME/${1#\~/}" ;;
    *)     echo "$1" ;;
  esac
}

# --- conf-driven checks ------------------------------------------------

check_disk() { # path min-free-percent
  used=$(df -P "$1" 2>/dev/null | awk 'NR==2 { gsub("%",""); print $5 }')
  if [ -z "$used" ]; then
    finding "disk: cannot stat filesystem of $1"
    return 0
  fi
  free=$((100 - used))
  if [ "$free" -lt "$2" ]; then
    finding "disk: only ${free}% free on $1 (threshold ${2}%)"
  fi
}

check_cert() { # pem warn-days
  if [ ! -f "$1" ]; then
    finding "cert: $1 missing"
    return 0
  fi
  if ! openssl x509 -checkend $(( $2 * 86400 )) -noout -in "$1" >/dev/null 2>&1; then
    end=$(openssl x509 -enddate -noout -in "$1" 2>/dev/null | sed 's/notAfter=//')
    finding "cert: $1 expires within $2 days (${end:-unreadable})"
  fi
}

check_mount() { # path
  mount | grep -qF " on $1 " || finding "mount: $1 is not mounted"
}

check_process() { # name label
  pgrep -xq "$1" 2>/dev/null || finding "process: $2 ($1) is not running"
}

check_age() { # file max-days label
  if [ ! -e "$1" ]; then
    finding "age: $3 — $1 missing"
    return 0
  fi
  if [ -z "$(find "$1" -mtime -"$2" 2>/dev/null)" ]; then
    finding "age: $3 — $1 not touched in over $2 day(s)"
  fi
}

# Rotation keeps the writer's open file descriptor valid: the tail is copied
# aside to <name>.1 (overwriting the previous archive) and the file is then
# truncated in place, never unlinked.
check_logs() { # dir-or-file max-mb
  max_bytes=$(( $2 * 1024 * 1024 ))
  if [ -f "$1" ]; then
    set -- "$1" "$2"; targets="$1"
  elif [ -d "$1" ]; then
    targets=$(find "$1" -maxdepth 1 -name '*.log' 2>/dev/null)
  else
    finding "logs: $1 missing"
    return 0
  fi
  [ -n "$targets" ] || return 0
  echo "$targets" | while read -r f; do
    [ -f "$f" ] || continue
    size=$(wc -c < "$f" | tr -d ' ')
    [ "$size" -gt "$max_bytes" ] || continue
    mb=$(( size / 1024 / 1024 ))
    if [ "$APPLY" -eq 1 ]; then
      if tail -n 2000 "$f" > "$f.1" 2>/dev/null && : > "$f"; then
        action "rotated $f (${mb}MB > ${2}MB cap; tail kept in $(basename "$f").1)"
      else
        finding "logs: failed to rotate $f (${mb}MB)"
      fi
    else
      action "[dry-run] would rotate $f (${mb}MB > ${2}MB cap)"
    fi
  done
}

# Not a pipeline: the check_* functions append to $FINDINGS through shell
# variables, and a subshell would swallow half of that.
read_conf() { # file
  conf_name=$(basename "$1")
  lineno=0
  while IFS= read -r raw; do
    lineno=$((lineno + 1))
    line=$(echo "$raw" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')
    case "$line" in ''|'#'*) continue ;; esac
    kind=${line%%|*}; rest=${line#*|}
    a1=${rest%%|*}; rest2=${rest#*|}
    a2=${rest2%%|*}; a3=${rest2#*|}
    a1=$(expand "$a1")
    case "$kind" in
      disk)    check_disk "$a1" "$a2" ;;
      cert)    check_cert "$a1" "$a2" ;;
      mount)   check_mount "$a1" ;;
      process) check_process "$a1" "$a2" ;;
      age)     check_age "$a1" "$a2" "$a3" ;;
      logs)    check_logs "$a1" "$a2" ;;
      *)       finding "conf: unknown directive in $conf_name line $lineno: $raw" ;;
    esac
  done < "$1"
}

read_conf "$CONF"
if [ -n "$PROFILE_CONF" ]; then
  read_conf "$PROFILE_CONF"
fi

# --- built-in checks ---------------------------------------------------

# Pending macOS updates. softwareupdate needs the network and ~10-60s.
SU="$TMPD/su"
if softwareupdate -l > "$SU" 2>&1; then :; fi
if grep -q '^\* Label' "$SU"; then
  finding "macOS updates pending:"
  grep '^\* Label' "$SU" | sed 's/^\* Label: /    /' >> "$FINDINGS"
elif ! grep -q 'No new software available' "$SU"; then
  finding "softwareupdate check failed: $(tail -1 "$SU")"
fi

# Pending App Store updates.
if command -v mas >/dev/null 2>&1; then
  MAS="$TMPD/mas"
  mas outdated > "$MAS" 2>/dev/null || true
  if [ -s "$MAS" ]; then
    finding "App Store updates pending:"
    sed 's/^/    /' "$MAS" >> "$FINDINGS"
  fi
fi

# Weekly docker prune, Sundays only: unused containers and dangling images.
# Volumes are NEVER pruned — several local services keep state in them.
if [ "$(date +%u)" = "7" ]; then
  if docker info >/dev/null 2>&1; then
    if [ "$APPLY" -eq 1 ]; then
      reclaimed=$(docker system prune -f 2>&1 | tail -1)
      action "docker system prune: $reclaimed"
    else
      action "[dry-run] would run docker system prune -f (Sunday)"
    fi
  else
    action "docker prune skipped — daemon not running"
  fi
fi

# AI session logs. They accumulate every day and hold whatever scrolled past —
# a weekly secret scan found ~20 live API keys sitting in codex shell snapshots
# here. Retention (per directory) and the list of directories that hold nothing
# but session data live in scan-for-secrets.sh, which is also what masks them;
# this only schedules the deletion half. Note what it does NOT reach: those
# keys are in current work, not old files, so this prune bounds accumulation
# and remediates no exposure (sd:1254).
S4S="$DIR/../local-scan-for-secrets/scan-for-secrets.sh"
if [ -x "$S4S" ]; then
  # if, not `[ -n "$x" ] && action ...`: under set -e an AND-list that ends
  # false at statement level takes the whole script with it, and on a night
  # with nothing to prune that would kill the report before it is emailed.
  if [ "$APPLY" -eq 1 ]; then
    pruned=$(sh "$S4S" prune --apply 2>&1 | sed -n 's/^  pruned //p')
    if [ -n "$pruned" ]; then
      action "AI session logs: pruned $pruned"
    fi
  else
    pruned=$(sh "$S4S" prune 2>&1 | sed -n 's/^  would prune \(.*\) (dry run.*/\1/p')
    if [ -n "$pruned" ]; then
      action "[dry-run] would prune $pruned of AI session logs"
    fi
  fi
fi

# --- report ------------------------------------------------------------

if [ -s "$FINDINGS" ]; then
  echo "findings (need a human):"
  cat "$FINDINGS"
fi
if [ -s "$ACTIONS" ]; then
  echo "actions:"
  cat "$ACTIONS"
fi
if [ ! -s "$FINDINGS" ] && [ ! -s "$ACTIONS" ]; then
  echo "all checks clean"
fi

if [ "$APPLY" -eq 1 ] && [ -s "$FINDINGS" ]; then
  n=$(grep -c '^- ' "$FINDINGS" | tr -d ' ')
  subject="maintenance: $n finding(s) on $(hostname -s)"
  body=$(printf 'maintenance nightly report — %s on %s\n\nFindings:\n%s\n' \
    "$(date '+%Y-%m-%d %H:%M')" "$(hostname -s)" "$(cat "$FINDINGS")")
  if [ -s "$ACTIONS" ]; then
    body=$(printf '%s\nActions taken:\n%s\n' "$body" "$(cat "$ACTIONS")")
  fi
  if ! sh "$NOTIFY" -t "$subject" -k status -c ntfy,email -b "$body"; then
    echo "email FAILED — exiting 1 so the cron failure push fires" >&2
    exit 1
  fi
fi

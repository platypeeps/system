#!/bin/sh
# After a merge to this checkout, restart only the sd services the merge
# changed, each in its safe form, and report what a restart cannot apply:
# a LaunchAgent change (an install) (sd:2725).
# `upgrade` wraps the whole hub upgrade: pull, apply, record (sd:2812).
# Usage: deploy.sh plan|apply <from-sha> <to-sha>|upgrade [--from SHA]|test|help
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$DIR/.." && pwd)"
# The command pack holds the dashboard since pack sd:5987.
SD_PACK_ROOT="${SD_PACK_ROOT:-$HOME/repos/platypeeps/sd-ai-command-pack}"
# shellcheck source=../lib/config.sh
. "$ROOT/lib/config.sh"
PREFIX="$(st_label_prefix)"
# sd-serve's default port; its plist passes no --port.
SERVE_PORT=8769
# The sha the last complete upgrade deployed; local disk, outside the checkout.
STATE="${XDG_STATE_HOME:-$HOME/.local/state}/system/deploy/deployed"

usage() {
  cat <<'HELPEOF'
usage: deploy.sh plan <from-sha> <to-sha> | apply <from-sha> <to-sha> |
       upgrade [--from SHA] | test | help

  plan    read `git diff --name-only from..to` in this checkout and print one
          action per line; an empty plan prints nothing and exits 0:
            report needs <folder> install (<path>)  a LaunchAgent plist, or the
                    code that writes one, changed; never applied
            restart dashboard   local-project-dashboard/ changed
          Changes under tests/ and to *.md files restart nothing. sd_db, its
          schema and sd-serve are the command pack's, which repo-sync.sh
          refresh moves, so no change here restarts, installs or migrates
          them (sd:3281).
  apply   print the reports, then restart. dashboard: `launchctl kickstart
          -k`, then `dashboard.sh health --wait DEPLOY_WAIT`. A service whose
          agent is not loaded is skipped. A dashboard agent that still runs
          the deleted local-project-dashboard is not restarted or started:
          the run exits 1 naming the pack's install. Stops at the first
          failed check and exits 1 naming it.
  upgrade [--from SHA]
          refuse unless this checkout is on main and clean; `git fetch
          origin`, then fast-forward to origin/main (refuse on divergence).
          From is the sha the last complete upgrade recorded in
          $XDG_STATE_HOME/system/deploy/deployed (default
          ~/.local/state/system/deploy/deployed); with no record, --from is
          required, and --from always overrides it. An empty plan records the
          new sha and exits 0. Then apply, and a start of each agent an older
          upgrade left stopped (the file `stopped` beside the record):
          sd-serve, then the dashboard (`health --wait`); a start whose wait
          fails boots its agent out again, and a failure names the rerun that
          finishes; it bootstraps only those launchd does not hold. It records
          the new sha only when every step passed and no LaunchAgent install
          is reported, so a rerun replays the same range.
  test    run the unittest suite (tests/); extra arguments go to unittest.

env:
  SYSTEM_TOOLS_LABEL_PREFIX  launchd label prefix (default local.system-tools)
  DEPLOY_WAIT                seconds to wait for sd-serve's listener and the
                             dashboard's health (default 30)
  XDG_STATE_HOME             holds upgrade's record (default ~/.local/state)
HELPEOF
}

# The changed paths, minus tests and docs, limited to the pathspecs given.
changed() {
  git -C "$ROOT" diff --name-only "$RANGE" -- "$@" ':!*/tests/*' ':!*.md'
}

# One report per LaunchAgent plist, or plist-writing code, the range changed.
installs() {
  # ponytail: a plist is code in a tool (the dashboard's runtime.py), so
  # this greps changed lines for launchd keys and the names that feed them; a
  # value computed elsewhere goes unseen. Upgrade: render each plist at both
  # commits and compare. local-deploy names those keys but writes no plist.
  { changed '*.plist' '*.plist.template'
    git -C "$ROOT" diff --name-only "$RANGE" \
      -G'plistlib|ProgramArguments|EnvironmentVariables|KeepAlive|RunAtLoad|ProcessType|ThrottleInterval|Standard(Out|Error)Path|_launch_environment|PASSED_THROUGH|LAUNCH_PATH|SD_DASHBOARD_PYTHON' \
      -- '*.py' '*.sh' ':!*/tests/*' ':!local-deploy/*'
  } | sort -u | while read -r path; do
    echo "report needs ${path%%/*} install ($path)"
  done
}

plan() {
  RANGE="$1..$2"
  git -C "$ROOT" rev-parse -q --verify "$1^{commit}" >/dev/null || { echo "deploy: not a commit: $1" >&2; exit 1; }
  git -C "$ROOT" rev-parse -q --verify "$2^{commit}" >/dev/null || { echo "deploy: not a commit: $2" >&2; exit 1; }
  # sd_db, its schema and sd-serve are the pack's (sd:3281): refresh moves
  # them, so no change here restarts sd-serve or installs or migrates a library.
  installs
  [ -z "$(changed local-project-dashboard/)" ] || echo "restart dashboard"
}

# Exits 0 when launchd holds the agent, else 1.
held() {
  launchctl print "gui/$(id -u)/$PREFIX.$1" >/dev/null 2>&1
}

refuse() {
  echo "deploy: refused before any restart: $1" >&2
  stopped_hint
  exit 1
}

fail() {
  echo "deploy: $1 failed; stopped" >&2
  # A start whose wait failed boots its agent out again, so the marker holds
  # true and a rerun bootstraps it once.
  [ -z "${starting:-}" ] || launchctl bootout "gui/$(id -u)/$starting" 2>/dev/null || true
  stopped_hint
  exit 1
}

# STOPPED lists the agents an upgrade stopped and has not started again.
# This version stops none; an older one stopped them to install sd_db, and a
# start whose wait fails stops its agent again (sd:3279).
stopped_hint() {
  [ -z "${STOPPED:-}" ] || [ ! -s "$STOPPED" ] ||
    echo "deploy: left stopped: $(tr '\n' ' ' < "$STOPPED" | sed 's/ $//'); finish the upgrade with: sh $ROOT/local-deploy/deploy.sh upgrade --from $from" >&2
}

# One upgrade at a time, from the first check to the record: a second one
# could start an agent while the first still restarts it. flock(2) on
# upgrade.lock, held by the descriptor the upgrade inherits through exec: the
# kernel drops it when the upgrade ends, so no lock outlives its holder.
LOCKER='import fcntl, os, sys
fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR, 0o600)
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    sys.exit("deploy: refused before any restart: another upgrade holds " + sys.argv[1])
os.set_inheritable(fd, True)
os.execvp(sys.argv[2], sys.argv[2:])'


# The pids lsof finds in one TCP state on sd-serve's port. lsof -t exits 1
# and prints nothing for no match; any other answer, or no lsof at all, says
# nothing about the port and fails.
serve_pids() {
  out=$(lsof -w -nP -iTCP:$SERVE_PORT -sTCP:$1 -t 2>&1) && { echo "$out"; return 0; }
  rc=$?
  [ "$rc" -eq 1 ] && [ -z "$out" ] && return 0
  echo "lsof exited $rc: $out" >&2
  return 1
}

# Waits for a listener on sd-serve's port after a start; sets new.
await_listener() {
  waited=0
  while :; do
    new=$(serve_pids LISTEN) || fail "sd-serve listener check (lsof)"
    if [ -n "$new" ]; then
      return 0
    fi
    [ "$waited" -lt "${DEPLOY_WAIT:-30}" ] || fail "sd-serve listener check (no new listener on port $SERVE_PORT)"
    sleep 1
    waited=$((waited + 1))
  done
}

# Upgrade starts every agent it stopped once apply ends, so only an agent
# nobody stopped is reported as skipped (sd:2837).
skip() { # service, agent
  stopped "$PREFIX.$2" || echo "skip $1: $PREFIX.$2 is not loaded"
}

apply() {
  actions=$(plan "$1" "$2")
  reports=$(printf '%s\n' "$actions" | grep '^report') || true
  [ -z "$reports" ] || printf '%s\n' "$reports"
  services=$(printf '%s\n' "$actions" | sed -n 's/^restart //p')
  for service in $services; do
    case "$service" in
      dashboard) held sd-dashboard || { skip dashboard sd-dashboard; continue; }
        old_dashboard "$PREFIX.sd-dashboard"
        launchctl kickstart -k "gui/$(id -u)/$PREFIX.sd-dashboard" || fail "dashboard kickstart"
        sh "$SD_PACK_ROOT/dashboard/dashboard.sh" health --wait "${DEPLOY_WAIT:-30}" || fail "dashboard health"
        echo "restarted dashboard" ;;
    esac
  done
}

loaded() {
  launchctl print "gui/$(id -u)/$1" >/dev/null 2>&1
}

# The dashboard left this repo for the pack (pack sd:5987). An agent whose
# loaded job or installed plist still runs the deleted folder cannot start
# again, so refuse before a restart stops the running one.
old_dashboard() { # label
  { launchctl print "gui/$(id -u)/$1" 2>/dev/null
    cat "$HOME/Library/LaunchAgents/$1.plist" 2>/dev/null; } | grep -qF local-project-dashboard || return 0
  echo "deploy: not restarting $1: it still runs local-project-dashboard, which is gone; install the pack's: sh $SD_PACK_ROOT/dashboard/dashboard.sh preflight, then install --apply --expected-fingerprint <sha>, then rerun this deploy" >&2
  stopped_hint
  exit 1
}

stopped() {
  [ -s "$STOPPED" ] && grep -qxF "$1" "$STOPPED"
}

# A marked agent that launchd holds already is not bootstrapped again; it
# still passes its check.
start_agent() {
  case "$1" in *.sd-dashboard) old_dashboard "$1" ;; esac
  loaded "$1" || launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/$1.plist" || fail "$1 bootstrap"
  starting="$1"
  case "$1" in
    *.sd-serve) await_listener ;;
    *.sd-dashboard) sh "$SD_PACK_ROOT/dashboard/dashboard.sh" health --wait "${DEPLOY_WAIT:-30}" ||
      fail "dashboard health" ;;
  esac
  starting=""
  grep -vxF "$1" "$STOPPED" > "$STOPPED.$$" || true
  mv "$STOPPED.$$" "$STOPPED"
  echo "started $1"
}

# sd-serve first, the dashboard last.
start_stopped() {
  [ -s "$STOPPED" ] || return 0
  for label in $(grep -v '\.sd-dashboard$' "$STOPPED") $(grep '\.sd-dashboard$' "$STOPPED"); do
    start_agent "$label"
  done
  rm -f "$STOPPED"
}

upgrade() {
  from=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --from) [ $# -ge 2 ] || { usage >&2; exit 1; }; from="$2"; shift 2 ;;
      *) echo "deploy: unknown option: $1" >&2; usage >&2; exit 1 ;;
    esac
  done
  # The agents an upgrade stopped, one label a line, each once; a rerun starts them.
  STOPPED="$(dirname "$STATE")/stopped"
  mkdir -p "$(dirname "$STATE")"
  [ "$(git -C "$ROOT" symbolic-ref -q --short HEAD)" = main ] || refuse "$ROOT is not on main"
  [ -z "$(git -C "$ROOT" status --porcelain)" ] || refuse "$ROOT has uncommitted or untracked files"
  git -C "$ROOT" fetch -q origin || fail "git fetch origin"
  git -C "$ROOT" merge-base --is-ancestor HEAD origin/main ||
    refuse "main and origin/main have diverged; reconcile them by hand"
  git -C "$ROOT" merge -q --ff-only origin/main || fail "fast-forward to origin/main"
  to=$(git -C "$ROOT" rev-parse HEAD)
  [ -n "$from" ] || from=$(cat "$STATE" 2>/dev/null) || true
  [ -n "$from" ] || refuse "no deployed sha recorded in $STATE; give --from SHA, the sha the services run now"
  actions=$(plan "$from" "$to")
  if [ -z "$actions" ] && [ ! -s "$STOPPED" ]; then
    echo "nothing to deploy from $from to $to"
    record "$to"
    return 0
  fi
  apply "$from" "$to"
  start_stopped
  # A restart keeps the old LaunchAgent: recording would drop the report.
  case "$actions" in *"install ("*)
    echo "deploy: a LaunchAgent install is pending (reports above); $to not recorded; install it, then record it: deploy.sh upgrade --from $to" >&2
    exit 1 ;;
  esac
  record "$to"
}

record() {
  mkdir -p "$(dirname "$STATE")"
  printf '%s\n' "$1" > "$STATE.$$"
  mv "$STATE.$$" "$STATE"
  echo "deployed $1"
}

case "${1:-}" in
  plan|apply)
    [ $# -eq 3 ] || { usage >&2; exit 1; }
    "$1" "$2" "$3" ;;
  upgrade)
    # The fast-forward can rewrite this file while sh reads it: exit here, so
    # nothing past this line is read. The functions above, from before the
    # pull, run the whole upgrade; a change to deploy.sh applies next time.
    shift
    if [ -z "${DEPLOY_UPGRADE_LOCKED:-}" ]; then
      mkdir -p "$(dirname "$STATE")"
      DEPLOY_UPGRADE_LOCKED=1 exec "${PYTHON:-python3}" -c "$LOCKER" "$(dirname "$STATE")/upgrade.lock" \
        sh "$DIR/deploy.sh" upgrade "$@"
    fi
    upgrade "$@"
    exit 0 ;;
  test)
    shift
    exec "${PYTHON:-python3}" -m unittest discover -s "$DIR/tests" -t "$DIR" "$@" ;;
  -h|--help|help) usage ;;
  '') usage >&2; exit 1 ;;
  *) echo "deploy: unknown command: $1" >&2; usage >&2; exit 1 ;;
esac

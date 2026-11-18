#!/usr/bin/env bash
# cswap.sh — one Claude account across the fleet (sd:7323).
#
# Remote Control sees only the sessions of the active account, so every
# machine must use the account the hub uses. The hub picks it; satellites
# follow. A local-cron-jobs job runs `sync` every minute on every machine
# (local-cron-jobs/examples/cswap-sync.job).
#
# Usage:
#   cswap.sh sync     lead or follow, by this machine's role
#   cswap.sh status   same account as the hub? (exit 0 yes, 1 no, 3 nothing to check)
#
# The role is the fence's: the machine whose `sd-db fence` reads holder (exit
# 0) leads, so a promoted satellite leads by itself. Any other answer follows.
# The account travels as its organizationUuid in the workflow database row
# sys.cswap_account (sd-plugin.json declares it); account numbers differ
# between machines, so they never travel.
#
# Flags for `cswap auto` live in cswap-auto.conf.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/config.sh
. "$ROOT/../lib/config.sh"
# shellcheck source=../lib/bounded.sh
. "$ROOT/../lib/bounded.sh"

# The always-on `cswap auto` LaunchAgent this script used to install. Sync
# replaces it: a machine that still runs it rotates on its own.
RETIRED_LABEL="$(st_label_prefix).cswap-auto"
RETIRED_SERVICE="gui/$(id -u)/$RETIRED_LABEL"
RETIRED_PLIST="$HOME/Library/LaunchAgents/$RETIRED_LABEL.plist"

PACK="${SD_PACK_ROOT:-$HOME/repos/platypeeps/sd-workflow}"
SETTING="sys.cswap_account"
UUID_RE='^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
# Consecutive sync runs whose read the hub refused for this machine's sd_db
# build, or found the hub unreachable (sd:7650), and the run that fails
# (system sd:7407). The follow job runs every
# 5 minutes and its follow takes up to 10; 30 minute runs cover two of its
# slots, so only a follow that keeps failing fails here too.
REFUSALS="$ROOT/state/build-refusals"
REFUSAL_LIMIT=30

# Remote Control (sd:9427): a switch drops every session's link, and /rc in
# a dropped session reconnects it under the new account. A switch writes
# each claude pane herdr lists here as owed (pane, session, epoch); every
# sync sends /rc to an owed pane that is idle or done and whose screen reads
# "Remote Control disconnected", not "/remote-control is active". A working
# or blocked pane gets no keys, as a key can answer a permission dialog; it
# stays owed for the next sync, until sent, gone, connected or RC_OWED_FOR
# seconds old. Cron's PATH may lack herdr, so HERDR_DIRS is tried after it.
RC_OWED="$ROOT/state/rc-owed"
RC_OWED_FOR=1800
HERDR_DIRS="${CSWAP_HERDR_DIRS-$HOME/.local/bin /opt/homebrew/bin /usr/local/bin}"
SWITCHED=""
HERDR=""
AGENTS=""

# shellcheck source=/dev/null
[[ -f "$ROOT/cswap-auto.conf" ]] && . "$ROOT/cswap-auto.conf"
CSWAP_BIN="${CSWAP_BIN:-$HOME/.local/bin/cswap}"
CSWAP_AUTO_ARGS="${CSWAP_AUTO_ARGS:-}"

# A failed step: one stderr line, one data-safe report line, exit 1. No
# account moves on a failure, so no data is at risk; the step names stay
# fixed so a repeating failure folds into one open report.
fail() { # step, message
  echo "cswap sync: $2" >&2
  echo "SD_REPORT_ATTENTION: data safe, one step failed: cswap sync $1"
  exit 1
}

# Whether this machine leads. The words count only with exit 0: a fence
# that fails is no answer, and an unknown answer must not make a second
# leader.
leads() {
  local out
  out=$(st_bounded 60 "$PACK/bin/sd-db" fence 2>/dev/null) || return 1
  case "$out" in *"fence: holder:"*) return 0 ;; esac
  return 1
}

# `cswap list --json` on stdout, bounded.
accounts() {
  st_bounded 60 "$CSWAP_BIN" list --json
}

# Reads `cswap list --json` on stdin. `active` prints "<number> <uuid>" of
# the active account; `number UUID` prints the number of the one account
# with that organizationUuid, exit 4 when none has it, 5 (and the count)
# when several do.
pick() { # active | number UUID
  python3 -I -c '
import json, sys
data = json.load(sys.stdin)
accounts = data.get("accounts") or []
if sys.argv[1] == "active":
    found = [a for a in accounts if a.get("number") == data.get("activeAccountNumber")]
    if len(found) != 1:
        sys.exit(3)
    print(found[0]["number"], found[0].get("organizationUuid") or "-")
else:
    found = [a["number"] for a in accounts if a.get("organizationUuid") == sys.argv[2]]
    if not found:
        sys.exit(4)
    if len(found) > 1:
        print(len(found))
        sys.exit(5)
    print(found[0])
' "$@"
}

# The published row's value on stdout. Exit 1 with sd's own words on stderr
# when it cannot be read, is unset or is no UUID: each is no answer.
# SD_FOLLOWED is the pack's marker (lib/sd_db/follow.py): with it, a hub
# that refuses this sd_db build is refused at once, instead of sd running
# `repo-sync.sh follow` for up to 600 s, which the 60 s bound cut part way.
published() {
  local value
  value=$(SD_FOLLOWED=1 st_bounded 60 "$PACK/bin/sd" config get "$SETTING") || return 1
  if [[ ! "$value" =~ $UUID_RE ]]; then
    echo "$SETTING holds '$value', which is no organizationUuid" >&2
    return 1
  fi
  printf '%s\n' "$value"
}

# Boot out the retired agent and delete its plist. launchd loads a plist
# left behind again at the next login, so the file goes even when launchd
# no longer holds the label.
retire_agent() {
  if launchctl print "$RETIRED_SERVICE" >/dev/null 2>&1; then
    launchctl bootout "$RETIRED_SERVICE" ||
      fail retire "could not boot out the retired $RETIRED_LABEL agent; run: launchctl bootout $RETIRED_SERVICE"
    echo "retired: booted out $RETIRED_LABEL"
  fi
  if [[ -e "$RETIRED_PLIST" ]]; then
    rm -f "$RETIRED_PLIST" || fail retire "could not delete $RETIRED_PLIST"
    echo "retired: deleted $RETIRED_PLIST"
  fi
}

# The herdr binary on stdout: PATH first, then HERDR_DIRS.
herdr_bin() {
  local d
  command -v herdr 2>/dev/null && return 0
  for d in $HERDR_DIRS; do
    [[ -x "$d/herdr" ]] && { echo "$d/herdr"; return 0; }
  done
  return 1
}

# Sets HERDR and AGENTS (`herdr agent list` JSON), or prints one skip line
# and exits 1: no herdr, or no herdr server answering.
herdr_agents() {
  if ! HERDR=$(herdr_bin); then
    echo "reconnect: skipped: no herdr on PATH${HERDR_DIRS:+ or in $HERDR_DIRS}"
    return 1
  fi
  AGENTS=$(st_step 20 "$HERDR" agent list) ||
    { echo "reconnect: skipped: herdr agent list did not answer; is herdr running?"; return 1; }
}

# The account just switched: every claude pane owes /rc. Written at once,
# before a later step can fail, so the next sync still sends it.
owe_rc() {
  SWITCHED=1
  herdr_agents || return 0
  if { mkdir -p "${RC_OWED%/*}" && python3 -I -c '
import json, sys, time
for a in json.loads(sys.stdin.read() or "{}").get("result", {}).get("agents", []):
    if a.get("agent") == "claude" and a.get("pane_id"):
        print(a["pane_id"], (a.get("agent_session") or {}).get("value") or "-", int(time.time()))
' <<<"$AGENTS" >"$RC_OWED.new" && mv "$RC_OWED.new" "$RC_OWED"; } 2>/dev/null; then
    echo "reconnect: $(wc -l <"$RC_OWED" | tr -d ' ') claude pane(s) owe /rc"
  else
    echo "reconnect: skipped: could not write $RC_OWED"
  fi
}

# Send /rc to each owed pane that may have it; keep the rest owed.
reconnect() {
  local pane session since action screen plan keep=""
  [[ -s "$RC_OWED" ]] || return 0
  herdr_agents || return 0
  plan=$(python3 -I -c '
import json, sys, time
agents = {a.get("pane_id"): a for a in json.loads(sys.argv[2] or "{}").get("result", {}).get("agents", [])}
for line in open(sys.argv[1]):
    try:
        pane, session, since = line.split()
        since = int(since)
    except ValueError:
        continue
    a = agents.get(pane)
    if not a or a.get("agent") != "claude" or ((a.get("agent_session") or {}).get("value") or "-") != session:
        print("gone", pane, session, since)
    elif time.time() - since > int(sys.argv[3]):
        print("old", pane, session, since)
    elif a.get("agent_status") in ("idle", "done"):
        print("try", pane, session, since)
    else:
        print("keep", pane, session, since)
' "$RC_OWED" "$AGENTS" "$RC_OWED_FOR") ||
    { echo "reconnect: skipped: herdr's agent list or $RC_OWED was not readable"; return 0; }
  while read -r action pane session since; do
    case "$action" in
      keep) keep+="$pane $session $since"$'\n' ;;
      gone) echo "reconnect: $pane is gone or runs another session; not owed" ;;
      old) echo "reconnect: $pane stayed busy or showed no drop for ${RC_OWED_FOR}s; not owed" ;;
      try)
        if ! screen=$(st_step 10 "$HERDR" agent read "$pane"); then
          keep+="$pane $session $since"$'\n'
        elif [[ "$screen" == *"/remote-control is active"* ]]; then
          echo "reconnect: $pane is connected; not owed"
        elif [[ "$screen" != *"Remote Control disconnected"* ]]; then
          keep+="$pane $session $since"$'\n'
        elif st_step 10 "$HERDR" agent prompt "$pane" /rc >/dev/null; then
          echo "reconnect: sent /rc to $pane"
        else
          keep+="$pane $session $since"$'\n'
        fi ;;
    esac
  done <<<"$plan"
  if [[ -z "$keep" ]]; then
    rm -f "$RC_OWED" 2>/dev/null || :
  else
    { printf '%s' "$keep" >"$RC_OWED.new" && mv "$RC_OWED.new" "$RC_OWED"; } 2>/dev/null ||
      echo "reconnect: could not update $RC_OWED"
    echo "reconnect: $(printf '%s' "$keep" | wc -l | tr -d ' ') pane(s) still owe /rc; the next sync tries again"
  fi
}

# The hub: one `cswap auto` tick, then publish the active account. The row
# is written only when it differs (one database row per write), and is
# compared on every run, so a failed write is retried by the next one.
lead() {
  local rc=0 json active row number uuid
  # shellcheck disable=SC2086 # deliberate word splitting of the configured flags
  st_bounded 120 "$CSWAP_BIN" auto --once $CSWAP_AUTO_ARGS --strategy consume-first || rc=$?
  [[ "$rc" != 0 ]] || owe_rc
  json=$(accounts) || fail list "cswap list --json failed"
  active=$(pick active <<<"$json") || fail list "cswap names no single active account"
  read -r number uuid <<<"$active"
  set -- "$number" "$uuid"
  [[ "$2" =~ $UUID_RE ]] || fail list "account $1 has no organizationUuid"
  row=$(published 2>/dev/null) || row=""
  if [[ "$row" != "$2" ]]; then
    st_bounded 60 "$PACK/bin/sd" config set "$SETTING" "$2" >/dev/null ||
      fail publish "could not write $SETTING; satellites keep the last account"
    echo "leads: published account $1 ($2)"
  else
    echo "leads: account $1 ($2), already published"
  fi
  # --once exits 0 switched, 2 no action, 3 blocked (no viable target).
  case "$rc" in
    0 | 2 | 3) ;;
    *) fail auto "cswap auto exited $rc; account $1 stays published" ;;
  esac
}

# A read the hub refused for this machine's sd_db build: the follow job has
# not caught up yet. Or a hub this machine cannot reach: the tailnet dropped
# for a minute or two (sd:7650). Skip the run and count it; fail from the
# REFUSAL_LIMIT-th in a row. A count that cannot be written fails at once:
# skipping without a count would never fail. $1 is build or unreachable.
# The two causes share one count, so the failure names both and this run's
# (sd:7722).
refused_build() {
  local runs now skip
  if [[ "$1" == unreachable ]]; then
    now="the hub is unreachable"
    skip="skipped: the hub is unreachable"
  else
    now="the hub refuses this sd_db build"
    skip="skipped: the hub refuses this sd_db build until repo-sync.sh follow catches up"
  fi
  runs=$(cat "$REFUSALS" 2>/dev/null) || runs=0
  [[ "$runs" =~ ^[0-9]+$ ]] || runs=0
  runs=$((runs + 1))
  { mkdir -p "${REFUSALS%/*}" && printf '%s\n' "$runs" >"$REFUSALS"; } 2>/dev/null ||
    fail read "$now, and $REFUSALS cannot be written; nothing switched"
  if ((runs >= REFUSAL_LIMIT)); then
    fail read "$runs runs in a row could not read the hub's account, each one a refused sd_db build or an unreachable hub; this run: $now; check repo-sync.sh follow, Tailscale and the hub; nothing switched"
  fi
  echo "$skip (run $runs of $REFUSAL_LIMIT); nothing switched"
}

# A satellite: switch to the local account with the published UUID. An
# unreadable hub, an unset row and a UUID no single account here has all
# change nothing.
follow() {
  local want json active number rc=0 err
  err=$(mktemp)
  if ! want=$(published 2>"$err"); then
    cat "$err" >&2
    if grep -q "sd_db build mismatch" "$err"; then
      rm -f "$err"
      refused_build build
      return 0
    fi
    if grep -Eq "HubUnreachable|Tailscale does not name this node" "$err"; then
      rm -f "$err"
      refused_build unreachable
      return 0
    fi
    rm -f "$err"
    fail read "cannot read the hub's account ($SETTING); nothing switched"
  fi
  rm -f "$err"
  # A count left behind only makes the next refusal fail sooner.
  rm -f "$REFUSALS" 2>/dev/null || :
  json=$(accounts) || fail list "cswap list --json failed; nothing switched"
  active=$(pick active <<<"$json") || fail list "cswap names no single active account; nothing switched"
  number=$(pick number "$want" <<<"$json") || rc=$?
  case "$rc" in
    0) ;;
    4) fail map "no account here has organizationUuid $want; add the hub's account with cswap add" ;;
    5) fail map "$number accounts here have organizationUuid $want; nothing switched" ;;
    *) fail map "cswap list --json was not readable; nothing switched" ;;
  esac
  if [[ "${active%% *}" == "$number" ]]; then
    echo "following the hub: account $number already active"
    return 0
  fi
  st_bounded 60 "$CSWAP_BIN" switch "$number" >/dev/null ||
    fail switch "cswap switch $number failed; the next run tries again"
  echo "following the hub: switched to account $number"
  owe_rc
}

cmd_sync() {
  retire_agent
  [[ -x "$CSWAP_BIN" ]] || fail list "cswap not executable at $CSWAP_BIN"
  # Panes an earlier run left owed, even one that failed after its switch.
  reconnect
  if leads; then lead; else follow; fi
  [[ -z "$SWITCHED" ]] || reconnect
}

cmd_status() {
  local json active want number uuid err
  if launchctl print "$RETIRED_SERVICE" >/dev/null 2>&1 || [[ -e "$RETIRED_PLIST" ]]; then
    echo "retired $RETIRED_LABEL agent still installed; the next cswap.sh sync removes it"
    exit 1
  fi
  if [[ ! -x "$CSWAP_BIN" ]]; then
    echo "cswap not installed at $CSWAP_BIN: nothing to check"
    exit 3
  fi
  json=$(accounts) || { echo "cswap list --json failed"; exit 1; }
  active=$(pick active <<<"$json") || { echo "cswap names no single active account"; exit 1; }
  read -r number uuid <<<"$active"
  set -- "$number" "$uuid"
  err=$(mktemp)
  if ! want=$(published 2>"$err"); then
    case "$(cat "$err")" in
      *"is not set"*) rm -f "$err"; echo "no account published in $SETTING yet: nothing to check"; exit 3 ;;
    esac
    echo "cannot read the hub's account ($SETTING): $(cat "$err")"
    rm -f "$err"
    exit 1
  fi
  rm -f "$err"
  if leads; then
    if [[ "$want" == "$2" ]]; then
      echo "leads: account $1, published"
      exit 0
    fi
    echo "leads: account $1 here, but $SETTING names another; the next sync publishes it"
    exit 1
  fi
  if [[ "$want" == "$2" ]]; then
    echo "following the hub, same account ($1)"
    exit 0
  fi
  if number=$(pick number "$want" <<<"$json"); then
    echo "account $1 here, the hub is on account $number"
  else
    echo "no account here has the hub's organizationUuid $want"
  fi
  exit 1
}

case "${1:-}" in
  sync)   cmd_sync ;;
  status) cmd_status ;;
  -h|--help|help)
    cat <<'HELPEOF'
usage: cswap.sh sync|status

  sync     the hub (sd-db fence reads holder) runs one
           `cswap auto --once --strategy consume-first` tick and publishes
           its active account's organizationUuid as sys.cswap_account;
           any other machine switches to its account with that id. An
           unreadable hub or an id no single account here has switches
           nothing and exits 1. A hub that refuses this machine's
           sd_db build skips the run (exit 0) until repo-sync.sh follow
           catches up, and fails from the 30th such run in a row. It
           also boots out the retired cswap-auto LaunchAgent. After a
           switch it sends /rc through herdr to each idle claude pane
           whose Remote Control dropped, and retries a busy one on the
           next sync; no herdr is one skip line.
           local-cron-jobs runs it every minute.
  status   whether this machine uses the hub's account; exits 0 same
           account / 1 another account, hub unreadable or the retired
           agent still installed / 3 nothing to check (no cswap, nothing
           published) — local-health-check reads these codes

environment: CSWAP_BIN, CSWAP_AUTO_ARGS (see cswap-auto.conf),
             SD_PACK_ROOT (default ~/repos/platypeeps/sd-workflow),
             CSWAP_HERDR_DIRS (where to look for herdr after PATH;
             default ~/.local/bin /opt/homebrew/bin /usr/local/bin)
HELPEOF
    exit 0
    ;;
  *)
    echo "usage: $(basename "$0") sync|status" >&2
    exit 1
    ;;
esac

#!/bin/sh
# fseventsd's resident size, and a TERM when the daemon is both oversized and
# hurting the machine (sd:8579).
# Usage: fseventsd.sh status|repair|check|test|help
#
# On 2026-11-11 fseventsd held 28.77 GB resident on one machine, against
# Apple's own footprint budget of about 30 MB, with swap at 19.70 of 20.48 GB
# and kern.memorystatus_vm_pressure_level at 2 (fleet sd:8557). There is no
# vendor fix. launchd respawns the daemon, and System Integrity Protection
# refuses `launchctl kickstart` on it, so TERM is the whole repair.
#
# Two conditions, never one: a large fseventsd on a machine with memory to
# spare is doing its job, and the TERM drops the FSEvents history that every
# backup and indexing tool reads. So nothing is signalled until the machine is
# actually hurting, and FSEVENTSD_MIN_HOURS keeps an hourly job from thrashing
# a daemon that grows back fast.
#
# A sysctl that does not answer leaves "is the machine hurting?" unknown. That
# is neither a yes nor a no: status reports it and repair sends no TERM.
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
. "$DIR/../lib/config.sh"
. "$DIR/../lib/bounded.sh"

TOOL=fseventsd
# sudo asks machine-setup's askpass for the Keychain item's password when no
# sudoers line covers the command (sd:6312), as local-maintenance does. With
# neither the line nor the item, sudo refuses and repair reports its reason.
SUDO_ASKPASS="$DIR/../local-machine-setup/sudo-askpass.sh"
export SUDO_ASKPASS
# The exact command the machine-setup-repairs sudoers line names, with its
# exact arguments: pkill by name, not kill by pid, because a pid argument
# would need a wildcard in that line and a wildcard there allows any pid.
TERM_COMMAND="/usr/bin/pkill -TERM -x fseventsd"

usage() {
  cat <<'USAGE'
fseventsd.sh — fseventsd's resident size, and a TERM when it hurts.

Usage: fseventsd.sh <command>

  status   One line, starting with the daemon's resident size, so the trend
           is visible on a healthy machine too. Exits 3 when there is
           nothing to check: FSEVENTSD_RSS_MB unset, not macOS, or no
           fseventsd running. Exits 1 when the daemon is over
           FSEVENTSD_RSS_MB and the machine is hurting with it, or when
           the pressure readings did not answer. Exits 0 otherwise --
           local-health-check reads these codes.
  repair   The same verdict, and only on a 1 that the machine is hurting, a
           TERM to the daemon through sudo -A; launchd respawns it. At most
           one TERM every FSEVENTSD_MIN_HOURS. Exits 1 when the TERM failed
           or the verdict was unknown, 3 when there is nothing to check,
           0 otherwise -- a refused TERM is a cron-jobs failure.
  check    One line per reading, then the status line, with status's codes.
  test     Run this folder's tests (extra args go to unittest).
  help     This text.

Configuration: $SYSTEM_TOOLS_CONFIG/fseventsd/.env or exported (copy
.env.example).
  FSEVENTSD_RSS_MB     resident megabytes the daemon may hold; 4096 in the
                       example. Unset, there is nothing to check, so a
                       machine that has not been set up stays silent.
  FSEVENTSD_SWAP_MB    swap megabytes in use that counts as hurting,
                       default 8192 -- the number local-health-check
                       already uses, not a second one.
  FSEVENTSD_MIN_HOURS  hours between two TERMs, default 6; 0 for no floor.

The TERM needs the machine-setup-repairs sudoers line, or the
machine-setup-sudo Keychain item (local-machine-setup/README.md,
"Unattended sudo"). With neither, sudo refuses and repair says so.
USAGE
}

# whole <value> <low> <high>: 0 when value is a whole number in that range.
whole() {
  case "$1" in ''|*[!0-9]*) return 1 ;; esac
  [ "$1" -ge "$2" ] || return 1
  [ "$1" -le "$3" ] || return 1
  return 0
}

# fs_daemon: "<pid> <resident-kb>" of the running fseventsd, or nothing. The
# pid and size are read together, so the size reported belongs to the pid
# reported. comm is the last field and can hold spaces, so it is what is left
# after the two numbers, and its basename is matched.
fs_daemon() {
  ps -axo pid=,rss=,comm= 2>/dev/null | awk '
    {
      daemon_pid = $1; daemon_rss = $2; command = $0
      sub(/^[ \t]*[0-9]+[ \t]+[0-9]+[ \t]+/, "", command)
      parts = split(command, part, "/")
      if (part[parts] == "fseventsd") { print daemon_pid, daemon_rss; exit }
    }'
}

# fs_pressure_level: kern.memorystatus_vm_pressure_level, or nothing when the
# read gave no whole number. Nothing means unknown, not level 0.
fs_pressure_level() {
  sysctl -n kern.memorystatus_vm_pressure_level 2>/dev/null |
    awk 'NR == 1 && $1 ~ /^[0-9]+$/ { print $1 }'
}

# fs_swap_used_mb: the used megabytes of vm.swapusage, or nothing when the
# read gave nothing it could parse. macOS prints
# "total = 20480.00M  used = 19700.25M  free = 779.75M  (encrypted)".
fs_swap_used_mb() {
  sysctl -n vm.swapusage 2>/dev/null | awk '
    NR == 1 {
      for (i = 1; i + 2 <= NF; i++) if ($i == "used" && $(i + 1) == "=") {
        value = $(i + 2)
        unit = substr(value, length(value))
        megabytes = value + 0
        if (unit == "G") megabytes = megabytes * 1024
        else if (unit == "K") megabytes = megabytes / 1024
        else if (unit != "M") exit
        printf "%d\n", megabytes
        exit
      }
    }'
}

# fs_collect: every reading status, check and repair work from.
# Sets fs_state (unconfigured|bad-config|not-darwin|no-daemon|measured), the
# limits, and on `measured` fs_pid, fs_rss_mb, fs_pressure and fs_swap_mb. An
# empty fs_pressure or fs_swap_mb means the read did not answer.
fs_collect() {
  st_source_env "$TOOL"
  fs_limit=${FSEVENTSD_RSS_MB:-}
  fs_swap_limit=${FSEVENTSD_SWAP_MB:-8192}
  fs_min_hours=${FSEVENTSD_MIN_HOURS:-6}
  fs_pid=""; fs_rss_mb=""; fs_pressure=""; fs_swap_mb=""; fs_bad=""; fs_uname=""
  if [ -z "$fs_limit" ]; then
    fs_state=unconfigured
    return 0
  fi
  whole "$fs_limit" 1 1048576 || fs_bad="FSEVENTSD_RSS_MB must be whole megabytes from 1 to 1048576"
  whole "$fs_swap_limit" 0 1048576 ||
    fs_bad="${fs_bad:-FSEVENTSD_SWAP_MB must be whole megabytes from 0 to 1048576}"
  whole "$fs_min_hours" 0 8760 ||
    fs_bad="${fs_bad:-FSEVENTSD_MIN_HOURS must be whole hours from 0 to 8760}"
  if [ -n "$fs_bad" ]; then
    fs_state=bad-config
    return 0
  fi
  fs_uname=$(uname -s 2>/dev/null || echo unknown)
  if [ "$fs_uname" != Darwin ]; then
    fs_state=not-darwin
    return 0
  fi
  fs_found=$(fs_daemon)
  if [ -z "$fs_found" ]; then
    fs_state=no-daemon
    return 0
  fi
  fs_state=measured
  fs_pid=${fs_found%% *}
  fs_rss_mb=$(( ${fs_found##* } / 1024 ))
  fs_pressure=$(fs_pressure_level)
  fs_swap_mb=$(fs_swap_used_mb)
}

# fs_judge: from the readings, set fs_line (one line), fs_code (0, 1 or 3) and
# fs_hurting (yes|no|unknown). A reading that did not answer makes the verdict
# unknown; one that answered "hurting" wins over one that did not answer.
fs_judge() {
  fs_hurting=unknown
  case "$fs_state" in
    unconfigured)
      fs_line="$TOOL: nothing to check: FSEVENTSD_RSS_MB is not set"
      fs_code=3
      return 0 ;;
    bad-config)
      fs_line="$TOOL: FAIL: $fs_bad"
      fs_code=1
      return 0 ;;
    not-darwin)
      fs_line="$TOOL: nothing to check: not macOS (uname -s is $fs_uname)"
      fs_code=3
      return 0 ;;
    no-daemon)
      fs_line="$TOOL: nothing to check: no fseventsd process is running"
      fs_code=3
      return 0 ;;
  esac
  fs_unknown=0
  fs_hurting=no
  if [ -z "$fs_pressure" ]; then
    fs_unknown=1
    fs_pressure_say="pressure unreadable"
  else
    fs_pressure_say="pressure level $fs_pressure"
    [ "$fs_pressure" -le 1 ] || fs_hurting=yes
  fi
  if [ -z "$fs_swap_mb" ]; then
    fs_unknown=1
    fs_swap_say="swap unreadable"
  else
    fs_swap_say="swap used $fs_swap_mb MB of $fs_swap_limit MB"
    [ "$fs_swap_mb" -le "$fs_swap_limit" ] || fs_hurting=yes
  fi
  [ "$fs_hurting" = yes ] || [ "$fs_unknown" -eq 0 ] || fs_hurting=unknown
  fs_size="$fs_rss_mb MB resident (pid $fs_pid)"
  if [ "$fs_rss_mb" -le "$fs_limit" ]; then
    fs_line="$TOOL: $fs_size, within the $fs_limit MB budget"
    fs_code=0
    return 0
  fi
  case "$fs_hurting" in
    yes)
      fs_line="$TOOL: $fs_size, over the $fs_limit MB budget and the machine is hurting: $fs_pressure_say, $fs_swap_say"
      fs_code=1 ;;
    unknown)
      fs_line="$TOOL: $fs_size, over the $fs_limit MB budget, and whether the machine is hurting is unknown: $fs_pressure_say, $fs_swap_say"
      fs_code=1 ;;
    *)
      fs_line="$TOOL: $fs_size, over the $fs_limit MB budget, machine not hurting: $fs_pressure_say, $fs_swap_say"
      fs_code=0 ;;
  esac
}

# fs_marker: the file holding the last TERM's epoch seconds. It is state a run
# writes and nobody edits, so it lives in the state folder and not in the
# config folder, which holds values only (CLAUDE.md convention 3, sd:3990).
fs_marker() {
  printf '%s/last-term\n' "$(st_state_dir "$TOOL")"
}

# fs_send_term: record the attempt, then signal. In that order: a TERM
# recorded only after it landed would be lost by a crash between the two, and
# the next hourly run would signal again -- the thrashing FSEVENTSD_MIN_HOURS
# exists to stop. A TERM that does not go through undoes the record, so the
# floor does not hold back the next run for a repair that never happened.
# Sets fs_term_line; returns 0 when the daemon was signalled, 1 otherwise.
fs_send_term() {
  fs_file=$(fs_marker)
  fs_now=$(date +%s)
  if [ -f "$fs_file" ]; then
    fs_last=$(cat "$fs_file" 2>/dev/null || true)
    case "$fs_last" in
      ''|*[!0-9]*)
        fs_term_line="$TOOL: FAIL: no TERM: $fs_file holds no timestamp; remove it to allow one"
        return 1 ;;
    esac
    if [ "$fs_last" -gt "$fs_now" ]; then
      fs_term_line="$TOOL: FAIL: no TERM: $fs_file is in the future; remove it to allow one"
      return 1
    fi
    if [ "$((fs_now - fs_last))" -lt "$((fs_min_hours * 3600))" ]; then
      fs_term_line="$TOOL: no TERM: the last was $(( (fs_now - fs_last) / 3600 ))h ago, under FSEVENTSD_MIN_HOURS=$fs_min_hours"
      return 0
    fi
    fs_had=$fs_last
  else
    fs_had=""
  fi
  mkdir -p "$(dirname "$fs_file")"
  printf '%s\n' "$fs_now" > "$fs_file"
  fs_err=$(mktemp "${TMPDIR:-/tmp}/fseventsd.XXXXXX")
  fs_rc=0
  # Bounded: a sudo whose askpass waits would otherwise hold an unattended
  # run until the job's own limit, with nothing in the log to say which step.
  # shellcheck disable=SC2086 # TERM_COMMAND is the sudoers line's words
  st_bounded 30 sudo -A $TERM_COMMAND 2> "$fs_err" || fs_rc=$?
  if [ "$fs_rc" -ne 0 ]; then
    if [ -n "$fs_had" ]; then printf '%s\n' "$fs_had" > "$fs_file"; else rm -f "$fs_file"; fi
    fs_reason=$(grep -v '^[[:space:]]*$' "$fs_err" 2>/dev/null | tail -1 || true)
    rm -f "$fs_err"
    fs_term_line="$TOOL: FAIL: the TERM did not go through (exit $fs_rc): ${fs_reason:-no fseventsd matched}"
    return 1
  fi
  rm -f "$fs_err"
  fs_term_line="$TOOL: TERM sent to pid $fs_pid; launchd respawns it. Recorded in $fs_file"
  return 0
}

run_status() {
  fs_collect
  fs_judge
  if [ "$1" = verbose ]; then
    echo "uname: ${fs_uname:-not read}"
    echo "daemon: ${fs_pid:+pid $fs_pid, }${fs_rss_mb:-no} MB resident"
    echo "budget: ${fs_limit:-unset} MB resident, ${fs_swap_limit} MB swap, ${fs_min_hours}h between TERMs"
    echo "pressure: ${fs_pressure:-unreadable}"
    echo "swap used: ${fs_swap_mb:-unreadable} MB"
    echo "last TERM: $(cat "$(fs_marker)" 2>/dev/null || echo none) ($(fs_marker))"
  fi
  echo "$fs_line"
  return "$fs_code"
}

run_repair() {
  fs_collect
  fs_judge
  echo "$fs_line"
  [ "$fs_code" -ne 3 ] || return 3
  # Only a machine that answered "hurting" is repaired. An unknown verdict and
  # a broken setting are reported and signalled nothing.
  [ "$fs_hurting" = yes ] || return "$fs_code"
  rc=0
  fs_send_term || rc=$?
  echo "$fs_term_line"
  return "$rc"
}

case "${1:-}" in
  status) [ "$#" -eq 1 ] || { usage >&2; exit 1; }; run_status quiet || exit $? ;;
  check) [ "$#" -eq 1 ] || { usage >&2; exit 1; }; run_status verbose || exit $? ;;
  repair) [ "$#" -eq 1 ] || { usage >&2; exit 1; }; run_repair || exit $? ;;
  test) shift; exec "${PYTHON:-python3}" -m unittest discover -s "$DIR/tests" -t "$DIR" "$@" ;;
  -h|--help|help) usage ;;
  *) usage >&2; exit 1 ;;
esac

#!/bin/sh
# fseventsd's resident size, and a TERM when an oversized daemon is also
# hurting the machine (sd:8579).
# Usage: fseventsd.sh status|repair|check|test|help
#
# On 2026-11-11 fseventsd held 28.77 GB resident on one machine, against
# Apple's own footprint budget of about 30 MB, with swap at 19.70 of 20.48 GB
# and kern.memorystatus_vm_pressure_level at 2 (fleet sd:8557). There is no
# vendor fix. launchd respawns the daemon, and System Integrity Protection
# refuses `launchctl kickstart` on it, so TERM is the whole repair.
#
# Two questions, never folded into one answer (sd:8579 round 1):
#
#   is the daemon oversized?  resident size against FSEVENTSD_RSS_MB
#   is the machine hurting?   pressure level, or swap against FSEVENTSD_SWAP_MB
#
# Every line answers both, and each caller reads the one it needs. status
# exits on the size alone: an oversized daemon is worth a nightly line before
# the machine starts swapping, and the sweep prints nothing for exit 0. Only
# repair reads the second answer, because the TERM drops the FSEvents history
# that every backup and indexing tool reads: it signals when both say yes, and
# FSEVENTSD_MIN_HOURS keeps an hourly job from thrashing a daemon that grows
# back fast.
#
# A sysctl that does not answer leaves "is the machine hurting?" unknown. That
# is neither a yes nor a no: the line says so and repair sends no TERM.
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
           is visible on a healthy machine too. It answers two questions:
           is the daemon oversized, and is the machine hurting. Exits 3
           when there is nothing to check: FSEVENTSD_RSS_MB unset, not
           macOS, or no fseventsd running. Exits 1 when the daemon is over
           FSEVENTSD_RSS_MB, whatever the machine is doing, when a setting
           is broken, or when a reading did not answer at all, which is
           never taken for nothing to check. Exits 0 otherwise --
           local-health-check reads these codes.
  repair   The same two answers, and a TERM to the daemon through sudo -A
           only when both say yes; launchd respawns it. At most one TERM
           every FSEVENTSD_MIN_HOURS. Exits 1 when the TERM failed, when
           a setting is broken, or when the daemon is oversized and the
           pressure readings did not answer. Exits 3 when there is nothing
           to check, 0 otherwise, an oversized daemon on a machine with
           memory to spare included: that one is status's line to report,
           and a refused TERM is a cron-jobs failure.
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
Write each value in plain decimal: a leading zero is refused, because shell
arithmetic reads 010 as eight.

A failing repair prints one SD_REPORT_ATTENTION line grading its data safe,
so local-cron-jobs opens a report without pushing "data at risk" to the
phone: no failure here risks data, since the TERM either went out or did not.

The TERM needs the machine-setup-repairs sudoers line, or the
machine-setup-sudo Keychain item (local-machine-setup/README.md,
"Unattended sudo"). With neither, sudo refuses and repair says so.
USAGE
}

# whole <value> <low> <high>: 0 when value is a whole number in that range.
# A leading zero
# is refused, because shell arithmetic reads it as octal: `08` aborts the run
# outright ("value too great for base") and `010` quietly means eight where
# the operator wrote ten. Two values here reach arithmetic, FSEVENTSD_MIN_HOURS
# and the floor's timestamp, and a value that means two things is refused
# rather than guessed at (round 3).
whole() {
  case "$1" in ''|*[!0-9]*|0?*) return 1 ;; esac
  [ "$1" -ge "$2" ] || return 1
  [ "$1" -le "$3" ] || return 1
  return 0
}

# fs_daemon: "<pid> <resident-kb>" of the running fseventsd, or nothing.
# pgrep names the process and ps reads one pid, the reading
# docs/work/2026-11-04-ramdisk-tmp/design.md:134 already used on this daemon.
# No comm column: ps cuts its output to the window width when it writes to a
# terminal, 80 characters at 79 columns, and the daemon's comm path is 134, so
# a basename match on that field finds nothing in a narrow window. pgrep -x
# matches the process name whole, so fseventsd-watcher is not the daemon. A
# pid ps cannot read is skipped, not reported as a daemon of no size.
fs_daemon() {
  # `|| fs_read_rc=$?` and not a bare assignment: this function is read
  # through `$(fs_daemon)`, and errexit is live inside a command substitution
  # even when the caller suppressed it, so a bare assignment from a pgrep that
  # exits non-zero kills the subshell and the status never reaches the case.
  fs_read_rc=0
  fs_pids=$(pgrep -x fseventsd 2>/dev/null) || fs_read_rc=$?
  # Only exit 1 is "no fseventsd is running". pgrep exits 2 on a usage error
  # and 3 on a fatal one, and the shell gives 127 when it is not on PATH;
  # taking any of those for no-daemon exits 3, the one code the nightly sweep
  # prints nothing for, so a machine whose pgrep cannot answer would stop
  # being checked in silence (round 3). The ps read below is the other way
  # round on purpose: a pid pgrep named and ps cannot read is the documented
  # race between the two commands, so that one is skipped.
  case "$fs_read_rc" in
    0|1) ;;
    *) return "$fs_read_rc" ;;
  esac
  for fs_candidate in $fs_pids; do
    fs_kb=$(ps -o rss= -p "$fs_candidate" 2>/dev/null |
      awk 'NR == 1 && $1 ~ /^[0-9]+$/ { print $1 }')
    if [ -n "$fs_kb" ]; then
      printf '%s %s\n' "$fs_candidate" "$fs_kb"
      return 0
    fi
  done
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
#
# Every run_* is dispatched as `run_x || exit $?`, so errexit is suppressed
# for this whole file's function bodies, not only for fs_send_term (round 2).
# Every step here that can fail is therefore checked where its failure
# changes the answer: `whole` into fs_bad, `uname` and the marker read with a
# default, and the pgrep, ps and sysctl readers give nothing when they do not
# answer, which reads as unknown and never as a healthy zero. The one
# unchecked step is this st_source_env, which returns 0 either way by
# contract; its failure needs no check here because `.` is a special builtin
# and a POSIX shell aborts when it cannot open its file, so an unreadable
# .env exits 1 with the path on stderr rather than reading as unconfigured
# (test_an_env_file_that_cannot_be_read_never_reads_as_unconfigured).
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
  fs_zero="with no leading zero, which shell arithmetic reads as octal"
  whole "$fs_limit" 1 1048576 ||
    fs_bad="FSEVENTSD_RSS_MB must be whole megabytes from 1 to 1048576, $fs_zero"
  whole "$fs_swap_limit" 0 1048576 ||
    fs_bad="${fs_bad:-FSEVENTSD_SWAP_MB must be whole megabytes from 0 to 1048576, $fs_zero}"
  whole "$fs_min_hours" 0 8760 ||
    fs_bad="${fs_bad:-FSEVENTSD_MIN_HOURS must be whole hours from 0 to 8760, $fs_zero}"
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
  fs_read_rc=$?
  if [ "$fs_read_rc" -ne 0 ]; then
    fs_state=no-reading
    fs_read_say="pgrep -x fseventsd exited $fs_read_rc"
    return 0
  fi
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

# fs_judge: from the readings, two separate answers and the line that carries
# both. fs_over (yes|no) is the size question, fs_hurting (yes|no|unknown) the
# machine question, and neither is folded into the other: fs_code is status's
# exit code and reads fs_over alone, while repair reads both. A reading that
# did not answer makes the machine question unknown; one that answered
# "hurting" wins over one that did not answer.
fs_judge() {
  fs_over=unknown
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
    no-reading)
      fs_line="$TOOL: FAIL: the daemon could not be read: $fs_read_say"
      fs_code=1
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
  case "$fs_hurting" in
    yes) fs_machine_say="the machine is hurting" ;;
    no) fs_machine_say="the machine is not hurting" ;;
    *) fs_machine_say="whether the machine is hurting is unknown" ;;
  esac
  if [ "$fs_rss_mb" -le "$fs_limit" ]; then
    fs_over=no
    fs_size_say="within the $fs_limit MB budget"
    fs_code=0
  else
    fs_over=yes
    fs_size_say="over the $fs_limit MB budget"
    fs_code=1
  fi
  fs_line="$TOOL: $fs_rss_mb MB resident (pid $fs_pid), $fs_size_say; $fs_machine_say: $fs_pressure_say, $fs_swap_say"
}

# fs_marker: the file holding the last TERM's epoch seconds. It is state a run
# writes and nobody edits, so it lives in the state folder and not in the
# config folder, which holds values only (CLAUDE.md convention 3, sd:3990).
fs_marker() {
  printf '%s/last-term\n' "$(st_state_dir "$TOOL")"
}

# fs_record <epoch> <file>: put that value in the floor, or fail and leave the
# floor holding exactly what it held. Each step is checked and the value read
# back, because the caller runs under `||`, which suppresses errexit for its
# whole body: an unchecked mkdir or redirection let the run reach the TERM and
# report a floor it never wrote, and the next hourly run then signalled again,
# every hour (round 2).
#
# The write never goes to the floor itself. `>` truncates before it writes, so
# a write that does not land left the floor empty or partial, every later run
# read that as garbage and refused with exit 1, and only a hand deleting the
# file let the repair run again -- while the hourly job failed every hour. The
# trigger is a full disk, which is this tool's own scenario: the machine it
# exists for has swap eating its boot volume. So the value goes to a file
# beside it, is read back there, and only then replaces the floor, which is a
# rename within one directory and cannot half-finish (round 3). A state folder
# that takes no new file refuses the TERM, as round 2 ruled for every floor
# that cannot be recorded.
fs_record() {
  mkdir -p "$(dirname "$2")" || return 1
  fs_new="$2.new"
  if ! printf '%s\n' "$1" > "$fs_new" ||
      [ "$(cat "$fs_new" 2>/dev/null)" != "$1" ]; then
    rm -f "$fs_new"
    return 1
  fi
  mv -f "$fs_new" "$2" || { rm -f "$fs_new"; return 1; }
}

# fs_send_term: record the attempt, then signal. In that order: a TERM
# recorded only after it landed would be lost by a crash between the two, and
# the next hourly run would signal again -- the thrashing FSEVENTSD_MIN_HOURS
# exists to stop. A TERM that does not go through undoes the record, so the
# floor does not hold back the next run for a repair that never happened.
#
# Every step that must hold before the daemon is signalled is checked here,
# and a floor that cannot be recorded refuses to signal. Two are deliberately
# unchecked, both read-only and both after the TERM: the reason read out of
# sudo's stderr, which has `${fs_reason:-...}` behind it, and removing that
# temporary file, where a leftover in TMPDIR costs nothing.
# Sets fs_term_line; returns 0 when the daemon was signalled, 1 otherwise.
fs_send_term() {
  fs_file=$(fs_marker)
  case "$fs_file" in
    /*) ;;
    *) fs_term_line="$TOOL: FAIL: no TERM: the state folder gave no path for the floor"
       return 1 ;;
  esac
  fs_now=$(date +%s 2>/dev/null || true)
  # An unread clock would be written as an empty floor, and every later run
  # would read that as garbage and refuse until a hand removed it.
  if ! whole "$fs_now" 1 99999999999; then
    fs_term_line="$TOOL: FAIL: no TERM: the clock gave no timestamp, so no floor can be recorded"
    return 1
  fi
  if [ -f "$fs_file" ]; then
    fs_last=$(cat "$fs_file" 2>/dev/null || true)
    # `whole`, not a looser pattern: a hand can edit this file, and a leading
    # zero reaches the arithmetic below, where 08 aborts the whole run with no
    # line printed at all and 010 reads as eight (round 3).
    if ! whole "$fs_last" 0 99999999999; then
      fs_term_line="$TOOL: FAIL: no TERM: $fs_file holds no timestamp; remove it to allow one"
      return 1
    fi
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
  # sudo's stderr is the only account of a refused TERM, and this is the last
  # step before the floor is written, so its failure leaves nothing to undo.
  fs_err=$(mktemp "${TMPDIR:-/tmp}/fseventsd.XXXXXX" 2>/dev/null || true)
  if [ -z "$fs_err" ] || [ ! -f "$fs_err" ]; then
    fs_term_line="$TOOL: FAIL: no TERM: no file for sudo's errors in ${TMPDIR:-/tmp}"
    return 1
  fi
  if ! fs_record "$fs_now" "$fs_file"; then
    rm -f "$fs_err"
    fs_term_line="$TOOL: FAIL: no TERM: could not record the floor in $fs_file, which still holds what it had; the TERM waits until it can be written"
    return 1
  fi
  fs_rc=0
  # Bounded: a sudo whose askpass waits would otherwise hold an unattended
  # run until the job's own limit, with nothing in the log to say which step.
  # shellcheck disable=SC2086 # TERM_COMMAND is the sudoers line's words
  st_bounded 30 sudo -A $TERM_COMMAND 2> "$fs_err" || fs_rc=$?
  if [ "$fs_rc" -ne 0 ]; then
    # The TERM has already happened or not; either way the record must go
    # back to what it was, and an undo that fails is the operator's to clear,
    # since it holds the next run back for a repair that never ran.
    fs_undone=yes
    if [ -n "$fs_had" ]; then
      fs_record "$fs_had" "$fs_file" || fs_undone=no
    else
      rm -f "$fs_file" || true
      [ ! -e "$fs_file" ] || fs_undone=no
    fi
    fs_reason=$(grep -v '^[[:space:]]*$' "$fs_err" 2>/dev/null | tail -1 || true)
    rm -f "$fs_err"
    fs_term_line="$TOOL: FAIL: the TERM did not go through (exit $fs_rc): ${fs_reason:-no fseventsd matched}"
    if [ "$fs_undone" = no ]; then
      fs_term_line="$fs_term_line; and the floor could not be undone, so remove $fs_file by hand"
    fi
    return 1
  fi
  rm -f "$fs_err"
  fs_term_line="$TOOL: TERM sent to pid $fs_pid; launchd respawns it. Recorded in $fs_file"
  return 0
}

# fs_data_safe <step>: grade this run's failure for local-cron-jobs. Its
# push_at_risk (cron-jobs.sh:524) sends an ntfy push titled "<job> failed on
# <host>: data at risk", at high priority, for any non-zero exit, unless the
# run's own output carries this line (ruling on sd:6030). No failing path here
# risks data: the TERM either went out, which is exit 0, or it did not, and the
# FSEvents history is what a TERM costs. Without this, a refused sudo and an
# unreadable sysctl woke the operator at 3am claiming data was at risk, which
# is a false alarm every time (round 3). The failure still reaches Reports and
# Today either way; only the phone push reads this line. The step names stay
# fixed, so a repeating failure folds into one open report.
#
# repair alone prints it: status is read by local-health-check's sweep, which
# shows its output as the finding's remedy text, and only repair runs under
# local-cron-jobs.
fs_data_safe() {
  echo "SD_REPORT_ATTENTION: data safe, one step failed: $TOOL $1"
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
  # Each question is asked on its own, and the state says which were answered
  # at all. Reading status's one code here is what TERMed a daemon within its
  # budget whenever another process drove the machine into swap (round 1).
  case "$fs_state" in
    unconfigured|not-darwin|no-daemon) return 3 ;;
    bad-config) fs_data_safe "a setting it cannot read"; return 1 ;;
    no-reading) fs_data_safe "a daemon it cannot read"; return 1 ;;
  esac
  if [ "$fs_over" = no ]; then
    echo "$TOOL: no TERM: the daemon is within its budget, whatever the machine is doing"
    return 0
  fi
  if [ "$fs_hurting" = unknown ]; then
    echo "$TOOL: FAIL: no TERM: the daemon is oversized and the pressure readings did not answer"
    fs_data_safe "a machine reading that did not answer"
    return 1
  fi
  if [ "$fs_hurting" = no ]; then
    echo "$TOOL: no TERM: the machine has memory to spare; status reports the size"
    return 0
  fi
  rc=0
  fs_send_term || rc=$?
  echo "$fs_term_line"
  [ "$rc" -eq 0 ] || fs_data_safe "the TERM did not go out"
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

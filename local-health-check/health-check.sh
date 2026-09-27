#!/bin/sh
# Nightly computer health review: crash and panic reports, launchd job
# exits, disk SMART, memory pressure and swap, unified-log fault spikes,
# new launch daemons/agents vs a baseline, network sanity, and the `status`
# verb of every sibling tool that declares convention 6. Findings with
# suggested fixes are emailed via local-notify.
# Usage: health-check.sh run|check|test
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"

NOTIFY="$DIR/../local-notify/notify.sh"
STATE="${HEALTH_CHECK_STATE:-$HOME/.config/health-check}"
# Where the status sweep looks for sibling tools. A seam for a fixture tree,
# not a per-machine setting: on every real machine it is this checkout.
TOOLS_ROOT="${HEALTH_CHECK_TOOLS_ROOT:-$DIR/..}"
# The launchd label prefix this repository's jobs and agents carry; cron jobs
# are "$LABEL_PREFIX.cron.<job>".
LABEL_PREFIX="${SYSTEM_TOOLS_LABEL_PREFIX:-local.system-tools}"

case "${1:-}" in
  run)   EMAIL=1 ;;
  check) EMAIL=0 ;;
  test)
    shift
    exec "${PYTHON:-python3}" -m unittest discover -s "$DIR/tests" -t "$DIR" "$@"
    ;;
  -h|--help|help)
    cat <<'HELPEOF'
usage: health-check.sh run|check|test

  run    run all checks and email findings + suggested fixes through
         local-notify's email channel (what the health-check-nightly cron
         job runs). No email when everything is clean. Exits 1 only when
         the email could not be delivered.
  check  print the same report to stdout without emailing.
  test   run the status sweep's unittest suite (tests/) against a fixture
         tree of scratch tools; extra arguments go to unittest (-v).

checks:
  - app crash reports (.ips) from the last day, kernel panics from the
    last week (~/Library and /Library DiagnosticReports)
  - launchd: every loaded $SYSTEM_TOOLS_LABEL_PREFIX.* (default
    local.system-tools.*) or com.platypeeps.* job/agent whose last exit
    was nonzero
  - disk0 SMART status, NVMe wear/media errors via smartctl if installed
  - memory pressure level and swap in use
  - unified-log fault volume: flagged when it more than doubles against
    the stored baseline (the raw count is meaningless noise on macOS)
  - new files in /Library/LaunchDaemons, /Library/LaunchAgents and
    ~/Library/LaunchAgents since the stored baseline (catches software
    that installed persistence behind your back)
  - default gateway ping and DNS resolution
  - status sweep: the `status` verb of every sibling tool that declares
    convention 6 — its `help` output contains the phrase `local-health-check`
    (as in "local-health-check reads these codes"). Exit 0 and 3 are silent,
    1 is a finding carrying the tool's own first output line, and any other
    code or no answer inside 30s is a finding naming the code or the bound.
    No tool is named here: the sweep asks each folder's entrypoint, so a
    tool joins by adding the sentence to its help. The report's notes carry
    `status sweep: N tool(s) declared, M checked`.

optional, on unless switched off: local-jev orders the findings loudest-first
when `jev enabled JEV_HEALTH_CHECK` exits 0. It reorders and labels, and
that is all it does: no finding is dropped, no exit code moves, no line a
stage printed is reworded. With JEV_HEALTH_CHECK set to 0, off, false, no or disabled, or
with Jev switched off, unkeyed or failing, this report is byte for byte the
one it has always been.
One request carries every finding. What leaves the machine is a shareable
form of each finding: the stage's own words and numbers, with a placeholder
where a launchd label, an app, a process or a tool's verdict line was. The
headline itself never leaves. See the README for the whole payload.

state (fault baseline, launchd baseline) lives in ~/.config/health-check;
the first run seeds it and reports "baseline seeded" instead of findings.

environment:
  HEALTH_CHECK_STATE        state dir (default ~/.config/health-check)
  HEALTH_CHECK_TOOLS_ROOT   folder whose subfolders the status sweep
                            probes (default: this checkout)
  SYSTEM_TOOLS_LABEL_PREFIX launchd label prefix of this repository's jobs
                            (default local.system-tools)
  JEV_HEALTH_CHECK          0, off, false, no or disabled switches this ordering off;
                            unset means on. local-jev orders findings by "does
                            this need a human tonight?" (Jev must also
                            answer `enabled` with 0)
  HEALTH_CHECK_JEV          local-jev's entrypoint (default: the
                            local-jev folder of this checkout)
HELPEOF
    exit 0
    ;;
  *)
    echo "usage: $(basename "$0") run|check|test" >&2
    exit 1
    ;;
esac

# The status sweep's bound is overridable for one reader: tests/test_sweep.py,
# whose hang fixture would otherwise cost every CI run 30 seconds of sleep. It
# is not in the help text on purpose — a tool that needs longer than 30
# seconds to say whether it is healthy has a different problem, and the fix is
# in that tool, not in a machine's environment. So the override may shorten
# the bound and never lengthen it: whole seconds from 1 to 30. Anything else
# is refused here, before minutes of machine stages, and not handed to
# `bounded` (sd:1414). There, `0` expires at once and `sleep` refuses `abc`
# or `-1` at once, so every declared tool's `status` would be killed on the
# spot and reported as a hang; `5m` would be five minutes per tool. HELP_BOUND
# and the cron-jobs bound are constants and need no check.
STATUS_BOUND=${HEALTH_CHECK_STATUS_BOUND:-30}
case "$STATUS_BOUND" in
  [1-9]|[12][0-9]|30) ;;
  *)
    echo "$(basename "$0"): HEALTH_CHECK_STATUS_BOUND must be whole seconds from 1 to 30, got '$STATUS_BOUND'" >&2
    exit 1
    ;;
esac

mkdir -p "$STATE"
TMPD=$(mktemp -d)
trap 'rm -rf "$TMPD"' EXIT INT TERM
FINDINGS="$TMPD/findings"
INFO="$TMPD/info"
: > "$FINDINGS"; : > "$INFO"

# finding HEADLINE FIX SHAREABLE
#
# SHAREABLE is the only form of the finding Jev may see (sd:1362). The
# headline stays on this machine: it names launchd labels, crashing apps,
# faulting processes and installed software, and a label such as
# <prefix>.cron.<routine> is a private routine in plain words. So SHAREABLE
# is built from this script's own words, numbers and this repository's tool
# folder names -- a placeholder stands where a machine-supplied name was.
# `=` means the headline already is that: it interpolates nothing else.
# Omitted or empty, the finding has no shareable form and the Jev ordering
# does not run at all, so a new finding that forgets it fails closed.
JEVLINES="$TMPD/jev.shareable"
: > "$JEVLINES"
finding() {
  printf -- '- %s\n  fix: %s\n' "$1" "$2" >> "$FINDINGS"
  case "${3:-}" in
    =) printf '%s\n' "$1" ;;
    *) printf '%s\n' "${3:-}" ;;
  esac | tr -d '\r' | head -1 >> "$JEVLINES"
}
info()    { printf -- '- %s\n' "$1" >> "$INFO"; }

# What stands for a launchd label in the shareable form: its kind, never its
# name. Jev ranks "does this need a human tonight?" and the name adds nothing.
jev_label() {
  case "$1" in
    "$LABEL_PREFIX".cron.*) echo "<cron job>" ;;
    *) echo "<agent>" ;;
  esac
}

# bounded SECS cmd... — run cmd under a wall-clock bound; exit 124 when it
# expires, the command's own code otherwise. 124 is also a code a command can
# exit with on its own, so the exit code does not say which happened:
# BOUNDED_EXPIRED does, 1 when the bound expired and 0 otherwise, and callers
# test that (sd:1224). Plain sh, not `timeout`: that is
# Homebrew coreutils on this machine, and the nightly job runs under launchd's
# bare PATH — a dependency that resolves in a terminal and not under launchd
# stays green for months. Callers redirect stdout/stderr to files, never a
# pipe: a child the killed command leaves behind (a curl, a sleep) would hold
# a pipe open and a `$(...)` around this would wait for it, bound or no bound.
bounded() {
  _secs=$1; shift
  _mark="$TMPD/bounded.$$.expired"
  rm -f "$_mark"
  BOUNDED_EXPIRED=0
  "$@" &
  _pid=$!
  # On expiry: note the command's descendants first -- its children, their
  # children, and so on down (they are reparented the moment their parent
  # dies and can no longer be found) -- kill it, then kill them: a
  # `sleep 600` or a curl left behind would otherwise run to its own end.
  # TERM first, then KILL a second later for whatever is still there: a
  # command that traps or ignores TERM would otherwise hold the `wait` below
  # for as long as it likes, which is the hang the bound exists to end
  # (sd:1224). A descendant that detached itself into another session before
  # the bound expired is no longer in the tree and is not found.
  # Once the bound has expired, TERM is ignored until the sequence is done:
  # the caller TERMs this watchdog the moment the command dies, which is the
  # middle of this sequence, and the trap used to exit here between killing
  # the command and killing its children. The children then outlived the run
  # (sd:1418); a TERM before this point still means "finished in time".
  ( trap 'kill $! 2>/dev/null; exit 0' TERM
    sleep "$_secs" & wait $!
    trap '' TERM
    _kids=$(ps -A -o pid=,ppid= | awk -v p="$_pid" '
      { kids[$2] = kids[$2] " " $1 }
      END {
        q[1] = p; n = 1
        for (i = 1; i <= n; i++) {
          k = split(kids[q[i]], a, " ")
          for (j = 1; j <= k; j++) q[++n] = a[j]
        }
        for (i = 2; i <= n; i++) print q[i]
      }')
    if kill "$_pid" 2>/dev/null; then
      : > "$_mark"
      [ -z "$_kids" ] || kill $_kids 2>/dev/null
      sleep 1
      kill -9 "$_pid" $_kids 2>/dev/null
    fi ) >/dev/null 2>&1 &
  _wd=$!
  _rc=0
  wait "$_pid" || _rc=$?
  kill "$_wd" 2>/dev/null || :
  wait "$_wd" 2>/dev/null || :
  if [ -e "$_mark" ]; then rm -f "$_mark"; BOUNDED_EXPIRED=1; return 124; fi
  return "$_rc"
}

# --- crash and panic reports -------------------------------------------

for d in "$HOME/Library/Logs/DiagnosticReports" "/Library/Logs/DiagnosticReports"; do
  [ -d "$d" ] || continue
  find "$d" -maxdepth 1 -name '*.panic' -mtime -7 2>/dev/null | while read -r p; do
    echo "$p" >> "$TMPD/panics"
  done
  find "$d" -maxdepth 1 -name '*.ips' -mtime -1 2>/dev/null | while read -r p; do
    basename "$p" | sed 's/[-_][0-9]\{4\}-.*//' >> "$TMPD/crashes"
  done
done
if [ -s "$TMPD/panics" ]; then
  finding "kernel panic report(s) in the last 7 days: $(tr '\n' ' ' < "$TMPD/panics")" \
    "open the .panic file in Console.app; recurring panics usually mean bad RAM, a kext, or failing hardware — run Apple Diagnostics (hold D at boot)" \
    "kernel panic report(s) in the last 7 days: $(wc -l < "$TMPD/panics" | tr -d ' ')"
fi
if [ -s "$TMPD/crashes" ]; then
  apps=$(sort "$TMPD/crashes" | uniq -c | sort -rn | awk '{ printf "%s(%s) ", $2, $1 }')
  finding "app crash report(s) in the last day: $apps" \
    "read the report in Console.app > Crash Reports; if one app repeats, update or reinstall it" \
    "app crash report(s) in the last day: $(wc -l < "$TMPD/crashes" | tr -d ' ') report(s) from $(sort -u "$TMPD/crashes" | wc -l | tr -d ' ') app(s)"
fi

# --- launchd job health --------------------------------------------------

uid=$(id -u)
for plist in "$HOME"/Library/LaunchAgents/"$LABEL_PREFIX".*.plist \
             "$HOME"/Library/LaunchAgents/com.platypeeps.*.plist; do
  [ -e "$plist" ] || continue
  label=$(basename "$plist" .plist)
  out=$(launchctl print "gui/$uid/$label" 2>/dev/null) || {
    finding "launchd: $label has a plist but is not loaded" \
      "launchctl bootstrap gui/$uid $plist — or remove the plist if retired" \
      "launchd: $(jev_label "$label") has a plist but is not loaded"
    continue
  }
  # A cron job's exit code is not launchd's to report — when a later run has
  # demonstrably succeeded. launchd counts only the runs it spawned, so a job
  # that failed at its slot and was re-run by hand keeps `last exit code = 1`
  # until the next scheduled fire: three of the six jobs flagged on 2026-09-20
  # had a log ending in "done" (sd:1201). A run through either path records its
  # own exit code beside its log, so that record can supersede the counter.
  #
  # Superseding is what it has to prove, per label, before the counter is
  # dropped. `cron-jobs.sh status <job>` exits 0 only on positive evidence: a
  # completed run that recorded exit 0 AND recorded itself as the run launchd
  # last spawned, counted through launchd's own `runs`, since `launchctl print`
  # gives no time for the exit it reports. So a 0 here is the demonstration and
  # anything else is not — including the absence of a record, which proves no
  # recovery and is left to launchd's own exit code. The three ways a job goes
  # quiet all land on "anything else": a run that died before recording an
  # outcome, a run launchd never got as far as starting, and a job whose `.job`
  # definition was deleted while its plist stayed loaded — the sweep enumerates
  # definitions, so it cannot see the last at all and exits 3 without naming
  # it. All three keep launchd's evidence, which is the only evidence left for
  # them.
  #
  # A record that says FAILURE is the fourth case, and testing for success
  # dropped it. `status` has three returns -- 0 healthy, 1 broken, 3 nothing
  # installed -- and `&& continue` collapsed 1 and 3 into one fall-through.
  # A job run by hand that failed records `exit=3` while launchd's last
  # SPAWNED run exited 0, so the fall-through read `last exit code = 0`,
  # raised nothing, and the recorded failure was gone: positive evidence of
  # failure discarded in favour of a counter that never saw the run, which is
  # the defect class sd:1201 exists to close. A 1 is the job's own answer and
  # needs no second opinion, so it is a finding here and the launchd
  # fall-through is skipped.
  #
  # Only 0 and 1 are the job's answers. Anything else falls through, which is
  # right for the 3 above and necessary for `bounded`, which returns 124 of
  # its own when it kills a `status` that hung -- a timeout is not a verdict,
  # and reading it as one would manufacture a finding out of a hang.
  #
  # The not-loaded finding above still applies either way: that one launchd
  # alone knows.
  case "$label" in
    "$LABEL_PREFIX".cron.*)
      cron_job=${label#"$LABEL_PREFIX".cron.}
      cron_rc=0
      bounded 15 sh "$TOOLS_ROOT/local-cron-jobs/cron-jobs.sh" status \
        "$cron_job" >/dev/null 2>&1 || cron_rc=$?
      case "$cron_rc" in
        0) continue ;;
        1) finding "launchd: $label reports a failed run that nothing has superseded" \
             "read local-cron-jobs/logs/$cron_job.log and the outcome recorded beside it in logs/.$cron_job.runs; re-run the job by hand to clear it" \
             "launchd: $(jev_label "$label") reports a failed run that nothing has superseded"
           continue ;;
      esac ;;
  esac
  code=$(echo "$out" | sed -n 's/.*last exit code = \([0-9-]*\).*/\1/p' | head -1)
  # `last exit code` describes the PREVIOUS instance. For a KeepAlive service
  # any restart (SIGTERM at sleep, a manual kickstart) leaves a nonzero code
  # behind forever, so a job that currently holds a pid has already recovered
  # and is not a finding. Scheduled jobs have no pid once they finish, so
  # their exit codes are still reported. Caveat: a service crash-looping fast
  # enough to always hold a pid at check time slips through this.
  pid=$(echo "$out" | awk -F'= ' '/^\tpid = /{print $2; exit}')
  case "$code" in
    ''|0) ;;
    *) [ -n "$pid" ] || finding "launchd: $label last exited $code" \
         "check its log — cron jobs: local-cron-jobs/logs/, others: the plist's StandardErrorPath" \
         "launchd: $(jev_label "$label") last exited $code" ;;
  esac
done

# --- disk SMART ----------------------------------------------------------

smart=$(diskutil info disk0 2>/dev/null | sed -n 's/.*SMART Status: *//p')
case "$smart" in
  Verified|"Not Supported"|"") ;;
  *) finding "disk0 SMART status: $smart" \
       "back up NOW (CCC) and plan a disk replacement — 'Failing' is terminal" = ;;
esac

# NVMe wear via smartmontools when installed (diskutil SMART is binary).
if command -v smartctl >/dev/null 2>&1; then
  sm=$(smartctl -a disk0 2>/dev/null || true)
  wear=$(echo "$sm" | sed -n 's/^Percentage Used:[[:space:]]*\([0-9]*\)%.*/\1/p')
  media=$(echo "$sm" | sed -n 's/^Media and Data Integrity Errors:[[:space:]]*\([0-9]*\).*/\1/p')
  cw=$(echo "$sm" | sed -n 's/^Critical Warning:[[:space:]]*\(0x[0-9a-fA-F]*\).*/\1/p')
  if [ -n "$wear" ] && [ "$wear" -ge 85 ] 2>/dev/null; then
    finding "SSD wear at ${wear}% of rated life" \
      "plan a replacement/new machine before it hits 100; keep backups tight" =
  fi
  if [ -n "$media" ] && [ "$media" -gt 0 ] 2>/dev/null; then
    finding "SSD media/data integrity errors: $media" \
      "back up NOW and watch this number — any growth means the disk is dying" =
  fi
  case "$cw" in ''|0x00) ;; *)
    finding "SSD critical warning flag: $cw" \
      "smartctl -a disk0 for details; treat as pre-failure" = ;;
  esac
fi

# --- memory pressure and swap ---------------------------------------------

pressure=$(sysctl -n kern.memorystatus_vm_pressure_level 2>/dev/null || echo 1)
if [ "$pressure" -gt 1 ] 2>/dev/null; then
  finding "memory pressure level $pressure (1=normal, 2=warning, 4=critical)" \
    "check Activity Monitor > Memory for the culprit; a leaking process grows without bound" =
fi
ncpu=$(sysctl -n hw.ncpu 2>/dev/null || echo 8)
load5=$(sysctl -n vm.loadavg 2>/dev/null | awk '{ print int($3) }')
if [ -n "$load5" ] && [ "$load5" -gt $(( ncpu * 2 )) ] 2>/dev/null; then
  finding "5-minute load average $load5 on $ncpu cores" \
    "something is pegging the CPU — Activity Monitor > CPU, sort by % CPU" =
fi
swap_used=$(sysctl -n vm.swapusage 2>/dev/null | sed -n 's/.*used = \([0-9.]*\)M.*/\1/p')
if [ -n "$swap_used" ] && [ "$(printf '%.0f' "$swap_used")" -gt 8192 ] 2>/dev/null; then
  finding "swap in use: ${swap_used}MB" \
    "something is leaking or the workload outgrew RAM — Activity Monitor > Memory, sort by memory" =
fi

# --- unified-log fault volume ---------------------------------------------

# Absolute fault counts are meaningless on macOS (a healthy day logs six
# figures), so only a big jump against the stored baseline is a finding.
faults=$(log show --last 24h --predicate 'messageType == fault' --style compact 2>/dev/null \
  | tee "$TMPD/faults" | wc -l | tr -d ' ')
prev=$(cat "$STATE/fault-count" 2>/dev/null || echo "")
if [ -z "$prev" ]; then
  info "fault baseline seeded: $faults faults/24h"
elif [ "$faults" -gt $(( prev * 2 )) ] && [ "$faults" -gt 50000 ]; then
  # compact format is: <date> <time> <type> <process>[<pid>:<tid>] ... and
  # the process name can contain spaces ("Google Drive"), so column-splitting
  # truncates it. Take everything between the type letter and the [pid:tid].
  top=$(sed -E 's/^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9:.]+ +[A-Za-z]+ +(.+)\[[0-9a-f]+:[0-9a-f]+\].*/\1/' \
    "$TMPD/faults" | sort | uniq -c | sort -rn \
    | head -3 | awk '{ n=$1; $1=""; sub(/^ /,""); printf "%s(%s) ", $0, n }')
  finding "log faults jumped: $faults in 24h (baseline $prev); top: $top" \
    "the top process is misbehaving — check for an update, or restart it and watch tomorrow's count" \
    "log faults jumped: $faults in 24h (baseline $prev)"
fi
echo "$faults" > "$STATE/fault-count"

# --- persistence baseline ---------------------------------------------------

# New launch daemons/agents are how software (wanted or not) survives
# reboots. Anything appearing since the baseline gets flagged once, then
# joins the baseline.
for d in /Library/LaunchDaemons /Library/LaunchAgents "$HOME/Library/LaunchAgents"; do
  [ -d "$d" ] && find "$d" -maxdepth 1 -mindepth 1 2>/dev/null
done | sort > "$TMPD/launchd.now"
if [ -f "$STATE/launchd-baseline" ]; then
  comm -13 "$STATE/launchd-baseline" "$TMPD/launchd.now" > "$TMPD/launchd.new" || :
  if [ -s "$TMPD/launchd.new" ]; then
    finding "new launch daemon(s)/agent(s) since last run: $(tr '\n' ' ' < "$TMPD/launchd.new")" \
      "if you did not install this, inspect the plist and the binary it points at before removing it" \
      "new launch daemon(s)/agent(s) since last run: $(wc -l < "$TMPD/launchd.new" | tr -d ' ')"
  fi
else
  info "launchd persistence baseline seeded: $(wc -l < "$TMPD/launchd.now" | tr -d ' ') entries"
fi
cp "$TMPD/launchd.now" "$STATE/launchd-baseline"

# --- network sanity ----------------------------------------------------------

gw=$(route -n get default 2>/dev/null | awk '/gateway/ { print $2 }')
if [ -z "$gw" ]; then
  finding "no default gateway" \
    "network is down or DHCP failed — check the cable/Wi-Fi and the router" =
elif ! ping -c 1 -t 3 "$gw" >/dev/null 2>&1; then
  finding "default gateway $gw not answering ping" \
    "local network problem — check switch/router (some gateways drop ICMP; verify before rebooting anything)" \
    "default gateway not answering ping"
fi
if ! dscacheutil -q host -a name apple.com 2>/dev/null | grep -q ip_address; then
  finding "DNS cannot resolve apple.com" \
    "check DNS servers in System Settings > Network; try dscacheutil -flushcache" =
fi

# --- status sweep ------------------------------------------------------------

# Convention 6: a tool whose `status` exits 0 healthy / 3 nothing to check /
# 1 up and broken says so in its `help` — the sentence contains
# `local-health-check` — and that sentence is how this sweep finds it. There
# is no list of tools here, on purpose: the previous stage named three paths,
# and a fourth tool that implemented the contract went unchecked for as long
# as nobody remembered to edit the list. Every sibling folder's convention-1
# entrypoint is asked `help` (bounded, because a probe that runs arbitrary
# entrypoints runs arbitrary code); the ones that answer with the phrase are
# asked `status` (bounded again). Only 1 is a finding, so a machine holding
# no credentials for a service stays silent instead of raising something it
# could never act on. A code outside {0, 1, 3} and a bound that expires are
# findings too: the first is a contract the tool declared and does not keep,
# the second is a hang, and both are worth one line naming the tool.
#
# The remedy line is the tool's own first output line. Nothing here knows
# what a 401 means for one tool and a dead container for another.
HELP_BOUND=5
# STATUS_BOUND is set and validated at the top, before the machine stages.
TOOLS_ROOT=$(cd "$TOOLS_ROOT" && pwd)
declared=0; checked=0
for d in "$TOOLS_ROOT"/*/; do
  d=${d%/}
  name=$(basename "$d")
  # Its own help now describes the phrase, so it would match itself.
  [ "$name" != "local-health-check" ] || continue
  stem=${name#local-}
  e="$d/$stem.sh"
  # Two entrypoints do not follow convention 1's name — local-sd-runner/runner.sh
  # and local-project-dashboard/dashboard.sh — and renaming them means editing
  # and reloading launchd plists and the pack's sd-plugin.json, which was
  # rejected. A folder with no <stem>.sh and exactly one *.sh at its root is
  # taken to mean that one; anything else is skipped as before.
  if [ ! -f "$e" ]; then
    n=0
    for f in "$d"/*.sh; do [ -f "$f" ] && { e=$f; n=$((n + 1)); }; done
    [ "$n" -eq 1 ] || continue
  fi
  rc=0
  bounded "$HELP_BOUND" sh "$e" help > "$TMPD/sweep.help" 2>&1 || rc=$?
  if [ "$BOUNDED_EXPIRED" -eq 1 ]; then
    finding "$name: help did not answer in ${HELP_BOUND}s" \
      "sh $e help should print a heredoc and exit 0; whatever it is doing first belongs behind a subcommand" \
      "$name: help did not answer in ${HELP_BOUND}s"
    continue
  fi
  grep -q 'local-health-check' "$TMPD/sweep.help" || continue
  declared=$((declared + 1))
  rc=0
  bounded "$STATUS_BOUND" sh "$e" status > "$TMPD/sweep.out" 2> "$TMPD/sweep.err" || rc=$?
  if [ "$BOUNDED_EXPIRED" -eq 1 ]; then
    finding "$name: status did not answer in ${STATUS_BOUND}s" \
      "run $e status by hand and see what it waits on; a nightly check cannot wait for it" \
      "$name: status did not answer in ${STATUS_BOUND}s"
    continue
  fi
  checked=$((checked + 1))
  first=$(head -1 "$TMPD/sweep.out")
  [ -n "$first" ] || first=$(head -1 "$TMPD/sweep.err")
  # Tools prefix their verdict with their own name; do not print it twice.
  case "$first" in "$name: "*) first=${first#"$name: "} ;; esac
  case "$rc" in
    0|3) ;;
    # The shareable form keeps the tool's folder name and drops its verdict
    # line: that line is the tool's own text, and local-cron-jobs names the
    # failed jobs in it.
    1) finding "$name: ${first:-status exited 1 without a word}" \
         "run $e status by hand for the full response" \
         "$name: status reports broken (exit 1)" ;;
    *) finding "$name: status exited $rc, which convention 6 does not define (0 healthy, 3 nothing to check, 1 broken)${first:+ — $first}" \
         "run $e status by hand; the tool declares the contract in its help and should keep it" \
         "$name: status exited $rc, which convention 6 does not define (0 healthy, 3 nothing to check, 1 broken)" ;;
  esac
done
info "status sweep: $declared tool(s) declared, $checked checked"

# --- Jev: order the findings, loudest first (on unless switched off) ---------

# `local-jev` answers one narrow question about some state -- here "does this
# finding need a human tonight?" -- and nothing may depend on the answer. So
# this stage is additive and subtractive only: with JEV_HEALTH_CHECK switching
# it off, or with Jev switched off, unkeyed or failing, the report is the
# report it has always been. Unset means on. When Jev does answer, the findings are sorted by that
# probability and each headline carries the number.
#
# It never suppresses a finding, never moves an exit code, and never changes a
# word a stage printed: it reorders pairs of lines and appends a label. The
# count `run` puts in the subject line is the same count either way.
#
# One request and not one per finding: `ask` runs its questions in parallel.
#
# The sweep above asks `local-jev status`, and that probe is a real request.
# When it raised the finding, this stage does not run: a judge that just said
# it is broken cannot usefully rank its own failure, and asking anyway would
# spend a second request to learn nothing. That is why this sits after the
# sweep and not before it, and it is the whole of the loop story -- `jev ask`
# runs nothing in this repository, so there is no recursion to stop, only a
# doubled bill to avoid.
JEV="${HEALTH_CHECK_JEV:-$DIR/../local-jev/jev.sh}"

# Bookkeeping only: one row saying the stages' own order -- the control arm --
# is what the report carried, under the same stage key the switch reads. It
# can never fail this script; `jev record` sends nothing and needs no key.
#
# `--outcome ok` always: the old path completed, and that this row exists at
# all is the fact nothing else carries. No `--decline`, because this script
# puts no deadline on the ask: it waits for jev, so jev returned, and a jev
# that returned has already written the cause on its own row. Repeating it
# here would count one decision as two -- DECLINES groups by stage and cause
# across both arms with no deduplication. A caller that kills jev instead
# does pass a cause, because a killed jev never reaches its flush; see
# `local-notify/notify.sh`.
jev_record_baseline() {
  [ -f "$JEV" ] || return 0
  sh "$JEV" record --caller local-health-check --stage JEV_HEALTH_CHECK \
    --arm baseline --outcome ok >/dev/null 2>&1 || true
  return 0
}

# Every Jev call leaves the machine, so what goes out is built here and never
# assembled from whatever a stage happened to print: each finding's shareable
# form only (see `finding`), with paths, addresses and host names replaced
# as a second net. The headlines and fix lines never leave -- the headlines
# name private jobs and apps, the fix lines are full of absolute paths, and
# Jev needs neither to answer. The redaction over-reaches on purpose (a clock
# time can read as an address); losing a timestamp costs the judgment
# nothing, and a leaked tailnet name cannot be taken back. A path runs to the
# end of its line: a folder name can hold a space, and nothing in a line says
# where such a path ends, so stopping at the space sent the words after it
# (sd:1224). Whatever followed the path is lost with it, which is the same
# trade.
jev_write_redactions() {
  cat > "$TMPD/redact.sed" <<'SEDEOF'
s|/Users/.*|<path>|
s|/private/.*|<path>|
s/[0-9][0-9]*\.[0-9][0-9]*\.[0-9][0-9]*\.[0-9][0-9]*/<ip>/g
s/[0-9a-fA-F:]*::[0-9a-fA-F:]*/<ip>/g
s/[A-Za-z0-9._-]*\.ts\.net/<host>/g
s/[A-Za-z0-9._-]*\.local/<host>/g
SEDEOF
  printf 's|%s.*|<path>|\n' "$HOME" >> "$TMPD/redact.sed"
  for _h in "$(hostname -s 2>/dev/null || true)" "$(hostname 2>/dev/null || true)"; do
    if [ -n "$_h" ]; then printf 's|%s|<host>|g\n' "$_h" >> "$TMPD/redact.sed"; fi
  done
}

# local-weekly-digest/weekly-digest.sh carries the same program over its own
# failure lines. The two folders are independent tools and share no file, for
# the reason convention 2's symlink loop is copied into every script that
# needs one: a sibling reaching into another tool's internals is the coupling
# this layout exists to avoid.
jev_order() {
  [ -f "$JEV" ] || return 0
  # One call asks both halves: can Jev answer on this machine, and has
  # JEV_HEALTH_CHECK been used to switch this stage off? Unset means on --
  # a per-caller switch that defaults to off makes every integration added
  # after it silently never run, which is the failure `jev off` exists for.
  _n=$(grep -c '^- ' "$FINDINGS" || true)
  [ "${_n:-0}" -ge 2 ] || return 0
  # Costs nothing and calls nothing. A machine with the switch off and a
  # machine that was never keyed answer this the same way, on purpose.
  # `--record` so the decline is counted: this is where most nights end, and
  # the stages' own order running is the only fact the control arm has.
  if ! sh "$JEV" enabled JEV_HEALTH_CHECK --record --caller local-health-check \
       >/dev/null 2>&1; then
    # No second row here. The gate above wrote one in the process this call
    # already started, and `judgment.py` counts gate events on their own line;
    # a `jev record` subprocess per decline would double the cost of the path
    # that declines on every run, which is the one path that must stay cheap.
    return 0
  fi
  if grep -q '^- local-jev: ' "$FINDINGS"; then
    info "jev ordering skipped: local-jev raised the finding it would be asked about"
    return 0
  fi
  # Only the shareable forms go out, one per finding and in the same order.
  # A finding without one stops the whole ordering: sending the others would
  # rank a report that is not the one printed, and sending its headline is
  # the leak sd:1362 closed.
  _shared=$(grep -c . "$JEVLINES" || true)
  if [ "$(wc -l < "$JEVLINES" | tr -d ' ')" -ne "$_n" ] || [ "${_shared:-0}" -ne "$_n" ]; then
    info "jev ordering skipped: $(( _n - ${_shared:-0} )) finding(s) have no shareable form"
    jev_record_baseline
    return 0
  fi
  jev_write_redactions
  sed -f "$TMPD/redact.sed" "$JEVLINES" > "$TMPD/jev.headlines"
  # Findings whose shareable forms are identical -- two cron jobs that both
  # failed, say -- are one question, not several. Asked twice, the same
  # evidence gets two answers, and any difference between them is noise that
  # a larger group has more chances to draw high. jev.map keeps, per finding
  # in report order, the id of the one question its form became.
  awk -v sfile="$TMPD/jev.state" -v qfile="$TMPD/jev.questions" -v mfile="$TMPD/jev.map" '
    BEGIN { SQ = sprintf("%c", 39); state = "{\"findings\":{"; qs = "{"; u = 0 }
    {
      line = $0
      # Sanitised and not escaped: a quote or a backslash in a finding would
      # have to be escaped into the JSON, and getting that wrong is how a
      # payload stops being the payload you read. The judgment needs neither
      # character.
      gsub(/\\/, "/", line); gsub(/"/, SQ, line); gsub(/[\t\r]/, " ", line)
      if (line in seen) { print seen[line] > mfile; next }
      id = "f" (++u); seen[line] = id; print id > mfile
      if (u > 1) { state = state ","; qs = qs "," }
      state = state "\"" id "\":\"" line "\""
      qs = qs "\"" id "\":{\"type\":\"noul\",\"instructions\":\"State findings." id \
           " is one line from a nightly health check on a personal Mac." \
           " Does it need a human tonight, rather than whenever someone next looks?" \
           " Judge findings." id " only.\"}"
    }
    END { print state "}}" > sfile; print qs "}" > qfile }
  ' "$TMPD/jev.headlines"
  if ! sh "$JEV" ask --questions "$TMPD/jev.questions" --state "$TMPD/jev.state" \
       --state-format json --caller local-health-check --stage JEV_HEALTH_CHECK \
       > "$TMPD/jev.answers" 2> "$TMPD/jev.err"; then
    _why=$(head -1 "$TMPD/jev.err" 2>/dev/null | cut -c1-120 || true)
    info "jev ordering skipped: ask failed${_why:+ -- $_why}"
    jev_record_baseline
    return 0
  fi
  # The answer shape is local-jev's own `ask` output: an "answers" object of
  # one object per question id, each carrying a "noul". Anything else -- a
  # short answer set, a probability outside 0..1, a reshaped response -- means
  # this stage does not run, because a half-ordered report is worse than the
  # order the stages produced.
  awk '
    match($0, /^[ \t]*"[^"]+"[ ]*:[ ]*\{/) {
      id = $0; sub(/^[ \t]*"/, "", id); sub(/"[ ]*:[ ]*\{.*/, "", id); next
    }
    /"noul"[ ]*:/ && id != "" {
      v = $0; sub(/.*"noul"[ ]*:[ ]*/, "", v); sub(/[^0-9.eE+-].*$/, "", v)
      if (v != "" && v + 0 >= 0 && v + 0 <= 1) { print v "\t" id }
      id = ""
    }
  ' "$TMPD/jev.answers" > "$TMPD/jev.probs"
  _got=$(wc -l < "$TMPD/jev.probs" | tr -d ' ')
  _asked=$(sort -u "$TMPD/jev.map" | wc -l | tr -d ' ')
  if [ "${_got:-0}" -ne "$_asked" ]; then
    info "jev ordering skipped: ${_got:-0} usable answer(s) for $_asked question(s)"
    jev_record_baseline
    return 0
  fi
  # The right count is not the right answers: an id nobody asked, or one id
  # answered twice, leaves a question with no answer of its own at the same
  # count. The ids answered must be the ids asked, each exactly once
  # (sd:1224).
  cut -f2 "$TMPD/jev.probs" | sort > "$TMPD/jev.answered"
  sort -u "$TMPD/jev.map" > "$TMPD/jev.asked"
  if ! cmp -s "$TMPD/jev.answered" "$TMPD/jev.asked"; then
    info "jev ordering skipped: the answer ids are not the $_asked question id(s) asked"
    jev_record_baseline
    return 0
  fi
  # Each finding carries the one answer its question got, so findings that
  # sent the same line tie, and the tie-break below keeps the stages' order
  # inside the group.
  awk -F'	' 'NR == FNR { p[$2] = $1; next } { print p[$1] "\t" "f" FNR }
  ' "$TMPD/jev.probs" "$TMPD/jev.map" > "$TMPD/jev.grouped"
  # Highest first, ties broken by the finding's number so two equal
  # probabilities keep the stages' order from one night to the next.
  sort -t'	' -k1,1rn -k2.2,2n "$TMPD/jev.grouped" > "$TMPD/jev.order"
  : > "$TMPD/findings.jev"
  while IFS='	' read -r _p _id; do
    _i=${_id#f}
    printf '%s  [jev: needs a human tonight %s]\n' \
      "$(sed -n "$(( _i * 2 - 1 ))p" "$FINDINGS")" "$_p" >> "$TMPD/findings.jev"
    sed -n "$(( _i * 2 ))p" "$FINDINGS" >> "$TMPD/findings.jev"
  done < "$TMPD/jev.order"
  cp "$TMPD/findings.jev" "$FINDINGS"
  info "jev ordered $_n finding(s) by \"needs a human tonight\" (one request)"
}
jev_order || :

# --- report ------------------------------------------------------------------

uptime_s=$(uptime | sed 's/^ *//')
echo "health check — $(hostname -s), $(date '+%Y-%m-%d %H:%M')"
echo "uptime: $uptime_s"
echo
if [ -s "$FINDINGS" ]; then
  echo "findings:"
  cat "$FINDINGS"
else
  echo "no findings — system healthy"
fi
if [ -s "$INFO" ]; then
  echo
  echo "notes:"
  cat "$INFO"
fi

if [ "$EMAIL" -eq 1 ] && [ -s "$FINDINGS" ]; then
  n=$(grep -c '^- ' "$FINDINGS" | tr -d ' ')
  subject="health check: $n finding(s) on $(hostname -s)"
  body=$(printf 'health check — %s on %s\nuptime: %s\n\nFindings and suggested fixes:\n%s\n' \
    "$(date '+%Y-%m-%d %H:%M')" "$(hostname -s)" "$uptime_s" "$(cat "$FINDINGS")")
  if [ -s "$INFO" ]; then
    body=$(printf '%s\nNotes:\n%s\n' "$body" "$(cat "$INFO")")
  fi
  if ! sh "$NOTIFY" -t "$subject" -k status -F -c ntfy,email -b "$body"; then
    echo "email FAILED — exiting 1 so the cron failure push fires" >&2
    exit 1
  fi
fi

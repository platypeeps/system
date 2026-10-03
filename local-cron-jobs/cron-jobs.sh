#!/bin/sh
# cron-jobs — run headless Claude Code jobs on a schedule via macOS launchd,
# fully independent of Claude Desktop or any open terminal.
#
# Each job is one file <name>.job in the config directory's jobs folder,
# <config>/cron-jobs/jobs/ (every machine), in its host folder
# <config>/cron-jobs/jobs/<host>/ (this machine only), or in a directory named
# by CRON_JOBS_EXTRA_DIRS (see below), as sourced shell vars. <config> is
# ${SYSTEM_TOOLS_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/system}.
# The repository ships no jobs; examples/ holds sample job files to copy.
#   JOB_SCHEDULE   5-field cron, LOCAL time (supported: * N a,b,c */N)
#   JOB_PROMPT     prompt or /skill invocation passed to `claude -p`
#   JOB_COMMAND    alternative to JOB_PROMPT: plain shell command run via
#                  `bash -c` — for jobs that don't need Claude at all
#   JOB_DIR        optional working directory for the run (default: $HOME)
#   JOB_MODEL      optional model override (JOB_PROMPT jobs only)
#   JOB_CLAUDE_ARGS optional extra flags for the claude CLI
#   JOB_RESULT_OK  optional ERE (JOB_PROMPT jobs only): the reply's last
#                  `RESULT: ` line must match it, or the run fails
#   JOB_TIMEOUT    optional run limit: seconds, or N followed by s, m or h;
#                  0 turns it off. Default: CRON_JOBS_JOB_TIMEOUT, else 2h.
#                  A run past it has its process group sent TERM, then KILL
#                  after CRON_JOBS_TIMEOUT_GRACE seconds (default 10), and
#                  fails with exit 124.
#
# Usage:
#   cron-jobs.sh list                     jobs, schedules, installed/loaded state, folder
#   cron-jobs.sh install <job>|--all      generate plist, load into launchd
#                                        (--all = every defined job)
#   cron-jobs.sh verify [job]|--all       installed plist vs the generator
#   cron-jobs.sh uninstall <job>|--all    unload and remove plist
#   cron-jobs.sh run <job>                run once now, foreground (logs too)
#   cron-jobs.sh status [job]             launchd state + last run result + log tail
#   cron-jobs.sh logs <job> [lines]       tail a job's log
#   cron-jobs.sh exec <job>               internal: what launchd invokes
#   cron-jobs.sh test                     run the unittest suite in tests/
#
# Failure reporting: on a non-zero exit the wrapper posts a macOS notification,
# appends to logs/failures.log, and — if NTFY_TOPIC is set in
# <config>/cron-jobs/notify.conf — pushes to ntfy.sh (reaches your phone).
# See notify.conf.example.
#
# Local configuration (environment, or <config>/cron-jobs/.env; a value
# already exported wins over .env). See .env.example.
#   CRON_JOBS_EXTRA_DIRS      colon-separated extra job directories, e.g. a
#                             private repo's jobs folder. A job there overrides
#                             a same-named job in <config>/cron-jobs/jobs.
#   CRON_JOBS_HOST            the host folder's name (default `hostname -s`),
#                             lower-cased. A job in jobs/<host>/ overrides a
#                             same-named shared one; other hosts' folders are
#                             not read.
#   SYSTEM_TOOLS_LABEL_PREFIX launchd label prefix (default local.system-tools);
#                             labels are <prefix>.cron.<job>.
#   CRON_JOBS_JOB_TIMEOUT     the run limit for a job that sets no JOB_TIMEOUT
#                             (default 2h; same format as JOB_TIMEOUT).
set -eu

ROOT="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=/dev/null
. "$ROOT/../lib/config.sh"
CONF_DIR="$(st_config_dir cron-jobs)"

# Read .env here, before anything resolves a job: launchd starts `exec` with
# only PATH and HOME, so a value exported in a login shell never reaches it.
# Put the values in <config>/cron-jobs/.env so install and the scheduled run
# agree. st_source_env exports what it reads, so a job inherits the values
# too: a health check or runner status a job calls needs the same label
# prefix as this script. The three named here are put back afterwards,
# because an exported value must win over the file.
_cj_extra="${CRON_JOBS_EXTRA_DIRS:-}"
_cj_prefix="${SYSTEM_TOOLS_LABEL_PREFIX:-}"
_cj_host="${CRON_JOBS_HOST:-}"
_cj_timeout="${CRON_JOBS_JOB_TIMEOUT:-}"
st_source_env cron-jobs
[ -z "$_cj_extra" ] || CRON_JOBS_EXTRA_DIRS="$_cj_extra"
[ -z "$_cj_prefix" ] || SYSTEM_TOOLS_LABEL_PREFIX="$_cj_prefix"
[ -z "$_cj_host" ] || CRON_JOBS_HOST="$_cj_host"
[ -z "$_cj_timeout" ] || CRON_JOBS_JOB_TIMEOUT="$_cj_timeout"
unset _cj_extra _cj_prefix _cj_host _cj_timeout
LABEL_PREFIX="${SYSTEM_TOOLS_LABEL_PREFIX:-local.system-tools}"
JOBS_DIR="$CONF_DIR/jobs"
# This machine's own jobs: jobs/<host>/, lower-cased so the folder name does
# not depend on how the machine happens to capitalise itself. The same rule is
# in lib/system_tools_config.py and local-sd-db's sd_db/config.py
# (cron_job_dirs); change all three together.
JOB_HOST="$(printf '%s' "${CRON_JOBS_HOST:-$(hostname -s 2>/dev/null || hostname)}" | tr '[:upper:]' '[:lower:]')"
HOST_JOBS_DIR="$JOBS_DIR/$JOB_HOST"
EXTRA_JOB_DIRS="${CRON_JOBS_EXTRA_DIRS:-}"
LOG_DIR="$ROOT/logs"
FAIL_LOG="$LOG_DIR/failures.log"
AGENT_DIR="$HOME/Library/LaunchAgents"
DOMAIN="gui/$(id -u)"

# Homebrew's prefix is /opt/homebrew on Apple Silicon and /usr/local on Intel.
# Both were spelled into the plist PATH so one of them always matched, which
# left every LaunchAgent carrying two directories that do not exist. Resolve it
# here instead: the plist is rendered on the machine it is for, so the answer is
# known at install time.
if [ -x /opt/homebrew/bin/brew ]; then
  BREW_PREFIX=/opt/homebrew
elif [ -x /usr/local/bin/brew ]; then
  BREW_PREFIX=/usr/local
else
  BREW_PREFIX=""
fi

# /usr/local/bin stays on the list even when it is not the brew prefix — it is
# a standard location on both architectures — but never twice.
# ~/bin/common holds the bin-links this repo installs. A login shell adds it
# (.zshrc, .bash_profile); launchd does not, so it has to be spelled here too.
# Without it a PATH-drift check reported "PATH MISSING" every night from the
# cron job while an interactive run of the same command was clean — the drift
# was in the job's environment, not on the machine.
JOB_PATH=""
[ -d "$HOME/bin/common" ] && JOB_PATH="$HOME/bin/common:"
JOB_PATH="$JOB_PATH$HOME/.local/bin"
[ -n "$BREW_PREFIX" ] && JOB_PATH="$JOB_PATH:$BREW_PREFIX/bin:$BREW_PREFIX/sbin"
[ "$BREW_PREFIX" = /usr/local ] || JOB_PATH="$JOB_PATH:/usr/local/bin"
JOB_PATH="$JOB_PATH:/usr/bin:/bin:/usr/sbin:/sbin"

mkdir -p "$LOG_DIR"

# Where the agent is, not what it is called. Same problem as the PATH above,
# worse consequence: this one is executed, not just listed. The default once
# named the Apple Silicon cask path, so on an Intel machine — or any machine
# running the native ~/.local/bin install — every prompt-driven job died with
# "no such file". The fix resolved it from PATH and fell back to the bare name,
# which resolves only where PATH already carries ~/.local/bin: a login shell
# does, an exec'd command does not. The jobs here survived that because the
# plist spells the directory into PATH, so it stayed latent — until sd-plan
# resolved the same binary the same way from the runner's queue and died with
# FileNotFoundError: 'claude' (local-sd-plan/sd_plan.py, claude_binary()).
# Same order as there: an explicit CLAUDE_BIN, then PATH, then the install
# location — and when none of them exists, one sentence now rather than an
# exec error later. Called where the binary is needed; list, verify and help
# never ask.
claude_binary() {
  local installed="$HOME/.local/bin/claude" found
  if [ -n "${CLAUDE_BIN:-}" ]; then
    echo "$CLAUDE_BIN"
    return 0
  fi
  if found="$(command -v claude 2>/dev/null)" && [ -n "$found" ]; then
    echo "$found"
    return 0
  fi
  if [ -x "$installed" ]; then
    echo "$installed"
    return 0
  fi
  echo "cron-jobs.sh: no claude on PATH and none at $installed; export CLAUDE_BIN to the binary that should run the job, or install claude at ~/.local/bin/claude" >&2
  return 1
}

# A prompt job's `claude -p` writes its runtime trace to
# logs/<job>.debug.<run>.log (sd:972: "error: An unknown error occurred
# (Unexpected)" is the Bun runtime's, printed before any session exists, and
# the binary writes no debug log unless --debug-file asks). A passed run
# removes its own file in cmd_exec; this keeps the newest five that remain,
# so a job failing every night cannot fill the log dir.
prune_debug_files() { # job
  local old
  # shellcheck disable=SC2012 # the names are this script's own (job, UTC stamp, pid): no whitespace, and ls -t is the mtime order wanted
  ls -t "$LOG_DIR/$1".debug.*.log 2>/dev/null | tail -n +6 | while IFS= read -r old; do
    rm -f "$old"
  done
}

label_for()  { echo "$LABEL_PREFIX.cron.$1"; }
plist_for()  { echo "$AGENT_DIR/$(label_for "$1").plist"; }

# The job directories, one per line: each CRON_JOBS_EXTRA_DIRS entry in order,
# then this host's <config>/cron-jobs/jobs/<host>, then the shared
# <config>/cron-jobs/jobs. The first directory holding <job>.job defines it, so
# an extra directory overrides the config directory and a host folder
# overrides the shared one. Other hosts' folders are never listed.
job_dirs() {
  local d old_ifs="$IFS"
  IFS=':'
  for d in $EXTRA_JOB_DIRS; do
    [ -n "$d" ] && echo "$d"
  done
  IFS="$old_ifs"
  [ -n "$JOB_HOST" ] && echo "$HOST_JOBS_DIR"
  echo "$JOBS_DIR"
}

job_file() {
  local d found
  found="$(job_dirs | while IFS= read -r d; do
    if [ -f "$d/$1.job" ]; then echo "$d/$1.job"; break; fi
  done)"
  echo "${found:-$JOBS_DIR/$1.job}"
}

# A run's limit when neither the job nor CRON_JOBS_JOB_TIMEOUT sets one. Long
# enough for the slowest prompt job; short enough that a hung run frees its
# lock the same night rather than holding it until someone looks (sd:2018).
DEFAULT_JOB_TIMEOUT=2h

# A limit as whole seconds: N, Ns, Nm or Nh. Leading zeros are dropped
# first, since `$(( ))` reads 010 as octal.
timeout_seconds() { # value
  local n="${1%[smh]}"
  case "$n" in '' | *[!0-9]*) return 1 ;; esac
  n="${n#"${n%%[!0]*}"}"
  n="${n:-0}"
  case "$1" in
    *h) echo $((n * 3600)) ;;
    *m) echo $((n * 60)) ;;
    *) echo "$n" ;;
  esac
}

load_job() {
  local f; f="$(job_file "$1")"
  [ -f "$f" ] || { echo "ERROR: no such job '$1' (expected $f)" >&2; exit 1; }
  JOB_SCHEDULE="" JOB_PROMPT="" JOB_COMMAND="" JOB_DIR="" JOB_MODEL="" JOB_CLAUDE_ARGS=""
  JOB_RESULT_OK="" JOB_TIMEOUT=""
  # shellcheck source=/dev/null
  # shellcheck disable=SC1090
  . "$f"
  [ -n "$JOB_SCHEDULE" ] && { [ -n "$JOB_PROMPT" ] || [ -n "$JOB_COMMAND" ]; } || {
    echo "ERROR: $f must set JOB_SCHEDULE and JOB_PROMPT or JOB_COMMAND" >&2; exit 1;
  }
  if [ -n "$JOB_PROMPT" ] && [ -n "$JOB_COMMAND" ]; then
    echo "ERROR: $f sets both JOB_PROMPT and JOB_COMMAND — pick one" >&2; exit 1
  fi
  local limit="${JOB_TIMEOUT:-${CRON_JOBS_JOB_TIMEOUT:-$DEFAULT_JOB_TIMEOUT}}"
  JOB_TIMEOUT_SECONDS="$(timeout_seconds "$limit")" || {
    echo "ERROR: $f: JOB_TIMEOUT (or CRON_JOBS_JOB_TIMEOUT) '$limit' is not N, Ns, Nm or Nh" >&2
    exit 1
  }
}

# Does the installed plist still match what the generator produces? A setup
# stage that installs cron jobs could only ever install a job whose plist was ABSENT, so a
# plist whose content had gone stale reported ok forever — every one of the 9
# jobs here read healthy while carrying a PATH that #109 had already fixed.
# Renders to a scratch AGENT_DIR and compares; never touches launchd.
cmd_verify() {
  local job="$1" tmp installed rc=0
  installed="$(plist_for "$job")"
  if [ ! -f "$installed" ]; then
    echo "  missing $job"
    return 1
  fi
  tmp="$(mktemp -d)"
  ( AGENT_DIR="$tmp"; load_job "$job"; write_plist "$job" )
  if cmp -s "$tmp/$(label_for "$job").plist" "$installed"; then
    echo "  ok      $job"
  else
    echo "  STALE   $job — installed plist differs from the generator"
    rc=1
  fi
  rm -rf "$tmp"
  return $rc
}

# Where a job's file sits, as `list` shows it: `jobs` (shared), `jobs/<host>`,
# or an extra directory's full path.
job_origin() {
  local d; d="$(dirname "$(job_file "$1")")"
  case "$d" in
    "$CONF_DIR"/*) echo "${d#"$CONF_DIR"/}" ;;
    *) echo "$d" ;;
  esac
}

all_jobs() {
  local d f
  job_dirs | while IFS= read -r d; do
    for f in "$d"/*.job; do
      [ -e "$f" ] || continue
      basename "$f" .job
    done
  done | sort -u
}

is_loaded() { launchctl print "$DOMAIN/$(label_for "$1")" >/dev/null 2>&1; }

# --- cron -> launchd StartCalendarInterval ---------------------------------
# Supports per field: "*", a number, a comma list, or "*/N".
# Emits the cross-product of minute x hour x (day|weekday|month) entries.

expand_field() { # value, max (exclusive upper bound for */N stepping), min
  local val="$1" max="$2" min="$3" step i out
  case "$val" in
    "*")
      echo ""
      ;;
    */*)
      step="${val#*/}"
      i="$min"
      out=""
      while [ "$i" -lt "$max" ]; do
        out="$out$i,"
        i=$((i + step))
      done
      echo "${out%,}"
      ;;
    *)
      echo "$val"
      ;;
  esac
}

calendar_xml() { # cron expression -> StartCalendarInterval plist XML
  local cron="$1" c_min c_hour c_dom c_mon c_dow had_noglob
  # Field splitting below is deliberate, but pathname expansion is NOT: a cron
  # "*" would otherwise glob against the working directory.
  case $- in *f*) had_noglob=1 ;; *) had_noglob=0 ;; esac
  set -f
  # shellcheck disable=SC2086
  set -- $cron
  c_min="$1" c_hour="$2" c_dom="$3" c_mon="$4" c_dow="$5"
  local mins hours doms mons dows
  mins="$(expand_field "$c_min" 60 0)"
  hours="$(expand_field "$c_hour" 24 0)"
  doms="$(expand_field "$c_dom" 32 1)"
  mons="$(expand_field "$c_mon" 13 1)"
  dows="$(expand_field "$c_dow" 7 0)"

  # Cross-product of the five fields. Splitting is done by setting IFS to a
  # comma and leaving the expansions unquoted on purpose; "x" stands for a
  # field that was "*", i.e. one the plist must not pin down.
  local entries="" e m h d mo w wd old_ifs="$IFS"
  IFS=','
  for m in ${mins:-x}; do
  for h in ${hours:-x}; do
  for d in ${doms:-x}; do
  for mo in ${mons:-x}; do
  for w in ${dows:-x}; do
    e="        <dict>\n"
    [ "$m"  = x ] || e="$e            <key>Minute</key><integer>$m</integer>\n"
    [ "$h"  = x ] || e="$e            <key>Hour</key><integer>$h</integer>\n"
    [ "$d"  = x ] || e="$e            <key>Day</key><integer>$d</integer>\n"
    [ "$mo" = x ] || e="$e            <key>Month</key><integer>$mo</integer>\n"
    if [ "$w" != x ]; then
      # cron allows 0 and 7 for Sunday; launchd only takes 0.
      if [ "$w" -eq 7 ]; then wd=0; else wd="$w"; fi
      e="$e            <key>Weekday</key><integer>$wd</integer>\n"
    fi
    e="$e        </dict>"
    entries="$entries$e\n"
  done; done; done; done; done
  IFS="$old_ifs"
  [ "$had_noglob" = 1 ] || set +f

  printf '    <key>StartCalendarInterval</key>\n    <array>\n'
  printf "%b" "$entries"
  printf '    </array>\n'
}

# The Label and the ProgramArguments below are how local-machine-setup's cron
# stage knows a plist as ours (cron_plist_ours): change them together.
write_plist() { # job name
  local job="$1" label plist
  label="$(label_for "$job")"
  plist="$(plist_for "$job")"
  {
    cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$label</string>

    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>$ROOT/cron-jobs.sh</string>
        <string>exec</string>
        <string>$job</string>
    </array>

    <key>WorkingDirectory</key>
    <string>$ROOT</string>

EOF
    calendar_xml "$JOB_SCHEDULE"
    cat <<EOF

    <key>RunAtLoad</key>
    <false/>

    <key>StandardOutPath</key>
    <string>$LOG_DIR/$job.log</string>
    <key>StandardErrorPath</key>
    <string>$LOG_DIR/$job.log</string>

    <key>EnvironmentVariables</key>
    <dict>
        <!-- launchd starts jobs from launchd, not from a login shell, so
             ~/.bash_profile is never read and this is the whole PATH a job
             gets. It has to be spelled out here; editing a shell rc file
             does not reach a LaunchAgent. The brew sbin directory was
             missing, which is what made brew-doctor-nightly warn about it
             every night while an interactive "brew doctor" stayed clean.
             \$JOB_PATH is built above from whichever Homebrew prefix this
             machine actually has, so the two entries for the other
             architecture are no longer carried here.
             NB: this heredoc is unquoted, because the template needs
             \$label/\$job/\$ROOT/\$HOME expanded. Backticks expand too — the
             word above used to be in backticks, so rendering a plist ran
             brew doctor and pasted its output into this comment. -->
        <key>PATH</key>
        <string>$JOB_PATH</string>
        <key>HOME</key>
        <string>$HOME</string>
    </dict>

    <key>ProcessType</key>
    <string>Background</string>
</dict>
</plist>
EOF
  } > "$plist"
}

notify_failure() { # job, exit code
  local job="$1" rc="$2" ts
  ts="$(date '+%Y-%m-%dT%H:%M:%S%z')"
  echo "$ts $job FAILED rc=$rc (log: logs/$job.log)" >> "$FAIL_LOG"
  osascript -e "display notification \"Job '$job' failed (rc=$rc). See local-cron-jobs/logs.\" with title \"cron-jobs failure\"" 2>/dev/null || true
  # Optional phone push via ntfy.sh — set NTFY_TOPIC in
  # <config>/cron-jobs/notify.conf
  local NTFY_TOPIC=""
  # shellcheck source=/dev/null
  [ -f "$CONF_DIR/notify.conf" ] && . "$CONF_DIR/notify.conf"
  if [ -n "$NTFY_TOPIC" ]; then
    curl -s -m 10 -H "Title: cron-jobs: $job failed" -H "Priority: high" \
      -d "Exit $rc at $ts on $(hostname -s). Log: local-cron-jobs/logs/$job.log" \
      "https://ntfy.sh/$NTFY_TOPIC" >/dev/null || true
  fi
}

# launchd points a job's stdout at its log (StandardOutPath in the plist); a
# hand-run points it at the terminal, so nothing reached the log. The gap was
# one-sided: notify_failure appends to failures.log on ANY run, while the
# "[job] <ts> done" line that failure_resolved and cmd_watchdog read only ever
# landed in the log under launchd. So a job run by hand could RAISE a failure
# that only the next launchd firing could clear, and still read as stale to the
# watchdog afterwards. A hand-run now mirrors its output into the same log.
#
# A FIFO, not `cmd_exec | tee`: an EXIT trap set inside a pipeline subshell does
# not fire in this shell, and the outcome line cmd_exec owes is written by
# exactly such a trap. This keeps cmd_exec in the main shell, where its trap
# works.
TEE_PID=""
TEE_FIFO=""
start_tee() { # log path
  # Is stdout already this file? Device+inode is the obvious test, but
  # /dev/fd/1 reports the devfs device rather than the file's, so compare
  # inodes. Read through a duplicate on fd 9: inside $( ) fd 1 is the
  # substitution's pipe, and stat'ing that compares the pipe to the log and
  # always disagrees — which duplicated every line of a launchd run.
  # Not isatty: `run` redirected into a file or piped to a pager is still a
  # hand-run and still has to reach the log.
  exec 9>&1
  tee_fd=$(stat -f '%i' /dev/fd/9 2>/dev/null || true)
  exec 9>&-
  if [ -f "$1" ] && [ -n "$tee_fd" ] &&
     [ "$tee_fd" = "$(stat -f '%i' "$1" 2>/dev/null)" ]; then
    return 0
  fi
  mkdir -p "$(dirname "$1")"
  TEE_FIFO="$1.fifo.$$"
  if ! mkfifo "$TEE_FIFO" 2>/dev/null; then TEE_FIFO=""; return 0; fi
  # Without the job's lock (fd 8): tee is a helper, not the job, and one left
  # blocked after a killed runner would otherwise hold the lock for good.
  tee -a "$1" < "$TEE_FIFO" 8>&- &
  TEE_PID=$!
  exec 3>&1 4>&2 > "$TEE_FIFO" 2>&1
}

# Restores the saved descriptors and waits, so the last lines are in the file
# before the process goes away. Called from cmd_exec's EXIT trap, which is what
# a failing job reaches.
#
# Closing the last writer is what hands tee its EOF, and on macOS that wakeup
# is sometimes lost: tee stays blocked in read() on a FIFO no process holds
# open for writing, a second reader of the same FIFO reads EOF at once, and
# the bare `wait` that used to follow never returned (sd:773; about one run in
# three hundred in a local loop under load). Opening and closing the FIFO
# again delivers the EOF. `<>` opens it read-write, which does not block even
# once tee is gone; on `true` and not `:`, whose failed redirection would end
# the shell.
# Bounded, because a reaped tee's pid can be reused; `wait` still follows, so
# a tee that is slow rather than stuck keeps every line.
#
# Read-write and not write-only, and that is the load-bearing half: tee can
# exit between the liveness test above and this open, and a write-only open of
# a FIFO no one is reading blocks until a reader arrives, which is for ever
# here. So `1>` would turn the rare lost wakeup into a certain hang on every
# run whose tee happens to die in that window. `1<>` is also O_RDWR|O_CREAT,
# so on a path that has gone it creates a zero-byte regular file (through a
# symlink, the target) rather than failing; it never truncates one that is
# there. Harmless because TEE_FIFO is always "$LOG_DIR/$job.log.fifo.$$" and
# the `rm -f` below removes whatever the open left.
#
# What the bound does NOT do is bound the hang. If 50 nudges over 5 s fail to
# release tee, control falls through to the `wait` below and the run blocks for
# ever, which is sd:773's own symptom. The nudge makes that much less likely and
# not impossible: measured at sd:773 on one machine, 4 hangs in 3000 runs
# without it against 0 in 3000 with it, and 6 in 2500 against 0 in 2500 in the
# run before that. A `kill "$TEE_PID"` once the bound is spent would trade the
# residual hang for a lost tail; that trade is not made here, because the tail
# is the half of the log an operator reads. The bare `wait` is the only
# unbounded wait I can find in this path, which is an argument from reading it
# rather than a proof.
#
# The loop adds time to a hand-run, and most of it is the one sleep after tee
# has already died. That delta is not a fixed figure: it is the real wall time
# of `sleep 0.1`, which overshoots by an amount that varies with timer behaviour
# and load. One machine, two sessions: +0.165 s at sd:808 (load 5.5-8.6) and
# +0.237 s at the verification of system #357 (load 3.9-5.1, where `sleep 0.1`
# took a median 0.23 s). The sd:808 session, 30 runs of a three-line job per
# variant, three repeats: every loop run did exactly one iteration -- one nudge,
# one sleep, then `kill -0` fails -- at `sleep 0.1` and at `sleep 0.01` alike,
# so tee is gone before even a 0.01 s sleep returns. Per run, the mean of the
# three in that session: no loop 0.043 s; this loop 0.208 s; `sleep 0.01`
# instead 0.083 s; 0.02 s for the first five tries and 0.1 s after, with the
# bound raised to 54, 0.120 s. Testing liveness again straight after the nudge
# saves nothing (0.276 s against 0.272 s, one pair of 30-run sides at another
# load), because tee takes well under a millisecond to be scheduled and exit
# (about 0.1-0.2 ms: with no sleep, `kill -0` failed after 2-4 tries of about
# 45 us each at the #357 verification) and that test comes sooner than that.
# The 0.1 s sleep and the bound of 50 are kept by owner decision (sd:808,
# 2026-09-14). The measured cuts saved 0.085-0.165 s a hand-run in those two
# sessions (sd:808: 0.125 s with `sleep 0.01`, 0.088 s with the bound-54 form
# below; the #357 verification: 0.165 s and 0.085 s), on hand-runs only. The
# alternative, five sleeps of 0.02 s and 49 of 0.1 s with a bound of 54, keeps
# the 5.0 s window by arithmetic only and was not timed against a stuck tee. A
# scheduled job pays none of this -- launchd already has the job's stdout on the
# log, start_tee returns before it makes a FIFO, TEE_PID stays empty and this
# function returns at its first line.
stop_tee() {
  [ -n "$TEE_PID" ] || return 0
  exec 1>&3 2>&4 3>&- 4>&-
  local nudges=0
  while [ "$nudges" -lt 50 ] && kill -0 "$TEE_PID" 2>/dev/null; do
    true 2>/dev/null 1<>"$TEE_FIFO" || true
    nudges=$((nudges + 1))
    sleep 0.1
  done
  wait "$TEE_PID" 2>/dev/null || true
  rm -f "$TEE_FIFO"
  TEE_PID=""; TEE_FIFO=""
}

record_run_report() { # job, run identity, start, end, exit, log offset
  local report_sd
  report_sd="${SD_REPORT_BIN:-$(command -v sd 2>/dev/null || true)}"
  if [ -z "$report_sd" ] || ! "$report_sd" reports ingest "$1" --run-id "$2" \
      --started "$3" --ended "$4" --exit-code "$5" --offset "$6" \
      --log "$LOG_DIR/$1.log" --json >/dev/null; then
    echo "[$1] report was not recorded in the workflow database; source remains in $LOG_DIR/$1.log (offset $6, run $2)" >&2
  fi
}

# The outcome line every run that started owes its log. cmd_exec writes "done"
# or "FAILED rc=N" itself on the paths that reach the end; an earlier exit
# reaches none of them. An unreadable JOB_DIR under `set -e`, a sourced env.sh
# that exits, a signal: each one left the log ending in "starting", and
# `last_outcome` answered with the PREVIOUS run's line. `status` then called a
# job healthy that had just died. That was survivable while launchd's counter
# was a second opinion; sd:1201 removed it for cron labels, so the log is now
# the only record and it has to be complete.
#
# Set while a run owns the log and owes it an outcome; cleared by whoever
# writes one. Global, not `local` to cmd_exec: a trap that fires on the way
# out of the shell cannot read a function's locals. EXEC_STARTED is the run's
# start, which the stamp's end is written beside, for the same reason.
EXEC_JOB=""
EXEC_STARTED=""

# The job's lock is a kernel `flock` on `logs/.<job>.flock`, taken on fd 8
# (sd:1251). The kernel releases it when the last process holding the file
# open exits, however it exits, so a killed run leaves no stale lock to judge
# and nothing to reclaim. The workload inherits fd 8, so the lock stays held
# while `bash -c` or claude outlives a runner killed alone. A lock directory
# with a pid in it needed a liveness guess and a remove-and-retake that two
# runs could both win. Perl, because macOS ships no flock(1); `>&=` locks the
# shell's own open file, so the lock outlives the perl that took it.
# shellcheck disable=SC2016 # perl source, expanded by perl
LOCK_TAKE='use Fcntl ":flock"; open(my $fh, ">&=", 8) or exit 2; flock($fh, LOCK_EX|LOCK_NB) or exit 1; exit 0'

# Is a run holding this job's lock? Asked by taking it on a fresh open in a
# subshell, which releases it again on exit. Only perl's own "held" (1) is a
# yes: a file it cannot open or a perl that cannot run says nothing, and
# reading that as held would call an abandoned run one in progress.
lock_is_live() { # lock file
  local rc=0
  [ -f "$1" ] && [ -w "$1" ] || return 1
  ( exec 8>>"$1" && perl -e "$LOCK_TAKE" ) 2>/dev/null || rc=$?
  [ "$rc" -eq 1 ]
}

# The job's workload, bounded by its JOB_TIMEOUT (sd:2018). Before this a
# hung run held the lock above for as long as it hung, and every later slot
# logged "skipped: previous run still active" and exited 0 -- hours of
# silence with nothing failing. Perl, for the reason the lock uses it: macOS
# ships no timeout(1).
#
# The workload runs in a process group of its own, so the limit ends all of
# it: a `bash -c` whose children run on, or claude's MCP servers, would
# otherwise keep the lock after their shell was killed. TERM first, then KILL
# for whatever is left after the grace period, and exit 124, the code GNU
# timeout uses. The lines go to stderr, which is the log, and never into the
# reply file a JOB_RESULT_OK run sends stdout to.
#
# A group of its own also leaves the group launchd signals on `bootout`, and
# a terminal's ^C. So perl passes INT, TERM and HUP on to the job's group and
# waits for it as before. A SIGKILL to perl itself cannot be passed on: the
# job then runs on, holding the lock, as it did before this.
#
# Perl closes its copy of the lock (fd 8) once the job has its own: it is a
# helper, like tee, and the job alone decides how long the lock is held.
#
# A terminal on stdin is swapped for /dev/null: a background group that reads
# it is stopped, and a hand-run would hang where a scheduled one, whose stdin
# is /dev/null already, does not.
# shellcheck disable=SC2016 # perl source, expanded by perl
RUN_BOUNDED='use strict; use POSIX (); use Time::HiRes ();
my ($job, $limit, $grace, @cmd) = @ARGV;
sub say_log { printf STDERR "[%s] %s %s\n", $job, POSIX::strftime("%Y-%m-%dT%H:%M:%S%z", localtime), $_[0] }
my $pid = fork;
defined $pid or do { say_log("cannot start the job: $!"); exit 1 };
if (!$pid) {
  setpgrp(0, 0);
  open(STDIN, "<", "/dev/null") if -t STDIN;
  exec { $cmd[0] } @cmd;
  say_log("cannot run $cmd[0]: $!");
  POSIX::_exit(127);
}
setpgrp($pid, $pid);
POSIX::close(8);
for my $sig (qw(INT TERM HUP)) { $SIG{$sig} = sub { kill $sig, -$pid } }
my $status;
eval {
  local $SIG{ALRM} = sub { die "limit\n" };
  alarm $limit;
  $status = $? if waitpid($pid, 0) == $pid;
  alarm 0;
};
if (defined $status) { exit($status & 127 ? 128 + ($status & 127) : $status >> 8) }
say_log("timed out after ${limit}s (JOB_TIMEOUT); sending TERM to process group $pid");
kill "TERM", -$pid;
my $end = Time::HiRes::time() + $grace;
while (1) {
  waitpid($pid, POSIX::WNOHANG());
  last unless kill 0, -$pid;
  if (Time::HiRes::time() >= $end) {
    say_log("process group $pid still running ${grace}s after TERM; sending KILL");
    kill "KILL", -$pid;
    last;
  }
  Time::HiRes::sleep(0.1);
}
waitpid($pid, 0);
exit 124;'

# Runs a command under the job's limit; 0 runs it directly, as before sd:2018.
run_bounded() { # job, seconds, command...
  local job="$1" limit="$2"
  shift 2
  if [ "$limit" -eq 0 ]; then
    "$@"
    return
  fi
  perl -e "$RUN_BOUNDED" "$job" "$limit" "${CRON_JOBS_TIMEOUT_GRACE:-10}" "$@"
}

exec_exit() {
  local code=$?
  if [ -n "$EXEC_JOB" ]; then
    # A run that still owes an outcome here did not reach the end of
    # `cmd_exec`, so it failed whatever the shell's status happens to be. The
    # record carries that code now, and `FAILED rc=0` beside a recorded 0
    # would be the log and the record disagreeing about the same run -- which
    # is the class of defect the record exists to end.
    [ "$code" -ne 0 ] || code=1
    echo "[$EXEC_JOB] $(date '+%Y-%m-%dT%H:%M:%S%z') FAILED rc=$code"
    notify_failure "$EXEC_JOB" "$code" || true
    record_outcome "$EXEC_JOB" "$code"
    EXEC_JOB=""
  fi
  stop_tee
}

# launchd's own spawn counter for a job, or "" when launchd does not hold the
# label at all. It counts a run from the moment launchd spawns it, so a run
# reading this sees itself counted -- which is the whole reason the comparison
# below works, and it was measured rather than assumed: a probe agent
# kickstarted three times wrote `runs = 1`, `runs = 2`, `runs = 3` from inside
# its own three runs.
launchd_runs() { # job
  launchctl print "$DOMAIN/$(label_for "$1")" 2>/dev/null \
    | grep -E '^[[:space:]]*runs = ' | head -1 | sed 's/.*= *//'
}

# launchd's verdict on the last run it spawned of this label, verbatim. It is
# a number for a run that exited, and `(never exited)` for a label that has
# been bootstrapped and not yet spawned -- measured here rather than assumed:
# a probe agent bootstrapped and never kickstarted printed
# `runs = 0` with `last exit code = (never exited)`, and printed it again
# after `bootout` plus `bootstrap` reset the counter of a label that had
# already exited 7. Empty when launchd does not hold the label at all.
launchd_last_exit() { # job
  launchctl print "$DOMAIN/$(label_for "$1")" 2>/dev/null \
    | grep -E '^[[:space:]]*last exit code = ' | head -1 | sed 's/.*= *//'
}

# Does launchd report a failed run? Only a plain zero and launchd's own
# non-answers are read as "no failure"; anything else counts, including a
# form this has not seen. That is the safe direction for this file: launchd's
# failure is what stands unless something proves a later success, so an
# unparsed value must not become a silent pass.
launchd_failed() { # job
  case "$(launchd_last_exit "$1")" in
    '' | 0 | '('* ) return 1 ;;
    *) return 0 ;;
  esac
}

# The launchd lifetime this job's counter belongs to. `runs` restarts at 1
# whenever the label is bootstrapped again: a reboot does it to every label,
# and `launchctl bootout` followed by `bootstrap` does it to one of them
# without rebooting anything. The number alone therefore cannot tell this
# lifetime's first run from the last one's.
#
# The resource coalition the label's processes are placed in does move, and
# exactly there. Measured, not assumed: three kickstarts of a probe agent read
# `runs = 1`, `3`, `5` with the same coalition ID 85948, while a bootout and
# bootstrap of that label read `runs = 1` under 85942 and then `runs = 1`
# under 85944. The ID changes when and only when the counter restarts.
#
# It is not an identity on its own, and the seventh round of the #486 review
# is where that showed. The id is a boot-local counter: it restarts low at
# every boot, so a label bootstrapped after a reboot can be handed the id a
# record written before that reboot already names, and a stale success then
# matches a fresh startup failure on both fields. Measured here rather than
# assumed: daemons bootstrapped at boot carry ids 329 and 339 while a label
# loaded four days into the same uptime carries 16294. `boot_token` below
# separates the boots this id cannot, and the record carries both.
#
# `launchctl print` names two coalitions, `resource` first and `jetsam` after
# it, each an `ID = ...` line inside its own block. This takes the first `ID`
# after the `resource coalition` header and stops there, so the jetsam block
# below it is never the one read.
launchd_lifetime() { # job
  launchctl print "$DOMAIN/$(label_for "$1")" 2>/dev/null | awk '
    /resource coalition/ { in_resource = 1; next }
    in_resource && /ID = / { sub(/.*ID = */, ""); sub(/[^0-9].*/, ""); print; exit }'
}

# The boot the coalition id was counted in. Neither token is an identity by
# itself: boot time does not move when one label is reloaded by hand, and the
# coalition id repeats across boots because it counts from near zero at each
# of them. Together they do -- a reload changes the id, a reboot changes both
# -- so the record holds the pair and a match needs both.
boot_token() {
  # `[^u]sec`, because the line carries `usec` as well and a greedy `.*`
  # otherwise walks past `sec` to it and reads the microseconds.
  sysctl -n kern.boottime 2>/dev/null | sed -n 's/.*[^u]sec = *\([0-9][0-9]*\).*/\1/p'
}

# Has launchd bootstrapped this label and not yet spawned it? launchd reports
# that state explicitly, and this is the eleventh round of the #486 review:
# `(never exited)` is not a verdict, it is launchd saying it has no verdict,
# and a job it has never run owes no outcome yet. Reported as a failure, a
# newly installed job was a finding every night until its first slot -- weeks
# for a monthly job -- with nothing anyone could act on.
#
# Both readings of the one state are required. `runs = 0` and
# `last exit code = (never exited)` were measured together on this machine, on
# a probe label bootstrapped and never kickstarted, and again after `bootout`
# plus `bootstrap` reset a label that had already exited 7. Requiring both
# keeps this away from the case it must not touch: a label launchd does not
# hold at all prints neither field, and an installed job whose plist is not
# loaded stays reported, which is round 3's rule and health-check's own
# separate finding.
launchd_never_spawned() { # job
  [ "$(launchd_runs "$1")" = "0" ] || return 1
  case "$(launchd_last_exit "$1")" in
    '('*) return 0 ;;
  esac
  return 1
}

# Is launchd running this job at this moment? `state = running` is that field;
# `job state = running|exited` a few lines further down is a different one, and
# the anchor separates them without a second pass -- `job ` is not whitespace,
# so the pattern cannot match the longer name.
launchd_is_running() { # job
  [ "$(launchctl print "$DOMAIN/$(label_for "$1")" 2>/dev/null \
       | grep -E '^[[:space:]]*state = ' | head -1 | sed 's/.*= *//')" = "running" ]
}

# The record a completed run writes beside its log: the outcome of that run,
# together with the identity of the run it was.
#
# The outcome is IN the record, and that is the ninth round of the #486
# review. Until it was, the record carried the run's identity and `status`
# re-derived the outcome by parsing the log text with an anchored pattern --
# two independent readings of one run, which disagree the moment the job's own
# output does not end in a newline. `JOB_COMMAND='printf error; exit 7'`
# writes `error[demo] ... FAILED rc=7`, the anchor does not match it, and the
# identity fields still advanced, so a stale `done` read as this run's
# outcome. A run knows its own exit code; it writes that down, and where
# launchd holds the label nothing parses log text to decide pass or fail any
# more. The log decides only at step 5 of `job_is_broken`'s ladder.
#
# Nothing is written at spawn, and that is the sixth round's correction read
# the other way round. A start is not a completion: recording one made a run
# that was killed afterwards indistinguishable from one that finished, and a
# probe SIGKILLed mid-run read `status_exit=0` beside a `done` from the run
# before it. The reader below stops comparing a job that is running instead,
# which needs no record to say so.
#
# `exit=` is always written; the three identity fields are written when
# launchd can supply them and left out when it cannot. That is the tenth
# round of the #486 review, and it follows from the ninth. Until it landed,
# an identity that could not be read aborted the write, which was right while
# the record's whole job was to name a launchd run -- and wrong the moment the
# record became the outcome itself. launchd holds no coalition for a label it
# has bootstrapped and not yet spawned, which is every label after a reboot
# or a `bootstrap`, so a run by hand in that window recorded NOTHING and the
# previous lifetime's record answered for it. Both directions bit: a manual
# failure after a recorded success read `exec_exit=7 status_exit=0`, and a
# manual success after a recorded failure read `exec_exit=0 status_exit=1`,
# so a hand-run recovery could not clear a finding until the next scheduled
# run. Every completed run has an outcome; only some have a launchd identity.
#
# The identity is all or nothing, because it is checked all or nothing: a
# record naming two of the three fields can supersede launchd's verdict no
# more than one naming none of them, and writing a part of it would only
# suggest otherwise.
#
# One `key=value` per line, because the fields are read back together and a
# fifth one added later must not change how these parse. A record with `exit=`
# and no identity is NOT a format of its own -- it is this format with fields
# absent, and every reader treats an absent field as absent rather than as a
# different file. A file written before `exit=` existed -- the bare number
# this began as, the `runs=`/`boot=` pair after it, the `runs=`/`lifetime=`
# pair after that, or the three-field record -- carries no outcome at all and
# reads as no record. That is the SILENT direction, not the reporting one: no
# record means no opinion, and launchd's own evidence decides.
#
# The record is written to a temp file and renamed over the old one, so a
# reader sees the previous record or this one, never a part of either (sd:1265).
# The attempt marker goes only once the rename succeeded: a run that could not
# record its outcome still owes one, and the marker withdraws the earlier
# record's claim instead of letting it answer for this run.
record_outcome() { # job, exit code
  local seen lifetime boot record
  record_stamp "$1" "$EXEC_STARTED" "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$2"
  seen="$(launchd_runs "$1")"
  lifetime="$(launchd_lifetime "$1")"
  boot="$(boot_token)"
  record="$LOG_DIR/.$1.runs"
  if [ -n "$seen" ] && [ -n "$lifetime" ] && [ -n "$boot" ]; then
    printf 'exit=%s\nruns=%s\nlifetime=%s\nboot=%s\n' "$2" "$seen" "$lifetime" "$boot" \
      > "$record.tmp" 2>/dev/null || return 0
  else
    printf 'exit=%s\n' "$2" > "$record.tmp" 2>/dev/null || return 0
  fi
  mv -f "$record.tmp" "$record" 2>/dev/null || return 0
  # The run owed an outcome and has now written one, so it owes nothing.
  rm -f "$LOG_DIR/.$1.attempt" 2>/dev/null || true
}

# An invocation that started and has not finished. Written before the command
# runs and removed by `record_outcome`, which every path that finishes calls,
# including the EXIT trap that catches a failure. What survives it is a run
# no trap could catch: SIGKILL, a power cut, a panic.
#
# This exists because launchd's identity cannot see a hand run. `runs` does
# not move for one, so two successive hand runs carry the same identity and
# the first one's record keeps proving it is "the run launchd last spawned"
# after a second one died without writing. A file the second run created and
# could not remove is the only evidence that there WAS a second run.
record_attempt() { # job
  boot_token > "$LOG_DIR/.$1.attempt" 2>/dev/null || true
}

# The run's times, for readers that want when a job last ran (sd:2210):
# launchd keeps no run time, and a log's write time is not one. `started=` is
# written when the run takes its lock, and `ended=` and `exit=` join it when
# the run records an outcome, so a start with no end is a run in progress or
# one no trap saw end. UTC, written to a temp file and renamed into place. A
# stamp that cannot be written costs the run nothing: `status` reads no time.
record_stamp() { # job, started, [ended, exit]
  local stamp="$LOG_DIR/.$1.stamp"
  {
    printf 'started=%s\n' "$2"
    [ -z "${3:-}" ] || printf 'ended=%s\nexit=%s\n' "$3" "$4"
  } > "$stamp.tmp" 2>/dev/null || return 0
  mv -f "$stamp.tmp" "$stamp" 2>/dev/null || return 0
}

# The marker carries the boot it was written in, and a marker from an earlier
# boot is debris rather than an unfinished run: the process that owed the
# outcome is gone and nothing will ever write it. It costs nothing to ignore,
# because a reboot moves `boot_token` and the record's own identity stops
# matching at the same moment.
attempt_is_dangling() { # job
  set -- "$(cat "$LOG_DIR/.$1.attempt" 2>/dev/null)"
  [ -n "$1" ] || return 1
  [ "$1" = "$(boot_token)" ] || return 1
  return 0
}

recorded_field() { # job, key
  sed -n "s/^$2=//p" "$LOG_DIR/.$1.runs" 2>/dev/null | tail -1
}

# The exit code of the run launchd last spawned, when the record proves the
# record IS that run. Prints nothing otherwise, and printing nothing is the
# whole point: it is the absence of evidence, and the caller turns that into
# silence rather than into a verdict of its own.
#
# The three identity fields are what "is that run" means, and each closes a
# hole the others leave. `runs` is launchd's own spawn counter, which it
# increments at spawn -- measured, not assumed: a probe agent kickstarted
# three times read `runs = 1`, `2`, `3` from inside its own three runs -- so a
# counter ahead of the recorded number is a run that recorded nothing. The
# test is equality, not "not behind": a counter BEHIND the record is a record
# from a lifetime the tokens failed to separate, and that is not this run
# either.
#
# A hand run is why the counting works both ways. `run` and `exec` from a
# shell never reach launchd, so they leave `runs` untouched and record the
# number the previous scheduled run already had. The re-run that fixed a job
# therefore records launchd's current count with its own exit code, which is
# sd:1201's whole complaint and the one thing that may retire launchd's
# failure.
#
# The coalition id changes on every bootstrap, including a reload inside one
# boot, which boot time cannot see. The boot token changes on every reboot,
# including the one that hands a fresh label a coalition id a pre-reboot
# record already names, which the id cannot see. Either alone leaves a class
# of stale success accepted; the pair leaves none.
#
# A lifetime launchd has spawned nothing in answers nothing. `runs = 0` is a
# label bootstrapped and not yet fired -- every label, for a while, after
# every reboot -- and launchd prints no coalition block and
# `last exit code = (never exited)` for it. There is no run here to be the
# record's, and no launchd verdict either.
current_run_exit() { # job
  local job="$1" now lifetime boot code
  now="$(launchd_runs "$job")"
  [ -n "$now" ] && [ "$now" != "0" ] || return 0
  [ "$now" = "$(recorded_field "$job" runs)" ] || return 0
  lifetime="$(launchd_lifetime "$job")"
  boot="$(boot_token)"
  [ -n "$lifetime" ] && [ -n "$boot" ] || return 0
  [ "$lifetime" = "$(recorded_field "$job" lifetime)" ] || return 0
  [ "$boot" = "$(recorded_field "$job" boot)" ] || return 0
  code="$(recorded_field "$job" exit)"
  [ -n "$code" ] || return 0
  printf '%s\n' "$code"
}

# Is this job broken? Prints the evidence for the answer it gives.
#
# launchd's failure stands by default, and this is the ninth round of the #486
# review turning the question the right way up. Every round before it asked
# when launchd's non-zero exit could be SUPPRESSED, and hardened the evidence
# that fed the suppression; each hardening closed the hole in front of it and
# opened the next one, because absence of recovery evidence was still being
# read as recovery. A failure is retired only by positive, self-contained
# evidence that a later run succeeded: a record that says which run it is and
# what that run exited with. Anything else -- no record, a record in an
# earlier format, a record from another lifetime, a fresh lifetime, a label
# launchd does not hold -- is silence, and silence neither suppresses a
# failure nor invents one.
#
# The ladder, in order:
#   1. the record, when it proves it is launchd's last spawned run;
#   2. the record on its own, when the outcome it holds is a failure;
#   3. launchd's own `last exit code`;
#   4. the record on its own, when launchd holds no verdict;
#   5. the job's own log, when there is nothing else at all.
#
# Steps 2 and 4 are the same record read under the two halves of one rule,
# and the asymmetry between them is the point. A recorded FAILURE needs no
# identity: the record holds the last completed run there was, a later
# completed run would have replaced the file, and reporting is the safe
# direction anyway. A recorded SUCCESS needs identity before it may retire
# launchd's failure, because "later than launchd's last spawn" is exactly
# what the three fields prove and nothing else does. Without them a success
# waits behind launchd's verdict at step 3 and is only reached at step 4,
# where launchd has no verdict to contradict it.
#
# A recorded success is also withdrawn by a run that started after it and
# recorded nothing -- steps 1 and 4 both ask `attempt_is_dangling` before they
# let a success retire anything. The withdrawal is not a failure of its own:
# it takes the success out of the ladder and leaves launchd's evidence to
# decide, which is a verdict of broken only where launchd already held one.
#
# The log reaches the verdict only at step 5, where the label is not loaded
# and no run has ever recorded one. Everywhere launchd holds the label -- which
# is every installed cron job -- pass and fail come from a recorded exit code,
# never from a pattern matched against job output.
job_is_broken() { # job
  # No `local`: this file is POSIX sh and every `local` in it is a shellcheck
  # SC3043 already. $1 stays the job and $2 carries whatever the step above
  # produced.
  set -- "$1" "$(current_run_exit "$1")"
  if [ -n "$2" ] && [ "$2" != "0" ]; then
    echo "  the run launchd last spawned recorded exit $2"
    return 0
  fi
  if [ -n "$2" ] && ! attempt_is_dangling "$1"; then
    return 1
  fi
  # A recorded failure stands on its own. The run that failed wrote it, and
  # no identity is needed to report what nothing has superseded.
  set -- "$1" "$(recorded_field "$1" exit)"
  if [ -n "$2" ] && [ "$2" != "0" ]; then
    echo "  this job's last completed run recorded exit $2"
    return 0
  fi
  if launchd_failed "$1"; then
    echo "  launchd's last run of this job exited $(launchd_last_exit "$1")," \
         "and no completed run has recorded a success for it since: either a" \
         "run wrote nothing, or the label was reloaded, or the machine" \
         "rebooted, or no run has recorded an outcome yet"
    return 0
  fi
  # launchd reports no failure. A plain `0` is a run that exited cleanly and
  # is a verdict, so it ends the ladder here.
  [ "$(launchd_last_exit "$1")" != "0" ] || return 1
  # No verdict from launchd: it does not hold the label, or it has spawned
  # nothing in this lifetime. The last completed run stands, and by here it
  # can only be a success -- step 2 took every failure.
  if [ -n "$(recorded_field "$1" exit)" ] && ! attempt_is_dangling "$1"; then
    return 1
  fi
  # Nothing has ever recorded an outcome for this job. The log is the only
  # record there is.
  case "$(last_outcome "$1")" in
    done) return 1 ;;
    FAILED)
      echo "  this job's log ends in a failed run and nothing has recorded a" \
           "success since"
      return 0 ;;
    *)
      # No outcome anywhere: no record, no log, no launchd verdict. That is
      # unknown, and unknown is reported -- 0 here retires launchd's counter
      # in local-health-check, so a 0 with nothing behind it retires the only
      # evidence left (round 3). The one exception is launchd saying, itself,
      # that it has not run this job yet: then there is no outcome BECAUSE
      # there has been no run, which is pending rather than unknown.
      if launchd_never_spawned "$1"; then
        echo "  installed and not spawned yet; launchd has run this job no" \
             "times and nothing has recorded an outcome for it"
        return 1
      fi
      echo "  no run recorded in this job's log, and launchd holds no verdict" \
           "for it either"
      return 0 ;;
  esac
}

cmd_exec() { # invoked by launchd (and by `run`)
  local job="$1"

  # The lock, the log and the trap come before `load_job`, not after it.
  # `load_job` exits when the definition is unreadable or gone, and a plist
  # outlives its `.job` file easily -- delete the definition and launchd keeps
  # firing the label. Loading first meant that run wrote nothing at all: no
  # outcome in the log, and `status` never names the job, because the sweep
  # enumerates definitions. Everything below needs only $job and $LOG_DIR,
  # both of which are known without the definition.
  #
  # No overlapping runs of the same job.
  # Never deleted: a file unlinked while held lets the next run lock a new
  # inode beside it. Perl failing to run at all must not read as "held", or
  # every run would skip itself quietly.
  local lock="$LOG_DIR/.$job.flock" taken=0
  exec 8>>"$lock" || {
    echo "[$job] cannot open the lock file $lock" >&2
    exit 1
  }
  perl -e "$LOCK_TAKE" || taken=$?
  if [ "$taken" -eq 1 ]; then
    echo "[$job] $(date '+%Y-%m-%dT%H:%M:%S%z') skipped: previous run still active" >&2
    exit 0
  elif [ "$taken" -ne 0 ]; then
    echo "[$job] could not take the lock on $lock (perl exit $taken)" >&2
    exit 1
  fi
  trap exec_exit EXIT

  # Mirror a hand-run into the job log; no-op when launchd already points
  # stdout there.
  local report_offset=0 report_started report_run
  [ ! -f "$LOG_DIR/$job.log" ] || report_offset="$(wc -c < "$LOG_DIR/$job.log" | tr -d ' ')"
  report_started="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  report_run="$(date -u '+%Y%m%dT%H%M%SZ')-$$"
  start_tee "$LOG_DIR/$job.log"
  EXEC_JOB="$job"
  EXEC_STARTED="$report_started"
  # From here the run owes an outcome, and the trap above writes one for every
  # exit it can see. The marker is what a run that dies unseen leaves behind.
  record_attempt "$job"
  record_stamp "$job" "$EXEC_STARTED"

  load_job "$job"

  # launchd starts jobs from launchd, not from a login shell, so no rc file is
  # read and the plist's PATH and HOME are the entire environment. Every
  # credential lived in ~/.bash_profile, which meant a job — and the Claude it
  # spawns — saw none of them: the MCP servers that authenticate by ${VAR}
  # resolved to empty. The exports now live in their own file that both a login
  # shell and this runner source, so the two cannot drift.
  # Not fatal when absent: a machine that has not split its profile yet still
  # runs its jobs exactly as before.
  if [ -f "$HOME/.config/shell/env.sh" ]; then
    # shellcheck source=/dev/null
    . "$HOME/.config/shell/env.sh"
  fi

  # launchd hands a job 256 open files (`launchctl limit maxfiles`), where a
  # login shell gets 1048576. Claude needs more than 256 and aborts at startup
  # with "possibly due to low max file descriptors", so every prompt-driven job
  # failed overnight while the same job passed by hand. The hard limit is
  # unlimited, so raising the soft one needs no privilege and no sudo
  # `launchctl limit` change; -S is required, since a bare `ulimit -n` lowers
  # the hard limit too and that direction is one-way.
  ulimit -S -n 65536 2>/dev/null || true

  cd "${JOB_DIR:-$HOME}" || {
    echo "[$job] JOB_DIR is not a usable directory: ${JOB_DIR:-$HOME}" >&2
    exit 1
  }
  echo "[$job] $(date '+%Y-%m-%dT%H:%M:%S%z') starting (cwd: $PWD)"
  local rc=0 claude_bin debug_file="$LOG_DIR/$job.debug.$report_run.log"
  if [ -n "$JOB_COMMAND" ]; then
    run_bounded "$job" "$JOB_TIMEOUT_SECONDS" bash -c "$JOB_COMMAND" || rc=$?
  # Resolved here and not at the top of the script: a missing agent is this
  # job's failure, and it reaches the log, failures.log and the notification
  # like any other non-zero exit instead of killing the script before the
  # lock and the tee are in place.
  elif claude_bin="$(claude_binary)"; then
    # The binary and what it links to: ~/.local/bin/claude is a symlink into
    # ~/.local/share/claude/versions/ that the installer moves, so a version
    # flip between two runs is readable from the log (sd:972).
    echo "[$job] $(date '+%Y-%m-%dT%H:%M:%S%z') agent: $claude_bin -> $(readlink -f "$claude_bin" 2>/dev/null || echo "$claude_bin")"
    # SD_HANDOFF_RESTORE=0 for this one call: the pack's SessionStart hook
    # (sd-ai-command-pack bin/sd-handoff-restore, which reads it at ~:535)
    # restores a pending handoff packet into whatever session starts in that
    # repository, and an unattended `-p` job is not the session the packet
    # was written for -- a 3 a.m. run would consume it. The pack's SKILL.md
    # has said since the hook shipped that this script exports the variable;
    # until 2026-09-11 nothing here did. On this command and not `export`ed
    # for the whole run: a JOB_COMMAND job runs whatever it names, and what
    # that inherits is its own business. Through `env`, because an
    # assignment before a shell function such as run_bounded is not scoped
    # to that call in sh.
    # JOB_RESULT_OK: `claude -p` exits 0 whatever the prompt concluded, so a
    # job whose prompt ends its reply with a `RESULT: ` line can have that
    # line judged. The reply is kept in a file for the check and printed
    # after the agent exits, so such a job's log fills at the end, not live.
    local reply="$LOG_DIR/.$job.reply"
    # shellcheck disable=SC2086 # word-splitting of extra args is deliberate
    if [ -n "$JOB_RESULT_OK" ]; then
      run_bounded "$job" "$JOB_TIMEOUT_SECONDS" \
        env SD_HANDOFF_RESTORE=0 "$claude_bin" -p "$JOB_PROMPT" \
        --dangerously-skip-permissions \
        --debug-file "$debug_file" \
        ${JOB_MODEL:+--model "$JOB_MODEL"} \
        $JOB_CLAUDE_ARGS > "$reply" || rc=$?
      cat "$reply"
      if [ "$rc" -eq 0 ]; then
        local result
        result="$(grep '^RESULT: ' "$reply" | tail -n 1)" || true
        if [ -z "$result" ]; then
          echo "[$job] $(date '+%Y-%m-%dT%H:%M:%S%z') no RESULT: line in the reply"
          rc=1
        elif ! printf '%s\n' "$result" | grep -Eq -- "$JOB_RESULT_OK"; then
          echo "[$job] $(date '+%Y-%m-%dT%H:%M:%S%z') $result does not match JOB_RESULT_OK"
          rc=1
        fi
      fi
      rm -f "$reply"
    else
      run_bounded "$job" "$JOB_TIMEOUT_SECONDS" \
        env SD_HANDOFF_RESTORE=0 "$claude_bin" -p "$JOB_PROMPT" \
        --dangerously-skip-permissions \
        --debug-file "$debug_file" \
        ${JOB_MODEL:+--model "$JOB_MODEL"} \
        $JOB_CLAUDE_ARGS || rc=$?
    fi
    # Only a failure keeps its trace; the job log never quotes it.
    [ "$rc" -ne 0 ] || rm -f "$debug_file"
    prune_debug_files "$job"
  else
    rc=1
  fi

  if [ "$rc" -ne 0 ]; then
    echo "[$job] $(date '+%Y-%m-%dT%H:%M:%S%z') FAILED rc=$rc"
    notify_failure "$job" "$rc"
  else
    echo "[$job] $(date '+%Y-%m-%dT%H:%M:%S%z') done"
  fi
  # The exit code this run reached, written down by the run that reached it.
  # `status` reads it back rather than re-deriving the outcome from the text
  # above, which the two cannot disagree about any more.
  record_outcome "$job" "$rc"
  EXEC_JOB=""
  stop_tee
  record_run_report "$job" "$report_run" "$report_started" "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$rc" "$report_offset"
  [ "$rc" -eq 0 ] || exit "$rc"
}

cmd_install() {
  local job="$1"
  load_job "$job"
  mkdir -p "$AGENT_DIR"
  write_plist "$job"
  if is_loaded "$job"; then
    launchctl bootout "$DOMAIN/$(label_for "$job")" 2>/dev/null || true
    sleep 0.5
  fi
  launchctl bootstrap "$DOMAIN" "$(plist_for "$job")"
  launchctl enable "$DOMAIN/$(label_for "$job")"
  # No record is written here, and that is the ninth round of the #486 review.
  # `install` bootstraps the label, so launchd is at `runs = 0` and
  # `last exit code = (never exited)`: it holds no verdict, the stale record
  # names a lifetime that has ended, and neither says anything. Writing a
  # record here would have to claim an exit code for a run that has not
  # happened, which is exactly the fabricated positive evidence the inverted
  # default exists to refuse. Silence needs no repair.
  echo "installed: $job  (schedule: $JOB_SCHEDULE local, plist: $(plist_for "$job"))"
}

cmd_uninstall() {
  local job="$1"
  if is_loaded "$job"; then
    launchctl bootout "$DOMAIN/$(label_for "$job")" 2>/dev/null || true
  fi
  rm -f "$(plist_for "$job")"
  echo "uninstalled: $job"
}

cmd_list() {
  local job state
  printf "%-28s %-18s %-10s %-16s %s\n" "JOB" "SCHEDULE (local)" "INSTALLED" "FROM" "PROMPT"
  [ -n "$(all_jobs)" ] ||
    echo "cron-jobs.sh: no jobs in $JOBS_DIR or $HOST_JOBS_DIR; copy one from $ROOT/examples/ to start" >&2
  for job in $(all_jobs); do
    load_job "$job"
    if is_loaded "$job"; then state="loaded"; elif [ -f "$(plist_for "$job")" ]; then state="stale"; else state="no"; fi
    printf "%-28s %-18s %-10s %-16s %.60s\n" "$job" "$JOB_SCHEDULE" "$state" "$(job_origin "$job")" "${JOB_PROMPT:-$JOB_COMMAND}"
  done
}

# The last outcome this job's own log recorded, "done" or "FAILED", for any
# run — scheduled or by hand. Empty when the job has never written one.
#
# This is the answer launchd cannot give. `last exit code` counts the runs
# launchd spawned and nothing else, so a hand-run that fixed a job leaves it
# reading the failed scheduled run for ever: a weekly job failed at its 05:30
# slot on 2026-09-19, was re-run by hand at 11:32 and finished, and launchd
# still said "runs = 1, last exit code = 1" the next day while the log ended
# in "done" (sd:1201). Reading the log makes the exit code and the log agree,
# because the log is the one record both paths write.
last_outcome() { # job
  grep -oE "^\[$1\] [0-9T:+-]+ (done|FAILED rc=[0-9]+)" "$LOG_DIR/$1.log" 2>/dev/null \
    | tail -1 | awk '{print $3}'
}

# Exit codes are CLAUDE.md convention 6, and local-health-check reads them:
# 0 healthy, 3 nothing to check (no job in this list is installed here), 1 a
# job whose evidence says its last run failed and nothing has recorded a
# success since.
#
# launchd's failure is the default, and only a recorded success retires it;
# see `job_is_broken` for the ladder and the round it came from. An installed
# job that nothing has an opinion about -- no launchd verdict, no record, an
# empty log -- is not healthy, it is unknown, and 0 is the one answer it must
# not give: local-health-check drops launchd's counter for a <prefix>.cron.*
# label on a 0 here, so a 0 with nothing behind it retires the only evidence
# left (the #486 review, third round). An empty log is the state a job has
# after `install` and before its first slot, and also after someone truncates
# the log of a job that is failing. Those two are indistinguishable from here,
# so this reports the second and accepts the false positive on the first.
#
# A job launchd is running right now is named rather than judged. launchd
# counted that run when it spawned it and the run has not recorded an outcome
# yet, and a sweep asking about the job it is itself running -- which is what
# local-health-check does -- would otherwise read its own slot as a run nobody
# recorded.
#
# The verdict is the first line of stdout. local-health-check quotes that line
# as the finding text when this exits 1, and a per-job heading there read
# "local-cron-jobs: === <job> ===", naming no fault (sd:1387). So
# the per-job detail goes to a scratch file and prints after the verdict.
#
# An abandoned run is its own verdict, neither broken nor healthy (sd:1259).
# A run that started, recorded nothing and whose process is gone left no
# evidence the job fails -- `job_is_broken` rightly says so -- but hiding it
# made a killed hand run invisible whenever launchd's last scheduled exit was
# 0. It is named in the verdict line and the exit code stays 0: reporting it
# as broken is how 41 of 41 jobs once read as failing.
cmd_status() {
  local jobs="${1:-}" job installed=0 broken=0 broken_jobs="" abandoned_jobs="" body
  [ -n "$jobs" ] || jobs="$(all_jobs)"
  body="$(mktemp)"
  {
  for job in $jobs; do
    echo "=== $job ==="
    if [ -f "$(plist_for "$job")" ]; then
      installed=$((installed + 1))
      if launchd_is_running "$job"; then
        echo "  a run is in progress; launchd has counted it and it has" \
             "not recorded an outcome yet"
      # A live hand run first: it is the recovery from whatever the last run
      # recorded, and a failure verdict would hide it behind FAIL.
      elif attempt_is_dangling "$job" && lock_is_live "$LOG_DIR/.$job.flock"; then
        echo "  a hand run is in progress; it has not recorded an outcome yet"
      elif job_is_broken "$job"; then
        broken=$((broken + 1))
        broken_jobs="$broken_jobs $job"
      elif attempt_is_dangling "$job"; then
        echo "  abandoned: a run started, recorded no outcome, and its" \
             "process is gone (killed, or the machine lost power)"
        abandoned_jobs="$abandoned_jobs $job"
      fi
    fi
    if is_loaded "$job"; then
      # These are launchd's own counters, and they count only what launchd
      # spawned. A scheduled fire is counted: the plist runs this script's
      # `exec` verb, so launchd owns that process. A hand-run `run` or `exec`
      # executes in the caller's shell instead and never reaches launchd, so
      # it leaves `runs` untouched -- which read as "this job has never run"
      # to someone who had just run it by hand, and to an audit that concluded
      # a newly installed mail job was not sending. Label them rather than
      # keeping a second counter: a script-side tally would drift from both
      # launchd and the log, and the log below already records every run
      # through either path.
      echo "  --- launchd, scheduled runs only (a hand-run does not count) ---"
      launchctl print "$DOMAIN/$(label_for "$job")" 2>/dev/null \
        | grep -E "state =|last exit code =|runs =" | sed 's/^[[:space:]]*/  /'
    else
      echo "  not loaded"
    fi
    if [ -f "$LOG_DIR/$job.log" ]; then
      echo "  --- job log, every run (hand or scheduled) ---"
      tail -n 3 "$LOG_DIR/$job.log" | sed 's/^/  /'
    fi
  done
  if [ -s "$FAIL_LOG" ]; then
    echo "=== recent failures ==="
    local unresolved
    unresolved=$(tail -n 20 "$FAIL_LOG" | while read -r line; do
      failure_resolved "$(echo "$line" | awk '{print $2}')" \
                       "$(echo "$line" | awk '{print $1}')" || echo "$line"
    done)
    if [ -n "$unresolved" ]; then
      echo "$unresolved" | tail -5
    else
      echo "  none outstanding — $(grep -c . "$FAIL_LOG") logged, every one followed by a successful run"
    fi
  fi
  } > "$body"
  if [ "$broken" -gt 0 ]; then
    echo "local-cron-jobs: FAIL — $broken job(s) whose last run failed:$broken_jobs${abandoned_jobs:+; abandoned run(s):$abandoned_jobs}"
  elif [ "$installed" -eq 0 ]; then
    echo "local-cron-jobs: SKIP — no job in this list is installed here"
  else
    echo "local-cron-jobs: OK — $installed job(s) installed, none failing${abandoned_jobs:+; abandoned run(s):$abandoned_jobs}"
  fi
  cat "$body"
  rm -f "$body"
  [ "$installed" -gt 0 ] || return 3
  [ "$broken" -eq 0 ] || return 1
  return 0
}

# A failure the job has since recovered from is history, not a problem. Three
# jobs failed one morning here, all three were fixed within the hour, and
# every status for the rest of the day still led with them — which is how a
# real failure gets read as more of the same. Compares against the last
# "[job] <ts> done" the job's own log recorded.
#
# String comparison, not date arithmetic: the timestamps are ISO 8601 with the
# same machine's UTC offset on both lines, so lexicographic order is
# chronological order. It would not be across an offset change (a DST shift
# mid-day), and the cost of being wrong there is showing a resolved failure
# for one day — cheaper than parsing dates in POSIX sh.
failure_resolved() { # job, failure-timestamp
  local job="$1" ts="$2" last
  last=$(grep -oE "^\[$job\] [0-9T:+-]+ done" "$LOG_DIR/$job.log" 2>/dev/null \
    | tail -1 | awk '{print $2}')
  [ -n "$last" ] && [ "$last" \> "$ts" ]
}

cmd_logs() {
  local job="$1" lines="${2:-40}"
  tail -n "$lines" "$LOG_DIR/$job.log"
}

# Is the automation layer itself alive? For every job installed on this
# machine, compare its log age against the cadence implied by its schedule
# (weekday pinned = weekly, day-of-month pinned = monthly, else daily, each
# with slack). A job whose LaunchAgent silently stopped firing — machine
# asleep/off at its slot, plist unloaded, launchd wedged — never writes a
# failure, so the ntfy failure push stays quiet; this is the check that
# notices the silence. Emails findings; exits 1 only when the email failed.
cmd_watchdog() {
  local job dom dow max log age ref now stale
  now=$(date +%s)
  stale="$LOG_DIR/.watchdog-stale"
  : > "$stale"
  for job in $(all_jobs); do
    [ -f "$(plist_for "$job")" ] || continue  # not installed on this machine
    [ "$job" = "watchdog-daily" ] && continue # its own log is this run
    load_job "$job"
    set -f
    # shellcheck disable=SC2086
    set -- $JOB_SCHEDULE
    set +f
    dom="$3"; dow="$5"
    if [ "$dow" != "*" ]; then max=$((8 * 86400))
    elif [ "$dom" != "*" ]; then max=$((32 * 86400))
    else max=$((26 * 3600)); fi
    log="$LOG_DIR/$job.log"
    if [ -f "$log" ]; then
      age=$(( now - $(stat -f %m "$log") ))
      ref="last log activity"
    else
      # Never produced a log: measure from install time (plist mtime).
      age=$(( now - $(stat -f %m "$(plist_for "$job")") ))
      ref="never ran, installed"
    fi
    if [ "$age" -gt "$max" ]; then
      echo "- $job: $ref $(( age / 3600 ))h ago (cadence allows $(( max / 3600 ))h)" >> "$stale"
      echo "STALE   $job ($ref $(( age / 3600 ))h ago)"
    else
      echo "ok      $job"
    fi
  done
  if [ ! -s "$stale" ]; then
    echo "watchdog: all installed jobs alive"
    return 0
  fi
  local n subject body
  n=$(wc -l < "$stale" | tr -d ' ')
  subject="watchdog: $n cron job(s) not running on $(hostname -s)"
  body=$(printf 'cron watchdog — %s on %s\n\nThese installed jobs show no activity within their cadence:\n%s\n\nfix: cron-jobs.sh status <job>; if the plist is stale, cron-jobs.sh install <job>. If the machine was off/asleep at the slot, launchd coalesces on wake — a still-stale job here means it did not.\n' \
    "$(date '+%Y-%m-%d %H:%M')" "$(hostname -s)" "$(cat "$stale")")
  if ! sh "$ROOT/../local-notify/notify.sh" -t "$subject" -k status -F -c ntfy,email -b "$body"; then
    echo "watchdog email FAILED" >&2
    return 1
  fi
}

each_or_one() { # cmd, target
  local cmd="$1" target="$2" j
  if [ "$target" = "--all" ] || [ "$target" = "--every" ]; then
    # --all is every job this machine should run: the shared jobs folder plus
    # this host's folder (and any extra directory); other hosts' folders are
    # never read. --every is its older spelling, kept working.
    local rc=0
    for j in $(all_jobs); do "$cmd" "$j" || rc=1; done
    return $rc
  else
    "$cmd" "$target"
  fi
}

case "${1:-}" in
  list)      cmd_list ;;
  watchdog)  cmd_watchdog ;;
  install)   [ -n "${2:-}" ] || { echo "usage: cron-jobs.sh install <job>|--all" >&2; exit 1; }
             each_or_one cmd_install "$2" ;;
  verify)    each_or_one cmd_verify "${2:---all}" ;;
  uninstall) [ -n "${2:-}" ] || { echo "usage: cron-jobs.sh uninstall <job>|--all" >&2; exit 1; }
             each_or_one cmd_uninstall "$2" ;;
  run|exec)  [ -n "${2:-}" ] || { echo "usage: cron-jobs.sh $1 <job>" >&2; exit 1; }
             cmd_exec "$2" ;;
  status)    cmd_status "${2:-}" ;;
  logs)      [ -n "${2:-}" ] || { echo "usage: cron-jobs.sh logs <job> [lines]" >&2; exit 1; }
             cmd_logs "$2" "${3:-}" ;;
  test)      shift
             exec "${PYTHON:-python3}" -m unittest discover -s "$ROOT/tests" -t "$ROOT" "$@" ;;
  -h|--help|help)
    cat <<'HELPEOF'
usage: cron-jobs.sh list|install <job>|--all|verify [job]|uninstall <job>|--all|run <job>|status [job]|logs <job> [lines]|watchdog|test

  list         jobs, schedules, installed state, the folder each came from
  install      generate plist + load into launchd (<job>, or --all for
               every job in the job directories: shared plus this host's)
  verify       compare each installed plist against what the generator
               produces now (<job> or --all); exits 1 on any STALE. Read
               only — never touches launchd.
  uninstall    unload + remove plist (<job> or --all)
  run          run a job once now, foreground — output goes to the terminal
               and to the job's log, so a hand-run clears an outstanding
               failure the same way a scheduled run does
  status       launchd state, last exit, log tail, recent failures. Exits 0
               healthy, 3 when no job asked about is installed here, 1 when
               the evidence says a job's last run failed and nothing has
               recorded a success since.
               local-health-check reads these codes.
               launchd's `last exit code` stands by default, and only a
               recorded later success retires it. That counter moves when
               launchd spawns a run and never when a hand-run fixes a job,
               which is what sd:1201 is about -- so a completed run writes its
               own exit code beside the log, in `logs/.<job>.runs`:
               `exit=<code>`, always, plus
               `runs=<n>`/`lifetime=<coalition id>`/
               `boot=<kern.boottime sec>` when launchd can supply them.
               `exit=` is the outcome, written by the run that reached it;
               the log's last line decides only for a label that is not
               loaded and has no record. The other three
               say which run it was, and all three must match launchd before
               the record may supersede launchd's verdict: the counter, the
               coalition id, which changes on every bootstrap including a
               reload inside one boot, and the boot token, which separates the
               boots across which that id repeats. A record without them still
               reports a failure it holds, and still outranks the log; it just
               cannot retire launchd's.
               Anything else is silence, not a verdict: no record, a record in
               an earlier format, a record from another lifetime, a lifetime
               launchd has spawned nothing in, a label launchd does not hold.
               A job with no outcome anywhere is unknown and is reported,
               except where launchd itself reports `runs = 0` and
               `last exit code = (never exited)`: it has not run the job, so
               the job is pending rather than unknown.
               Silence suppresses no failure and invents none -- launchd's own
               exit decides, and where launchd holds no verdict either the
               last recorded outcome does, and failing that the log. A job
               launchd reports as running is named rather than judged, because
               its run has recorded no outcome yet.
               A run that started and recorded nothing withdraws a
               recorded success, because launchd's counter cannot see a hand
               run and two of them carry the same identity. `logs/.<job>.attempt`
               is written before the command runs and removed when an outcome
               is recorded; what survives it is a run no trap could see. It
               adds no failure of its own -- it leaves launchd's evidence to
               decide -- and a marker from an earlier boot is debris.
  logs         tail a job's log (default 40 lines)
  watchdog     flag installed jobs with no log activity within their cadence
               (daily/weekly/monthly from the schedule) and email the list
  exec         internal: what the LaunchAgent invokes
  test         run the unittest suite in tests/ (extra args go to unittest)

configuration: <config> is ${SYSTEM_TOOLS_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/system}
  <config>/cron-jobs/jobs/*.job  the shared job definitions, every machine
                                 (examples/ has samples)
  <config>/cron-jobs/jobs/<host>/*.job
                                 this machine's own jobs; one here overrides
                                 a same-named shared job. Other hosts'
                                 folders are ignored. `list` shows the FROM
                                 folder of each job.
  <config>/cron-jobs/.env        the variables below; an exported value wins
                                 (copy local-cron-jobs/.env.example)
  <config>/cron-jobs/notify.conf NTFY_TOPIC for a phone push on failure
                                 (copy local-cron-jobs/notify.conf.example)
  CRON_JOBS_EXTRA_DIRS       colon-separated extra job directories; a job
                             there overrides a same-named one in
                             <config>/cron-jobs/jobs
  CRON_JOBS_HOST             <host> above (default `hostname -s`), lower-cased
  SYSTEM_TOOLS_LABEL_PREFIX  launchd label prefix (default local.system-tools)
HELPEOF
    exit 0
    ;;
  *)
    echo "usage: $(basename "$0") list|install <job>|--all|uninstall <job>|--all|run <job>|status [job]|logs <job> [lines]|watchdog" >&2
    exit 1
    ;;
esac

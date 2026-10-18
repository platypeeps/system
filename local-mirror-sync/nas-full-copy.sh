#!/bin/sh
# Nightly additive copy of one disk (NAS_COPY_SRC) to a NAS share
# (NAS_COPY_DST), run by the cron job nas-full-copy at 23:00.
# Usage: nas-full-copy.sh run|help
#
# - Runs only between 23:00 and 05:00; a run launchd starts late after a
#   sleep outside that window skips. rsync is stopped at 05:00 and the next
#   night continues: files already copied are skipped.
# - Additive: no --delete, so nothing on the NAS is ever removed.
# - Refuses (exit 1, cron failure notification) when either volume is not
#   mounted, so a missing NAS never fills the system disk under /Volumes.
# - Files one brief, shown on the dashboard's Today, when a run completes
#   after an incomplete one: every source file then exists on the NAS with
#   the same size and mtime. Later complete runs stay quiet until a run is
#   incomplete again. A brief that cannot be filed prints an
#   SD_REPORT_ATTENTION line, so the run's report asks for a person.
set -u

DIR="$(cd "$(dirname "$0")" && pwd)"
. "$DIR/../lib/config.sh"

case "${1:-}" in
  run) ;;
  -h|--help|help)
    cat <<'HELPEOF'
usage: nas-full-copy.sh run|help

  run   copy NAS_COPY_SRC to NAS_COPY_DST with rsync (-rlt, no --delete) until
        05:00; exits 0 outside the 23:00-05:00 window, 1 when a volume is not
        mounted or rsync fails

environment (exported, or in <config>/mirror-sync/.env; see .env.example):
  NAS_COPY_SRC, NAS_COPY_DST  the disk to copy and the share to copy it to;
                              both are required
  NAS_COPY_LOG_DIR            per-file rsync logs (default
                              $NAS_COPY_SRC/repo-storage/system/nas-full-copy);
                              a folder inside the source is excluded from the copy
  NAS_COPY_TIMEOUT, NAS_COPY_RSYNC, NAS_COPY_HOUR, NAS_COPY_SECONDS,
  NAS_COPY_SKIP_MOUNT_CHECK   test hooks

state (the last run's outcome and the lock) is in
${XDG_STATE_HOME:-~/.local/state}/system/mirror-sync/nas-full-copy/.
HELPEOF
    exit 0 ;;
  *) echo "usage: nas-full-copy.sh run|help" >&2; exit 1 ;;
esac

# An exported value wins over the file (st_source_env's one rule).
st_source_env mirror-sync
SRC="${NAS_COPY_SRC:-}"
DST="${NAS_COPY_DST:-}"
for need in SRC DST; do
  eval "val=\$$need"
  [ -n "$val" ] || { st_missing "NAS_COPY_$need" mirror-sync .env local-mirror-sync; exit 1; }
done
STATE_DIR="${NAS_COPY_STATE:-$(st_state_dir mirror-sync)/nas-full-copy}"
# Per-file logs are large; they live on the source disk, excluded from the copy.
LOG_DIR="${NAS_COPY_LOG_DIR:-$SRC/repo-storage/system/nas-full-copy}"
case "$LOG_DIR" in
  "$SRC"/*) LOG_EXCLUDE="${LOG_DIR#"$SRC"}/" ;;
  *) LOG_EXCLUDE="" ;;
esac
END_HOUR=5
START_HOUR=23
KEEP_LOG_DAYS=14
TIMEOUT="${NAS_COPY_TIMEOUT:-/opt/homebrew/bin/timeout}"
RSYNC="${NAS_COPY_RSYNC:-/usr/bin/rsync}"

say() { echo "[nas-full-copy] $(date '+%Y-%m-%dT%H:%M:%S%z') $*"; }

# A completed copy is news, not an alarm: one brief in the workflow database,
# shown on the dashboard's Today (operator ruling 2026-10-17), not a push.
file_brief() { # text
  bdir=$(mktemp -d) || return 1
  brief="$bdir/$(date +%Y-%m-%d) - NAS Full Copy.md"
  printf -- '---\njob: nas-full-copy\n---\n\n%s\n' "$1" > "$brief" \
    && sd reports brief add --belongs-to "$(cd "$DIR/.." && pwd)" --replace "$brief"
  filed=$?
  rm -rf "$bdir"
  return "$filed"
}

mounted() { # path fstype
  [ -n "${NAS_COPY_SKIP_MOUNT_CHECK:-}" ] && return 0
  # index() matches the path literally; grep read "." and "[" in a volume name as pattern characters.
  mount | awk -v needle=" on $1 ($2," 'index($0, needle) { found = 1 } END { exit !found }'
}

hour=$(date +%H | sed 's/^0//'); hour="${NAS_COPY_HOUR:-$hour}"
if [ "$hour" -lt "$START_HOUR" ] && [ "$hour" -ge "$END_HOUR" ]; then
  say "skipped: ${hour}h is outside the ${START_HOUR}:00-0${END_HOUR}:00 window"
  exit 0
fi

mounted "$SRC" apfs || { say "refused: $SRC is not mounted (apfs)"; exit 1; }
mounted "$DST" smbfs || { say "refused: $DST is not mounted (smbfs)"; exit 1; }
[ -x "$TIMEOUT" ] || { say "refused: $TIMEOUT missing (brew install coreutils)"; exit 1; }

mkdir -p "$STATE_DIR" "$LOG_DIR"
lock="$STATE_DIR/lock"
if ! mkdir "$lock" 2>/dev/null; then
  pid=$(cat "$lock/pid" 2>/dev/null || echo "")
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    say "skipped: run $pid still active"; exit 0
  fi
  say "removing stale lock (pid ${pid:-unknown})"
  rm -rf "$lock"; mkdir "$lock" || exit 1
fi
echo $$ > "$lock/pid"
trap 'rm -rf "$lock"' EXIT
trap 'exit 143' INT TERM

# Seconds until the next END_HOUR:00.
if [ -z "${NAS_COPY_SECONDS:-}" ]; then
  if [ "$hour" -ge "$START_HOUR" ]; then day="-v+1d"; else day="-v+0d"; fi
  deadline=$(date -j "$day" -v"${END_HOUR}"H -v0M -v0S +%s)
  secs=$(( deadline - $(date +%s) ))
else
  secs="$NAS_COPY_SECONDS"
fi
[ "$secs" -gt 300 ] || { say "skipped: only ${secs}s left in the window"; exit 0; }

stamp=$(date +%Y-%m-%dT%H%M%S)
log="$LOG_DIR/$stamp.log"; err="$LOG_DIR/$stamp.err"
find "$LOG_DIR" -type f -name '*.log*' -mtime +"$KEEP_LOG_DAYS" -delete 2>/dev/null
find "$LOG_DIR" -type f -name '*.err' -mtime +"$KEEP_LOG_DAYS" -delete 2>/dev/null

say "copying $SRC/ -> $DST/ for up to $(( secs / 60 )) minutes; log $log"
# No -o/-g: neither the noowners source nor the SMB share keeps owners, and one
# failed fchownat stopped the whole 2026-09-28 run (openrsync aborts on any error).
# No -p: the SMB share keeps no modes, so -p re-set every file's mode each night
# (134k of 187k changes on 2026-10-10) and the window closed before new files.
caffeinate -i "$TIMEOUT" --kill-after=120 "$secs" "$RSYNC" -rlt --itemize-changes --stats \
  --exclude=/.Spotlight-V100/ --exclude=/.Trashes/ --exclude=/.fseventsd/ \
  --exclude=/.TemporaryItems/ --exclude=/.DocumentRevisions-V100/ \
  ${LOG_EXCLUDE:+"--exclude=$LOG_EXCLUDE"} \
  "$SRC/" "$DST/" > "$log" 2> "$err"
rc=$?
files=$(grep -c '^>f' "$log" 2>/dev/null || true)
last=$(cat "$STATE_DIR/state" 2>/dev/null || echo incomplete)

case $rc in
  0)
    echo synced > "$STATE_DIR/state"
    say "complete: $files files copied this run; every source file is on the NAS"
    if [ "$last" != synced ]; then
      file_brief "$SRC is fully copied to $DST ($files files in the last run)." \
        || echo "SD_REPORT_ATTENTION: nas-full-copy: the completed copy's brief was not filed"
    fi
    exit 0 ;;
  124|137)
    echo incomplete > "$STATE_DIR/state"
    say "window closed at 0${END_HOUR}:00: $files files copied; continues tomorrow night"
    exit 0 ;;
  *)
    echo incomplete > "$STATE_DIR/state"
    say "FAILED rc=$rc after $files files; last errors:"
    tail -5 "$err"
    exit 1 ;;
esac

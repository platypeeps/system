#!/bin/sh
# Nightly additive copy of one disk (NAS_COPY_SRC) to a NAS share
# (NAS_COPY_DST), run by the cron job nas-full-copy at 23:00.
# Usage: nas-full-copy.sh run|help
#
# - Runs only between 23:00 and 05:00; a run launchd starts late after a
#   sleep outside that window skips. rsync is stopped at 05:00 and the next
#   night continues after the last chunk copied in full.
# - Copies a sorted list of the source's names in chunks of NAS_COPY_CHUNK,
#   not one rsync -r from the top: rsync looks up every name on the share,
#   about 14 h for 2.29M names, so each night ran out near the same 30% of
#   the tree (sd:7088). A pass now spans nights; a cursor marks its place.
# - Additive: no --delete, so nothing on the NAS is ever removed.
# - Refuses (exit 1, cron failure notification) when either volume is not
#   mounted, so a missing NAS never fills the system disk under /Volumes.
# - Retries a failed rsync up to 3 times, 5 minutes apart, while more than
#   300 s of window are left and both volumes are still mounted: a passing
#   SMB stall used to end the night (sd:2268).
# - Files one brief, shown on the dashboard's Today, when a run completes
#   a pass after an incomplete one: every source file then exists on the NAS
#   with the size and mtime it had when its chunk ran. The next run starts a
#   new pass at the top. Later complete runs stay quiet until a run is
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

  run   copy NAS_COPY_SRC to NAS_COPY_DST with rsync (-lt, no --delete) in
        chunks of a sorted list of names until 05:00; the next run goes on
        after the last chunk copied; exits 0 outside the 23:00-05:00 window,
        1 when a volume is not mounted, the source cannot be listed, or rsync
        still fails after 3 retries 5 minutes apart

environment (exported, or in <config>/mirror-sync/.env; see .env.example):
  NAS_COPY_SRC, NAS_COPY_DST  the disk to copy and the share to copy it to;
                              both are required
  NAS_COPY_LOG_DIR            per-file rsync logs (default
                              $NAS_COPY_SRC/repo-storage/system/nas-full-copy);
                              a folder inside the source is excluded from the copy
  NAS_COPY_TIMEOUT, NAS_COPY_RSYNC, NAS_COPY_HOUR, NAS_COPY_SECONDS,
  NAS_COPY_SKIP_MOUNT_CHECK, NAS_COPY_RETRY_WAIT, NAS_COPY_CHUNK   test hooks

state (the last run's outcome, the pass's cursor and the lock) is in
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
  "$SRC"/*) LOG_EXCLUDE="${LOG_DIR#"$SRC"/}"; LOG_EXCLUDE="${LOG_EXCLUDE%/}" ;;
  *) LOG_EXCLUDE="" ;;
esac
# ponytail: 26 to 45 names a second went by on the share (2026-10-30 and
# 2026-11-01 logs), so a chunk takes 20 to 30 minutes: the 05:00 stop
# repeats at most that much the next night.
CHUNK="${NAS_COPY_CHUNK:-50000}"
END_HOUR=5
START_HOUR=23
KEEP_LOG_DAYS=14
# ponytail: 3 retries 5 minutes apart ride out a passing SMB stall; a share
# still failing after 15 minutes is down, and the cron failure says so.
RETRIES=3
RETRY_WAIT="${NAS_COPY_RETRY_WAIT:-300}"
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

# Every source name, one per line in byte order, without the volume's own
# folders. A newline in a name travels as \001, so each name stays one line;
# next_chunk's caller turns the chunk back into NUL-separated names for -0.
# A name that holds \001 would come back with a newline, so the same find
# leaves it, and a folder's contents with it, out of the list, and stderr
# counts them and shows the first three (sd:7136).
# Exits non-zero when find or sort fails: a short list is not a full pass.
list_source() ( # file
  LC_ALL=C; export LC_ALL
  a=$(printf '\001')
  (cd "$SRC" && find . -mindepth 1 \( -path ./.Spotlight-V100 -o -path ./.Trashes \
      -o -path ./.fseventsd -o -path ./.TemporaryItems -o -path ./.DocumentRevisions-V100 \) \
      -prune -o -name "*$a*" -prune -exec sh -c 'printf "%s\0" "$@" >&3' sh {} + \
      -o -print0) > "$1.find" 3> "$1.refused" \
    && tr '\n\0' '\001\n' < "$1.find" > "$1.lines" \
    && sort -o "$1" "$1.lines"
  listed=$?
  if [ -s "$1.refused" ]; then
    echo "left out $(tr -cd '\0' < "$1.refused" | wc -c | tr -d ' ') names that hold a 0x01 byte; the first:" >&2
    tr '\0\n' '\n\037' < "$1.refused" | head -n 3 | cat -v >&2
  fi
  rm -f "$1.find" "$1.lines" "$1.refused"
  exit "$listed"
)

# The next CHUNK names after the cursor, without the log folder. The cursor
# and the log folder go in through the environment: -v reads "\" as an
# escape. Concatenating "" makes awk compare names as strings, not numbers.
next_chunk() ( # list cursor out
  LC_ALL=C; export LC_ALL
  NAS_COPY_AFTER="$2" NAS_COPY_SKIP="$LOG_EXCLUDE" awk -v max="$CHUNK" '
    BEGIN { after = ENVIRON["NAS_COPY_AFTER"] ""; skip = ENVIRON["NAS_COPY_SKIP"] "" }
    { name = substr($0, 3) "" }
    skip != "" && (name == skip || index(name, skip "/") == 1) { next }
    name > after { print name; if (++n >= max) exit }' "$1" > "$3"
)

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

# The window ends at the next END_HOUR:00 (epoch seconds).
if [ -z "${NAS_COPY_SECONDS:-}" ]; then
  if [ "$hour" -ge "$START_HOUR" ]; then day="-v+1d"; else day="-v+0d"; fi
  deadline=$(date -j "$day" -v"${END_HOUR}"H -v0M -v0S +%s)
else
  deadline=$(( $(date +%s) + NAS_COPY_SECONDS ))
fi
secs=$(( deadline - $(date +%s) ))
[ "$secs" -gt 300 ] || { say "skipped: only ${secs}s left in the window"; exit 0; }

stamp=$(date +%Y-%m-%dT%H%M%S)
find "$LOG_DIR" -type f -name '*.log*' -mtime +"$KEEP_LOG_DAYS" -delete 2>/dev/null
find "$LOG_DIR" -type f -name '*.err' -mtime +"$KEEP_LOG_DAYS" -delete 2>/dev/null

# One list per attempt, each attempt with its own .log and .err, then one
# rsync per chunk after the cursor, until the list ends (rc 0), the window
# closes, or a chunk fails. A failed attempt is retried until the retries
# run out, too little window is left after the wait, or a volume is gone.
# The cursor moves only after a chunk ends with 0: a stop between the two
# copies that chunk again, which costs lookups and skips nothing.
attempt=0
files=0
names="$LOG_DIR/names"; chunk="$LOG_DIR/chunk"
while :; do
  suffix=""; [ "$attempt" -eq 0 ] || suffix="-retry$attempt"
  log="$LOG_DIR/$stamp$suffix.log"; err="$LOG_DIR/$stamp$suffix.err"
  say "copying $SRC/ -> $DST/ for up to $(( secs / 60 )) minutes; log $log"
  rc=0
  if ! list_source "$names" 2> "$err"; then
    rc=1; say "could not list $SRC"
  fi
  while [ "$rc" -eq 0 ]; do
    # A cursor that cannot be read starts the pass again: more lookups, no gap.
    cursor=$(cat "$STATE_DIR/cursor" 2>/dev/null) || cursor=""
    next_chunk "$names" "$cursor" "$chunk" 2>> "$err" || { rc=1; break; }
    [ -s "$chunk" ] || break
    secs=$(( deadline - $(date +%s) ))
    [ "$secs" -gt 0 ] || { rc=124; break; }
    tr '\n\001' '\0\n' < "$chunk" > "$chunk.0" || { rc=1; break; }
    # No -r: the list names every folder and file. No -o/-g: neither the
    # noowners source nor the SMB share keeps owners, and one failed fchownat
    # stopped the whole 2026-09-28 run (openrsync aborts on any error).
    # No -p: the SMB share keeps no modes, so -p re-set every file's mode each
    # night (134k of 187k changes on 2026-10-10) and the window closed before new files.
    caffeinate -i "$TIMEOUT" --kill-after=120 "$secs" "$RSYNC" -lt -0 --files-from="$chunk.0" \
      --itemize-changes --stats "$SRC/" "$DST/" >> "$log" 2>> "$err"
    rc=$?
    [ "$rc" -eq 0 ] || break
    tail -n 1 "$chunk" > "$STATE_DIR/cursor.new" && mv "$STATE_DIR/cursor.new" "$STATE_DIR/cursor" \
      || { rc=1; break; }
  done
  n=$(grep -c '^>f' "$log" 2>/dev/null) || :
  files=$(( files + ${n:-0} ))
  case $rc in 0|124|137) break ;; esac
  [ "$attempt" -lt "$RETRIES" ] || break
  [ $(( deadline - $(date +%s) - RETRY_WAIT )) -gt 300 ] || break
  attempt=$(( attempt + 1 ))
  say "rsync failed rc=$rc; retry $attempt of $RETRIES in ${RETRY_WAIT}s"
  # In the background, so a TERM from the job's limit ends the wait at once.
  sleep "$RETRY_WAIT" & wait $!
  mounted "$SRC" apfs || { say "refused: $SRC is not mounted (apfs)"; break; }
  mounted "$DST" smbfs || { say "refused: $DST is not mounted (smbfs)"; break; }
  secs=$(( deadline - $(date +%s) ))
done
last=$(cat "$STATE_DIR/state" 2>/dev/null || echo incomplete)

case $rc in
  0)
    # State first: a stop before the cursor goes finds an empty chunk and
    # completes again, quietly.
    echo synced > "$STATE_DIR/state"
    rm -f "$STATE_DIR/cursor"
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

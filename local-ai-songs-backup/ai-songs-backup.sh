#!/bin/sh
# Selectively mirror WAV, MP3 and MP4 files from a source folder into a backup.
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
. "$DIR/../lib/config.sh"

case "${1:-}" in
  sync|plan|list)
    MODE="$1"
    ;;
  test)
    shift
    exec "${PYTHON:-python3}" -m unittest discover -s "$DIR/tests" -t "$DIR" "$@"
    ;;
  -h|--help|help)
    cat <<'HELPEOF'
usage: ai-songs-backup.sh sync|plan|list|test

  list   show the source, destination, and matching-file count
  plan   show a dry run without changing the destination
  sync   create an exact selective mirror of WAV, MP3 and MP4 files
  test   run this folder's tests (extra args go to unittest)

AI_SONGS_SOURCE and AI_SONGS_DESTINATION come from the environment or from
$SYSTEM_TOOLS_CONFIG/ai-songs-backup/.env (see .env.example).

Relative paths below the source are preserved. Files in the destination that
are absent from the source, or are not WAV, MP3 or MP4 files, are deleted.
HELPEOF
    exit 0
    ;;
  *)
    echo "usage: $(basename "$0") sync|plan|list|test" >&2
    exit 1
    ;;
esac

st_source_env ai-songs-backup
for var in AI_SONGS_SOURCE AI_SONGS_DESTINATION; do
  eval "val=\${$var:-}"
  [ -n "$val" ] || { st_missing "$var" ai-songs-backup .env; exit 1; }
done
SOURCE="$AI_SONGS_SOURCE"
DESTINATION="$AI_SONGS_DESTINATION"

[ -d "$SOURCE" ] || {
  echo "ai-songs-backup.sh: source missing: $SOURCE" >&2
  exit 1
}
[ -d "$DESTINATION" ] || {
  echo "ai-songs-backup.sh: destination missing: $DESTINATION" >&2
  exit 1
}
[ "$SOURCE" != "$DESTINATION" ] || {
  echo "ai-songs-backup.sh: source equals destination" >&2
  exit 1
}
case "$DESTINATION/" in
  "$SOURCE/"*)
    echo "ai-songs-backup.sh: destination is inside source" >&2
    exit 1
    ;;
esac
case "$SOURCE/" in
  "$DESTINATION/"*)
    echo "ai-songs-backup.sh: source is inside destination" >&2
    exit 1
    ;;
esac

MATCH_COUNT=$(find "$SOURCE" -type f \
  \( -iname '*.wav' -o -iname '*.mp3' -o -iname '*.mp4' \) \
  -print | wc -l | tr -d ' ')
[ "$MATCH_COUNT" -gt 0 ] || {
  echo "ai-songs-backup.sh: no WAV, MP3 or MP4 files found; refusing to empty destination" >&2
  exit 1
}

if [ "$MODE" = "list" ]; then
  echo "$SOURCE  ->  $DESTINATION"
  echo "media files: $MATCH_COUNT"
  exit 0
fi

RSYNC_FLAGS="-a --delete --delete-excluded --prune-empty-dirs"
if [ "$MODE" = "plan" ]; then
  RSYNC_FLAGS="$RSYNC_FLAGS --dry-run -v"
fi

# shellcheck disable=SC2086
rsync $RSYNC_FLAGS \
  --include '*/' \
  --include '*.[mM][pP]3' \
  --include '*.[mM][pP]4' \
  --include '*.[wW][aA][vV]' \
  --exclude '*' \
  "$SOURCE/" "$DESTINATION/"

if [ "$MODE" = "plan" ]; then
  echo "plan complete: $MATCH_COUNT media file(s)"
else
  echo "backed up: $MATCH_COUNT media file(s)"
fi

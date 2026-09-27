#!/bin/sh
set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT INT TERM
# An empty config root, so a real .env on this machine cannot leak in.
SYSTEM_TOOLS_CONFIG="$WORK_DIR/config"
export SYSTEM_TOOLS_CONFIG

SOURCE="$WORK_DIR/source"
DESTINATION="$WORK_DIR/destination"
mkdir -p "$SOURCE/song-one/audio" "$SOURCE/song-one/video" "$SOURCE/notes"
mkdir -p "$DESTINATION/removed"
printf 'audio\n' > "$SOURCE/song-one/audio/song.mp3"
printf 'video\n' > "$SOURCE/song-one/video/song.MP4"
# Uppercase, because the WAV include is a new bracket expression: a
# lowercase fixture would pass whether or not the case folding is right.
printf 'master\n' > "$SOURCE/song-one/audio/master.WAV"
printf 'ignore\n' > "$SOURCE/notes/readme.txt"
printf 'delete\n' > "$DESTINATION/removed/old.mp3"
printf 'delete\n' > "$DESTINATION/unrelated.txt"

AI_SONGS_SOURCE="$SOURCE" AI_SONGS_DESTINATION="$DESTINATION" \
  sh "$SCRIPT_DIR/ai-songs-backup.sh" sync >/dev/null

cmp "$SOURCE/song-one/audio/song.mp3" \
  "$DESTINATION/song-one/audio/song.mp3"
cmp "$SOURCE/song-one/video/song.MP4" \
  "$DESTINATION/song-one/video/song.MP4"
cmp "$SOURCE/song-one/audio/master.WAV" \
  "$DESTINATION/song-one/audio/master.WAV"
test ! -e "$DESTINATION/notes/readme.txt"
test ! -e "$DESTINATION/removed/old.mp3"
test ! -e "$DESTINATION/unrelated.txt"

EMPTY_SOURCE="$WORK_DIR/empty-source"
mkdir -p "$EMPTY_SOURCE"
printf 'not media\n' > "$EMPTY_SOURCE/readme.txt"
if AI_SONGS_SOURCE="$EMPTY_SOURCE" AI_SONGS_DESTINATION="$DESTINATION" \
  sh "$SCRIPT_DIR/ai-songs-backup.sh" sync >/dev/null 2>&1; then
  echo "expected a source without media to be refused" >&2
  exit 1
fi
test -e "$DESTINATION/song-one/audio/song.mp3"

# A missing variable names itself and both remedies.
if ERR=$(AI_SONGS_DESTINATION="$DESTINATION" \
  sh "$SCRIPT_DIR/ai-songs-backup.sh" list 2>&1 >/dev/null); then
  echo "expected a missing AI_SONGS_SOURCE to fail" >&2
  exit 1
fi
case "$ERR" in
  *AI_SONGS_SOURCE*Export*.env.example*) ;;
  *) echo "unexpected error text: $ERR" >&2; exit 1 ;;
esac

echo "ok: selective AI Songs backup"

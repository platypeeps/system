#!/bin/sh
set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT INT TERM

mkdir -p "$WORK_DIR/directory-source" "$WORK_DIR/directory-destination"
mkdir -p "$WORK_DIR/file-destination"
printf 'directory source\n' > "$WORK_DIR/directory-source/keep.txt"
printf 'delete me\n' > "$WORK_DIR/directory-destination/extra.txt"
printf 'file source\n' > "$WORK_DIR/source.dmg"
printf 'leave me\n' > "$WORK_DIR/file-destination/unrelated.txt"

printf '%s|%s\n%s|%s\n' \
  "$WORK_DIR/directory-source" "$WORK_DIR/directory-destination" \
  "$WORK_DIR/source.dmg" "$WORK_DIR/file-destination" \
  > "$WORK_DIR/mirrors.conf"

MIRROR_SYNC_CONF="$WORK_DIR/mirrors.conf" sh "$SCRIPT_DIR/mirror-sync.sh" sync >/dev/null
cmp "$WORK_DIR/directory-source/keep.txt" \
  "$WORK_DIR/directory-destination/keep.txt"
test ! -e "$WORK_DIR/directory-destination/extra.txt"
cmp "$WORK_DIR/source.dmg" "$WORK_DIR/file-destination/source.dmg"
test -e "$WORK_DIR/file-destination/unrelated.txt"

: > "$WORK_DIR/empty.dmg"
printf '%s|%s\n' "$WORK_DIR/empty.dmg" "$WORK_DIR/file-destination" \
  > "$WORK_DIR/mirrors.conf"
if MIRROR_SYNC_CONF="$WORK_DIR/mirrors.conf" sh "$SCRIPT_DIR/mirror-sync.sh" plan >/dev/null 2>&1; then
  echo "expected empty file source to be refused" >&2
  exit 1
fi

echo "ok: directory and file mirrors"

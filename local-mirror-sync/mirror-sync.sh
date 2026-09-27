#!/bin/sh
# Mirror configured directories and files with rsync in archive mode.
# Directory destinations become exact copies, including deletions
# (rsync -a --delete). File sources are copied into destination directories.
# Pairs live in the list MIRROR_SYNC_CONF names, and default to
# mirrors.conf beside this script -- not beside the caller's cwd.
# Usage: mirror-sync.sh sync|plan|list
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
# One script, several schedules: MIRROR_SYNC_CONF names the pair list to
# run. The nightly job takes the default; the weekly repos mirror passes
# its own, so a 118G tree does not ride the nightly pass.
CONF="${MIRROR_SYNC_CONF:-$DIR/mirrors.conf}"

case "${1:-}" in
  sync|plan|list)
    MODE="$1"
    ;;
  -h|--help|help)
    cat <<'HELPEOF'
usage: mirror-sync.sh sync|plan|list

  list   print the configured source|destination pairs and their excludes
  plan   rsync dry run: show what sync would copy and DELETE, change nothing
  sync   mirror every pair (rsync -a --delete); exits 1 if any pair failed,
         having tried all the others — what the mirror-sync-nightly cron
         job runs

MIRROR_SYNC_CONF names the pair list to read. It defaults to mirrors.conf
beside this script, whatever the working directory, which is the list the
nightly job runs. The weekly repos mirror passes its own list.

The pair-list format, one pair per line ('|' separated, # comments ok):
  /path/to/source-directory|/path/to/destination-directory
  /path/to/source-file|/path/to/destination-directory
  /path/to/source-directory|/path/to/destination|node_modules/,target/

An optional THIRD field holds rsync exclude patterns for that pair alone,
comma separated. --delete-excluded is always on, so an excluded path is
never copied AND is removed from the destination if an earlier run put it
there. The patterns are per pair, so excluding build output from a repo
mirror leaves every other pair untouched.

Directory contents mirror into the destination. The destination becomes an
exact copy, so anything absent from the source is deleted. A file source is
copied into its existing destination directory using the source basename.

MIRROR_SYNC_ADDITIVE, if set, drops --delete and --delete-excluded: the
destination is added to and never trimmed, so a source deletion is not
propagated. MIRROR_SYNC_BACKUP_ROOT, if set, moves whatever rsync is about to
OVERWRITE into $MIRROR_SYNC_BACKUP_ROOT/<run>/<destination basename>/ first;
it must be disjoint from every source and destination (see below). Unset, both leave the mirror exactly as it always was.

With MIRROR_SYNC_BACKUP_ROOT set, each sync also prunes that store: a run
folder (a direct child named like 2026-09-26T043000) older than
MIRROR_SYNC_BACKUP_KEEP_DAYS days (default 14) is deleted and logged. Nothing
else under the root is touched: not other names, not symlinks, not the
current run. plan names what sync would prune.

The root must be disjoint from every source and destination. A root that is
not absolute, resolves to /, equals a source or destination, lies inside one,
or encloses one stops the pass before any pair runs, and nothing is deleted.
Links are followed to their end: a link to a file, a dangling link and a
chain of links. Overlap is then decided by device and inode, not by path
text, so "." and links, a case alias (/Backups for /backups) and a firmlink
do not hide one. A part that does not exist yet is judged by its deepest
existing folder plus its names, folded to lower case. A root, source or destination that cannot be resolved (a "." or
".." below a part that does not exist, more than 40 links, or an identity
stat cannot read) stops the pass too. The prune walks the root only when the root as written, trailing
slashes aside, is already its physical path; a root through a link or with a
"." component is skipped with a note, and the pass still succeeds. KEEP_DAYS
is decimal (08 is eight); a value that is not a whole number stops the pass
before any pair runs. A run folder whose name is not a real date and time
(2020-99-99T999999) is never pruned.

Safety rails before each pair runs:
  - source must exist and be non-empty (an empty directory would wipe the
    destination), and file destinations must already be directories
  - a directory destination's parent must exist
  - source and destination must not be the same path or nested in each other,
    by text and by identity (device and inode, as for the backup root)
HELPEOF
    exit 0
    ;;
  *)
    echo "usage: $(basename "$0") sync|plan|list" >&2
    exit 1
    ;;
esac

[ -f "$CONF" ] || { echo "mirror-sync.sh: missing $CONF (copy $(basename "$CONF").example beside it, or see help for the format)" >&2; exit 1; }

pairs() {
  sed -e 's/#.*//' -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' "$CONF" | grep -v '^$' || :
}

if [ "$MODE" = "list" ]; then
  n=0
  pairs | while IFS='|' read -r src dst excl; do
    if [ -n "${excl:-}" ]; then
      printf '%s  ->  %s  (excluding %s)\n' "$src" "$dst" "$excl"
    else
      printf '%s  ->  %s\n' "$src" "$dst"
    fi
  done
  n=$(pairs | grep -c . || :)
  echo "total: $n pair(s)"
  [ "$n" -eq 0 ] && echo "(edit $CONF to add source|destination lines)"
  exit 0
fi

# The replaced store grows by one run folder per pass that sets a backup root,
# and nothing else trims it. Keep MIRROR_SYNC_BACKUP_KEEP_DAYS days of runs.
# The age comes from the folder NAME, which is BACKUP_RUN; only a direct
# child with exactly that shape is a candidate, so a name this script did not
# write is never deleted, and an mtime a copy or a restore reset cannot make a
# fresh run look old. A symlink is skipped, never followed.
RUN_NAME='[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9][0-9][0-9][0-9][0-9]'
# The shape counts digits only; 2020-99-99T999999 has it too. Succeed only
# when the name $1, already of that shape, is a real date and time, so a
# stamp BACKUP_RUN could never have written is not a candidate.
run_name_is_real() {
  y="${1%%-*}"; r="${1#*-}"; mo="${r%%-*}"; r="${r#*-}"; d="${r%%T*}"; t="${r#*T}"
  hh="${t%????}"; ms="${t#??}"; mi="${ms%??}"; ss="${ms#??}"
  case "$mo" in 0[1-9]|1[0-2]) ;; *) return 1 ;; esac
  case "$d" in 0[1-9]|[12][0-9]|3[01]) ;; *) return 1 ;; esac
  case "$hh" in [01][0-9]|2[0-3]) ;; *) return 1 ;; esac
  case "$mi$ss" in [0-5][0-9][0-5][0-9]) ;; *) return 1 ;; esac
  case "$mo-$d" in 0[469]-31|11-31|02-3?) return 1 ;; 02-29) ;; *) return 0 ;; esac
  # 29 February: a leap year only. Strip the zeros, or 0400 reads as octal.
  while :; do case "$y" in 0?*) y="${y#0}" ;; *) break ;; esac; done
  [ $((y % 4)) -eq 0 ] && { [ $((y % 100)) -ne 0 ] || [ $((y % 400)) -eq 0 ]; }
}
# Containment is decided on PHYSICAL paths. A text prefix is not containment:
# "/a/./mirror/replaced" and "/a/link-to-mirror/replaced" both name a folder
# inside /a/mirror without starting with "/a/mirror/".
# Print the physical path of the existing directory $1, or nothing.
physical_dir() {
  (CDPATH='' cd -P -- "$1" 2>/dev/null && pwd -P) || :
}
# Print the physical path of $1, which need not exist yet: a destination or
# a backup root may appear only on the first pass. Resolve the deepest
# existing directory with cd -P, which follows every directory link on the
# way. The first component below it may still be a link that cd -P cannot
# enter: to a file, dangling, or in a loop. Follow it by hand and start over
# on its target, so a source linked to a file under the root is judged by the
# file. Past that component nothing exists, so nothing below it can be a link.
# A "." or ".." in that part cannot be resolved, and neither can a chain of
# more than MAX_LINKS links; print nothing and let the caller refuse.
MAX_LINKS=40
physical_path() {
  path="$1"; hops=0
  while :; do
    p="$path"; rest=""
    while [ ! -d "$p" ]; do
      case "$p" in /|'') break ;; esac
      rest="/$(basename "$p")$rest"; p=$(dirname "$p")
    done
    base=$(physical_dir "$p")
    [ -n "$base" ] || return 0
    base="${base%/}"
    first="${rest#/}"; first="${first%%/*}"
    [ -n "$first" ] && [ -L "$base/$first" ] || break
    hops=$((hops + 1))
    [ "$hops" -le "$MAX_LINKS" ] || return 0
    target=$(readlink "$base/$first") || return 0
    case "$target" in /*) ;; *) target="$base/$target" ;; esac
    path="$target${rest#/"$first"}"
  done
  case "$rest/" in */./*|*/../*) return 0 ;; esac
  # "/" with its slash stripped and nothing appended is empty, which callers
  # read as "does not resolve".
  resolved="$base$rest"
  printf '%s\n' "${resolved:-/}"
}
# Physical path TEXT does not identify a folder. A case-folding volume (APFS
# by default) takes /Backups for /backups, and a firmlink gives one folder
# two spellings (/private/var and /System/Volumes/Data/private/var); cd -P
# keeps whichever was typed. So overlap is decided by filesystem identity,
# device and inode, and text never makes two paths different.
# stat and date take different flags in each flavour, and each reads the
# other's differently: GNU stat reads -f as "file system" (with a file named
# like the format in the cwd, it succeeds and prints the wrong thing), and GNU
# date reads -r as a reference FILE (a file named like the epoch would set the
# cutoff). So ask once which flavour answers, by what it prints for a known
# input, and from then on hand each only its own flags. GNU is asked first:
# BSD stat has no -c and BSD date has no -d, so they fail and fall through,
# while the BSD question is never put to GNU. Neither answering leaves the
# flavour empty: an identity then cannot be read and the checks refuse, and
# the prune has no cutoff and says so.
STAT_FLAVOUR=""
DATE_FLAVOUR=""
probe_flavours() {
  case "$(stat -c '%d:%i' -- / 2>/dev/null || :)" in
    *[!0-9:]*|'') case "$(stat -f '%d:%i' -- / 2>/dev/null || :)" in
         *[!0-9:]*|'') ;;
         *) STAT_FLAVOUR=bsd ;;
       esac ;;
    *) STAT_FLAVOUR=gnu ;;
  esac
  if [ "$(date -d @0 +%s 2>/dev/null || :)" = 0 ]; then
    DATE_FLAVOUR=gnu
  elif [ "$(date -r 0 +%s 2>/dev/null || :)" = 0 ]; then
    DATE_FLAVOUR=bsd
  fi
}
# Print "<device>:<inode>" of the existing path $1, or fail.
file_id() {
  case "$STAT_FLAVOUR" in
    gnu) id=$(stat -c '%d:%i' -- "$1" 2>/dev/null) || return 1 ;;
    bsd) id=$(stat -f '%d:%i' -- "$1" 2>/dev/null) || return 1 ;;
    *) return 1 ;;
  esac
  case "$id" in *[!0-9:]*|:*|*:|*:*:*|'') return 1 ;; esac
  printf '%s\n' "$id"
}
# Print "<ids>|<rest>" for the physical path $1, as physical_path prints it:
# <ids> is the identity of "/" and of each existing component below it, in
# order; <rest> is the part that does not exist yet, folded to lower case.
# No component of $1 is a link, since physical_path followed them all. Print
# nothing when an identity cannot be read; the caller refuses.
identity_chain() {
  ids=$(file_id /) || return 0
  cur=""; rest=""
  set -f; IFS=/
  for comp in $1; do
    [ -n "$comp" ] || continue
    if [ -z "$rest" ] && { [ -e "$cur/$comp" ] || [ -L "$cur/$comp" ]; }; then
      cur="$cur/$comp"
      id=$(file_id "$cur") || return 0
      ids="$ids $id"
    else
      rest="$rest/$comp"
    fi
  done
  # A name that does not exist yet has no identity. Fold its case, so two
  # spellings that one volume would take as one folder are treated as one:
  # on a case-sensitive volume that only errs toward refusing.
  rest=$(printf '%s' "$rest" | tr '[:upper:]' '[:lower:]')
  printf '%s|%s\n' "$ids" "$rest"
}
# Succeed when the chain $1 encloses or equals the chain $2. A path that
# fully exists encloses whatever passes through its identity. One with a
# missing part encloses only a path whose deepest existing folder is the
# same one and whose missing part starts with the same names: the first
# missing name does not exist, so nothing that exists can lie under it.
chain_encloses() {
  ids_a="${1%%|*}"; rest_a="${1#*|}"
  ids_b="${2%%|*}"; rest_b="${2#*|}"
  last_a="${ids_a##* }"
  if [ -z "$rest_a" ]; then
    case " $ids_b " in *" $last_a "*) return 0 ;; esac
    return 1
  fi
  [ "${ids_b##* }" = "$last_a" ] || return 1
  case "$rest_b/" in "$rest_a/"*) return 0 ;; esac
  return 1
}
# Print one line for each configured source or destination that overlaps the
# root, whose identity chain is $1: the root equals it, lies inside it, or
# encloses it. A path that does not resolve, or whose identity cannot be
# read, is printed too; its text is not a stand-in. Inside a source, the
# pair copies the store into its own mirror; inside a destination, --delete
# eats it; enclosing either, the prune takes one named like a run for a run.
# A function and not an inline loop: macOS /bin/sh (bash 3.2) misparses a
# case inside $( ).
paths_overlapping_root() {
  pairs | while IFS='|' read -r src dst _excl; do
    for side in source destination; do
      if [ "$side" = source ]; then path="$src"; else path="$dst"; fi
      # Strip a trailing slash, but "/" stays "/": emptied, it would read as
      # no path at all and overlap nothing.
      case "$path" in ?*/) path="${path%/}" ;; esac
      [ -n "$path" ] || continue
      real=$(physical_path "$path")
      chain=""
      [ -z "$real" ] || chain=$(identity_chain "$real")
      if [ -z "$chain" ]; then
        echo "cannot be checked against the $side $path, which does not resolve"
        continue
      fi
      if chain_encloses "$chain" "$1"; then
        if chain_encloses "$1" "$chain"; then
          echo "is the $side $path"
        else
          echo "is inside the $side $path"
        fi
      elif chain_encloses "$1" "$chain"; then
        echo "encloses the $side $path"
      fi
    done
  done
}
# Settle MIRROR_SYNC_BACKUP_ROOT BEFORE any pair runs: rsync takes it as
# --backup-dir, so a root that resolves inside a destination would already
# hold that pass's overwrites by the time a later check saw it. A bad root
# stops the whole pass. Sets REAL_ROOT, its physical path, for the prune.
REAL_ROOT=""
check_backup_root() {
  root="${MIRROR_SYNC_BACKUP_ROOT:-}"
  [ -n "$root" ] || return 0
  case "$root" in
    /*) ;;
    *) echo "!!! refusing: MIRROR_SYNC_BACKUP_ROOT must be an absolute path: $root" >&2
       exit 1 ;;
  esac
  REAL_ROOT=$(physical_path "$root")
  root_chain=""
  [ -z "$REAL_ROOT" ] || root_chain=$(identity_chain "$REAL_ROOT")
  # "/" is the chain of one identity, that of "/", with nothing missing: no
  # space between identities and nothing after the "|".
  case "$root_chain" in *' '*|*'|'?*) ;; *) root_chain="" ;; esac
  if [ -z "$root_chain" ]; then
    echo "!!! refusing: MIRROR_SYNC_BACKUP_ROOT resolves to /, or does not resolve: $root" >&2
    exit 1
  fi
  overlaps=$(paths_overlapping_root "$root_chain")
  if [ -n "$overlaps" ]; then
    printf '%s\n' "$overlaps" | while IFS= read -r overlap; do
      echo "!!! refusing: MIRROR_SYNC_BACKUP_ROOT $overlap (root resolves to $REAL_ROOT)" >&2
    done
    exit 1
  fi
}
# Settle MIRROR_SYNC_BACKUP_KEEP_DAYS before any pair runs, like the root: a
# value the prune cannot use must not first let the pass fill a store it will
# never trim. Sets KEEP_DAYS, the decimal value, for the prune.
KEEP_DAYS=""
check_keep_days() {
  [ -n "${MIRROR_SYNC_BACKUP_ROOT:-}" ] || return 0
  KEEP_DAYS="${MIRROR_SYNC_BACKUP_KEEP_DAYS:-14}"
  case "$KEEP_DAYS" in
    ''|*[!0-9]*)
      echo "!!! refusing: MIRROR_SYNC_BACKUP_KEEP_DAYS must be a whole number of days, got '$KEEP_DAYS'" >&2
      exit 1 ;;
  esac
  # Shell arithmetic reads a leading zero as octal: 08 is an error and 010
  # is eight. Strip the zeros; all zeros is 0.
  while :; do case "$KEEP_DAYS" in 0?*) KEEP_DAYS="${KEEP_DAYS#0}" ;; *) break ;; esac; done
}
prune_replaced() {
  root="${MIRROR_SYNC_BACKUP_ROOT:-}"
  [ -n "$root" ] || return 0
  # No root yet means no runs to prune.
  [ -d "$REAL_ROOT" ] || return 0
  # Walk the root only when the root as written, trailing slashes aside, IS
  # its physical path, now and as check_backup_root resolved it. A link as the
  # root (`-L` misses "link/"), a link in any component, or a "." or ".."
  # component all fail that, so no spelling can steer the delete somewhere the
  # text does not say. The backup itself is unaffected; only the prune skips.
  while :; do case "$root" in ?*/) root="${root%/}" ;; *) break ;; esac; done
  if [ "$root" != "$(physical_dir "$root")" ] || [ "$root" != "$REAL_ROOT" ]; then
    echo "!!! not pruning: MIRROR_SYNC_BACKUP_ROOT is not its own physical path (link or dot component): $MIRROR_SYNC_BACKUP_ROOT -> $REAL_ROOT" >&2
    return 0
  fi
  # Names sort as numbers once the separators go: YYYYMMDDHHMMSS. The cutoff
  # is formatted the same way, with the flavour probe_flavours found.
  cutoff=""
  now=$(date +%s 2>/dev/null) || now=""
  case "$now" in *[!0-9]*|'') ;; *)
    cutoff_epoch=$((now - KEEP_DAYS * 86400))
    case "$DATE_FLAVOUR" in
      gnu) cutoff=$(date -d "@$cutoff_epoch" +%Y%m%d%H%M%S 2>/dev/null) || cutoff="" ;;
      bsd) cutoff=$(date -r "$cutoff_epoch" +%Y%m%d%H%M%S 2>/dev/null) || cutoff="" ;;
    esac ;;
  esac
  case "$cutoff" in
    [0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]) ;;
    *) echo "!!! not pruning: no date on PATH formats an epoch as GNU (-d @) or BSD (-r) date does" >&2
       echo "backup prune (no usable date)" >> "$FAILLOG"; return 0 ;;
  esac
  for run in "$REAL_ROOT"/*; do
    name="${run##*/}"
    # shellcheck disable=SC2254
    case "$name" in $RUN_NAME) ;; *) continue ;; esac
    run_name_is_real "$name" || continue
    [ "$name" != "$BACKUP_RUN" ] || continue
    [ ! -L "$run" ] || continue
    [ -d "$run" ] || continue
    stamp=$(printf '%s' "$name" | tr -d 'T-')
    [ "$stamp" -le "$cutoff" ] || continue
    if [ "$MODE" = "plan" ]; then
      echo "would prune replaced run: $run"
    elif rm -rf -- "$run"; then
      echo "pruned replaced run: $run"
    else
      echo "!!! could not prune: $run" >&2
      echo "$run (prune failed)" >> "$FAILLOG"
    fi
  done
  return 0
}

FAILLOG=$(mktemp)
# Per-pair excludes are handed to rsync through a file, never through argv:
# a pattern written on the command line would be word-split and glob-expanded
# by the shell before rsync ever saw it.
EXCLUDES=$(mktemp)
trap 'rm -f "$FAILLOG" "$EXCLUDES"' EXIT INT TERM

RSYNC_FLAGS="-a --delete --delete-excluded"
# MIRROR_SYNC_ADDITIVE drops both deletes, and with them the property that
# makes an exact mirror dangerous as a backup: it copies a destructive edit as
# faithfully as a useful one. `git reset --hard` deletes the work, the next
# pass deletes the copy, and the pass reports success -- the loss in sd:1107
# reproduced with the backup working perfectly. Additive, a deletion is never
# propagated and the copy outlives the mistake. The cost is that the
# destination keeps what the source dropped, including anything an exclude
# put there before; that is the right trade for the repo fleet's copy of
# uncommitted work, and the wrong one for the other lists, which stay exact.
if [ -n "${MIRROR_SYNC_ADDITIVE:-}" ]; then
  RSYNC_FLAGS="-a"
fi
# The other half of the same problem: an additive pass still OVERWRITES a file
# whose source changed, so the previous bytes are gone. MIRROR_SYNC_BACKUP_ROOT
# names a directory OUTSIDE every destination where rsync puts what it was
# about to replace, filed under the run that displaced it. Unset, nothing
# changes. Together with MIRROR_SYNC_ADDITIVE, neither a deletion nor an
# overwrite can take the last copy of something with it.
#
# The directory is named for the RUN and not the day because this machine's
# rsync is openrsync ("rsync version 2.6.9 compatible"), whose backup path
# fails with "mk_backup_dir: File exists" against a directory that is already
# there. A pass that displaced nothing leaves an empty one behind.
BACKUP_RUN="$(date +%Y-%m-%dT%H%M%S)"
# The source is read-only: rsync writes only to the destination, and
# --delete/--delete-excluded act on the destination alone. The only
# flags that would ever touch the source are the --*-source-files pair,
# so refuse outright if one is ever added here. This is a guarantee, not
# a preference: these pairs mirror live data whose only other copy may be
# the source itself.
case "$RSYNC_FLAGS" in
  *--remove-source-files*|*--delete-source-files*)
    echo "!!! refusing: RSYNC_FLAGS would modify the SOURCE" >&2; exit 1 ;;
esac
[ "$MODE" = "plan" ] && RSYNC_FLAGS="$RSYNC_FLAGS --dry-run -v"
# Finder metadata is noise on both sides: never copied, and (via
# --delete-excluded) scrubbed from destinations. ICON_CR is the classic
# custom-folder-icon file, literally named "Icon<carriage return>".
# .tmp.drivedownload is Google Drive's download staging directory: it appears
# and disappears as Drive syncs, and its contents are transient symlinks into
# .shortcut-targets-by-id whose numeric names are regenerated each time. Left
# alone it churns every run -- copied when Drive happens to be mid-sync,
# deleted when not.
ICON_CR="$(printf 'Icon\015')"

probe_flavours
check_backup_root
check_keep_days

pairs | while IFS='|' read -r src dst excl; do
  # Normalize: strip any trailing slash, then mirror CONTENTS (src/ -> dst/).
  src="${src%/}"; dst="${dst%/}"
  echo "=== $src -> $dst"
  # Where this pair's replaced and deleted files go, if versioning is on. The
  # directory is named for the destination, under this run. check_backup_root
  # has already refused a root inside any destination, where --delete would
  # eat it on the next pass.
  KEEP=""
  if [ -n "${MIRROR_SYNC_BACKUP_ROOT:-}" ]; then
    KEEP="$MIRROR_SYNC_BACKUP_ROOT/$BACKUP_RUN/$(basename "$dst")"
  fi
  # The optional third field: comma-separated rsync patterns for THIS pair.
  # Written to a file, one per line, so a pattern reaches rsync intact. The
  # file is rewritten per pair, so one pair's excludes never leak into the
  # next. Empty for a two-field line, and an empty --exclude-from is a no-op,
  # which is why it can be passed unconditionally below.
  : > "$EXCLUDES"
  if [ -n "${excl:-}" ]; then
    printf '%s\n' "$excl" | tr ',' '\n' \
      | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' \
      | grep -v '^$' >> "$EXCLUDES" || :
  fi
  if [ -z "$src" ] || [ -z "$dst" ]; then
    echo "!!! malformed line (want source|destination)" >&2
    echo "malformed line" >> "$FAILLOG"; continue
  fi
  if [ ! -e "$src" ]; then
    echo "!!! source missing: $src" >&2
    echo "$src (missing source)" >> "$FAILLOG"; continue
  fi
  if [ "$src" = "$dst" ]; then
    echo "!!! source equals destination" >&2
    echo "$src (same path)" >> "$FAILLOG"; continue
  fi
  case "$dst/" in "$src/"*) echo "!!! destination inside source" >&2
    echo "$dst (nested)" >> "$FAILLOG"; continue ;; esac
  case "$src/" in "$dst/"*) echo "!!! source inside destination" >&2
    echo "$src (nested)" >> "$FAILLOG"; continue ;; esac
  # Text is the first rail, not the last: a link, a case alias or a firmlink
  # nests two paths whose spellings do not. Compare identity chains too, as
  # check_backup_root does; a side whose identity cannot be read is refused.
  src_chain=""; dst_chain=""
  real=$(physical_path "$src"); [ -z "$real" ] || src_chain=$(identity_chain "$real")
  real=$(physical_path "$dst"); [ -z "$real" ] || dst_chain=$(identity_chain "$real")
  if [ -z "$src_chain" ] || [ -z "$dst_chain" ]; then
    echo "!!! cannot compare source and destination by identity: $src -> $dst" >&2
    echo "$src (identity unreadable)" >> "$FAILLOG"; continue
  fi
  if chain_encloses "$src_chain" "$dst_chain"; then
    echo "!!! destination is or lies inside source, by identity: $dst" >&2
    echo "$dst (nested by identity)" >> "$FAILLOG"; continue
  fi
  if chain_encloses "$dst_chain" "$src_chain"; then
    echo "!!! source lies inside destination, by identity: $src" >&2
    echo "$src (nested by identity)" >> "$FAILLOG"; continue
  fi
  if [ -d "$src" ]; then
    if [ -z "$(ls -A "$src" 2>/dev/null)" ]; then
      echo "!!! source empty: $src — refusing, a sync would wipe $dst" >&2
      echo "$src (empty source)" >> "$FAILLOG"; continue
    fi
    if [ ! -d "$(dirname "$dst")" ]; then
      echo "!!! destination parent missing: $(dirname "$dst")" >&2
      echo "$dst (no parent)" >> "$FAILLOG"; continue
    fi
    src_arg="$src/"
  elif [ -f "$src" ]; then
    if [ ! -s "$src" ]; then
      echo "!!! source file empty: $src" >&2
      echo "$src (empty source file)" >> "$FAILLOG"; continue
    fi
    if [ ! -d "$dst" ]; then
      echo "!!! file destination is not a directory: $dst" >&2
      echo "$dst (file destination missing)" >> "$FAILLOG"; continue
    fi
    src_arg="$src"
  else
    echo "!!! unsupported source type: $src" >&2
    echo "$src (unsupported source type)" >> "$FAILLOG"; continue
  fi
  if [ -n "$KEEP" ]; then
    set -- --backup --backup-dir "$KEEP"
  else
    set --
  fi
  # shellcheck disable=SC2086
  if ! rsync $RSYNC_FLAGS "$@" --exclude .DS_Store --exclude '._*' \
       --exclude "$ICON_CR" --exclude .tmp.drivedownload \
       --exclude-from "$EXCLUDES" "$src_arg" "$dst/"; then
    echo "!!! rsync failed: $src" >&2
    echo "$src (rsync error)" >> "$FAILLOG"
  fi
done

prune_replaced

echo "----------------------------------------"
if [ -s "$FAILLOG" ]; then
  echo "failed: $(wc -l < "$FAILLOG" | tr -d ' ') pair(s)"
  sed 's/^/  /' "$FAILLOG"
  exit 1
fi
n=$(pairs | grep -c . || :)
if [ "$n" -eq 0 ]; then
  echo "nothing configured — edit $CONF"
else
  echo "all $n pair(s) mirrored"
fi

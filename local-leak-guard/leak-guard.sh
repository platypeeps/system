#!/bin/sh
# Refuse a push whose new commits add a line that matches a private pattern.
#
# The patterns are extended regular expressions, one per line, in
# <config>/privacy-patterns (see lib/config.sh). They never enter the
# repository: the file names exactly what must stay out of it. Blank lines and
# lines starting with `#` are ignored. With no file, the guard warns and passes,
# so a machine without the file can still push.
#
# What is checked, per commit being pushed: every added line of its diff, the
# path of every file it touches, and its message. Author and committer fields
# are not checked; they carry the copyright holder's name by design.
#
# A hit prints the commit, the file and the kind of line, never the line
# itself: hook output ends up in terminals, logs and agent transcripts.
#
# Exit codes:
#   0  clean, or no pattern file
#   1  a pushed commit matches a pattern
#   2  usage error or git failure
set -eu

# local-bin-links may link this script into ~/bin; walk $0 to the real file so
# DIR is this folder and ../lib resolves.
SELF="$0"
while [ -L "$SELF" ]; do
  link="$(readlink "$SELF")"
  case "$link" in
    /*) SELF="$link" ;;
    *) SELF="$(dirname "$SELF")/$link" ;;
  esac
done
DIR="$(cd "$(dirname "$SELF")" && pwd)"
. "$DIR/../lib/config.sh"

ME="leak-guard"
PATTERNS="${LEAK_GUARD_PATTERNS:-$(st_config_file "$ME" privacy-patterns)}"
MARKER="# installed by local-leak-guard"
ZERO="0000000000000000000000000000000000000000"

usage() {
  cat <<'USAGE'
leak-guard.sh — refuse a push that adds a line matching a private pattern.

Usage:
  leak-guard.sh check [--range A..B]
  leak-guard.sh hook REMOTE [URL]      (git pre-push; refs on stdin)
  leak-guard.sh install [REPO]
  leak-guard.sh remove [REPO]
  leak-guard.sh test [-v]
  leak-guard.sh help

Verbs:
  check    Check the commits in A..B. Without --range, check the commits on
           HEAD that no remote-tracking ref has.
  hook     What the pre-push hook runs. Reads git's pre-push lines on stdin
           and checks each pushed ref's new commits.
  install  Write REPO's pre-push hook (default: the current repository). It
           honours core.hooksPath and refuses to replace a hook it did not
           write.
  remove   Delete the pre-push hook that install wrote.
  test     Run the unittest suite.

Patterns:
  <config>/privacy-patterns, one extended regular expression per line, as
  `grep -E -f` reads them. <config> is SYSTEM_TOOLS_CONFIG, default
  ${XDG_CONFIG_HOME:-$HOME/.config}/system. LEAK_GUARD_PATTERNS names another
  file. A missing file prints a warning and passes.

Exit codes: 0 clean, 1 a match, 2 usage or git error.
USAGE
}

die() {
  printf '%s: %s\n' "$ME" "$1" >&2
  exit 2
}

# Print the pattern file without blank or comment lines into $1. A blank line
# given to grep -f matches every line, so it must never reach grep. Returns 1
# when the file is missing or holds no pattern.
load_patterns() {
  if [ ! -f "$PATTERNS" ]; then
    printf '%s: warning: no pattern file at %s; push not checked.\n' "$ME" "$PATTERNS" >&2
    printf '  Create it with one extended regular expression per line.\n' >&2
    return 1
  fi
  grep -Ev '^[[:space:]]*(#|$)' "$PATTERNS" >"$1" || true
  if [ ! -s "$1" ]; then
    printf '%s: warning: %s holds no pattern; push not checked.\n' "$ME" "$PATTERNS" >&2
    return 1
  fi
}

# check_revs <rev-list args...>: check every commit those arguments select.
# Prints one line per hit to stderr and returns 1 on any hit.
check_revs() {
  work="$(mktemp -d "${TMPDIR:-/tmp}/leak-guard.XXXXXX")"
  # shellcheck disable=SC2064
  trap "rm -rf '$work'" EXIT
  if ! load_patterns "$work/patterns"; then
    return 0
  fi
  git rev-list "$@" >"$work/commits" || die "git rev-list $* failed"

  # Two parallel files: text holds what grep reads, one item per line; where
  # holds, on the same line number, what a hit reports instead of the text:
  # <sha> US <path> US <kind>, split on the unit separator (\037) because a
  # tab is IFS whitespace and two in a row would collapse an empty path.
  : >"$work/text"
  : >"$work/where"
  while read -r sha; do
    git log -1 --format=%B "$sha" |
      awk -v s="$sha" -v t="$work/text" -v w="$work/where" '
        { print >> t; print s "\037\037message" >> w }'
    git show --no-color --no-ext-diff --no-renames --format= -p "$sha" |
      awk -v s="$sha" -v t="$work/text" -v w="$work/where" '
        /^diff --git / { hdr = 1; path = ""; next }
        hdr && /^\+\+\+ / {
          path = substr($0, 5); sub(/^b\//, "", path)
          if (path == "/dev/null") path = ""
          else { print path >> t; print s "\037" path "\037path" >> w }
          next
        }
        /^@@ / { hdr = 0; next }
        !hdr && /^\+/ { print substr($0, 2) >> t; print s "\037" path "\037added line" >> w }'
  done <"$work/commits"

  grep -n -E -f "$work/patterns" "$work/text" >"$work/matches" || true
  [ -s "$work/matches" ] || return 0
  cut -d: -f1 "$work/matches" >"$work/hits"
  count="$(wc -l <"$work/hits" | tr -d ' ')"
  printf '%s: %s line(s) match a pattern in %s:\n' "$ME" "$count" "$PATTERNS" >&2
  awk 'NR == FNR { hit[$1] = 1; next } FNR in hit' "$work/hits" "$work/where" |
    sort -u >"$work/report"
  us="$(printf '\037')"
  while IFS="$us" read -r sha path kind; do
    # A path that matches is itself the leak; name the commit, not the path.
    if [ -n "$path" ] && printf '%s\n' "$path" | grep -qE -f "$work/patterns"; then
      path="(path withheld: it matches)"
    fi
    printf '  %s %s %s\n' "$sha" "$kind" "$path" >&2
  done <"$work/report"
  printf '%s: push refused. Rewrite those commits, or move the values to the config folder.\n' "$ME" >&2
  return 1
}

cmd_check() {
  range=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --range)
        [ $# -ge 2 ] || die "--range needs A..B"
        range="$2"
        shift 2
        ;;
      *) die "unknown option: $1" ;;
    esac
  done
  git rev-parse --git-dir >/dev/null 2>&1 || die "not a git repository"
  if [ -n "$range" ]; then
    check_revs "$range"
  else
    check_revs HEAD --not --remotes
  fi
}

# git runs pre-push with the remote name and URL as arguments and one line per
# ref on stdin: <local ref> <local sha> <remote ref> <remote sha>.
cmd_hook() {
  [ $# -ge 1 ] || die "hook needs the remote name"
  remote="$1"
  status=0
  while read -r _lref lsha _rref rsha; do
    [ -n "${lsha:-}" ] || continue
    # A deleted ref pushes no commit.
    [ "$lsha" = "$ZERO" ] && continue
    if [ "$rsha" != "$ZERO" ] && git cat-file -e "$rsha^{commit}" 2>/dev/null; then
      ( check_revs "$lsha" --not "$rsha" ) || status=1
    else
      # A new ref, or a remote tip this clone has never seen: check what no
      # ref of that remote already holds. On a first push that is everything.
      ( check_revs "$lsha" --not --remotes="$remote" ) || status=1
    fi
  done
  return "$status"
}

hooks_dir() {
  git -C "$1" rev-parse --git-path hooks 2>/dev/null ||
    die "not a git repository: $1"
}

cmd_install() {
  repo="${1:-.}"
  dir="$(hooks_dir "$repo")"
  case "$dir" in /*) ;; *) dir="$(cd "$repo" && pwd)/$dir" ;; esac
  hook="$dir/pre-push"
  if [ -f "$hook" ] && ! grep -qF "$MARKER" "$hook"; then
    die "$hook exists and was not written by $ME; add \`$DIR/leak-guard.sh hook \"\$@\"\` to it by hand"
  fi
  mkdir -p "$dir"
  cat >"$hook" <<EOF
#!/bin/sh
$MARKER
exec sh '$DIR/leak-guard.sh' hook "\$@"
EOF
  chmod +x "$hook"
  printf '%s: installed %s\n' "$ME" "$hook"
  [ -f "$PATTERNS" ] || printf '%s: warning: no pattern file at %s yet.\n' "$ME" "$PATTERNS" >&2
  return 0
}

cmd_remove() {
  repo="${1:-.}"
  dir="$(hooks_dir "$repo")"
  case "$dir" in /*) ;; *) dir="$(cd "$repo" && pwd)/$dir" ;; esac
  hook="$dir/pre-push"
  if [ ! -f "$hook" ]; then
    printf '%s: no pre-push hook in %s\n' "$ME" "$dir"
    return 0
  fi
  grep -qF "$MARKER" "$hook" || die "$hook was not written by $ME; left in place"
  rm -f "$hook"
  printf '%s: removed %s\n' "$ME" "$hook"
}

if [ $# -eq 0 ]; then
  usage >&2
  exit 1
fi

cmd="$1"
shift
case "$cmd" in
  check) cmd_check "$@" ;;
  hook) cmd_hook "$@" ;;
  install) cmd_install "$@" ;;
  remove) cmd_remove "$@" ;;
  test) exec "${PYTHON:-python3}" -m unittest discover -s "$DIR/tests" -t "$DIR" "$@" ;;
  -h|--help|help) usage; exit 0 ;;
  *) usage >&2; exit 1 ;;
esac

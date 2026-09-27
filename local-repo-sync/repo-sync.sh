#!/bin/sh
# Clone-or-pull the repo fleet listed in repos.<profile>.conf.
# Usage: repo-sync.sh sync|check|list|reconcile|nightly|test
set -e

# The repo convention is DIR="$(cd "$(dirname "$0")" && pwd)", but this script
# is invoked through the ~/bin/common/repo-sync symlink and reads config files
# sitting next to it, so $0 has to be walked to its real location first.
SELF="$0"
while [ -L "$SELF" ]; do
  link=$(readlink "$SELF")
  case "$link" in
    /*) SELF="$link" ;;
    *)  SELF="$(dirname "$SELF")/$link" ;;
  esac
done
DIR="$(cd "$(dirname "$SELF")" && pwd)"
. "$DIR/../lib/config.sh"

# The work checkout root. REPO_SYNC_WORK_ROOT and local-ai-apps'
# AI_APPS_WORK_ROOT mean the same thing; SYSTEM_TOOLS_WORK_ROOT sets both.
WORK_ROOT="${REPO_SYNC_WORK_ROOT:-${SYSTEM_TOOLS_WORK_ROOT:-}}"

# Machines carry different repo lists, so the profile picks the config file.
# machine-setup already records what this machine is, and that answer beats
# guessing: the directory heuristic below calls anything without a work
# checkout root "personal", which on a terra machine resolved a 68-repo
# fleet and made `nightly` reconcile 67 absent checkouts out of the tracked
# confs. The heuristic stays as the fallback for a machine that predates the
# state file: it calls the machine `work` when the work root names an
# existing directory, else `personal`.
MACHINE_SETUP_PROFILE_FILE="${MACHINE_SETUP_STATE:-$HOME/.config/machine-setup}/profile"
if [ -n "${REPO_SYNC_PROFILE:-}" ]; then
  PROFILE="$REPO_SYNC_PROFILE"
elif [ -r "$MACHINE_SETUP_PROFILE_FILE" ]; then
  PROFILE="$(cat "$MACHINE_SETUP_PROFILE_FILE")"
elif [ -n "$WORK_ROOT" ] && [ -d "$WORK_ROOT" ]; then
  PROFILE="work"
else
  PROFILE="personal"
fi

case "$PROFILE" in
  work)     DEFAULT_ROOT="${WORK_ROOT:-$HOME/repos}" ;;
  personal) DEFAULT_ROOT="$HOME/repos" ;;
  terra)  DEFAULT_ROOT="$HOME/repos" ;;
  *)
    echo "repo-sync.sh: unknown profile '$PROFILE' (want: personal, work, terra)" >&2
    exit 1
    ;;
esac

ROOT="${REPO_SYNC_ROOT:-$DEFAULT_ROOT}"
# The confs are private, per-machine lists, so they live in the shared config
# directory outside the checkout. REPO_SYNC_CONF_DIR points elsewhere; a
# private fork that tracks its confs sets it to this folder.
CONF_DIR="${REPO_SYNC_CONF_DIR:-$(st_config_dir repo-sync)}"
COMMON_CONF="$CONF_DIR/repos.common.conf"
PROFILE_CONF="$CONF_DIR/repos.$PROFILE.conf"

# Which conf files this profile reads, newline-separated (paths may contain
# spaces, so consumers iterate with IFS=$NL rather than plain word splitting).
# Every profile is common + profile, except terra: common carries the shared
# `ai` checkouts, and a terra machine holds nothing but `system`. See the
# header of repos.terra.conf.
NL='
'
if [ "$PROFILE" = terra ]; then
  CONF_FILES="$PROFILE_CONF"
else
  CONF_FILES="$COMMON_CONF$NL$PROFILE_CONF"
fi

# Every conf this profile reads is a commit scope, not just the profile one.
# `reconcile`'s remove_entry loops over $CONF_FILES, so a removal rewrites
# repos.common.conf as well. Committing the profile conf alone left the common
# one dirty after every removal, for good -- and a permanently dirty tracked
# file is where a real edit hides.
conf_scopes_phrase() {
  ( old=$IFS; out=""
    IFS=$NL
    for f in $CONF_FILES; do
      IFS=$old
      out="${out:+$out }local-repo-sync/$(basename "$f")"
      IFS=$NL
    done
    IFS=$old
    printf '%s' "$out" )
}

# Run autocommit.sh with one --scope per conf. The scopes are appended after
# the caller's own arguments, which parse_options accepts in any order, so the
# caller's "$@" survives intact -- a POSIX shell has no arrays to rebuild it.
autocommit_confs() {
  verb=$1; shift
  ( old=$IFS
    IFS=$NL
    for f in $CONF_FILES; do
      IFS=$old
      set -- "$@" --scope "local-repo-sync/$(basename "$f")"
      IFS=$NL
    done
    IFS=$old
    sh "$AUTOCOMMIT" "$verb" "$@" )
}

# The confs normally live in the config directory, outside the checkout, and a
# file outside the checkout (or a gitignored one inside it) has no commit to
# land in. `nightly` then skips the commit step instead of asking autocommit
# to stage a path it cannot. A private fork that keeps tracked confs in this
# folder (REPO_SYNC_CONF_DIR=<this folder>) keeps the commit.
confs_gitignored() {
  [ "$(cd "$CONF_DIR" 2>/dev/null && pwd)" = "$DIR" ] || return 0
  ( IFS=$NL
    for f in $CONF_FILES; do
      git -C "$DIR" check-ignore -q "$f" 2>/dev/null || exit 1
    done )
}

# `test` and `help` read no conf, so a fresh checkout without its local
# confs can still run the suite and print usage.
case "${1:-}" in
  test|help|-h|--help|"") ;;
  *)
    OLDIFS=$IFS
    IFS=$NL
    for conf in $CONF_FILES; do
      if [ ! -f "$conf" ]; then
        echo "repo-sync.sh: missing config $conf" >&2
        echo "  copy local-repo-sync/$(basename "$conf").example to $conf and list your repos" >&2
        exit 1
      fi
    done
    IFS=$OLDIFS
    ;;
esac

NOTIFY="$DIR/../local-notify/notify.sh"

# Emits "<subdir> <owner/repo>" lines with comments and blanks stripped,
# separators collapsed to one space, and each entry emitted once in the order
# it first appears. Every caller either iterates this list or counts it, so a
# repeated entry is a repo cloned and pulled twice and a total that overstates
# the fleet. Reconcile cannot write one since it learned to match on the
# directory a conf line actually clones to, but a hand-edited conf still can,
# and nothing downstream would notice: `sync` and `check` iterate straight off
# this. Deduping here rather than at each caller means the four totals are
# right because the list is right, not because nothing upstream went wrong.
repo_list() {
  ( IFS=$NL; for f in $CONF_FILES; do cat "$f"; done ) \
    | sed -e 's/#.*//' -e 's/[[:space:]][[:space:]]*/ /g' \
          -e 's/^ //' -e 's/ $//' \
    | grep -v '^$' \
    | awk '!seen[$0]++'
}

list() {
  echo "profile : $PROFILE"
  echo "root    : $ROOT"
  echo
  repo_list | awk '{ printf "%-12s %s\n", $1, $2 }'
  echo
  echo "total   : $(repo_list | wc -l | tr -d ' ') repos"
}

# Emits "<subdir> <owner/repo>" for every GitHub-origin checkout under ROOT,
# one level deep ("<subdir>/<repo>") plus repos sitting directly in ROOT
# (subdir "."). Checkouts with no origin or a non-GitHub origin cannot be
# recreated from a conf line, so they go to $UNMANAGED for reporting instead.
scan_disk() {
  for g in "$ROOT"/*/.git "$ROOT"/*/*/.git; do
    [ -e "$g" ] || continue
    d=${g%/.git}
    rel=${d#"$ROOT"/}
    case "$rel" in
      */*) subdir=${rel%/*} ;;
      *)   subdir="." ;;
    esac
    url=$(git -C "$d" remote get-url origin 2>/dev/null || true)
    ownrepo=""
    case "$url" in
      git@github.com:*)        ownrepo=${url#git@github.com:} ;;
      ssh://git@github.com/*)  ownrepo=${url#ssh://git@github.com/} ;;
      https://github.com/*)    ownrepo=${url#https://github.com/} ;;
    esac
    ownrepo=${ownrepo%.git}
    if [ -z "$ownrepo" ]; then
      echo "$rel  (origin: ${url:-none})" >> "$UNMANAGED"
    elif [ "${rel##*/}" != "${ownrepo##*/}" ]; then
      # A conf line derives its checkout directory from the repo name --
      # "<subdir> <owner/repo>" clones to $ROOT/<subdir>/<repo>, in `sync`
      # and `check` alike -- so a checkout sitting in a differently named
      # directory cannot be written as a conf line at all. Adding one anyway
      # is what looped: the appended entry keys as <subdir>/<repo> while this
      # checkout keys as <subdir>/<dirname>, the two never match, so it was
      # re-added every night; and the first night's `sync` cloned the entry
      # into a second checkout, which made the removal probe below pass
      # forever and hid the growth. Report it and let a human decide.
      echo "MISMATCH $rel  (origin: $ownrepo)" >> "$MISMATCHED"
    else
      echo "$subdir $ownrepo ${rel##*/}"
    fi
  done
}

# Drops the "<subdir> <owner/repo>" entry from whichever conf file holds it.
# Only the confs this profile actually reads are touched, so a profile that
# does not layer on common can never edit common.
# The IFS change stays inside a subshell: reconcile calls this from a
# `while read` loop, which would mis-split its own fields if IFS leaked out.
remove_entry() {
  ( IFS=$NL
    for f in $CONF_FILES; do
      awk -v s="$1" -v r="$2" '{
        line = $0; sub(/#.*/, "", line)
        gsub(/^[ \t]+|[ \t]+$/, "", line)
        n = split(line, p, /[ \t]+/)
        if (n == 2 && p[1] == s && p[2] == r) next
        print
      }' "$f" > "$f.reconcile.tmp" && mv "$f.reconcile.tmp" "$f"
    done )
}

# Reconciles the conf files against what is actually checked out: repos found
# on disk but not in any conf are appended to the profile conf, conf entries
# whose checkout is gone are deleted (disk is the source of truth — a repo
# deleted on purpose must not be re-cloned every night). Entries are matched
# by target path (subdir + directory name), so a checkout whose directory no
# longer matches its origin repo name (renamed upstream) is not dropped --
# its conf entry still names the directory that exists. It is not added
# under the new name either: the conf cannot express a directory that
# differs from the repo name, so scan_disk reports it as MISMATCH instead of
# offering it as an addition. Writes added/removed/unmanaged/mismatched
# lists into $TMPD.
reconcile() {
  scan="$TMPD/scan"; ADDED="$TMPD/added"; REMOVED="$TMPD/removed"
  UNMANAGED="$TMPD/unmanaged"; MISMATCHED="$TMPD/mismatched"
  : > "$ADDED"; : > "$REMOVED"; : > "$UNMANAGED"; : > "$MISMATCHED"

  scan_disk | sort -u > "$scan"
  repo_list | awk '{ n = split($2, a, "/"); print $1 "/" a[n] }' \
    | sort -u > "$TMPD/conf.keys"

  # The scan key uses the checkout's directory name, not the origin repo
  # name — a repo renamed upstream keeps its old directory and old conf
  # entry, and must not be re-added under the new name every night.
  while read -r subdir ownrepo dirname; do
    grep -qxF "$subdir/$dirname" "$TMPD/conf.keys" \
      || echo "$subdir $ownrepo" >> "$ADDED"
  done < "$scan"

  repo_list | while read -r subdir ownrepo; do
    [ -e "$ROOT/$subdir/${ownrepo##*/}/.git" ] \
      || echo "$subdir $ownrepo" >> "$REMOVED"
  done

  # Defence in depth against a re-add loop. Nothing should reach here twice
  # now that dir-mismatched checkouts are reported rather than added, but a
  # duplicate conf line is pulled twice by `sync` and counted twice in every
  # total, and three of them accumulated unnoticed before the cause was
  # found. Both sides are normalised to one space: conf lines are written by
  # hand with varying spacing, while $ADDED always uses a single space.
  if [ -s "$ADDED" ]; then
    repo_list | awk '{ print $1 " " $2 }' | sort -u > "$TMPD/conf.lines"
    sort -u "$ADDED" > "$TMPD/added.uniq"
    : > "$ADDED"
    while read -r line; do
      grep -qxF "$line" "$TMPD/conf.lines" || echo "$line" >> "$ADDED"
    done < "$TMPD/added.uniq"
  fi
  if [ -s "$ADDED" ]; then
    {
      echo
      echo "# auto-added by reconcile on $(date '+%Y-%m-%d')"
      cat "$ADDED"
    } >> "$PROFILE_CONF"
  fi
  if [ -s "$REMOVED" ]; then
    while read -r subdir ownrepo; do
      remove_entry "$subdir" "$ownrepo"
    done < "$REMOVED"
  fi

  echo "profile : $PROFILE"
  echo "root    : $ROOT"
  echo
  if [ -s "$ADDED" ]; then
    echo "added to $(basename "$PROFILE_CONF"):"
    sed 's/^/  /' "$ADDED"
  fi
  if [ -s "$REMOVED" ]; then
    echo "removed (checkout gone):"
    sed 's/^/  /' "$REMOVED"
  fi
  if [ ! -s "$ADDED" ] && [ ! -s "$REMOVED" ]; then
    echo "conf matches disk — no changes"
  fi
  if [ -s "$UNMANAGED" ]; then
    echo
    echo "unmanaged checkouts (no GitHub origin, not tracked in conf):"
    sed 's/^/  /' "$UNMANAGED"
  fi
  if [ -s "$MISMATCHED" ]; then
    echo
    echo "directory name does not match repo name — not representable as a"
    echo "conf line, so left alone (rename the directory, or the repo):"
    sed 's/^/  /' "$MISMATCHED"
  fi
}

# The read-only counterpart to sync, for callers that need to know what is
# absent without cloning it. machine-setup's `status` is the reason it exists:
# `list` prints the configured fleet without looking at disk and `sync` clones,
# so the repos stage had nothing it could report and sat outside the drift
# count entirely. Touches neither the network nor the working copies — origin
# comes from the local config, so a repo that is behind is still `ok` here.
check() {
  echo "profile : $PROFILE"
  echo "root    : $ROOT"
  echo
  repo_list | while read -r subdir full_repo; do
    repo=$(echo "$full_repo" | awk -F/ '{ print $2 }')
    target="$ROOT/$subdir/$repo"

    if [ ! -d "$target/.git" ]; then
      if [ -e "$target" ]; then
        echo "  DIFFERS $full_repo — $target exists but is not a git checkout"
      else
        echo "  MISSING $full_repo — no checkout at $target"
      fi
      echo "$full_repo" >> "$FINDINGS"
      continue
    fi

    url=$(git -C "$target" remote get-url origin 2>/dev/null || true)
    case "$url" in
      git@github.com:*)       have=${url#git@github.com:} ;;
      ssh://git@github.com/*) have=${url#ssh://git@github.com/} ;;
      https://github.com/*)   have=${url#https://github.com/} ;;
      *)                      have="" ;;
    esac
    have=${have%.git}

    if [ "$have" = "$full_repo" ]; then
      echo "  ok      $full_repo"
    else
      echo "  DIFFERS $full_repo — origin is ${url:-none}"
      echo "$full_repo" >> "$FINDINGS"
    fi
  done

  echo
  # Same subshell problem sync has: the loop above cannot set a variable the
  # caller would see, so findings are counted through a temp file.
  if [ -s "$FINDINGS" ]; then
    echo "total   : $(repo_list | wc -l | tr -d ' ') repos, $(wc -l < "$FINDINGS" | tr -d ' ') needing attention"
    return 1
  fi
  echo "total   : $(repo_list | wc -l | tr -d ' ') repos, all present"
}

sync() {
  echo "profile : $PROFILE"
  echo "root    : $ROOT"
  echo

  # A single unreachable repo must not abandon the rest of the sweep, so every
  # git call is guarded and its failure recorded for the summary instead.
  repo_list | while read -r subdir full_repo; do
    repo=$(echo "$full_repo" | awk -F/ '{ print $2 }')
    target="$ROOT/$subdir/$repo"

    if [ -d "$target/.git" ]; then
      echo "=== refreshing $full_repo"
      if ! (cd "$target" && git pull --ff-only && git submodule update --init --recursive); then
        echo "!!! failed: $full_repo" >&2
        echo "$full_repo" >> "$FAILLOG"
      fi
    else
      echo "=== cloning $full_repo"
      mkdir -p "$ROOT/$subdir"
      if ! git clone "git@github.com:$full_repo.git" "$target"; then
        echo "!!! failed: $full_repo" >&2
        echo "$full_repo" >> "$FAILLOG"
      fi
    fi
    echo
  done

  echo "----------------------------------------"
  if [ -s "$FAILLOG" ]; then
    echo "failed: $(wc -l < "$FAILLOG" | tr -d ' ') repo(s)"
    sed 's/^/  /' "$FAILLOG"
    return 1
  fi
  echo "all repos synced"
}

case "$1" in
  sync)
    # The `while read` loop runs in a subshell, so failures are collected in a
    # temp file rather than a variable, which would not survive the pipeline.
    FAILLOG=$(mktemp)
    trap 'rm -f "$FAILLOG"' EXIT INT TERM
    sync
    ;;
  check)
    FINDINGS=$(mktemp)
    trap 'rm -f "$FINDINGS"' EXIT INT TERM
    check
    ;;
  reconcile)
    TMPD=$(mktemp -d)
    trap 'rm -rf "$TMPD"' EXIT INT TERM
    reconcile
    ;;
  nightly)
    # Cron flavor: reconcile the conf against disk first (emailing the diff
    # when the fleet changed), then sync; a sync failure emails the summary
    # instead of relying on someone reading the log. Exits 1 when an email
    # could not be sent, and when half or more of the fleet failed (the
    # check at the end of this case says why that threshold), so the cron
    # failure push covers a lost report or an outage, not mere findings.
    TMPD=$(mktemp -d)
    FAILLOG="$TMPD/faillog"; : > "$FAILLOG"
    trap 'rm -rf "$TMPD"' EXIT INT TERM
    EMAIL_FAILED=0
    SYNC_FAILED=0

    # Asked before reconcile writes. `reconcile` appends to and removes lines
    # from $PROFILE_CONF, so a hand edit sitting there would be swept into an
    # unattended commit -- and unlike a wholesale regeneration it survives,
    # which makes the sweep silent rather than merely destructive.
    # SD_AUTOCOMMIT=0 turns both calls off. Set by this script's own test
    # fixture, which is a throwaway tree with no sibling tool and no git
    # repository around it, and available to anyone rehearsing nightly by
    # hand. It is an explicit opt-out and not a fallback: a missing
    # local-autocommit/autocommit.sh on a real machine is a broken install,
    # and this exits 127 saying so rather than skipping quietly. A lane that
    # silently stops running is the failure this whole change is about.
    AUTOCOMMIT="$DIR/../local-autocommit/autocommit.sh"
    CONF_SCOPE=$(conf_scopes_phrase)
    CHECK_RC=0
    COMMIT_RC=0
    if [ "${SD_AUTOCOMMIT:-1}" != "0" ] && confs_gitignored; then
      echo "repo-sync: $CONF_SCOPE is outside the checkout or gitignored; skipping the commit step."
      SD_AUTOCOMMIT=0
    fi
    if [ "${SD_AUTOCOMMIT:-1}" != "0" ]; then
      autocommit_confs check --author repo-sync || CHECK_RC=$?
      if [ "$CHECK_RC" -ne 0 ]; then
        echo "repo-sync: reconcile skipped; $CONF_SCOPE was not safe to write." >&2
        exit "$CHECK_RC"
      fi
    fi

    RECON="$TMPD/recon"
    reconcile > "$RECON" 2>&1
    cat "$RECON"

    # The conf is this job's own output and belongs in the repository. Left
    # dirty it makes `git status` permanently noisy, which is how a real edit
    # hides, and it rides onto whatever branch somebody switches to next.
    # Captured rather than let to fail the case: a push that could not land
    # must not also cost the list-change email below.
    # An `if`, not `[ ... ] && sh ... || COMMIT_RC=$?`. In that form a false
    # test short-circuits the `&&` and falls straight into the `||`, which
    # records the *test's* exit status as the commit's and fails the job for
    # being switched off. Same AND-OR trap the tool's own push guard is about.
    if [ "${SD_AUTOCOMMIT:-1}" != "0" ]; then
    autocommit_confs commit --author repo-sync --empty-ok \
      --message "chore(repo-sync): reconcile $PROFILE repo list" \
      --body "Written by local-repo-sync/repo-sync.sh nightly on $(hostname -s).
Checkouts added or removed on disk, recorded in the profile conf. Scoped to
$CONF_SCOPE and nothing else." \
      || COMMIT_RC=$?
    fi
    # A MISMATCH notifies too. Before this path existed, such a checkout made
    # $ADDED non-empty every night and so emailed every night -- badly, as a
    # spurious list change, but the operator did hear about it. Reporting it
    # to stdout alone would have bought silence with the fix: a green night
    # whose only trace is a cron log nobody reads, which is how the loop this
    # replaced went unnoticed for four nights. It cannot be auto-resolved, so
    # it repeats until a human renames the directory or the repo.
    if [ -s "$ADDED" ] || [ -s "$REMOVED" ] || [ -s "$MISMATCHED" ]; then
      if [ -s "$ADDED" ] || [ -s "$REMOVED" ]; then
        subject="repo-sync: repo list changed on $(hostname -s)"
      else
        subject="repo-sync: $(wc -l < "$MISMATCHED" | tr -d ' ') checkout(s) need a rename on $(hostname -s)"
      fi
      body=$(printf 'repo-sync reconcile — %s on %s\n\n%s\n' \
        "$(date '+%Y-%m-%d %H:%M')" "$(hostname -s)" "$(cat "$RECON")")
      if ! sh "$NOTIFY" -t "$subject" -k status -c ntfy,email -b "$body"; then
        echo "list-change email FAILED" >&2
        EMAIL_FAILED=1
      fi
    fi

    OUT="$TMPD/out"
    if ! sync > "$OUT" 2>&1; then
      cat "$OUT"
      SYNC_FAILED=1
      n=$(wc -l < "$FAILLOG" | tr -d ' ')
      subject="repo-sync: $n repo(s) failed on $(hostname -s)"
      body=$(printf 'repo-sync nightly report — %s on %s\n\nFailed repos:\n%s\n\nLast output:\n%s\n' \
        "$(date '+%Y-%m-%d %H:%M')" "$(hostname -s)" \
        "$(sed 's/^/  /' "$FAILLOG")" \
        "$(tail -60 "$OUT")")
      if ! sh "$NOTIFY" -t "$subject" -k status -F -c ntfy,email -b "$body"; then
        echo "failure email FAILED" >&2
        EMAIL_FAILED=1
      fi
    else
      cat "$OUT"
    fi

    if [ "$EMAIL_FAILED" -ne 0 ]; then
      echo "email FAILED — exiting 1 so the cron failure push fires" >&2
      exit 1
    fi
    # A sync that failed the whole fleet used to exit 0, because only a dead
    # email was treated as job failure. On the work machine that hid four
    # straight nights of 41-of-41 failures: no failures.log entry, no wrapper
    # push, and `cron-jobs.sh status` reporting "(never exited)".
    #
    # Findings are still not a failure — one unreachable repo out of forty is a
    # report, not an outage. Half or more is an outage. The threshold is not
    # "all": a dead network still lets the odd repo through (a stubbed-SSH
    # rehearsal of this path failed 36 of 38, not 38), so an equality test
    # would have gone on reporting success.
    n_failed=$(wc -l < "$FAILLOG" | tr -d ' ')
    n_total=$(repo_list | wc -l | tr -d ' ')
    if [ "$SYNC_FAILED" -ne 0 ] && [ "$n_total" -gt 0 ] && [ $((n_failed * 2)) -ge "$n_total" ]; then
      echo "sync failed $n_failed of $n_total repos — exiting 1 so the cron failure push fires" >&2
      exit 1
    fi
    # Reported last, so an unpushed conf raises the cron banner without
    # swallowing the list-change report or the fleet-outage check above.
    if [ "$COMMIT_RC" -ne 0 ]; then
      echo "repo-sync: $CONF_SCOPE was not committed and pushed (rc=$COMMIT_RC)" >&2
      exit "$COMMIT_RC"
    fi
    ;;
  list)
    list
    ;;
  test)
    shift
    exec "${PYTHON:-python3}" -m unittest discover -s "$DIR/tests" -t "$DIR" "$@"
    ;;
  -h|--help|help)
    cat <<'HELPEOF'
usage: repo-sync.sh sync|check|list|reconcile|nightly|test

  sync       clone missing repos and fast-forward existing ones, then print a
             summary; exits 1 if any repo failed, having tried all the others
  check      read-only: report which configured repos have no checkout
             (MISSING) and which sit on a different origin than the conf
             names (DIFFERS). Touches no network, so a repo that is merely
             behind still reports ok. Exits 1 when anything was reported —
             this is what machine-setup's `status` folds into its drift count
  list       print the resolved profile, root, and repo list without touching
             disk
  reconcile  update the conf files to match the checkouts on disk: GitHub
             repos found under the root but missing from the conf are added
             to the profile conf, entries whose checkout is gone are removed
             (disk is the source of truth). Checkouts without a GitHub origin
             are listed as unmanaged and left alone, as are checkouts whose
             directory name differs from their repo name (MISMATCH) — the
             conf format cannot express those.
  nightly    reconcile (emailing the diff when the repo list changed, then
             committing and pushing the conf), then sync (emailing the
             failure summary); this is what the repo-sync-nightly cron job
             runs. Exits 1 when an email could not be delivered, when half
             or more of the fleet failed, or when the conf could not be
             pushed; 3 when the conf was not safe to write and reconcile
             was skipped. SD_AUTOCOMMIT=0 skips the commit step, and so do confs
             outside the checkout (the default) or gitignored.
  test       run the regression suite in tests/ (unittest; override the
             interpreter with PYTHON). Covers reconcile, list and nightly
             against fixture trees; sync's clone path is not covered,
             because it clones over SSH — the suite reaches it only through
             nightly, with the transport stubbed dead.

The repo fleet lives in repos.common.conf plus repos.<profile>.conf, in
<config>/repo-sync/ (<config> is $SYSTEM_TOOLS_CONFIG, default
~/.config/system). Copy the committed repos.<profile>.conf.example there. Format
is "<subdir> <owner/repo>", with # comments and blank lines ignored; subdir
"." means the repo sits directly in the root.

The terra profile is the exception: it reads repos.terra.conf alone and
skips repos.common.conf, because a terra machine deliberately carries only
the system repo. reconcile therefore can never edit common from such a
machine.

environment:
  REPO_SYNC_PROFILE   personal | work | terra (default: the profile
                      machine-setup recorded in
                      ~/.config/machine-setup/profile, else work if
                      the work root names an existing directory,
                      else personal)
  REPO_SYNC_ROOT      checkout root (default: ~/repos, or the work root
                      on the work profile)
  REPO_SYNC_WORK_ROOT work checkout root; also selects the work profile
                      when no profile is recorded (default:
                      SYSTEM_TOOLS_WORK_ROOT, which local-ai-apps reads too)
  REPO_SYNC_CONF_DIR  where the confs live (default: <config>/repo-sync)
  SYSTEM_TOOLS_CONFIG shared config root (default: ~/.config/system)
  MACHINE_SETUP_STATE where to look for the recorded profile
                      (default: ~/.config/machine-setup)
HELPEOF
    exit 0
    ;;
  *)
    echo "usage: $(basename "$0") sync|check|list|reconcile|nightly|test" >&2
    exit 1
    ;;
esac

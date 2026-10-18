#!/bin/sh
# Clone-or-pull the repo fleet the workflow database lists for this machine's
# profile (sd-db repo list --clones), else its last answer, else the confs.
# Usage: repo-sync.sh sync|check|list|reconcile|hygiene|nightly|refresh|follow|test
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
. "$DIR/../lib/bounded.sh"
. "$DIR/../lib/brew_unlinked.sh"

# fd 4 is this run's stderr, the job log under cron. bound_step is st_step with
# its step line there, also when its caller captures the call's own output, as
# nightly captures sync's: a step that hangs then names itself in the log
# (sd:3536). A failed step names itself on the captured stderr too.
exec 4>&2
bound_step() { # seconds, command...
  # shellcheck disable=SC2034 # read by st_step in lib/bounded.sh
  (ST_STEP_FD3=1; st_step "$@" 3>&4)
}

# Prints failure line $1, and keeps it in $TMPD/flagged when there is a TMPD:
# a failed refresh prints those lines again on stderr. The lane's post-merge
# report keeps stderr alone when there is any, and a step line always is, so
# a reason on stdout alone was lost (sd:4045).
flag() {
  echo "$1"
  [ -z "${TMPD:-}" ] || printf '%s\n' "$1" >> "$TMPD/flagged"
}

# After a failed `make setup`: name a brew python that brew doctor lists as
# unlinked, which the pack's installer then cannot run, and the fix. It links
# nothing: local-brew-doctor does that on its own schedule (sd:4864). A doctor
# that did not answer is said so, not read as no unlinked python.
unlinked_python_hint() {
  command -v brew >/dev/null 2>&1 || return 0
  u_rc=0
  bound_step 300 brew doctor < /dev/null > "$TMPD/doctor.raw" 2>&1 || u_rc=$?
  # brew doctor exits 1 on a warning; any other code is no answer.
  if [ "$u_rc" -gt 1 ]; then
    flag "!!! cause unknown: could not check brew for an unlinked python (brew doctor exit $u_rc)"
    return 0
  fi
  grep -v '^\[step ' "$TMPD/doctor.raw" > "$TMPD/doctor" || :
  st_unlinked_kegs "$TMPD/doctor" | grep '^python' > "$TMPD/unlinked" || :
  while IFS= read -r u_keg; do
    flag "!!! cause: $u_keg is unlinked; relink it: brew link $u_keg"
  done < "$TMPD/unlinked"
  return 0
}

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
# directory outside the checkout. Since sd:3983 they are the list's third
# source, after the database and its snapshot (load_list). REPO_SYNC_CONF_DIR points elsewhere; a
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

# The pack's sd-db: the workflow database holds the list (sd:3983). Each call
# reads /dev/null and is bounded, since on a satellite it crosses the tailnet.
# The pack writes `repo list --clones` lines relative to SD_REPO_ROOT (default
# ~/repos), so every call gets the root this script resolved: else a root
# moved by REPO_SYNC_ROOT or a work root has every row refused.
SD_PACK_ROOT="${SD_PACK_ROOT:-$HOME/repos/platypeeps/sd-ai-command-pack}"
sd_db() {
  st_bounded 120 env SD_REPO_ROOT="$ROOT" "$SD_PACK_ROOT/bin/sd-db" "$@" < /dev/null
}

# The last list the database gave for this profile. Its name carries the
# profile, so another profile's answer is never read as this one's. Its first
# line, `# root <ROOT>`, names the physical root its lines are relative to:
# after the root moves (or its symlink does), the pack refuses rows outside it,
# and an old snapshot must not stand in for them. A comment, so the README's
# revert reads it as a conf.
SNAPSHOT="$(st_state_dir repo-sync)/clones.$PROFILE"

# Comments and blanks stripped, separators collapsed to one space, and each
# entry once in the order it first appears. Every caller either iterates the
# list or counts it, so a repeated entry is a repo cloned and pulled twice and
# a total that overstates the fleet. A hand-edited conf can repeat one, and
# nothing downstream would notice: `sync` and `check` iterate straight off it.
normalise() {
  sed -e 's/#.*//' -e 's/[[:space:]][[:space:]]*/ /g' \
      -e 's/^ //' -e 's/ $//' \
    | grep -v '^$' \
    | awk '!seen[$0]++'
}

# Reads the run's list once into $TMPD/list (design sd:3983):
#   1. `sd-db repo list --clones PROFILE`, when it exits 0 and every line is
#      `<subdir> <owner/repo>`. It then replaces the snapshot (temp file, then
#      mv: a temp file a killed run left is overwritten, never read).
#   2. Else the snapshot, the database's last answer, when it names this root.
#   3. Else the conf files: the bootstrap of a machine that never reached it.
#   4. Else refuse, naming both remedies.
# A failed or empty answer is unknown, never an empty list: every profile
# lists at least `system`, so no line means the profile was never imported.
# LIST_SOURCE names the source; LIST_WHY why the database did not answer.
load_list() {
  LIST_FILE="$TMPD/list"
  LIST_WHY=""
  l_rc=0
  # A fresh machine has no root until sync makes one.
  l_root=$(cd "$ROOT" 2> /dev/null && pwd -P) || l_root=$ROOT
  sd_db repo list --clones "$PROFILE" > "$TMPD/list.db" 2> "$TMPD/list.err" || l_rc=$?
  if [ "$l_rc" -ne 0 ]; then
    LIST_WHY="sd-db repo list --clones $PROFILE exited $l_rc: $(tail -1 "$TMPD/list.err")"
  elif [ ! -s "$TMPD/list.db" ]; then
    LIST_WHY="it lists nothing for $PROFILE"
  else
    l_bad=$(awk 'NF != 2 || $2 !~ /^[^\/]+\/[^\/]+$/ { print; exit }' "$TMPD/list.db")
    [ -z "$l_bad" ] || LIST_WHY="its line '$l_bad' is not a <subdir> <owner/repo> line"
  fi
  if [ -z "$LIST_WHY" ]; then
    normalise < "$TMPD/list.db" > "$LIST_FILE"
    LIST_SOURCE=database
    if ! { mkdir -p "${SNAPSHOT%/*}" && { printf '# root %s\n' "$l_root" && cat "$LIST_FILE"; } > "$SNAPSHOT.tmp" \
        && mv -f "$SNAPSHOT.tmp" "$SNAPSHOT"; }; then
      echo "repo-sync.sh: cannot write $SNAPSHOT; without the database, the next run reads an older answer or the confs" >&2
    fi
    return 0
  fi
  if [ -s "$SNAPSHOT" ] && [ "$(head -1 "$SNAPSHOT")" = "# root $l_root" ]; then
    normalise < "$SNAPSHOT" > "$LIST_FILE"
    LIST_SOURCE=snapshot
    echo "repo-sync.sh: the database did not answer ($LIST_WHY); the list is its last answer, $SNAPSHOT" >&2
    return 0
  fi
  l_missing=""
  OLDIFS=$IFS
  IFS=$NL
  for conf in $CONF_FILES; do
    { [ -f "$conf" ] && [ -r "$conf" ]; } || { l_missing=$conf; break; }
  done
  IFS=$OLDIFS
  # A conf that fails to read is no list, not an empty one.
  if [ -z "$l_missing" ] \
      && ( IFS=$NL; for f in $CONF_FILES; do cat "$f" || exit 1; done ) > "$TMPD/list.conf"; then
    normalise < "$TMPD/list.conf" > "$LIST_FILE"
    LIST_SOURCE=conf
    echo "repo-sync.sh: the database did not answer ($LIST_WHY) and there is no snapshot for $ROOT ($SNAPSHOT); the list is the conf files" >&2
    return 0
  fi
  echo "repo-sync.sh: no repo list: the database did not answer ($LIST_WHY), there is no snapshot for $ROOT ($SNAPSHOT), and the config ${l_missing:-files} is missing or unreadable" >&2
  echo "  list the profile in the database: SD_REPO_ROOT='$ROOT' sd-db repo seed --clones $PROFILE, or sd-db repo clones PATH +$PROFILE" >&2
  l_missing=${l_missing:-$PROFILE_CONF}
  echo "  or copy local-repo-sync/$(basename "$l_missing").example to $l_missing and list your repos" >&2
  exit 1
}

# Emits the run's "<subdir> <owner/repo>" lines, as load_list read them.
repo_list() {
  cat "$LIST_FILE"
}

list() {
  echo "profile : $PROFILE"
  echo "root    : $ROOT"
  echo "source  : $LIST_SOURCE"
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
    # A linked worktree placed under the root has a .git file and a git dir
    # that differs from its common dir (a submodule's .git file has the two
    # equal). It is not a checkout: it belongs to its parent, which `hygiene`
    # reaches through the parent's registrations. Listing it as a MISMATCH
    # would ask for a rename that makes no sense (sd:1987).
    if [ -f "$g" ]; then
      gdir=$(git -C "$d" rev-parse --path-format=absolute --git-dir 2>/dev/null || true)
      cdir=$(git -C "$d" rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)
      if [ -n "$cdir" ] && [ "$gdir" != "$cdir" ]; then
        parent=${cdir%/.git}
        root_real=$(cd "$ROOT" && pwd -P)
        case "$parent" in
          "$root_real"/*) parent=${parent#"$root_real"/} ;;
          "$ROOT"/*)      parent=${parent#"$ROOT"/} ;;
        esac
        echo "WORKTREE $rel (of $parent)" >> "$WORKTREES"
        continue
      fi
    fi
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

# Compares the run's list with the checkouts on disk, and writes one thing:
# a GitHub checkout with no row joins the database, through `sd-db repo add`
# then `sd-db repo clones PATH +PROFILE`, so a checkout cloned by hand still
# joins the list (design sd:3983). It writes only when the database gave the
# list, and never removes a profile, so no outage or empty disk can look like
# a deletion. The rest it reports: a checkout whose row does not list this
# profile (the operator may have cleared it), and a listed one that is gone
# (sync clones it). Entries are matched by target path (subdir + directory
# name). A checkout whose directory differs from its origin repo name cannot
# be a list line, so scan_disk reports it as MISMATCH. Writes its lists into
# $TMPD; a non-empty $JOINED means the caller reads the list again.
reconcile() {
  scan="$TMPD/scan"; JOINED="$TMPD/joined"; UNJOINED="$TMPD/unjoined"
  UNLISTED="$TMPD/unlisted"; ABSENT="$TMPD/absent"
  UNMANAGED="$TMPD/unmanaged"; MISMATCHED="$TMPD/mismatched"
  WORKTREES="$TMPD/worktrees"
  : > "$JOINED"; : > "$UNJOINED"; : > "$UNLISTED"; : > "$ABSENT"
  : > "$UNMANAGED"; : > "$MISMATCHED"; : > "$WORKTREES"

  scan_disk | sort -u > "$scan"
  repo_list | awk '{ n = split($2, a, "/"); print $1 "/" a[n] }' \
    | sort -u > "$TMPD/conf.keys"
  repo_list | while read -r subdir ownrepo; do
    [ -e "$ROOT/$subdir/${ownrepo##*/}/.git" ] \
      || echo "$subdir $ownrepo" >> "$ABSENT"
  done

  # The registered paths, absolute: `repo list` prints a row under $HOME as
  # ~/..., and two spaces end the path. A failed read writes nothing.
  r_rows=""
  r_why=""
  if [ "$LIST_SOURCE" != database ]; then
    r_why="the database did not answer ($LIST_WHY)"
  elif sd_db repo list > "$TMPD/rows.out" 2> "$TMPD/rows.err"; then
    sed -n 's/^sd-db: //p' "$TMPD/rows.out" \
      | awk -v home="$HOME" '{ sub(/  .*/, ""); if (substr($0, 1, 2) == "~/") $0 = home substr($0, 2); print }' \
      > "$TMPD/rows"
    r_rows=1
  else
    r_why="sd-db repo list failed: $(tail -1 "$TMPD/rows.err")"
  fi
  # A fresh machine has no root until sync makes one.
  r_root=$(cd "$ROOT" 2> /dev/null && pwd -P) || r_root=$ROOT

  # The scan key uses the checkout's directory name, not the origin repo
  # name — a repo renamed upstream keeps its old directory and old entry.
  while read -r subdir ownrepo dirname; do
    if grep -qxF "$subdir/$dirname" "$TMPD/conf.keys"; then continue; fi
    if [ "$subdir" = . ]; then r_rel=$dirname; else r_rel=$subdir/$dirname; fi
    if [ -z "$r_rows" ] || grep -qxF -e "$ROOT/$r_rel" -e "$r_root/$r_rel" "$TMPD/rows"; then
      echo "$subdir $ownrepo" >> "$UNLISTED"
    elif sd_db repo add "$ROOT/$r_rel" > /dev/null 2> "$TMPD/join.err" \
        && sd_db repo clones "$ROOT/$r_rel" "+$PROFILE" > /dev/null 2> "$TMPD/join.err"; then
      echo "$subdir $ownrepo" >> "$JOINED"
    else
      echo "$subdir $ownrepo ($(tail -1 "$TMPD/join.err"))" >> "$UNJOINED"
    fi
  done < "$scan"

  echo "profile : $PROFILE"
  echo "root    : $ROOT"
  echo "source  : $LIST_SOURCE"
  echo
  [ -z "$r_why" ] || echo "reconcile wrote nothing: $r_why"
  if [ -s "$JOINED" ]; then
    echo "joined (registered, and listed for $PROFILE):"
    sed 's/^/  /' "$JOINED"
  fi
  if [ -s "$UNJOINED" ]; then
    echo "not joined (sd-db repo add, then repo clones +$PROFILE, failed; the next run reports it):"
    sed 's/^/  /' "$UNJOINED"
  fi
  if [ -s "$UNLISTED" ]; then
    echo "present, not listed for $PROFILE: list it with sd-db repo clones PATH +$PROFILE, or delete the checkout:"
    sed 's/^/  /' "$UNLISTED"
  fi
  if [ -s "$ABSENT" ]; then
    echo "listed for $PROFILE, no checkout: sync clones it; to stop, sd-db repo clones PATH -$PROFILE:"
    sed 's/^/  /' "$ABSENT"
  fi
  if [ ! -s "$JOINED" ] && [ ! -s "$UNJOINED" ] && [ ! -s "$UNLISTED" ] && [ ! -s "$ABSENT" ]; then
    echo "list matches disk — no changes"
  fi
  if [ -s "$UNMANAGED" ]; then
    echo
    echo "unmanaged checkouts (no GitHub origin, not in the list):"
    sed 's/^/  /' "$UNMANAGED"
  fi
  if [ -s "$MISMATCHED" ]; then
    echo
    echo "directory name does not match repo name — not representable as a"
    echo "list line, so left alone (rename the directory, or the repo):"
    sed 's/^/  /' "$MISMATCHED"
  fi
  if [ -s "$WORKTREES" ]; then
    echo
    echo "linked worktrees under the root (not checkouts; hygiene sweeps them"
    echo "through their parent):"
    sed 's/^/  /' "$WORKTREES"
  fi
}

# The read-only counterpart to sync, for callers that need to know what is
# absent without cloning it. machine-setup's `status` is the reason it exists:
# `list` prints the configured fleet without looking at disk and `sync` clones,
# so the repos stage had nothing it could report and sat outside the drift
# count entirely. Touches neither a git remote nor the working copies — origin
# comes from the local config, so a repo that is behind is still `ok` here.
# Only the list's read reaches the database, the hub's on a satellite.
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

# --- GitHub SSH preflight (sd:2160) ----------------------------------------
# `nightly` runs under launchd. After a reboot the agent holds no key until
# someone unlocks one, so every SSH fetch fails the same way: on 2026-09-29,
# 63 of 65 failures were "Permission denied (publickey)". Check once before
# the sweep. When no key answers, load the passphrases the keychain holds
# (`mac-utils.sh addkey` stores one) and check again. A key that is still
# locked is named once in the report, instead of in 63 identical errors.

# git runs GIT_SSH_COMMAND through the shell, so this does too.
ssh_to_github() {
  sh -c "${GIT_SSH_COMMAND:-ssh} -o BatchMode=yes -o ConnectTimeout=10 -T git@github.com" < /dev/null 2>&1
}

# Prints one "ssh     :" line when it acted or failed; silent when a key
# already answers. Returns 1 when no key authenticates.
ssh_preflight() {
  case $(ssh_to_github) in *"successfully authenticated"*) return 0 ;; esac
  # Not every ssh-add knows --apple-load-keychain; one that does not fails
  # here, and the check below reports the key as locked.
  ${REPO_SYNC_SSH_ADD:-ssh-add} --apple-load-keychain < /dev/null > /dev/null 2>&1 || true
  case $(ssh_to_github) in
    *"successfully authenticated"*)
      echo "ssh     : loaded key(s) from the keychain before the sweep"
      return 0
      ;;
  esac
  echo "ssh     : no key authenticates to git@github.com, so every SSH clone and"
  echo "          pull fails. After a reboot, a key with a passphrase stays locked"
  echo "          until someone unlocks it. Run 'mac-utils.sh addkey' once: it keeps"
  echo "          the passphrase in the keychain, which this job then loads."
  return 1
}

# --- pinned checkouts (sd:3097) ---------------------------------------------
# A checkout on a detached HEAD is pinned. The hub runs system and the command
# pack from pinned checkouts, so they change only when the operator says so.
# `sync` fetches a pinned checkout but never moves it; `refresh` moves it to
# origin's default branch. `git switch main` unpins one.

# Returns 0 when checkout $1 is pinned: HEAD names a commit, not a branch.
is_pinned() {
  ! git -C "$1" symbolic-ref -q HEAD > /dev/null 2>&1 \
    && git -C "$1" rev-parse -q --verify HEAD > /dev/null 2>&1
}

# Prints origin's default branch for checkout $1 from its remote-tracking
# refs: origin/HEAD, else main, else master. Returns 1 when none exists.
pin_default() {
  p_ref=$(git -C "$1" symbolic-ref -q --short refs/remotes/origin/HEAD 2>/dev/null || true)
  if [ -n "$p_ref" ]; then
    echo "${p_ref#origin/}"; return 0
  fi
  for p_name in main master; do
    if git -C "$1" show-ref -q --verify "refs/remotes/origin/$p_name"; then
      echo "$p_name"; return 0
    fi
  done
  return 1
}

# Prints "on <branch>, N ahead, M behind <upstream>" when checkout $1 is on
# a branch other than origin's default, and its upstream, just fetched, moved
# past the branch's own commits (sd:3125). That is work in progress, often a
# pull request branch; `pull --ff-only` can never move it. Returns 1
# otherwise, and when any read or the fetch fails, so the pull still runs
# and reports that failure.
side_branch_diverged() {
  d_branch=$(git -C "$1" symbolic-ref -q --short HEAD) || return 1
  d_default=$(pin_default "$1") || return 1
  [ "$d_branch" != "$d_default" ] || return 1
  d_up=$(git -C "$1" rev-parse -q --abbrev-ref '@{u}' 2>/dev/null) || return 1
  bound_step 300 git -C "$1" fetch -q < /dev/null 2>/dev/null || return 1
  d_counts=$(git -C "$1" rev-list --left-right --count 'HEAD...@{u}') || return 1
  set -- $d_counts
  [ "$1" -gt 0 ] && [ "$2" -gt 0 ] || return 1
  echo "on $d_branch, $1 ahead, $2 behind $d_up"
}

# Prints "on <branch>" when checkout $1 is on a local branch with no upstream
# and returns 0; returns 1 otherwise. `git pull` fails there with "no tracking
# information", and the operator keeps some checkouts that way on purpose
# (sd:3744).
local_branch_no_upstream() {
  n_branch=$(git -C "$1" symbolic-ref -q --short HEAD) || return 1
  git -C "$1" rev-parse -q --abbrev-ref '@{u}' > /dev/null 2>&1 && return 1
  echo "on $n_branch"
}

# A satellite follows the hub (sd:3100): `$HOME/.config/sd/hub.json`, the
# file sd_db's hub.py reads, makes this machine one.
is_satellite() {
  [ -e "$HOME/.config/sd/hub.json" ]
}

# Prints which hub checkout $1 is, `system` or `pack`, or returns 1. Only
# `follow` moves these on a satellite. System by this script alone: the pack
# owns sd_db (sd:3279).
hub_checkout() {
  if [ -f "$1/bin/sd_install.py" ]; then echo pack
  elif [ -f "$1/local-repo-sync/repo-sync.sh" ]; then echo system
  else return 1
  fi
}

# Prints "pinned at <sha>" (or "on <branch> at <sha>"), plus ", N behind
# origin/<default>" when it is.
pin_state() {
  if p_br=$(git -C "$1" symbolic-ref -q --short HEAD 2>/dev/null); then
    p_line="on $p_br at $(git -C "$1" rev-parse --short HEAD)"
  else
    p_line="pinned at $(git -C "$1" rev-parse --short HEAD)"
  fi
  if p_def=$(pin_default "$1"); then
    p_n=$(git -C "$1" rev-list --count "HEAD..refs/remotes/origin/$p_def" 2>/dev/null || echo 0)
    if [ "$p_n" -gt 0 ]; then p_line="$p_line, $p_n behind origin/$p_def"; fi
  fi
  echo "$p_line"
}

sync() {
  echo "profile : $PROFILE"
  echo "root    : $ROOT"
  echo

  # A single unreachable repo must not abandon the rest of the sweep, so every
  # git call is guarded and its failure recorded for the summary instead. Each
  # one that reaches a remote is bounded, so one stuck SSH session cannot hold
  # the sweep, and reads /dev/null, not the repo list (sd:3536).
  repo_list | while read -r subdir full_repo; do
    repo=$(echo "$full_repo" | awk -F/ '{ print $2 }')
    target="$ROOT/$subdir/$repo"

    if [ -d "$target/.git" ] && is_satellite && hub_checkout "$target" > /dev/null; then
      mover=follow
    else
      mover=refresh
    fi
    if [ -d "$target/.git" ] && { is_pinned "$target" || [ "$mover" = follow ]; }; then
      # A pinned checkout is fetched, so its behind count is fresh, and never
      # pulled; `git pull` would refuse a detached HEAD anyway. On a
      # satellite, system and pack are never pulled, pinned or not.
      echo "=== pinned $full_repo"
      if bound_step 300 git -C "$target" fetch -q origin < /dev/null; then
        state=$(pin_state "$target")
        echo "$state; not pulled (repo-sync.sh $mover moves it)"
        echo "$full_repo $state" >> "$PINLOG"
      else
        echo "!!! failed: $full_repo (fetch; $(pin_state "$target"))" >&2
        echo "$full_repo" >> "$FAILLOG"
      fi
    elif [ -d "$target/.git" ]; then
      echo "=== refreshing $full_repo"
      if state=$(local_branch_no_upstream "$target"); then
        # Fetched so its refs stay fresh, never pulled. A failed fetch is an
        # unknown result, so it fails like any other repo.
        if bound_step 300 git -C "$target" fetch -q < /dev/null; then
          echo "$state, no upstream; fetched, not pulled"
          echo "$full_repo $state" >> "$KEPTLOG"
        else
          echo "!!! failed: $full_repo (fetch; $state)" >&2
          echo "$full_repo" >> "$FAILLOG"
        fi
      elif state=$(side_branch_diverged "$target"); then
        echo "$state; left alone"
        echo "$full_repo $state" >> "$DIVLOG"
      elif ! { bound_step 600 git -C "$target" pull --ff-only < /dev/null \
          && bound_step 1800 git -C "$target" submodule update --init --recursive < /dev/null; }; then
        echo "!!! failed: $full_repo" >&2
        echo "$full_repo" >> "$FAILLOG"
      fi
    else
      echo "=== cloning $full_repo"
      mkdir -p "$ROOT/$subdir"
      if ! bound_step 1800 git clone "git@github.com:$full_repo.git" "$target" < /dev/null; then
        echo "!!! failed: $full_repo" >&2
        echo "$full_repo" >> "$FAILLOG"
      fi
    fi
    echo
  done

  echo "----------------------------------------"
  if [ -s "$PINLOG" ]; then
    echo "pinned: $(wc -l < "$PINLOG" | tr -d ' ') checkout(s), not pulled"
    sed 's/^/  /' "$PINLOG"
  fi
  if [ -s "$KEPTLOG" ]; then
    echo "kept: $(wc -l < "$KEPTLOG" | tr -d ' ') checkout(s), on a local branch with no upstream, fetched, not pulled"
    sed 's/^/  /' "$KEPTLOG"
  fi
  if [ -s "$DIVLOG" ]; then
    echo "diverged: $(wc -l < "$DIVLOG" | tr -d ' ') checkout(s), left alone; rebase or merge by hand"
    sed 's/^/  /' "$DIVLOG"
  fi
  if [ -s "$FAILLOG" ]; then
    echo "failed: $(wc -l < "$FAILLOG" | tr -d ' ') repo(s)"
    sed 's/^/  /' "$FAILLOG"
    return 1
  fi
  echo "all repos synced"
}

# The SCHEMA_VERSION that the pack's lib/sd_db holds at commit $2 of checkout
# $1, or nothing when that commit has no lib/sd_db. Only the pack's: it owns
# sd_db (sd:3279).
pin_schema() {
  git -C "$1" show "$2:lib/sd_db/schema.py" 2>/dev/null \
    | sed -n 's/^SCHEMA_VERSION = \([0-9][0-9]*\).*/\1/p' | head -1
}

# Checks and fetches pinned checkout $1, then queues its move to origin's
# default branch in $TMPD/plan, so the rollback point is in place before
# anything moves. Returns 1 when a check or the fetch failed. Refreshing the
# checkout this script runs from is safe: git replaces each file rather than
# rewriting it, and the shell keeps reading the old one.
refresh_one() {
  if ! r_dir=$(cd "$1" 2>/dev/null && pwd) \
     || ! git -C "$r_dir" rev-parse -q --git-dir > /dev/null 2>&1; then
    flag "!!! failed: $1 is not a git checkout"
    return 1
  fi
  if ! is_pinned "$r_dir"; then
    r_br=$(git -C "$r_dir" symbolic-ref -q --short HEAD 2>/dev/null || echo "an unborn HEAD")
    echo "=== $r_dir is on $r_br, not pinned; left alone"
    return 0
  fi
  r_old=$(git -C "$r_dir" rev-parse HEAD)
  r_short=$(git -C "$r_dir" rev-parse --short HEAD)
  echo "=== refresh $r_dir (pinned at $r_short)"
  # A tracked change would ride along to the new commit, or block the switch
  # halfway. Untracked files stay where they are, so they do not count.
  if [ -n "$(git -C "$r_dir" status --porcelain --untracked-files=no 2>/dev/null || echo unreadable)" ]; then
    flag "!!! refused: $r_dir has uncommitted changes; commit or stash them, then refresh again"
    return 1
  fi
  if ! bound_step 300 git -C "$r_dir" fetch -q origin < /dev/null; then
    flag "!!! failed: fetch in $r_dir; still at $r_short"
    return 1
  fi
  if ! r_def=$(pin_default "$r_dir"); then
    flag "!!! failed: $r_dir has no origin/HEAD, origin/main or origin/master; still at $r_short"
    return 1
  fi
  printf '%s\n' "$r_dir${US}origin/$r_def" >> "$TMPD/plan"
}

# Moves checkout $1 to commit $2, detached, as `refresh` or `follow` ($3),
# which reports "$4: <old> -> <new>". Then `make setup` in the pack, and the
# migrate steps when lib/sd_db's SCHEMA_VERSION changed. Returns 1 when a
# step failed.
pin_move() {
  m_dir=$1; m_rev=$2
  m_old=$(git -C "$m_dir" rev-parse HEAD)
  m_short=$(git -C "$m_dir" rev-parse --short HEAD)
  # `git submodule update` would overwrite an ignored file in a submodule and
  # has no switch to stop it, so a checkout with submodules is moved by hand.
  for m_at in HEAD "$m_rev"; do
    if git -C "$m_dir" ls-tree -r "$m_at" | awk '$2 == "commit" { f = 1 } END { exit !f }'; then
      flag "!!! refused: $m_at in $m_dir holds a submodule; $3 does not move one; still at $m_short"
      return 1
    fi
  done
  # An ignored file is often local config or data; git overwrites one that
  # the target commit tracks unless told not to, and then refuses the switch.
  if ! git -C "$m_dir" switch -q --detach --no-overwrite-ignore "$m_rev"; then
    flag "!!! failed: switch to $m_rev in $m_dir; HEAD is now $(git -C "$m_dir" rev-parse --short HEAD)"
    return 1
  fi
  m_new=$(git -C "$m_dir" rev-parse --short HEAD)
  if [ "$(git -C "$m_dir" rev-parse HEAD)" = "$m_old" ]; then
    echo "already at $m_rev ($m_new)"
  else
    echo "$4: $m_short -> $m_new ($m_rev)"
  fi
  m_rc=0
  # The command pack installs its commands from the checkout.
  if [ -f "$m_dir/bin/sd_install.py" ]; then
    echo "--- make setup"
    if ! make -C "$m_dir" setup; then
      flag "!!! failed: make setup; the checkout is at $m_new, rerun: make -C $m_dir setup"
      unlinked_python_hint
      m_rc=1
    fi
  fi
  m_s_old=$(pin_schema "$m_dir" "$m_old")
  m_s_new=$(pin_schema "$m_dir" HEAD)
  # With a refresh record, this refresh migrates the database itself.
  if [ "$3" = refresh ] && [ -e "$REFRESH_INTENT" ]; then
    :
  elif [ -n "$m_s_new" ] && [ "$m_s_old" != "$m_s_new" ]; then
    echo "note: lib/sd_db SCHEMA_VERSION ${m_s_old:-none} -> $m_s_new; the database needs a migrate."
    echo "  Stop the dashboard, the runner and sd-serve, then run:"
    echo "    $m_dir/bin/sd-db backup"
    echo "    $m_dir/bin/sd-db migrate"
    echo "  and start them again. $3 migrates nothing."
  fi
  return "$m_rc"
}

# The hub's long-running sd_db consumers, in start order (sd:3279). Each
# imports lazily from the system and pack checkouts, so none may run while
# they move: a new module in an old process serves a mixed build. Refresh
# stops them before the move and starts them after it (pack design D2,
# steps 1 and 5). REFRESH_STOPPED lists each one stopped and not started
# again, written before its bootout, so a killed refresh's rerun starts it.
CONSUMERS="sd-serve sd-dashboard"
REFRESH_STOPPED="${XDG_STATE_HOME:-$HOME/.local/state}/repo-sync/refresh-stopped"
LABEL_PREFIX="$(st_label_prefix)"

# Prints launchd's state of label $1, loaded or unloaded. 113 is launchctl's
# "could not find service"; any other failure is no answer and returns 1.
svc_state() {
  # set -e would end the caller's $( ) subshell at a 113 before the case.
  s_rc=0
  launchctl print "gui/$(id -u)/$1" > /dev/null 2>&1 || s_rc=$?
  case $s_rc in
    0) echo loaded ;;
    113) echo unloaded ;;
    *) return 1 ;;
  esac
}

# Waits up to 10 s until label $1 reads $2. bootout and bootstrap can return
# before launchd has changed the registration (the dashboard's
# _wait_unloaded, sd:3279). No answer keeps waiting, so it is never a pass.
svc_wait() {
  w_n=0
  while [ "$(svc_state "$1" || :)" != "$2" ]; do
    w_n=$((w_n + 1))
    [ "$w_n" -le 50 ] || return 1
    sleep 0.2
  done
}

# Boots out label $1 and waits until launchd has unloaded it.
svc_stop() {
  if ! launchctl bootout "gui/$(id -u)/$1"; then
    flag "!!! failed: launchctl bootout $1"
    return 1
  fi
  if ! svc_wait "$1" unloaded; then
    flag "!!! failed: $1 did not unload within 10 s of its bootout"
    return 1
  fi
}

# Boots out each loaded consumer, listed first. Returns 1 when launchd gives
# no answer, a list write fails, or one does not unload.
stop_consumers() {
  mkdir -p "$(dirname "$REFRESH_STOPPED")" || return 1
  for c in $CONSUMERS; do
    c_label="$LABEL_PREFIX.$c"
    if ! c_state=$(svc_state "$c_label"); then
      flag "!!! failed: launchctl print $c_label gave no answer"
      return 1
    fi
    [ "$c_state" = loaded ] || continue
    if ! grep -qxF "$c_label" "$REFRESH_STOPPED" 2>/dev/null; then
      { cat "$REFRESH_STOPPED" 2>/dev/null || :; echo "$c_label"; } > "$REFRESH_STOPPED.tmp" \
        && mv "$REFRESH_STOPPED.tmp" "$REFRESH_STOPPED" || return 1
    fi
    svc_stop "$c_label" || return 1
    echo "stopped $c_label"
  done
}

# Starts each listed consumer, once the database's schema is the pack lib's:
# code at another schema refuses the database. A listed one still loaded is
# the old process, so it is stopped first. A line leaves the list only once
# launchd shows the fresh load. Returns 1, the list kept, while a refresh
# record is left (its backup is a rollback point only while nothing writes),
# when the schema differs or cannot be read, or when a step fails.
start_consumers() {
  [ -s "$REFRESH_STOPPED" ] || return 0
  if [ -e "$REFRESH_INTENT" ]; then
    flag "!!! left stopped: $(tr '\n' ' ' < "$REFRESH_STOPPED")($REFRESH_INTENT is left: its migrate is not settled)"
    return 1
  fi
  c_pack=$(hub_dir pack)
  c_lib=$(lib_version "$c_pack")
  c_db=$(db_version "$REFRESH_DB" || :)
  if [ -z "$c_lib" ] || [ "$c_db" != "$c_lib" ]; then
    flag "!!! left stopped: $(tr '\n' ' ' < "$REFRESH_STOPPED")(database schema ${c_db:-unknown}, the pack's lib/ ${c_lib:-unknown})"
    schema_remedy "$c_db" "$c_lib" "$c_pack" "to start them"
    return 1
  fi
  for c in $CONSUMERS; do
    c_label="$LABEL_PREFIX.$c"
    grep -qxF "$c_label" "$REFRESH_STOPPED" || continue
    if ! c_state=$(svc_state "$c_label"); then
      flag "!!! failed: launchctl print $c_label gave no answer; left stopped, refresh again to start it"
      return 1
    fi
    if [ "$c_state" = loaded ] && ! svc_stop "$c_label"; then
      echo "  left listed; refresh again to restart it"
      return 1
    fi
    if ! launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/$c_label.plist"; then
      flag "!!! failed: launchctl bootstrap $c_label; left stopped, refresh again to start it"
      return 1
    fi
    if ! svc_wait "$c_label" loaded; then
      flag "!!! failed: $c_label did not load within 10 s of its bootstrap; left listed, refresh again to start it"
      return 1
    fi
    grep -vxF "$c_label" "$REFRESH_STOPPED" > "$REFRESH_STOPPED.tmp" || :
    mv "$REFRESH_STOPPED.tmp" "$REFRESH_STOPPED" || return 1
    echo "started $c_label"
  done
  rm -f "$REFRESH_STOPPED"
}

# Prints the way to bring database schema $1 to the pack's lib/ schema $2,
# for pack $3, ending "refresh again $4". No migrate goes down, so a database
# above the library needs a newer pack on origin or an older backup restored
# by a library at that backup's schema; a newer one would migrate it up
# (sd:3535).
schema_remedy() {
  case "$1:$2" in
    :*|*:|*[!0-9:]*) sr_above=no ;;
    *) if [ "$1" -gt "$2" ]; then sr_above=yes; else sr_above=no; fi ;;
  esac
  if [ "$sr_above" = yes ]; then
    echo "  no migrate goes down: move the default branch of $3's origin to a commit whose lib/ is at schema $1 or later, or restore a backup at schema $2 with the sd-db of a pack whose lib/ is at schema $2, then refresh again${4:+ $4}"
  else
    echo "  after $3/bin/sd-db backup and $3/bin/sd-db migrate, refresh again${4:+ $4}"
  fi
}

# --- the refresh record (pack design D2, sd:3345) ---------------------------
# Refresh migrates the database when its schema is below the SCHEMA_VERSION of
# the pack's lib/ it moves to. It reads the database and the library at every
# refresh, never the checkout movement, so a rerun finds what a killed run
# left. Before anything moves, it backs the database up and writes this
# record: the system and pack shas, the schema version and that backup, the
# one rollback point. Every rerun reuses the record and never replaces it, so
# a failed migrate lands at the pair and version from before the first run,
# however many runs a refresh takes. It lives outside the database, which a
# restore replaces. Refresh deletes it just before it starts the services:
# once one writes, the backup is no longer a rollback point.
REFRESH_INTENT="${XDG_STATE_HOME:-$HOME/.local/state}/repo-sync/refresh-intent"
# Each refresh backup gets its own folder here, outside the backup root that
# the pack's retention prunes; prune_backups keeps the newest three.
REFRESH_BACKUPS="${XDG_STATE_HOME:-$HOME/.local/state}/repo-sync/refresh-backups"
REFRESH_DB="${REPO_SYNC_SD_DB:-$HOME/.local/share/sd/sd.db}"

# Prints the schema version, the user_version, of database file $1. Returns 1
# when it cannot be read: no answer, never a version. Not -readonly: that
# cannot open a WAL database whose -shm went with its last connection, as
# it does once the services stop. The PRAGMA writes nothing, and the -f
# check keeps sqlite3 from creating a database that is not there. A lock,
# such as an sd call's checkpoint as it closes, is waited out for up to 30 s:
# sqlite3 alone fails at once, and refresh refused (sd:4045).
db_version() {
  [ -f "$1" ] || return 1
  d_v=$(sqlite3 -cmd '.timeout 30000' "$1" 'PRAGMA user_version' 2>/dev/null) || return 1
  case $d_v in ''|*[!0-9]*) return 1 ;; esac
  echo "$d_v"
}

# Prints the SCHEMA_VERSION of the lib/sd_db checked out in pack $1, or nothing.
lib_version() {
  sed -n 's/^SCHEMA_VERSION = \([0-9][0-9]*\).*/\1/p' "$1/lib/sd_db/schema.py" 2>/dev/null | head -1
}

# Prints the rev $TMPD/plan moves checkout $1 to, else HEAD.
planned_rev() {
  p_real=$(cd "$1" && pwd -P)
  while IFS=$US read -r p_dir p_rev; do
    if [ "$(cd "$p_dir" && pwd -P)" = "$p_real" ]; then echo "$p_rev"; return 0; fi
  done < "$TMPD/plan"
  echo HEAD
}

# Reads the record into i_sys, i_pack, i_schema and i_backup. Returns 1, with
# the reason in i_why, when it cannot be read, a field is malformed, or its
# backup is missing or not at its schema version: an unknown rollback point.
read_intent() {
  if ! i_text=$(cat "$REFRESH_INTENT" 2>/dev/null); then
    i_why="$REFRESH_INTENT cannot be read"; return 1
  fi
  i_sys=$(printf '%s\n' "$i_text" | sed -n 's/^system=//p' | head -1)
  i_pack=$(printf '%s\n' "$i_text" | sed -n 's/^pack=//p' | head -1)
  i_schema=$(printf '%s\n' "$i_text" | sed -n 's/^schema=//p' | head -1)
  i_backup=$(printf '%s\n' "$i_text" | sed -n 's/^backup=//p' | head -1)
  i_why="$REFRESH_INTENT is malformed"
  printf '%s\n' "$i_pack" | grep -Eq '^[0-9a-f]{40}([0-9a-f]{24})?$' || return 1
  # Empty only on a hub with no system checkout in the conf.
  if [ -n "$i_sys" ] || [ -n "$(hub_dir system)" ]; then
    printf '%s\n' "$i_sys" | grep -Eq '^[0-9a-f]{40}([0-9a-f]{24})?$' || return 1
  fi
  case $i_schema in ''|*[!0-9]*) return 1 ;; esac
  [ -n "$i_backup" ] || return 1
  if ! i_got=$(db_version "$i_backup/sd.db"); then
    i_why="its backup $i_backup/sd.db is missing or unreadable"; return 1
  fi
  if [ "$i_got" != "$i_schema" ]; then
    i_why="its backup $i_backup reads schema $i_got, not the record's $i_schema"; return 1
  fi
}

# Deletes the record; returns 1 when it is still there.
forget_intent() {
  rm -f "$REFRESH_INTENT"
  if [ -e "$REFRESH_INTENT" ]; then
    flag "!!! failed: cannot delete $REFRESH_INTENT"
    return 1
  fi
  echo "refresh: deleted $REFRESH_INTENT"
  prune_backups
}

# Removes all but the newest three refresh backup folders, named for their
# UTC time, so they sort oldest first (sd:4022). Only with no record left:
# a record names the backup of a refresh not yet done, the newest folder.
prune_backups() {
  [ ! -e "$REFRESH_INTENT" ] || return 0
  b_n=0
  for b_dir in "$REFRESH_BACKUPS"/[0-9]*Z-*; do
    if [ -d "$b_dir" ]; then b_n=$((b_n + 1)); fi
  done
  for b_dir in "$REFRESH_BACKUPS"/[0-9]*Z-*; do
    [ "$b_n" -gt 3 ] || break
    [ -d "$b_dir" ] || continue
    b_n=$((b_n - 1))
    if rm -rf "$b_dir"; then
      echo "refresh: removed the old backup $b_dir"
    else
      echo "note: cannot remove the old backup $b_dir"
    fi
  done
}

# Writes the record: system sha $1, pack sha $2, schema $3, backup $4. The
# rename replaces it whole, so a kill leaves the old record or the new one.
write_intent() {
  { mkdir -p "${REFRESH_INTENT%/*}" \
    && printf 'system=%s\npack=%s\nschema=%s\nbackup=%s\n' "$1" "$2" "$3" "$4" > "$REFRESH_INTENT.tmp" \
    && mv -f "$REFRESH_INTENT.tmp" "$REFRESH_INTENT"; } 2> /dev/null
}

# Design D2 step 2, with the services stopped and the moves planned. Returns
# 0 to move: a record is left and checks out, none is needed, or the backup
# and the record were written. Returns 1, nothing moved and no record, so the
# services start again; 2, the rollback point unknown, so they stay stopped.
rollback_point() {
  if [ -e "$REFRESH_INTENT" ]; then
    if ! read_intent; then
      flag "!!! refused: $REFRESH_INTENT is left by a migrate that did not finish, and its rollback point is unknown: $i_why"
      echo "  nothing moved; the services stay stopped and no hub-pin goes out; put the database and checkouts right by hand, then delete $REFRESH_INTENT"
      return 2
    fi
    echo "refresh: $REFRESH_INTENT is left: system ${i_sys:-none}, pack $i_pack, schema $i_schema, backup $i_backup; reused, no new backup"
    return 0
  fi
  p_pack=$(hub_dir pack)
  [ -n "$p_pack" ] || return 0
  p_rev=$(planned_rev "$p_pack")
  p_lib=$(pin_schema "$p_pack" "$p_rev")
  if [ -z "$p_lib" ] || [ ! -e "$REFRESH_DB" ]; then return 0; fi
  if ! p_db=$(db_version "$REFRESH_DB"); then
    flag "!!! refused: cannot read the schema version of $REFRESH_DB, so whether it needs a migrate is unknown; nothing moved"
    return 1
  fi
  if [ "$p_db" -gt "$p_lib" ]; then
    flag "!!! refused: $REFRESH_DB is at schema $p_db, above the $p_lib of the pack's lib/ at $p_rev; nothing moved"
    schema_remedy "$p_db" "$p_lib" "$p_pack" ""
    return 1
  fi
  [ "$p_db" -lt "$p_lib" ] || return 0
  p_root="$REFRESH_BACKUPS/$(date -u +%Y%m%dT%H%M%SZ)-$$"
  echo "--- $p_pack/bin/sd-db backup: the database is at schema $p_db, the pack's lib/ at $p_rev at $p_lib"
  if ! "$p_pack/bin/sd-db" backup --destination "$p_root" --no-row-prune < /dev/null; then
    flag "!!! refused: the backup before the migrate failed; nothing moved, no record written"
    return 1
  fi
  p_backup=""
  for p_found in "$p_root"/*; do
    [ -d "$p_found" ] || continue
    [ -z "$p_backup" ] || { p_backup=""; break; }
    p_backup=$p_found
  done
  if [ -z "$p_backup" ] || [ "$(db_version "$p_backup/sd.db" || :)" != "$p_db" ]; then
    flag "!!! refused: $p_root holds no one backup at schema $p_db; nothing moved, no record written"
    return 1
  fi
  # A sha that cannot be read is no rollback point: an empty one would skip
  # that checkout's move back.
  p_sys=$(hub_dir system)
  p_sys_sha=""
  if { [ -n "$p_sys" ] && ! p_sys_sha=$(git -C "$p_sys" rev-parse HEAD); } \
      || ! p_pack_sha=$(git -C "$p_pack" rev-parse HEAD); then
    flag "!!! refused: cannot read the system or pack sha; nothing moved, no record written; the backup $p_backup stays"
    return 1
  fi
  if ! write_intent "$p_sys_sha" "$p_pack_sha" "$p_db" "$p_backup"; then
    flag "!!! refused: cannot write $REFRESH_INTENT; nothing moved; the backup $p_backup stays"
    return 1
  fi
  echo "refresh: wrote $REFRESH_INTENT: system ${p_sys_sha:-none}, pack $p_pack_sha, schema $p_db, backup $p_backup"
}

# Moves checkout $1 back to sha $2, detached.
back_to() {
  [ "$(git -C "$1" rev-parse HEAD 2>/dev/null)" != "$2" ] || return 0
  if git -C "$1" switch -q --detach --no-overwrite-ignore "$2"; then
    echo "refresh: put $1 back at $(git -C "$1" rev-parse --short HEAD)"
    return 0
  fi
  flag "!!! failed: cannot put $1 back at $2; by hand: git -C '$1' switch --detach $2"
  return 1
}

# Design D2 step 4's recovery: puts the database and both checkouts back at
# the record. The pack goes first, so the restore runs the lib/ that took the
# backup: the target's would migrate the restored copy up through the
# migration that just failed. Then the restore, when the database reads above
# the record's version; then system and make setup. Deletes the record once
# the database is at the checked-out library's schema. Returns 1 at the first
# step that fails or cannot tell, with the record kept.
recover() {
  v_pack=$(hub_dir pack)
  v_sys=$(hub_dir system)
  back_to "$v_pack" "$i_pack" || return 1
  if ! v_db=$(db_version "$REFRESH_DB"); then
    flag "!!! failed: cannot read the schema version of $REFRESH_DB; by hand: $v_pack/bin/sd-db restore '$i_backup'"
    return 1
  fi
  if [ "$v_db" -gt "$i_schema" ]; then
    # sd_db's restore verb, from this lib/ alone: bin/sd-db would take a
    # provisioned copy that matches the partly migrated database, and that
    # copy migrates the restored one up to its own version.
    echo "--- sd_db restore $i_backup from $v_pack/lib: schema $v_db back to $i_schema"
    if ! PYTHONPATH="$v_pack/lib" python3 -P -m sd_db.jobs.cli restore "$i_backup" < /dev/null; then
      flag "!!! failed: the restore of $i_backup"
      return 1
    fi
    v_db=$(db_version "$REFRESH_DB" || :)
    if [ "$v_db" != "$i_schema" ]; then
      flag "!!! failed: the restore left $REFRESH_DB at schema ${v_db:-unknown}, not $i_schema"
      return 1
    fi
    echo "note: the runner dispatches nothing until \`sd restore resume\` clears the restore record"
  elif [ "$v_db" -lt "$i_schema" ]; then
    flag "!!! failed: $REFRESH_DB reads schema $v_db, below the record's $i_schema"
    return 1
  fi
  if [ -n "$i_sys" ] && [ -n "$v_sys" ]; then back_to "$v_sys" "$i_sys" || return 1; fi
  if [ -f "$v_pack/bin/sd_install.py" ]; then
    echo "--- make setup at $(git -C "$v_pack" rev-parse --short HEAD)"
    if ! make -C "$v_pack" setup; then
      flag "!!! failed: make setup at the record's pack; by hand: make -C '$v_pack' setup"
      unlinked_python_hint
      return 1
    fi
  fi
  if [ "$(lib_version "$v_pack")" != "$i_schema" ]; then
    flag "!!! failed: the record's pack $i_pack holds lib/ schema $(lib_version "$v_pack"), not the database's $i_schema"
    return 1
  fi
  forget_intent
}

# Design D2 step 4, first half (sd:3507): the services stop and the lanes
# drain, but cron reports and sd calls still write the database with the old
# lib/ until the pack moves; once it has, the new lib/ refuses to write an
# older schema. So the moved pack backs the database up again, read-only,
# into the record's folder: the newest snapshot, which the migrate checks
# against the database. At the record's schema the record names it, so
# recover restores it; above, a migrate a kill cut short, the record keeps
# its own. A kill here leaves the database as it was and the old record or
# the new one. Returns 1, with the record as it was, on a failure.
fresh_point() {
  f_root=${i_backup%/*}
  if ! f_before=$(ls "$f_root"); then
    flag "!!! failed: cannot list $f_root"
    return 1
  fi
  echo "--- $s_pack/bin/sd-db backup: again, with the moved pack, right before the migrate"
  if ! "$s_pack/bin/sd-db" backup --destination "$f_root" --no-row-prune < /dev/null; then
    flag "!!! failed: the second backup into $f_root"
    return 1
  fi
  f_backup=""
  for f_found in "$f_root"/*; do
    [ -d "$f_found" ] || continue
    if printf '%s\n' "$f_before" | grep -qxF "${f_found##*/}"; then continue; fi
    [ -z "$f_backup" ] || { f_backup=""; break; }
    f_backup=$f_found
  done
  if [ -z "$f_backup" ] || [ "$(db_version "$f_backup/sd.db" || :)" != "$s_db" ]; then
    flag "!!! failed: $f_root holds no one new backup at schema $s_db"
    return 1
  fi
  if [ "$s_db" != "$i_schema" ]; then
    echo "refresh: the database is at schema $s_db, above the record's $i_schema; the record keeps $i_backup"
    return 0
  fi
  if ! write_intent "$i_sys" "$i_pack" "$i_schema" "$f_backup"; then
    flag "!!! failed: cannot write $REFRESH_INTENT; it still names $i_backup"
    return 1
  fi
  i_backup=$f_backup
  echo "refresh: $REFRESH_INTENT names the second backup $f_backup"
}

# Design D2 steps 4 and 5, with a record left: migrates the database to the
# checked-out pack's schema, or recovers to the record. Returns 0 there with
# the record deleted; 1 recovered, record deleted, as a failed migrate; 2
# stuck, the record and the stopped services kept for the operator.
settle() {
  if ! read_intent; then
    flag "!!! failed: $i_why"
    return 2
  fi
  s_pack=$(hub_dir pack)
  s_lib=$(lib_version "$s_pack")
  s_db=$(db_version "$REFRESH_DB" || :)
  s_ran=0
  if [ -n "$s_lib" ] && [ -n "$s_db" ] && [ "$s_db" -lt "$s_lib" ]; then
    if ! fresh_point; then
      echo "refresh: no migrate"
      s_ran=1
    else
      echo "--- $s_pack/bin/sd-db migrate: schema $s_db to $s_lib"
      # A failed migrate recovers whatever the version reads: its exit is
      # the one proof the run ended where it meant to. The pack's migrate
      # wants a verified snapshot under its backup root (sd:3507); the
      # record's backup sits outside that root, so its folder is named here.
      SD_DB_BACKUP_ROOT="${i_backup%/*}" "$s_pack/bin/sd-db" migrate < /dev/null || s_ran=$?
      s_db=$(db_version "$REFRESH_DB" || :)
      if [ "$s_ran" -ne 0 ]; then
        flag "!!! failed: $s_pack/bin/sd-db migrate exited $s_ran"
      fi
    fi
  fi
  if [ "$s_ran" -eq 0 ] && [ -n "$s_lib" ] && [ "$s_db" = "$s_lib" ]; then
    forget_intent || return 2
    return 0
  fi
  echo "refresh: the database reads schema ${s_db:-unknown}, the pack's lib/ ${s_lib:-unknown}; recovering to $REFRESH_INTENT"
  recover || return 2
  return 1
}

# Design D2 step 6: satellites follow only a hub whose database is at its
# pack's schema, so the version is read again before hub-pin.
pin_ready() {
  q_lib=$(lib_version "$(hub_dir pack)")
  if [ -z "$q_lib" ] || [ ! -e "$REFRESH_DB" ]; then return 0; fi
  q_db=$(db_version "$REFRESH_DB" || :)
  [ "$q_db" != "$q_lib" ] || return 0
  flag "!!! failed: hub-pin not published: the database reads schema ${q_db:-unknown}, the pack's lib/ $q_lib"
  schema_remedy "$q_db" "$q_lib" "$(hub_dir pack)" "to publish it"
  return 1
}

# Refreshes each path given, or with none every pinned checkout in the conf.
refresh() {
  : > "$TMPD/targets"
  if [ "$#" -gt 0 ]; then
    for r_arg in "$@"; do printf '%s\n' "$r_arg" >> "$TMPD/targets"; done
  else
    repo_list | while read -r subdir full_repo; do
      target="$ROOT/$subdir/${full_repo##*/}"
      if [ -d "$target/.git" ] && is_pinned "$target"; then
        echo "$target" >> "$TMPD/targets"
      fi
    done
    if [ ! -s "$TMPD/targets" ]; then
      echo "no pinned checkouts in the conf; nothing to refresh"
      return 0
    fi
  fi
  # System moves first, whatever the conf or argument order. The reason, the
  # pack's make setup installing sd_db from system (sd:3218), went with the
  # move into the pack (sd:2997); the order stays because it costs nothing.
  while IFS= read -r r_target; do
    [ "$(hub_checkout "$r_target" || true)" != system ] || printf '%s\n' "$r_target"
  done < "$TMPD/targets" > "$TMPD/ordered"
  while IFS= read -r r_target; do
    [ "$(hub_checkout "$r_target" || true)" = system ] || printf '%s\n' "$r_target"
  done < "$TMPD/targets" >> "$TMPD/ordered"
  # r_failed counts every failed step, for the exit code. r_hub_failed
  # counts those that leave the hub's own state unknown: hub-pin must name
  # what the hub serves, so only these withhold it (sd:3710). Another
  # checkout's failure, or a service that did not start again, leaves system
  # and the pack where they are, and the satellites follow them.
  r_failed=0
  r_hub_failed=0
  : > "$TMPD/plan"
  # Every check and fetch runs before the services stop (sd:3535):
  # refresh_one only plans, so a refusal or a slow fetch costs no outage.
  # The list is on fd 3, so a git or make that reads stdin cannot eat it.
  while IFS= read -r r_target <&3; do
    if ! refresh_one "$r_target"; then
      r_failed=$((r_failed + 1))
      if hub_checkout "$r_target" > /dev/null; then r_hub_failed=$((r_hub_failed + 1)); fi
    fi
  done 3< "$TMPD/ordered"
  echo
  # System and the pack move together or not at all: one refused keeps the
  # other where it is.
  if [ "$r_hub_failed" -gt 0 ]; then
    : > "$TMPD/plan.kept"
    while IFS=$US read -r r_dir r_rev; do
      if hub_checkout "$r_dir" > /dev/null; then
        flag "!!! held: $r_dir stays at $(git -C "$r_dir" rev-parse --short HEAD): the other hub checkout failed"
      else
        printf '%s\n' "$r_dir$US$r_rev" >> "$TMPD/plan.kept"
      fi
    done < "$TMPD/plan"
    mv "$TMPD/plan.kept" "$TMPD/plan"
  fi
  # The services stop only for a planned move of system or the pack.
  r_hub=""
  while IFS=$US read -r r_dir r_rev; do
    if hub_checkout "$r_dir" > /dev/null; then r_hub=1; fi
  done < "$TMPD/plan"
  # A record left by a refresh that did not finish is settled first,
  # whatever this one moves: nothing may start the services before that.
  [ ! -e "$REFRESH_INTENT" ] || r_hub=1
  if [ -n "$r_hub" ] && ! stop_consumers; then
    flag "!!! refused: a library consumer did not stop; nothing moved"
    r_failed=$((r_failed + 1)) r_hub_failed=$((r_hub_failed + 1))
  else
    r_point=0
    if [ -n "$r_hub" ]; then rollback_point || r_point=$?; fi
    if [ "$r_point" -ne 0 ]; then
      r_failed=$((r_failed + 1)) r_hub_failed=$((r_hub_failed + 1))
    else
      while IFS=$US read -r r_dir r_rev <&3; do
        if ! pin_move "$r_dir" "$r_rev" refresh refreshed; then
          r_failed=$((r_failed + 1))
          if hub_checkout "$r_dir" > /dev/null; then r_hub_failed=$((r_hub_failed + 1)); fi
        fi
        echo
      done 3< "$TMPD/plan"
      if [ -e "$REFRESH_INTENT" ]; then
        r_settle=0
        settle || r_settle=$?
        if [ "$r_settle" -ne 0 ]; then r_failed=$((r_failed + 1)) r_hub_failed=$((r_hub_failed + 1)); fi
        if [ "$r_settle" -eq 2 ]; then
          flag "!!! the services stay stopped and $REFRESH_INTENT stays, so no hub-pin goes out; refresh again to retry, or put the database and checkouts right by hand"
        fi
      fi
    fi
  fi
  # A failed move still starts them: the checkouts stay where git left them.
  # A record left keeps them stopped.
  start_consumers || r_failed=$((r_failed + 1))
  if [ "$r_hub_failed" -eq 0 ] && ! pin_ready; then
    r_failed=$((r_failed + 1)) r_hub_failed=$((r_hub_failed + 1))
  fi
  if [ "$r_hub_failed" -eq 0 ] && ! push_pins; then
    r_failed=$((r_failed + 1))
  fi
  echo "----------------------------------------"
  if [ "$r_failed" -gt 0 ]; then
    echo "refresh : $r_failed step(s) failed"
    { cat "$TMPD/flagged" 2> /dev/null || :; echo "refresh : $r_failed step(s) failed"; } >&2
    return 1
  fi
  echo "refresh : done"
}

# Prints "<dir>" for the conf checkout that is hub checkout $1 (system or
# pack), or nothing.
hub_dir() {
  repo_list | while read -r subdir full_repo; do
    target="$ROOT/$subdir/${full_repo##*/}"
    if [ -d "$target/.git" ] && [ "$(hub_checkout "$target" || true)" = "$1" ]; then
      echo "$target"; break
    fi
  done
}

# After a hub refresh whose system and pack steps all passed, publishes the
# pinned system and pack shas as one annotated tag, hub-pin, on the system
# origin: the tag names the system sha, and its message carries `pack=<sha>`.
# One push publishes both,
# so a satellite never sees one without the other (sd:3100). The `+` lets
# that one tag move backwards; nothing else is forced.
push_pins() {
  p_sys=$(hub_dir system)
  if [ -z "$p_sys" ] || ! is_pinned "$p_sys"; then return 0; fi
  p_pack=$(hub_dir pack)
  if [ -z "$p_pack" ] || ! is_pinned "$p_pack"; then
    flag "!!! failed: hub-pin not published: system is pinned and no pinned pack is in the conf; satellites keep the old pins; pin the pack, then run refresh again"
    return 1
  fi
  # The fence (sd:4010): a returning old hub publishes no pins. `sd-db fence`
  # exits 0 on the hub role's holder, or with no sd.hub_name set; anything
  # else, a pack without the verb included, withholds the tag.
  if ! p_fence=$(bound_step 120 "$p_pack/bin/sd-db" fence < /dev/null 2>&1); then
    echo "!!! refused: hub-pin not published: this machine may not be the hub: $(printf '%s\n' "$p_fence" | grep -v '^\[step ' | tail -1); satellites keep the old pins (a pack before sd:4044 has no sd-db fence: move its pin)"
    return 1
  fi
  p_sha=$(git -C "$p_sys" rev-parse HEAD)
  p_pack_sha=$(git -C "$p_pack" rev-parse HEAD)
  if git -C "$p_sys" tag -f -a -m "hub-pin: the system and pack a hub refresh moved to (sd:3100)" \
      -m "pack=$p_pack_sha" hub-pin "$p_sha" > /dev/null \
      && bound_step 300 git -C "$p_sys" push -q origin +refs/tags/hub-pin < /dev/null; then
    echo "hub-pin : system $(git -C "$p_sys" rev-parse --short HEAD), pack $(git -C "$p_pack" rev-parse --short HEAD), for satellites"
  else
    flag "!!! failed: push of the hub-pin tag in $p_sys; satellites keep the old pins; run refresh again"
    return 1
  fi
}

# Holds the lanes through refresh_drain.py, which runs verb $1 of this
# script again as its child, with REPO_SYNC_LANES_HELD=1 (sd:3099).
drain_exec() {
  # A lane folder is named after its checkout: the drain holds these
  # lanes too, before a first runner makes their folder.
  REPO_SYNC_LANE_NAMES=$(repo_list | while read -r subdir full_repo; do printf '%s ' "${full_repo##*/}"; done)
  export REPO_SYNC_LANE_NAMES
  # exec runs no EXIT trap; the child reads its own list.
  rm -rf "$TMPD"
  exec python3 "$DIR/refresh_drain.py" "$DIR/repo-sync.sh" "$@"
}

# The intent marker (sd:3100, operator ruling): written before any move and
# deleted only once make setup and the machine-setup update are proven at the
# pin (sd:3168). A killed or rolled-back follow leaves it, so the next run
# finishes, sets up and updates again.
FOLLOW_INTENT="${XDG_STATE_HOME:-$HOME/.local/state}/repo-sync/follow-intent"

# The words machine-setup's `status` counts as drift (its status_stage grep);
# test_follow holds the two equal.
FOLLOW_DRIFT='DIFFERS|MISSING|STALE|ABSENT|UNLOADED|EXTRA|defaults (-currentHost )?write|^  extra '

# No drift is no proof: a SKIP, a hub that did not answer or a check that
# stopped part way prints no drift word. So each stage's dry run must also
# print the line it prints only once it checked the end state: bin-links
# status's last line, and the satellite's line for a build the hub accepted.
# test_follow holds both to what the stages print.
FOLLOW_PROOF_bin='^  PATH +on PATH$'
FOLLOW_PROOF_satellite='^  ok +sd_db .* matches the hub'

# Queues checkout $3 ($1, system or pack) to move to sha $2 when it is not
# already there and is clean, or a pack at $2 for `make setup` while the
# intent marker is left; counts a dirty one in f_failed. Fields split on
# $US, so a path with spaces stays whole. Its status lines wait in
# $TMPD/says: a pass that drains runs follow again as its own child, and the
# child prints them, so printing them in both passes showed each twice and
# lost the pack line behind them (sd:4721).
follow_want() {
  if is_pinned "$3" && [ "$(git -C "$3" rev-parse HEAD)" = "$2" ]; then
    if [ "$1" = pack ] && [ -e "$FOLLOW_INTENT" ]; then
      echo "follow: pack at the hub's pin, but a follow stopped before its make setup finished, or its machine-setup update failed ($FOLLOW_INTENT); setting up again" >> "$TMPD/says"
      printf '%s\n' "setup$US$2$US$3" >> "$TMPD/moves"
    else
      echo "follow: $1 already at the hub's pin $(git -C "$3" rev-parse --short HEAD)" >> "$TMPD/says"
    fi
  elif [ -n "$(git -C "$3" status --porcelain --untracked-files=no 2>/dev/null || echo unreadable)" ]; then
    flag "!!! refused: $3 has uncommitted changes; commit or stash them"
    f_failed=$((f_failed + 1))
  else
    printf '%s\n' "$1$US$2$US$3" >> "$TMPD/moves"
  fi
}

# Runs system checkout $1's `agent-prompt.sh refresh --apply`, bounded at
# 120 s, after a follow whose move changed shared.md (sd:3262). A refusal
# (a DIFFERS or UNKNOWN target) or a failure leaves those targets as they
# were; it names them and the commands to run by hand, and the move stands.
follow_prompt() {
  p_ap="$1/local-agent-prompt/agent-prompt.sh"
  [ -f "$p_ap" ] || return 0
  echo "--- agent-prompt.sh refresh --apply"
  if st_bounded 120 sh "$p_ap" refresh --apply < /dev/null > "$TMPD/prompt" 2>&1; then
    cat "$TMPD/prompt"
    return 0
  fi
  cat "$TMPD/prompt"
  p_refused=$(sed -nE 's/^  (DIFFERS|UNKNOWN) ([^ ]+) .*/\2/p' "$TMPD/prompt" | tr '\n' ' ')
  p_refused=${p_refused% }
  if [ -n "$p_refused" ]; then p_what="refused $p_refused"; else p_what=failed; fi
  flag "!!! agent-prompt.sh refresh --apply $p_what; the move stands; by hand: sh '$p_ap' diff, then capture <target> --apply or refresh --force"
}

# On a satellite, moves system and pack to the shas of the hub-pin tag on the
# system origin, never to origin's default branch (sd:3100). Every check runs
# before any move, so a refusal moves nothing; a failed move puts back every
# checkout this run moved, so the two never stay out of step.
follow() {
  if ! is_satellite; then
    echo "follow: this machine is the hub (no $HOME/.config/sd/hub.json); nothing to do, refresh moves its checkouts"
    return 0
  fi
  f_sys=$(hub_dir system)
  if [ -z "$f_sys" ]; then
    echo "follow: no system checkout in the conf, and the hub-pin tag lives there; nothing to follow"
    return 0
  fi
  # The error named below is git's last line, not the step's own. The local
  # tag moves too, even backwards: promote's hub-ready reads it (sd:4683).
  if ! bound_step 300 git -C "$f_sys" fetch -q origin +refs/tags/hub-pin:refs/tags/hub-pin < /dev/null 2> "$TMPD/err"; then
    f_rc=0
    bound_step 120 git -C "$f_sys" ls-remote --exit-code origin refs/tags/hub-pin \
      < /dev/null > /dev/null 2>&1 || f_rc=$?
    if [ "$f_rc" -eq 2 ]; then
      echo "follow: the system origin has no hub-pin tag yet; nothing to follow"
      return 0
    fi
    flag "!!! refused: cannot fetch the hub-pin tag in $f_sys: $(grep -v '^\[step ' "$TMPD/err" | tail -1)"
    echo "follow  : refused, nothing moved"
    return 1
  fi
  f_tag=$(git -C "$f_sys" rev-parse FETCH_HEAD)
  f_sys_sha=$(git -C "$f_sys" rev-parse "$f_tag^{commit}")
  f_pack_sha=$(git -C "$f_sys" cat-file tag "$f_tag" 2>/dev/null | sed -n 's/^pack=//p' | head -1)
  if ! printf '%s\n' "$f_pack_sha" | grep -Eq '^[0-9a-f]{40}([0-9a-f]{24})?$'; then
    flag "!!! refused: the hub-pin tag carries no pack=<sha> line, so the pair is unknown"
    echo "follow  : refused, nothing moved"
    return 1
  fi
  : > "$TMPD/moves"
  : > "$TMPD/says"
  f_failed=0
  follow_want system "$f_sys_sha" "$f_sys"
  f_pack=$(hub_dir pack)
  if [ -z "$f_pack" ]; then
    echo "follow: no pack checkout in the conf; skipped" >> "$TMPD/says"
  elif ! git -C "$f_pack" cat-file -e "$f_pack_sha^{commit}" 2>/dev/null \
      && ! bound_step 300 git -C "$f_pack" fetch -q origin "$f_pack_sha" < /dev/null 2> "$TMPD/err"; then
    flag "!!! refused: cannot fetch the hub's pack pin $f_pack_sha in $f_pack: $(grep -v '^\[step ' "$TMPD/err" | tail -1)"
    f_failed=$((f_failed + 1))
  else
    follow_want pack "$f_pack_sha" "$f_pack"
  fi
  if [ "$f_failed" -gt 0 ]; then
    cat "$TMPD/says"
    echo "follow  : refused, nothing moved"
    return 1
  fi
  if [ ! -s "$TMPD/moves" ]; then
    # Nothing to move or set up (no pack in the conf). A marker left is still
    # an update not proven, so it runs the stages again.
    [ -e "$FOLLOW_INTENT" ] || { cat "$TMPD/says"; return 0; }
    echo "follow: at the hub's pin, but a follow's update was not proven ($FOLLOW_INTENT); updating again" >> "$TMPD/says"
  fi
  if [ "${REPO_SYNC_LANES_HELD:-}" != 1 ]; then
    drain_exec follow
  fi
  cat "$TMPD/says"
  # A marker left means an earlier run may have moved system past a
  # shared.md change and stopped before its refresh: refresh again.
  f_prompt=0
  [ ! -e "$FOLLOW_INTENT" ] || f_prompt=1
  mkdir -p "${FOLLOW_INTENT%/*}" && printf 'system=%s\npack=%s\n' "$f_sys_sha" "$f_pack_sha" \
    > "$FOLLOW_INTENT.tmp" && mv -f "$FOLLOW_INTENT.tmp" "$FOLLOW_INTENT" \
    || { flag "!!! refused: cannot write $FOLLOW_INTENT; nothing moved"; return 1; }
  : > "$TMPD/moved"
  f_back=1
  while IFS=$US read -r f_name f_sha f_dir <&3; do
    echo "=== follow $f_name $f_dir"
    if [ "$f_name" = setup ]; then
      echo "--- make setup"
      if make -C "$f_dir" setup; then continue; fi
      flag "!!! failed: make setup; by hand: make -C '$f_dir' setup"
      unlinked_python_hint
      f_failed=1
      f_back=0
      break
    fi
    f_old=$(git -C "$f_dir" rev-parse HEAD)
    printf '%s\n' "$f_old$US$f_dir" >> "$TMPD/moved"
    if ! pin_move "$f_dir" "$f_sha" follow followed; then
      f_failed=1
      break
    fi
    # git diff: 1 is a change; 2 or more cannot tell, so it counts as one.
    if [ "$f_name" = system ] \
        && ! git -C "$f_dir" diff --quiet "$f_old" HEAD -- local-agent-prompt/prompt/shared.md; then
      f_prompt=1
    fi
  done 3< "$TMPD/moves"
  echo "----------------------------------------"
  # The two update stages that need the locks run here, with the lanes still
  # held, so they never run under a lane run (sd:3168): satellite writes the
  # hub's hub.json and providers.yaml, and bin relinks the commands. Neither
  # loads or unloads a LaunchAgent. A full update would not do: its cron and agents stages
  # reinstall this job's own LaunchAgent, and launchd then boots out the
  # running follow mid-update. Those stages run in the job's chained update,
  # after this. A failure leaves the move and the marker: the next run sets
  # up and updates again. Each stage and its check share one 450 s bound, so
  # both stay inside the job's limit after the drain's 45 minutes.
  # The exit alone does not say the stage worked: bin pipes bin-links through
  # sed, and the satellite stage reports a failed install as a line. So each
  # --apply is followed by the stage's dry run, and a drift word there fails
  # the stage as a failed exit does; a check that cannot answer fails it too,
  # and so does one without the stage's proof line (FOLLOW_PROOF_*).
  if [ "$f_failed" -eq 0 ]; then
    f_ms="$f_sys/local-machine-setup/machine-setup.sh"
    for f_stage in bin satellite; do
      echo "--- machine-setup update $f_stage --apply, then its dry run"
      f_why=
      if [ "$f_stage" = bin ]; then f_proof=$FOLLOW_PROOF_bin; else f_proof=$FOLLOW_PROOF_satellite; fi
      if st_bounded 450 sh -c 'sh "$1" update "$2" --apply && sh "$1" update "$2" > "$3"' \
          sh "$f_ms" "$f_stage" "$TMPD/check" < /dev/null; then
        # grep: 0 is a match, 1 none, 2 an unreadable check (a failure).
        f_rc=0
        grep -E "$FOLLOW_DRIFT" "$TMPD/check" > "$TMPD/drift" || f_rc=$?
        if [ "$f_rc" -eq 0 ]; then
          cat "$TMPD/drift"
          f_why=" left drift, named above"
        elif [ "$f_rc" -eq 1 ]; then
          grep -Eq "$f_proof" "$TMPD/check" && continue
          sed 's/^/    /' "$TMPD/check"
          f_why=" did not prove its end state, printed above"
        fi
      fi
      flag "!!! failed: machine-setup update $f_stage$f_why; the checkouts stay at the hub's pin; by hand: sh '$f_ms' update $f_stage --apply"
      echo "follow  : moved, update failed; $FOLLOW_INTENT stays, so the next run sets up and updates again"
      return 1
    done
    [ "$f_prompt" = 0 ] || follow_prompt "$f_sys"
    rm -f "$FOLLOW_INTENT"
    echo "follow  : done"
    return 0
  fi
  # Back to the old pair: the next run sees both off their pins and retries.
  # The pack's make setup runs again at the old sha, so the commands it
  # installs match its HEAD again. The pack goes back first, the reverse of
  # the moves: with system still at the pin, that setup finds no installed
  # sd_db newer than system's, so it refuses nothing (sd:3218).
  sed -n '1!G;h;$p' "$TMPD/moved" > "$TMPD/back"
  while IFS=$US read -r f_old f_dir <&3; do
    if [ "$(git -C "$f_dir" rev-parse HEAD)" = "$f_old" ]; then continue; fi
    f_setup=
    if [ -f "$f_dir/bin/sd_install.py" ]; then f_setup=" && make -C '$f_dir' setup"; fi
    if ! git -C "$f_dir" switch -q --detach --no-overwrite-ignore "$f_old"; then
      flag "!!! failed: cannot put $f_dir back; by hand: git -C '$f_dir' switch --detach $f_old$f_setup"
      f_back=0
      continue
    fi
    echo "follow: put $f_dir back at $(git -C "$f_dir" rev-parse --short HEAD)"
    if [ -n "$f_setup" ]; then
      echo "--- make setup at the old sha"
      if ! make -C "$f_dir" setup; then
        flag "!!! failed: make setup at the old sha; the pack's commands may be from the hub's pin; by hand: make -C '$f_dir' setup"
        unlinked_python_hint
        f_back=0
      fi
    fi
  done 3< "$TMPD/back"
  # The marker stays either way: the rollback's make setup at the old sha
  # proves no update, and only that proof deletes it.
  if [ "$f_back" = 1 ]; then
    echo "follow  : failed; each checkout it moved is back at its old sha, so a migrate note above does not apply; the next run retries"
  else
    echo "follow  : failed; $FOLLOW_INTENT stays, so the next run finishes the move and sets up again"
  fi
  return 1
}

# --- hygiene (sd:1987) ------------------------------------------------------
# Sweeps what agents leave behind in each conf checkout: worktree
# registrations whose directory is gone, locks held by a dead process,
# remote-tracking refs for deleted remote branches, local branches whose
# content is already on the default branch, and old lane files under the bulk
# storage root. It acts only with --apply, and only on those classes;
# everything else it lists. It reads the same fleet as sync and uses git
# plumbing only, so it runs offline except for the remote prune. It never
# touches a stash, a remote branch, the default branch, a dirty worktree, or
# a worktree some process has its cwd or an open file in.
#
# Loops run in pipelines (subshells), so findings are counted through files
# in $HYG_TMP: acted, found (would act with --apply), listed, failed.

# A field separator that is not whitespace, so `read` keeps empty fields.
US=$(printf '\037')

# acted holds each action's line, so nightly can leave them out of its
# attention lines (sd:4914); the other files count lines only.
hyg_act()    { echo "  $*"; echo "  $*" >> "$HYG_TMP/acted"; }
hyg_found()  { echo "  would $*"; echo x >> "$HYG_TMP/found"; }
hyg_list()   { echo "  $*"; echo x >> "$HYG_TMP/listed"; }
hyg_fail()   { echo "  FAILED $*"; echo x >> "$HYG_TMP/failed"; }
hyg_note()   { echo "  note: $*"; }

# Prints "<name> <compare-ref>" for the default branch: origin/HEAD first,
# else a local main or master. The compare ref is the remote-tracking ref
# when there is one, so a branch counts as landed only once it is upstream;
# a local compare ref serves the report-only classes but deletes nothing.
hyg_default() {
  h_ref=$(git -C "$1" symbolic-ref -q --short refs/remotes/origin/HEAD 2>/dev/null || true)
  h_name=""
  if [ -n "$h_ref" ]; then
    h_name=${h_ref#origin/}
  elif git -C "$1" show-ref -q --verify refs/heads/main; then
    h_name=main
  elif git -C "$1" show-ref -q --verify refs/heads/master; then
    h_name=master
  fi
  [ -n "$h_name" ] || return 1
  if git -C "$1" show-ref -q --verify "refs/remotes/origin/$h_name"; then
    echo "$h_name refs/remotes/origin/$h_name"
  else
    echo "$h_name refs/heads/$h_name"
  fi
}

# Prints how <branch> landed on <compare-ref> and returns 0, or returns 1.
# Three ways: the tip is an ancestor; the branch tree equals its merge base
# (nothing left to land); or a synthetic squash of the branch onto its merge
# base is patch-equivalent to a commit on the default branch, which is what a
# squash merge leaves. The synthetic commit goes to a throwaway object
# directory, with the repository's objects as an alternate, so even report
# mode writes nothing into the checkout. The identity is inline so the probe
# needs no user config.
#
# `git cherry` compares patch ids, which ignore whitespace, so a branch that
# differs from what landed only in whitespace would pass it. A cherry match is
# therefore confirmed with `git patch-id --verbatim` against every commit on
# the default branch since the merge base; without that confirmation the
# branch stays. Prefixes, renames and colour are pinned so user config cannot
# make the two sides differ.
hyg_merged() {
  if git -C "$1" merge-base --is-ancestor "$2" "$3" 2>/dev/null; then
    echo "ancestor"; return 0
  fi
  h_mb=$(git -C "$1" merge-base "$3" "$2" 2>/dev/null) || return 1
  h_bt=$(git -C "$1" rev-parse "$2^{tree}") || return 1
  h_mt=$(git -C "$1" rev-parse "$h_mb^{tree}") || return 1
  if [ "$h_bt" = "$h_mt" ]; then
    echo "tree equals merge base"; return 0
  fi
  h_objs=$(git -C "$1" rev-parse --path-format=absolute --git-common-dir 2>/dev/null) || return 1
  rm -rf "$HYG_TMP/objects"; mkdir -p "$HYG_TMP/objects"
  h_sq=$(GIT_OBJECT_DIRECTORY="$HYG_TMP/objects" \
    GIT_ALTERNATE_OBJECT_DIRECTORIES="$h_objs/objects" \
    GIT_AUTHOR_NAME=repo-sync GIT_AUTHOR_EMAIL=repo-sync@example.invalid \
    GIT_COMMITTER_NAME=repo-sync GIT_COMMITTER_EMAIL=repo-sync@example.invalid \
    git -C "$1" commit-tree "$h_bt" -p "$h_mb" -m "repo-sync hygiene squash probe" \
    2>/dev/null) || return 1
  case "$(GIT_OBJECT_DIRECTORY="$HYG_TMP/objects" \
    GIT_ALTERNATE_OBJECT_DIRECTORIES="$h_objs/objects" \
    git -C "$1" cherry "$3" "$h_sq" 2>/dev/null)" in
    -*) ;;
    *) return 1 ;;
  esac
  h_want=$(git -C "$1" diff-tree -p --no-renames --no-color --no-ext-diff \
      --src-prefix=a/ --dst-prefix=b/ "$h_mt" "$h_bt" 2>/dev/null \
    | git -C "$1" patch-id --verbatim 2>/dev/null | cut -d ' ' -f 1)
  [ -n "$h_want" ] || return 1
  if git -C "$1" log -p --no-renames --no-color --no-ext-diff --no-merges \
      --src-prefix=a/ --dst-prefix=b/ --pretty=medium "$h_mb..$3" 2>/dev/null \
    | git -C "$1" patch-id --verbatim 2>/dev/null | cut -d ' ' -f 1 \
    | grep -qxF "$h_want"; then
    # A squash that landed and was reverted later still matches above. The
    # branch counts as landed only while every path it changed still holds,
    # on the default branch, what the branch tip holds.
    git -C "$1" -c core.quotePath=false diff-tree -r --no-renames --name-only \
      "$h_mt" "$h_bt" 2>/dev/null > "$HYG_TMP/squash.paths" || return 1
    while IFS= read -r h_path; do
      # git still quotes a name holding a quote, tab or newline; such a name
      # cannot be looked up as printed, so the branch stays.
      case "$h_path" in \"*) return 1 ;; esac
      # ls-tree prints mode and object id, so a mode change counts too.
      h_a=$(git -C "$1" ls-tree "$2" -- "$h_path" 2>/dev/null || true)
      h_b=$(git -C "$1" ls-tree "$3" -- "$h_path" 2>/dev/null || true)
      [ "$h_a" = "$h_b" ] || return 1
    done < "$HYG_TMP/squash.paths"
    echo "squash"; return 0
  fi
  return 1
}

# One record per worktree registration, fields split by $US:
# path, branch (empty when detached), locked (0|1), lock reason.
# The first record is the main checkout. Fails when git cannot list them:
# a pipeline would hide that, and an empty list reads as "nothing holds it".
hyg_worktrees() {
  h_wl=$(git -C "$1" worktree list --porcelain) || return 1
  printf '%s\n' "$h_wl" | awk -v us="$US" '
    function out() { if (p != "") printf "%s%s%s%s%s%s%s\n", p, us, br, us, lk, us, lr }
    /^worktree / { out(); p = substr($0, 10); br = ""; lk = 0; lr = ""; next }
    /^branch /   { br = substr($0, 8); sub(/^refs\/heads\//, "", br); next }
    /^locked/    { lk = 1; lr = substr($0, 8); next }
    END          { out() }'
}

# Returns 0 when any worktree of repo $1, the main checkout included, has
# branch $2 checked out, 1 when none has, and 2 when git cannot list them.
hyg_checked_out() {
  h_co_list=$(hyg_worktrees "$1") || return 2
  printf '%s\n' "$h_co_list" | awk -F "$US" -v b="$2" '$2 == b { f = 1 } END { exit !f }'
}

# Decides whether a lock still belongs to a live process. The lock text reads
# `... (pid <n> start <lstart>)`. Returns 0 (dead: safe to clear) when the pid
# is not running, or runs with a different start time, which means the pid
# was reused. Returns 1 (keep) when the process lives, and 2 when the lock
# names no pid: nothing can prove such a lock stale. Only `kill -0` saying
# "No such process" counts as not running; any other failure to look (no
# permission, ps failing or printing no time) keeps the lock.
#
# The lock's start time was formatted in the lock writer's timezone, which
# need not be this run's. Every timezone offset is a whole number of
# minutes, so the seconds field survives any offset: only a live pid whose
# start differs in the seconds is proof of reuse. Any other difference is
# ambiguous and keeps the lock; a reused pid with the same seconds (1 in 60)
# is a missed cleanup, never a wrong one.
hyg_lock_dead() {
  h_pid=$(printf '%s\n' "$1" | sed -n 's/.*(pid \([0-9][0-9]*\).*/\1/p' | head -1)
  [ -n "$h_pid" ] || return 2
  h_start=$(printf '%s\n' "$1" | sed -n 's/.*(pid [0-9][0-9]* start \(.*\))[[:space:]]*$/\1/p' | head -1)
  if ! kill -0 "$h_pid" 2>/dev/null; then
    case "$(kill -0 "$h_pid" 2>&1 || true)" in
      *"No such process"*|*"no such process"*) return 0 ;;
    esac
  fi
  h_now=$(ps -p "$h_pid" -o lstart= 2>/dev/null) || return 1
  # ps pads fields; compare with runs of blanks collapsed.
  h_now=$(echo $h_now)
  h_start=$(echo $h_start)
  [ "$h_now" = "$h_start" ] && return 1
  h_sec_now=$(printf '%s\n' "$h_now" | sed -n 's/.*[0-9][0-9]*:[0-9][0-9]:\([0-9][0-9]\).*/\1/p')
  h_sec_lock=$(printf '%s\n' "$h_start" | sed -n 's/.*[0-9][0-9]*:[0-9][0-9]:\([0-9][0-9]\).*/\1/p')
  [ -n "$h_sec_now" ] && [ -n "$h_sec_lock" ] || return 1
  [ "$h_sec_now" != "$h_sec_lock" ] && return 0
  return 1
}

# Lists every process cwd and open file into $HYG_TMP/inuse: /proc on Linux,
# lsof elsewhere. Returns 1 when neither can answer, or lsof fails; the
# caller then counts the path as in use. It runs afresh for each candidate,
# right before its removal, so a process that moved in after an earlier
# candidate's scan is still seen.
#
# A /proc entry whose cwd cannot be read is skipped only when the process
# has exited since the listing. One of this user's processes that stays
# unreadable fails the scan. Another user's cwd is unreadable without root;
# lsof does not show it either, so both paths leave it out alike. An open
# file that closes during the scan is not read, which is what it would be.
hyg_inuse() {
  : > "$HYG_TMP/inuse"
  # REPO_SYNC_PROC exists for the tests, to reach the lsof path on Linux.
  h_proc="${REPO_SYNC_PROC:-/proc}"
  if [ -d "$h_proc/self" ] && [ -e "$h_proc/self/cwd" ]; then
    for h_p in "$h_proc"/[0-9]*; do
      if readlink "$h_p/cwd" 2>/dev/null >> "$HYG_TMP/inuse"; then
        for h_fd in "$h_p"/fd/*; do
          readlink "$h_fd" 2>/dev/null >> "$HYG_TMP/inuse" || true
        done
        continue
      fi
      [ -d "$h_p" ] && [ -O "$h_p" ] && return 1
    done
  elif command -v lsof >/dev/null 2>&1; then
    lsof -nP -Fn > "$HYG_TMP/lsof" 2>/dev/null || return 1
    sed -n 's/^n//p' "$HYG_TMP/lsof" >> "$HYG_TMP/inuse"
  else
    return 1
  fi
  [ -s "$HYG_TMP/inuse" ]
}

# Returns 0 when some process has its cwd or an open file at or under $1, or
# when that cannot be known.
hyg_busy() {
  hyg_inuse || return 0
  h_real=$(cd "$1" 2>/dev/null && pwd -P) || return 0
  awk -v d="$h_real" '$0 == d || index($0, d "/") == 1 { found = 1 } END { exit !found }' \
    "$HYG_TMP/inuse"
}

# Drops the ignored entries of `git status --porcelain --ignored=matching`
# (each ignored path itself, not a folder holding only ignored files) that are
# build output: it rebuilds, and `worktree remove` takes it with the tree.
# Only a directory counts, which git prints with a trailing slash; a regular
# file of the same name may be anyone's data. Any other line stays, so an
# .env or a local database still keeps a worktree; so does a quoted path,
# which this cannot read.
hyg_not_build() {
  awk '!/^!! .*\/$/ { print; next }
    { p = substr($0, 4); sub(/\/$/, "", p); n = split(p, s, "/")
      if (s[n] ~ /^(__pycache__|\.pytest_cache|\.mypy_cache|\.ruff_cache|node_modules|target|dist)$/) next
      print }'
}

# Lane logs and scratch under the bulk storage root (sd:1677): in each
# <root>/<repo>/lane/, a file unmodified for 14 days is deleted, then a
# folder that saw no change in 14 days and is empty after that. lane/queue/
# is left alone: it is the sd-ship lane queue when sd.lane_root names this
# root. The root is `sd config get sd.bulk_storage_root`; unset, unreadable,
# or not mounted, nothing is swept and nothing is said. A file a process
# holds open, or a folder that is some process's cwd, stays.
hyg_lane() {
  command -v sd >/dev/null 2>&1 || return 0
  h_bulk=$(sd config get sd.bulk_storage_root 2>/dev/null) || return 0
  case "$h_bulk" in "~"|"~/"*) h_bulk="$HOME${h_bulk#"~"}" ;; esac
  [ -n "$h_bulk" ] && [ -d "$h_bulk" ] || return 0
  echo "=== lane storage ($h_bulk)"
  for h_lane in "$h_bulk"/*/lane; do
    h_lane=$(cd "$h_lane" 2>/dev/null && pwd -P) || continue
    # Both lists are taken before anything is deleted: a delete refreshes
    # its folder's mtime.
    if ! find "$h_lane" -mindepth 1 -path "$h_lane/queue" -prune -o -type d -mtime +13 -print \
         > "$HYG_TMP/lane.dirs" 2>/dev/null \
       || ! find "$h_lane" -path "$h_lane/queue" -prune -o -type f -mtime +13 -print \
         > "$HYG_TMP/lane.files" 2>/dev/null; then
      hyg_fail "list lane files under $h_lane"; continue
    fi
    [ -s "$HYG_TMP/lane.files" ] || [ -s "$HYG_TMP/lane.dirs" ] || continue
    if ! hyg_inuse; then
      hyg_note "lane files under $h_lane not swept: cannot tell which a process uses"; continue
    fi
    awk -v d="$h_lane" 'index($0, d "/") == 1' "$HYG_TMP/inuse" > "$HYG_TMP/lane.inuse"
    h_del=0; h_kept=0; h_bad=0
    while IFS= read -r h_f; do
      if grep -qxF "$h_f" "$HYG_TMP/lane.inuse"; then
        h_kept=$((h_kept + 1)); continue
      fi
      # Read the age again: the file may have been written since the listing.
      [ -n "$(find "$h_f" -prune -type f -mtime +13 2>/dev/null)" ] || continue
      if [ "$APPLY" != 1 ]; then
        h_del=$((h_del + 1))
      elif rm -f "$h_f"; then
        h_del=$((h_del + 1))
      else
        h_bad=$((h_bad + 1))
      fi
    done < "$HYG_TMP/lane.files"
    h_rmd=0
    if [ "$APPLY" = 1 ]; then
      # Deepest first; a folder that is not empty, or no longer there, stays
      # as it is.
      sort -r "$HYG_TMP/lane.dirs" | while IFS= read -r h_dir; do
        grep -qxF "$h_dir" "$HYG_TMP/lane.inuse" && continue
        rmdir "$h_dir" 2>/dev/null && echo x >> "$HYG_TMP/lane.rmd"
      done
      [ -f "$HYG_TMP/lane.rmd" ] && h_rmd=$(wc -l < "$HYG_TMP/lane.rmd" | tr -d ' ')
      rm -f "$HYG_TMP/lane.rmd"
    fi
    [ "$h_kept" -eq 0 ] || hyg_note "kept $h_kept lane file(s) under $h_lane a process holds open"
    [ "$h_bad" -eq 0 ] || hyg_fail "delete $h_bad lane file(s) under $h_lane"
    if [ "$APPLY" = 1 ]; then
      [ "$h_del" -eq 0 ] && [ "$h_rmd" -eq 0 ] || \
        hyg_act "deleted $h_del lane file(s) unmodified for 14 days under $h_lane, and $h_rmd empty folder(s)"
    elif [ "$h_del" -gt 0 ]; then
      hyg_found "delete $h_del lane file(s) unmodified for 14 days under $h_lane"
    fi
  done
}

# The workflow item state for number $1, or nothing. Reads the sd database
# read-only when it exists on this machine and sqlite3 is installed; a
# machine without it simply lists no done-item branches.
hyg_item_state() {
  h_db="${REPO_SYNC_SD_DB:-$HOME/.local/share/sd/sd.db}"
  [ -f "$h_db" ] || return 0
  command -v sqlite3 >/dev/null 2>&1 || return 0
  sqlite3 -readonly "$h_db" "select status from item where id = $1" 2>/dev/null || true
}

# The item number a branch name carries: sd-<n>, sd_<n> or a trailing -<n>.
hyg_item_number() {
  printf '%s\n' "$1" | sed -n \
    -e 's/.*sd[-_:]\([0-9][0-9]*\).*/\1/p' \
    -e 't' \
    -e 's/.*-\([0-9][0-9]*\)$/\1/p' | head -1
}

hyg_repo() {
  d=$1
  echo "=== $2 ($d)"

  # 3.3: remote-tracking refs for deleted remote branches. The lines are
  # matched by text, so both calls run in the C locale: a translated git
  # would prune and print nothing the sed below knows.
  # $HYG_TMP/stale holds the refs the dry run names (`origin/<branch>`): report
  # mode leaves them in place, so the branch pass reads GONE from this list.
  : > "$HYG_TMP/stale"
  for h_remote in $(git -C "$d" remote); do
    if [ "$APPLY" = 1 ]; then
      if h_out=$(LC_ALL=C git -C "$d" remote prune "$h_remote" 2>&1); then
        printf '%s\n' "$h_out" | sed -n 's/^ \* \[pruned\] /pruned remote-tracking ref /p' \
          | while read -r h_l; do hyg_act "$h_l"; done
      else
        hyg_fail "remote prune $h_remote: $(printf '%s' "$h_out" | tail -1)"
      fi
    else
      if h_out=$(LC_ALL=C git -C "$d" remote prune --dry-run "$h_remote" 2>&1); then
        printf '%s\n' "$h_out" | sed -n 's/^ \* \[would prune\] //p' >> "$HYG_TMP/stale"
        printf '%s\n' "$h_out" | sed -n 's/^ \* \[would prune\] /prune remote-tracking ref /p' \
          | while read -r h_l; do hyg_found "$h_l"; done
      else
        hyg_note "remote prune $h_remote not checked: remote unreachable"
      fi
    fi
  done

  # 3.1 and 3.2: registrations whose directory is gone. $HYG_TMP/gone holds
  # the paths pruned (or to be pruned), so the branch pass below treats their
  # branches as free in report mode too.
  : > "$HYG_TMP/gone"
  : > "$HYG_TMP/wts"
  if hyg_worktrees "$d" > "$HYG_TMP/wts.all"; then
    tail -n +2 "$HYG_TMP/wts.all" > "$HYG_TMP/wts"
  else
    hyg_fail "worktree list"
  fi
  h_prune=0
  while IFS=$US read -r w_path w_branch w_locked w_reason; do
    [ -d "$w_path" ] && continue
    if [ "$w_locked" = 1 ]; then
      h_rc=0; hyg_lock_dead "$w_reason" || h_rc=$?
      case $h_rc in
        1) hyg_note "kept locked worktree $w_path (directory gone; lock holder running: $w_reason)"; continue ;;
        2) hyg_list "KEEP     worktree $w_path (directory gone; lock names no pid: $w_reason)"; continue ;;
      esac
      if [ "$APPLY" = 1 ]; then
        if git -C "$d" worktree unlock "$w_path" 2>/dev/null; then
          hyg_act "unlocked worktree $w_path (lock holder not running: $w_reason)"
        else
          hyg_fail "unlock worktree $w_path"; continue
        fi
      else
        hyg_found "unlock worktree $w_path (lock holder not running: $w_reason)"
      fi
    fi
    echo "$w_path" >> "$HYG_TMP/gone"
    h_prune=1
  done < "$HYG_TMP/wts"
  if [ "$h_prune" = 1 ]; then
    if [ "$APPLY" = 1 ]; then
      if git -C "$d" worktree prune; then
        hyg_worktrees "$d" > "$HYG_TMP/left.all" || hyg_fail "worktree list after prune"
        cut -d "$US" -f 1 "$HYG_TMP/left.all" > "$HYG_TMP/left"
        while read -r w_path; do
          if grep -qxF "$w_path" "$HYG_TMP/left"; then
            hyg_fail "prune worktree $w_path (still registered)"
          else
            hyg_act "pruned worktree $w_path (directory gone)"
          fi
        done < "$HYG_TMP/gone"
      else
        hyg_fail "worktree prune"
      fi
    else
      while read -r w_path; do
        hyg_found "prune worktree $w_path (directory gone)"
      done < "$HYG_TMP/gone"
    fi
  fi

  # Who holds each branch now: the main checkout first, then linked
  # worktrees that were not pruned above.
  # An unreadable list proves nothing about holders, so it deletes nothing.
  h_wt_ok=1
  hyg_worktrees "$d" > "$HYG_TMP/holders.all" || { h_wt_ok=0; hyg_fail "worktree list"; }
  : > "$HYG_TMP/holders"
  h_first=1
  while IFS=$US read -r w_path w_branch w_locked w_reason; do
    if [ "$h_first" = 1 ]; then
      h_first=0; h_kind=main
    else
      grep -qxF "$w_path" "$HYG_TMP/gone" && continue
      h_kind=linked
    fi
    [ -n "$w_branch" ] || continue
    printf '%s%s%s%s%s%s%s%s%s\n' "$w_branch" "$US" "$w_path" "$US" "$h_kind" "$US" \
      "$w_locked" "$US" "$w_reason" >> "$HYG_TMP/holders"
  done < "$HYG_TMP/holders.all"

  # 3.4, 3.5 and the report-only branch classes. They need a default branch;
  # the prunes above do not, so they ran first.
  : > "$HYG_TMP/branches"
  if h_def=$(hyg_default "$d"); then
    def_name=${h_def%% *}
    def_ref=${h_def#* }
    # "Landed" is judged against the local origin/<default>. A remote that
    # moved it since (a force-push can drop content), made another branch
    # its default, or cannot be asked leaves that judgement stale, so no
    # landed branch is deleted. A local main or master proves nothing is on
    # origin, so it deletes nothing.
    h_fresh=$h_wt_ok
    case "$def_ref" in
      refs/remotes/origin/*)
        h_sym=$(bound_step 120 git -C "$d" ls-remote --symref origin HEAD < /dev/null 2>/dev/null || true)
        h_head=$(printf '%s\n' "$h_sym" | awk '$1 == "ref:" && $NF == "HEAD" { print $2; exit }')
        h_there=$(printf '%s\n' "$h_sym" | awk '$1 != "ref:" && $NF == "HEAD" { print $1; exit }')
        h_here=$(git -C "$d" rev-parse -q --verify "$def_ref" || true)
        if [ "$h_head" != "refs/heads/$def_name" ]; then
          h_fresh=0
          hyg_note "origin/HEAD here names $def_name, on origin ${h_head:-not read}; landed branches not deleted"
        elif [ -z "$h_there" ] || [ "$h_there" != "$h_here" ]; then
          h_fresh=0
          hyg_note "origin/$def_name here ${h_here:-missing}, on origin ${h_there:-not read}; landed branches not deleted"
        fi
        ;;
      *)
        h_fresh=0
        hyg_note "no origin/$def_name to verify against; landed branches not deleted"
        ;;
    esac
    git -C "$d" for-each-ref --format="%(refname:lstrip=2)$US%(objectname)$US%(upstream:track)$US%(upstream:short)" \
      refs/heads > "$HYG_TMP/branches"
  else
    hyg_note "no default branch found; branches not classified"
  fi
  while IFS=$US read -r b_name b_sha b_track b_up; do
    [ "$b_name" = "$def_name" ] && continue
    if h_how=$(hyg_merged "$d" "refs/heads/$b_name" "$def_ref"); then
      [ "$h_fresh" = 1 ] || continue
      # A branch made or moved within HYG_MIN_AGE seconds is kept: a fresh
      # agent branch sits at the default tip and counts as landed. The age
      # is the newest reflog entry's; no reflog means no age, so it is kept.
      if [ "$HYG_MIN_AGE" -gt 0 ]; then
        h_at=$(git -C "$d" reflog show --date=unix -n 1 --format=%gd "refs/heads/$b_name" 2>/dev/null \
          | sed -n 's/.*@{\([0-9][0-9]*\)}$/\1/p')
        if [ -z "$h_at" ]; then
          hyg_note "kept branch $b_name $b_sha (landed by $h_how; no reflog gives its age)"
          continue
        elif [ $((HYG_NOW - h_at)) -lt "$HYG_MIN_AGE" ]; then
          hyg_note "kept branch $b_name $b_sha (landed by $h_how; moved $((HYG_NOW - h_at))s ago, under ${HYG_MIN_AGE}s)"
          continue
        fi
      fi
      h_hold=$(awk -F "$US" -v b="$b_name" '$1 == b { print; exit }' "$HYG_TMP/holders")
      if [ -n "$h_hold" ]; then
        w_path=$(printf '%s\n' "$h_hold" | cut -d "$US" -f 2)
        h_kind=$(printf '%s\n' "$h_hold" | cut -d "$US" -f 3)
        w_locked=$(printf '%s\n' "$h_hold" | cut -d "$US" -f 4)
        w_reason=$(printf '%s\n' "$h_hold" | cut -d "$US" -f 5-)
        # A worktree in live use is kept with a note, not a listed line: a
        # fresh agent branch sits at the default tip, counts as landed, and
        # would otherwise mail every night the agent runs.
        h_why=""; h_live=0
        if [ "$h_kind" = main ]; then
          h_why="checked out in the main checkout"
        elif [ "$w_locked" = 1 ]; then
          h_rc=0; hyg_lock_dead "$w_reason" || h_rc=$?
          [ "$h_rc" = 1 ] && h_live=1
          h_why="worktree $w_path is locked: $w_reason"
        elif [ ! -d "$w_path" ]; then
          h_why="worktree $w_path is registered but missing"
        elif [ -n "$({ git -C "$w_path" status --porcelain --ignored=matching 2>/dev/null || echo unreadable; } | hyg_not_build)" ]; then
          # Ignored files count, build output aside: an .env or a local
          # database is not rebuildable, and `worktree remove` would take it
          # with the tree.
          h_why="worktree $w_path holds uncommitted, untracked or ignored files"
        elif hyg_busy "$w_path"; then
          h_why="worktree $w_path is in use by a process"; h_live=1
        fi
        if [ "$h_live" = 1 ]; then
          hyg_note "kept branch $b_name $b_sha (landed by $h_how; $h_why)"
          continue
        elif [ -n "$h_why" ]; then
          hyg_list "KEEP     branch $b_name $b_sha (landed by $h_how; $h_why)"
          continue
        fi
        if [ "$APPLY" = 1 ]; then
          if git -C "$d" worktree remove "$w_path" 2>/dev/null; then
            hyg_act "removed worktree $w_path (held landed branch $b_name)"
          else
            hyg_fail "remove worktree $w_path"; continue
          fi
        else
          hyg_found "remove worktree $w_path (holds landed branch $b_name)"
        fi
      fi
      if [ "$APPLY" = 1 ]; then
        # update-ref compares the tip it deletes with the one classified, in
        # one step. No git lock covers a checkout, so the worktree list is
        # read before the delete and again after it; a checkout that landed
        # in between gets its ref back. `branch -D` has the same window: it
        # also reads the worktrees, then deletes.
        # An unreadable worktree list counts as checked out: fail closed.
        h_co=0; hyg_checked_out "$d" "$b_name" || h_co=$?
        if [ "$h_co" != 1 ]; then
          hyg_fail "delete branch $b_name $b_sha (checked out since it was classified, or worktrees unreadable)"
        elif git -C "$d" update-ref -d "refs/heads/$b_name" "$b_sha" 2>/dev/null; then
          h_co=0; hyg_checked_out "$d" "$b_name" || h_co=$?
          if [ "$h_co" != 1 ]; then
            if git -C "$d" update-ref "refs/heads/$b_name" "$b_sha" "" 2>/dev/null; then
              hyg_fail "delete branch $b_name $b_sha (checked out during the delete; restored, reflog lost)"
            else
              hyg_fail "delete branch $b_name $b_sha (checked out during the delete; restore it: git -C $d branch $b_name $b_sha)"
            fi
            continue
          fi
          git -C "$d" config --remove-section "branch.$b_name" 2>/dev/null || true
          hyg_act "deleted branch $b_name $b_sha (landed by $h_how; restore: git -C $d branch $b_name $b_sha)"
        else
          h_now=$(git -C "$d" rev-parse -q --verify "refs/heads/$b_name" || true)
          hyg_fail "delete branch $b_name $b_sha (tip now ${h_now:-gone})"
        fi
      else
        hyg_found "delete branch $b_name $b_sha (landed by $h_how)"
      fi
      continue
    fi
    if [ "$b_track" = "[gone]" ] || { [ -n "$b_up" ] && grep -qxF "$b_up" "$HYG_TMP/stale"; }; then
      hyg_list "GONE     branch $b_name $b_sha (upstream gone; content not on $def_name)"
    else
      h_n=$(git -C "$d" rev-list --count "refs/heads/$b_name" --not --remotes 2>/dev/null || echo 0)
      if [ "$h_n" -gt 0 ]; then
        hyg_list "LOCAL    branch $b_name $b_sha ($h_n commit(s) on no remote ref)"
      fi
    fi
    h_item=$(hyg_item_number "$b_name")
    if [ -n "$h_item" ] && [ "$(hyg_item_state "$h_item")" = done ]; then
      hyg_list "DONE     branch $b_name $b_sha (sd:$h_item is done; a landing candidate)"
    fi
  done < "$HYG_TMP/branches"

  # A checkout sync could not fast-forward: still behind its upstream.
  if h_up=$(git -C "$d" rev-parse -q --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null); then
    h_behind=$(git -C "$d" rev-list --count "HEAD..@{u}" 2>/dev/null || echo 0)
    if [ "$h_behind" -gt 0 ]; then
      h_ahead=$(git -C "$d" rev-list --count "@{u}..HEAD" 2>/dev/null || echo 0)
      if [ -n "$(git -C "$d" status --porcelain --untracked-files=no 2>/dev/null)" ]; then
        h_why="local changes"
      elif [ "$h_ahead" -gt 0 ]; then
        h_why="$h_ahead local commit(s)"
      else
        h_why="not pulled"
      fi
      hyg_list "BEHIND   checkout is $h_behind commit(s) behind $h_up ($h_why)"
    fi
  fi
}

# Runs one part of the sweep and prints its output. A part with nothing to
# say stays out of the report; a note alone still prints, though it counts
# nowhere.
hyg_section() {
  h_before=$(cat "$HYG_TMP/acted" "$HYG_TMP/found" "$HYG_TMP/listed" "$HYG_TMP/failed" | wc -l)
  "$@" > "$HYG_TMP/repo.out" 2>&1 || true
  h_after=$(cat "$HYG_TMP/acted" "$HYG_TMP/found" "$HYG_TMP/listed" "$HYG_TMP/failed" | wc -l)
  if [ "$h_after" -ne "$h_before" ] || grep -q '^  note: ' "$HYG_TMP/repo.out"; then
    cat "$HYG_TMP/repo.out"; echo
  fi
}

hygiene() {
  APPLY=0
  for h_arg in "$@"; do
    case "$h_arg" in
      --apply) APPLY=1 ;;
      *) echo "repo-sync.sh hygiene: unknown option '$h_arg' (want: --apply)" >&2; return 2 ;;
    esac
  done
  # Seconds a landed branch must sit unmoved before it is deleted.
  HYG_MIN_AGE="${REPO_SYNC_HYGIENE_MIN_AGE:-86400}"
  case "$HYG_MIN_AGE" in
    ''|*[!0-9]*) echo "repo-sync.sh hygiene: REPO_SYNC_HYGIENE_MIN_AGE must be a number of seconds, got '$HYG_MIN_AGE'" >&2; return 2 ;;
  esac
  # A value past the shell's integer range makes `[ -gt ]` fail, which would
  # skip the guard. Twelve digits (over 30,000 years) is the limit; leading
  # zeros are dropped first, so the value stays decimal.
  HYG_MIN_AGE=$(printf '%s\n' "$HYG_MIN_AGE" | sed 's/^0*//')
  HYG_MIN_AGE=${HYG_MIN_AGE:-0}
  if [ "${#HYG_MIN_AGE}" -gt 12 ]; then
    echo "repo-sync.sh hygiene: REPO_SYNC_HYGIENE_MIN_AGE must have at most 12 digits, got '$HYG_MIN_AGE'" >&2; return 2
  fi
  HYG_NOW=$(date +%s)
  : > "$HYG_TMP/acted"; : > "$HYG_TMP/found"; : > "$HYG_TMP/listed"; : > "$HYG_TMP/failed"
  echo "profile : $PROFILE"
  echo "root    : $ROOT"
  if [ "$APPLY" = 1 ]; then echo "mode    : apply"; else echo "mode    : report only (--apply acts)"; fi
  echo

  repo_list | while read -r subdir full_repo; do
    target="$ROOT/$subdir/${full_repo##*/}"
    [ -d "$target/.git" ] || continue
    hyg_section hyg_repo "$target" "$full_repo"
  done
  hyg_section hyg_lane

  h_acted=$(wc -l < "$HYG_TMP/acted" | tr -d ' ')
  h_found=$(wc -l < "$HYG_TMP/found" | tr -d ' ')
  h_listed=$(wc -l < "$HYG_TMP/listed" | tr -d ' ')
  h_failed=$(wc -l < "$HYG_TMP/failed" | tr -d ' ')
  echo "----------------------------------------"
  if [ "$APPLY" = 1 ]; then
    echo "hygiene : $h_acted done, $h_listed listed, $h_failed failed"
    [ "$h_failed" -eq 0 ] || return 1
  elif [ "$h_found" -eq 0 ] && [ "$h_listed" -eq 0 ]; then
    echo "hygiene : clean"
  else
    echo "hygiene : $h_found to act on with --apply, $h_listed listed"
    return 1
  fi
}

# --- Jev: shadow ordering of the hygiene report (sd:2094) -------------------
# local-jev asks, per listed line, whether its work is live, abandoned or
# superseded: the order a reader would want (superseded first, live last).
# It runs in shadow only. `--shadow` records Jev's answers beside the
# report's own order, and the report is mailed unchanged, word for word and
# in its order, whatever Jev says, fails or costs. Unset means on; the
# JEV_REPO_SYNC_HYGIENE switch, `jev off` or a spent budget skips it.
#
# Every call leaves the machine, so the state is built from fixed words and
# numbers only: the line's class, its counts, and its tip's age in days. No
# repository, branch, sha, path or lock reason is sent.
JEV="${REPO_SYNC_JEV:-$DIR/../local-jev/jev.sh}"
# One id for every jev call this run makes,
# so the ledger groups them (sd:2953). A run that started this one keeps its own.
[ -n "${JEV_RUN:-}" ] || \
  JEV_RUN="repo-sync-$(date -u +%Y%m%dT%H%M%S)-$(od -An -N2 -tx1 /dev/urandom | tr -d ' \n')"
export JEV_RUN
hyg_jev_shadow() {
  [ -f "$JEV" ] || return 0
  grep -qE '^  (GONE|LOCAL|DONE|BEHIND|KEEP) ' "$1" || return 0
  sh "$JEV" enabled JEV_REPO_SYNC_HYGIENE --record --caller local-repo-sync \
    >/dev/null 2>&1 || return 0
  # One record per listed line: class, count, why, sha, checkout. Only the
  # first three are sent; the sha and checkout date the tip here.
  awk -v us="$US" '
    /^=== / { d = $0; sub(/^=== [^ ]* \(/, "", d); sub(/\)$/, "", d); next }
    /^  (GONE|LOCAL|DONE|KEEP) +branch / {
      n = ""; why = ""
      if ($1 == "LOCAL") { n = $5; sub(/^\(/, "", n) }
      if ($1 == "KEEP") why = "on the default branch, kept: worktree dirty, locked or missing"
      if ($1 == "GONE") why = "upstream branch deleted; content not on the default branch"
      if ($1 == "DONE") why = "named for a work item marked done; content not on the default branch"
      print $1 us n us why us $4 us d; next
    }
    /^  KEEP +worktree / { print "KEEP" us "" us "worktree directory gone; lock names no process" us "" us ""; next }
    /^  BEHIND / {
      why = "not pulled"
      if ($0 ~ /\(local changes\)$/) why = "local changes"
      else if ($0 ~ /local commit\(s\)\)$/) why = "local commits"
      print "BEHIND" us $4 us why us "" us ""
    }
  ' "$1" > "$TMPD/jev.listed"
  h_now=$(date +%s)
  h_i=0
  : > "$TMPD/jev.entries"
  while IFS=$US read -r j_kind j_n j_why j_sha j_dir; do
    h_i=$((h_i + 1))
    case "$j_n" in *[!0-9]*) j_n="" ;; esac
    case "$j_kind" in
      LOCAL)  h_text="branch with ${j_n:-some} commit(s) on no remote" ;;
      BEHIND) h_text="checkout ${j_n:-some} commit(s) behind its upstream; $j_why" ;;
      KEEP)   case "$j_sha" in "") h_text="$j_why" ;; *) h_text="branch whose content is $j_why" ;; esac ;;
      *)      h_text="branch: $j_why" ;;
    esac
    if [ -n "$j_sha" ] && h_at=$(git -C "$j_dir" log -1 --format=%ct "$j_sha" 2>/dev/null) \
       && [ -n "$h_at" ]; then
      h_text="$h_text; last commit $(( (h_now - h_at) / 86400 )) day(s) ago"
    fi
    printf 'h%s%s%s\n' "$h_i" "$US" "$h_text" >> "$TMPD/jev.entries"
  done < "$TMPD/jev.listed"
  # Fixed words, digits and `;:,()` only, so nothing needs escaping.
  awk -F "$US" -v sfile="$TMPD/jev.state" -v qfile="$TMPD/jev.questions" '
    BEGIN { s = "{\"entries\":{"; q = "{" }
    {
      if (NR > 1) { s = s ","; q = q "," }
      s = s "\"" $1 "\":\"" $2 "\""
      q = q "\"" $1 "\":{\"type\":\"choice\",\"instructions\":\"State entries." $1 \
          " is one leftover from a nightly sweep of git checkouts on a developer" \
          " machine. Is the work it holds still live, abandoned, or superseded by" \
          " work that already landed? Judge entries." $1 " only.\",\"criteria\":{" \
          "\"live\":\"someone is still working on it\"," \
          "\"abandoned\":\"nobody will finish it\"," \
          "\"superseded\":\"its work landed another way or was replaced\"}}"
    }
    END { print s "}}" > sfile; print q "}" > qfile }
  ' "$TMPD/jev.entries"
  # The ledger's name for this call (sd:2953): the first 16 hex of the sha256
  # of the listed records above -- class, count, reason, sha and checkout,
  # unit-separated -- sorted, one per line. No repository or branch leaves in
  # it; an outcome recomputes it from the same hygiene report.
  h_subject="repo-sync:$(LC_ALL=C sort "$TMPD/jev.listed" | shasum -a 256 | cut -c1-16)"
  sh "$JEV" ask --questions "$TMPD/jev.questions" --state "$TMPD/jev.state" \
    --state-format json --caller local-repo-sync --stage JEV_REPO_SYNC_HYGIENE \
    --subject "$h_subject" --shadow '{}' >/dev/null 2>&1 || true
}

case "$1" in
  sync)
    # The `while read` loop runs in a subshell, so failures are collected in a
    # temp file rather than a variable, which would not survive the pipeline.
    TMPD=$(mktemp -d)
    trap 'rm -rf "$TMPD"' EXIT INT TERM
    FAILLOG="$TMPD/faillog"; : > "$FAILLOG"
    PINLOG="$TMPD/pinlog"; : > "$PINLOG"
    DIVLOG="$TMPD/divlog"; : > "$DIVLOG"
    KEPTLOG="$TMPD/keptlog"; : > "$KEPTLOG"
    load_list
    sync
    ;;
  refresh)
    shift
    # The helper holds every lane's runner lock and waits out the gate
    # holders from before, then runs this again as its child with
    # REPO_SYNC_LANES_HELD=1 (sd:3099, sd:3265).
    if is_satellite; then
      echo "repo-sync.sh refresh: this machine is a satellite ($HOME/.config/sd/hub.json); only follow moves its system and pack checkouts, to the hub's pins" >&2
      exit 1
    fi
    # The list is read once, before the drain and before any move, so a
    # pack move in this run cannot change it.
    TMPD=$(mktemp -d)
    trap 'rm -rf "$TMPD"' EXIT INT TERM
    load_list
    if [ "${REPO_SYNC_LANES_HELD:-}" != 1 ]; then
      drain_exec refresh "$@"
    fi
    refresh "$@"
    ;;
  follow)
    shift
    if [ "$#" -ne 0 ]; then
      echo "repo-sync.sh follow: takes no arguments" >&2
      exit 2
    fi
    TMPD=$(mktemp -d)
    trap 'rm -rf "$TMPD"' EXIT INT TERM
    load_list
    follow
    ;;
  check)
    TMPD=$(mktemp -d)
    trap 'rm -rf "$TMPD"' EXIT INT TERM
    FINDINGS="$TMPD/findings"; : > "$FINDINGS"
    load_list
    check
    ;;
  reconcile)
    TMPD=$(mktemp -d)
    trap 'rm -rf "$TMPD"' EXIT INT TERM
    load_list
    reconcile
    ;;
  nightly)
    # Cron flavor: read the list, reconcile it against disk, then sync, then
    # hygiene. A changed fleet, a failed repo or a hygiene finding prints an
    # SD_REPORT_ATTENTION line, so the run's report reaches Reports and Today
    # instead of relying on someone reading the log; nothing is mailed
    # (sd:3454). Exits 1 when half or more of the fleet failed (the check at
    # the end of this case says why that threshold): an outage, not findings.
    TMPD=$(mktemp -d)
    FAILLOG="$TMPD/faillog"; : > "$FAILLOG"
    PINLOG="$TMPD/pinlog"; : > "$PINLOG"
    DIVLOG="$TMPD/divlog"; : > "$DIVLOG"
    KEPTLOG="$TMPD/keptlog"; : > "$KEPTLOG"
    trap 'rm -rf "$TMPD"' EXIT INT TERM
    SYNC_FAILED=0
    # The kinds of finding this run has, one per line, without a count; the
    # SD_REPORT_ATTENTION lines at the end name them and each finding.
    KINDS="$TMPD/kinds"; : > "$KINDS"

    load_list
    RECON="$TMPD/recon"
    reconcile > "$RECON" 2>&1
    cat "$RECON"
    # A join changed the database: read the list again, so sync pulls it.
    [ ! -s "$JOINED" ] || load_list

    # A MISMATCH needs attention too. Before this path existed, such a
    # checkout made $ADDED non-empty every night and so reported every night
    # -- badly, as a spurious list change, but the operator did hear about
    # it. Reporting it to stdout alone would have bought silence with the
    # fix: a green night whose only trace is a cron log nobody reads, which
    # is how the loop this replaced went unnoticed for four nights. It cannot
    # be auto-resolved, so it repeats until a human renames the directory or
    # the repo.
    # A list from the snapshot or the confs is the database not answering,
    # and reconcile wrote nothing (sd:3983).
    {
      [ ! -s "$UNJOINED" ] || echo "checkouts not joined to the list"
      if [ "$LIST_SOURCE" != database ]; then
        echo "the database did not answer; the list came from the $LIST_SOURCE"
      elif [ -n "$r_why" ]; then
        echo "reconcile could not read the repo rows"
      fi
      if [ -s "$JOINED" ] || [ -s "$ABSENT" ]; then echo "repo list changed"; fi
      [ ! -s "$UNLISTED" ] || echo "checkouts present, not listed"
      [ ! -s "$MISMATCHED" ] || echo "checkouts need a rename"
    } >> "$KINDS"

    SSH_NOTE="$TMPD/ssh"
    ssh_preflight > "$SSH_NOTE" 2>&1 || true
    cat "$SSH_NOTE"

    OUT="$TMPD/out"
    if ! sync > "$OUT" 2>&1; then
      cat "$OUT"
      SYNC_FAILED=1
      # The SSH note above names a locked key when that is the cause of
      # most of this list.
      echo "failed repos:"
      sed 's/^/  /' "$FAILLOG"
      echo "repos failed" >> "$KINDS"
    else
      cat "$OUT"
    fi

    # Hygiene after sync, so it sees the fleet as sync left it. Its report
    # needs attention when it failed or lists something for the operator,
    # like the reconcile diff. Its actions (prunes, landed branches, old lane
    # files) are routine: the log names each one with its restore line, and
    # they alone report nothing (sd:4914). Its own failures are in the report
    # and do not fail the job.
    HYG_TMP="$TMPD/hygiene"; mkdir -p "$HYG_TMP"
    HYG_OUT="$TMPD/hygiene.out"
    ( hygiene --apply ) > "$HYG_OUT" 2>&1 || true
    cat "$HYG_OUT"
    if [ -s "$HYG_TMP/listed" ] || [ -s "$HYG_TMP/failed" ]; then
      echo "hygiene report" >> "$KINDS"
    fi
    # One SD_REPORT_ATTENTION line per kind and per finding, not the date:
    # why the list did not come from the database, reconcile's lists (sd:3983),
    # each line under the name of its list, a failed join with its error; each
    # failed repo's section of the sync output with a checksum of it, its step
    # and fetch lines left out; and each of hygiene's lines under its section,
    # its notes and its counts left out. The ingest's reason is the run's set of lines:
    # the same set folds into the open report, a smaller one folds into it too
    # (pack sd:4658), and a new finding opens its own report and closes the
    # older one on this host, with the exceptions local-cron-jobs/README.md
    # names (pack sd:4641). The host stays in each line, as every host files
    # under one job name. No 2>/dev/null: an input missing here is a bug to see
    # in the log, not an empty list (review round 1).
    if [ -s "$KINDS" ]; then
      # Each failed repo's section to its own file, as the checksum's input;
      # sync's only --- line is the closing rule. Its step lines and git's
      # fetch lines (`From <url>`, `old..new main -> origin/main`) are left
      # out: they move with the clock and the upstream, not the cause.
      awk -v d="$TMPD" 'function flush() { if (f) { printf "%s", b > (d "/failed." n); close(d "/failed." n) } f = 0 }
           /^=== |^---/ { flush(); n++; b = "" } /^\[step |^From |^ .* -> / { next } /^!!! failed: / { f = 1 }
           { b = b $0 "\n" } END { flush() }' "$OUT"
      {
        cat "$KINDS"
        [ "$LIST_SOURCE" = database ] || [ -z "$LIST_WHY" ] || echo "list: $LIST_WHY"
        [ -z "$r_why" ] || echo "rows: $r_why"
        sed 's/^/joined: /' "$JOINED"; sed 's/^/unjoined: /' "$UNJOINED"
        sed 's/^/unlisted: /' "$UNLISTED"; sed 's/^/absent: /' "$ABSENT"
        sed 's/^/rename: /' "$MISMATCHED"
        for fs in "$TMPD"/failed.*; do
          [ -f "$fs" ] || continue
          echo "failed: $(head -1 "$fs" | sed 's/^=== //') [$(cksum < "$fs" | cut -d' ' -f1)]"
        done
        # A checkout further behind is the same finding: its counts are left out.
        # An action's line is left out too (sd:4914).
        awk -v acted="$HYG_TMP/acted" 'BEGIN { while ((getline l < acted) > 0) done[l] = 1 }
             /^=== /{ h = substr($0, 5); next } /^  note: /||/^$/{ next } /^---/{ h = ""; next }
             $0 in done { next }
             h != "" { sub(/is [0-9]+ commit\(s\) behind/, "is behind"); sub(/\([0-9]+ local commit\(s\)\)/, "(local commits)")
                       print "hygiene: " h ": " $0 }' "$HYG_OUT"
      } | awk -v p="SD_REPORT_ATTENTION: repo-sync on $(hostname -s): " 'NF && !seen[$0]++ { print p $0 }'
    fi
    # After the report, so Jev never delays or changes it.
    hyg_jev_shadow "$HYG_OUT" || true

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
      echo "sync failed $n_failed of $n_total repos — exiting 1, an outage, not findings" >&2
      exit 1
    fi
    ;;
  hygiene)
    shift
    TMPD=$(mktemp -d)
    trap 'rm -rf "$TMPD"' EXIT INT TERM
    HYG_TMP="$TMPD/hygiene"; mkdir "$HYG_TMP"
    load_list
    hygiene "$@"
    ;;
  list)
    TMPD=$(mktemp -d)
    trap 'rm -rf "$TMPD"' EXIT INT TERM
    load_list
    list
    ;;
  test)
    shift
    exec "${PYTHON:-python3}" -m unittest discover -s "$DIR/tests" -t "$DIR" "$@"
    ;;
  -h|--help|help)
    cat <<'HELPEOF'
usage: repo-sync.sh sync|check|list|reconcile|hygiene|nightly|refresh|follow|test

  sync       clone missing repos and fast-forward existing ones, then print a
             summary; exits 1 if any repo failed, having tried all the others.
             A checkout on a detached HEAD is pinned: sync fetches it, never
             pulls it, and lists it as "pinned at <sha>", with ", N behind
             origin/<default>" when it is. A branch other than the default,
             with its own commits, that its upstream moved past is listed
             under "diverged:" and left alone, not failed (sd:3125).
             A branch with no upstream is fetched, not pulled, and listed
             under "kept:", not failed (sd:3744).
             A pinned checkout is not a failure
  check      read-only: report which configured repos have no checkout
             (MISSING) and which sit on a different origin than the list
             names (DIFFERS). Fetches nothing, so a repo that is merely
             behind still reports ok. Exits 1 when anything was reported —
             this is what machine-setup's `status` folds into its drift count
  list       print the resolved profile, root, the list's source and the
             repo list without touching the checkouts
  reconcile  compare the list with the checkouts on disk. A GitHub checkout
             with no row in the workflow database joins it: `sd-db repo add`,
             then `sd-db repo clones PATH +<profile>`; only when the database
             gave the list. It reports, and never writes: a checkout whose
             row does not list the profile, and a listed checkout that is
             gone (sync clones it again; `sd-db repo clones PATH -<profile>`
             stops that). No run removes a profile. Checkouts without a
             GitHub origin are listed as unmanaged and left alone, as are
             checkouts whose directory name differs from their repo name
             (MISMATCH): a list line cannot express those. A linked worktree
             under the root is listed as WORKTREE <dir> (of <parent>).
  hygiene [--apply]
             sweep what agents leave in each conf checkout. Without
             --apply it only reports; with it, it prunes worktree
             registrations whose directory is gone, clears a lock whose
             pid is not running (or runs with another start time), prunes
             remote-tracking refs for deleted remote branches, and deletes
             local branches whose content is on the default branch
             (ancestor, tree equal to the merge base, or a patch-equivalent
             squash), removing a clean, unused worktree that holds one
             (build output does not count against clean; it goes with the
             worktree). It deletes lane files under the bulk storage root
             (sd.bulk_storage_root, <root>/<repo>/lane/, queue/ aside)
             unmodified for 14 days, unless a process holds them open.
             Every deletion prints the branch and its tip sha. It lists,
             never deletes: branches whose upstream is gone, branches
             with commits on no remote, branches named for a done sd item,
             and checkouts left behind their upstream. It never touches a
             stash, a remote branch, the default branch, or a dirty or
             in-use worktree. Without --apply it exits 1 when it found
             anything; with --apply it exits 1 only when an action failed.
             It is not a status subcommand.
  nightly    reconcile, then sync, then hygiene --apply; a failed repo, a
             list not from the database, a changed or unlisted checkout,
             or a hygiene report that changed, listed or failed anything
             prints an SD_REPORT_ATTENTION line for Reports and Today; this
             is what the repo-sync-nightly cron job runs. It writes no conf
             and commits nothing. Exits 1 when half or more of the fleet
             failed.
  refresh [path ...]
             wait while another refresh or follow holds refresh.lock in
             the lane root, so two drains never split the lanes, then
             drain the lanes: hold every lane's runner lock under the
             lane root (SD_LANE_ROOT, else sd.lane_root, else
             ~/.local/state/sd/lanes), making it for a lane folder, a
             registered repository (sd-db repo list) or a conf checkout
             whose runner never ran, so `lane run` exits at once,
             and wait until each gate holder `sd gate status` listed once
             every lock was held has ended; a waiter or a later gate is no
             lane's and does not count. TERM, INT, HUP or kill -9
             of the helper leaves the locks held until the refresh steps
             end. The wait is
             bounded: 45 minutes in total for the lane locks and the gate
             together, the refresh lock included (REPO_SYNC_DRAIN_WAIT
             seconds overrides it); past it,
             refresh refuses with nothing moved and names what was busy.
             Then move each pinned checkout named, or with no path every
             pinned conf checkout, to origin's default branch, still
             detached, and release the locks. It
             refuses a checkout with uncommitted changes or a submodule,
             never overwrites an ignored file, and leaves a checkout on a
             branch alone. In the command pack (bin/sd_install.py) it then
             runs `make setup`. Moving
             system or the pack, it boots out <prefix>.sd-serve and
             <prefix>.sd-dashboard first, waiting up to 10 s for each to
             unload, and bootstraps them after, once the
             database's user_version is the pack lib's SCHEMA_VERSION;
             otherwise they stay stopped, listed in
             ~/.local/state/repo-sync/refresh-stopped for the next refresh,
             and refresh exits 1. When the database (REPO_SYNC_SD_DB) is
             below the SCHEMA_VERSION of the lib/sd_db the pack moves to,
             it first runs the pack's `bin/sd-db backup` into
             ~/.local/state/repo-sync/refresh-backups/, where a finished
             refresh keeps the newest three folders, and writes
             ~/.local/state/repo-sync/refresh-intent: the
             system and pack shas, the version and the backup. A failed
             backup or record write moves nothing. After the moves it runs
             `bin/sd-db migrate`. On a failure it moves the pack back to the
             record's sha, restores the backup there when the version moved,
             moves system back, runs `make setup`, starts the services and
             exits 1 with no hub-pin. A rerun reuses a record left, never
             replaces it, and migrates or recovers from what it reads. An
             unreadable record, a missing backup or a failed recovery keeps
             the record and the services stopped and exits 1. The record
             goes just before the services start. Prints the
             old and new sha; exits 1 if any checkout failed. After a
             refresh with no failure, the hub pushes one annotated tag,
             hub-pin, to the system origin for `follow`: it names the
             pinned system sha and its message carries pack=<sha> (that one
             tag may move backwards). A failed push, or a pinned system
             without a pinned pack, exits 1. On a satellite refresh refuses
             and names follow.
  follow     on a satellite (one with ~/.config/sd/hub.json), fetch the
             hub-pin tag from the system origin and the pack sha it names
             from the pack origin and, where a checkout differs, drain the
             lanes as refresh does, without the gate wait (a lane gates
             under its runner lock), then move it, detached, to exactly that
             sha, never to origin's default branch, and run `make setup` in
             the pack. Then, lanes still held, it runs this system
             checkout's `machine-setup.sh update bin --apply` and
             `update satellite --apply`, each followed by its dry run and
             bounded at 450 s; a failure, a drift word in the dry run, or a
             dry run without the stage's proof line (a SKIP proves nothing)
             exits 1, keeps the move and names the command. When the system
             move changed local-agent-prompt/prompt/shared.md, or a run
             finishes one the marker below names, it then runs
             `agent-prompt.sh refresh --apply`, bounded at 120 s; a refused
             target is named with the commands to run by hand, and follow
             still exits 0. Every check
             runs first: a failed fetch, a tag with no
             pack= line or uncommitted changes refuses with nothing moved;
             a drain timeout skips with nothing moved and exits 0, and the
             next run tries again. When a move fails, each checkout it moved goes
             back to its old sha and the pack runs `make setup` again there,
             so the next run retries the pair. A run killed or rolled back
             after it wrote $XDG_STATE_HOME/repo-sync/follow-intent leaves
             it, and the next run finishes, sets the pack up and updates
             again, even at the pin; only a proven update deletes it.
             Already there, or no hub-pin tag, is a no-op. On the hub it says so and
             does nothing. On a satellite, sync and nightly never pull system or
             pack; only follow moves them.
  test       run the regression suite in tests/ (unittest; override the
             interpreter with PYTHON). Covers reconcile, list and nightly
             against fixture trees and a stub sd-db; sync's clone path is not covered,
             because it clones over SSH — the suite reaches it only through
             nightly, with the transport stubbed dead.

The repo fleet is the workflow database's `repo` rows whose clone_profiles
hold this profile: every command reads `sd-db repo list --clones <profile>`
once, and keeps each answer in ~/.local/state/system/repo-sync/clones.<profile>.
When the database does not answer (no hub, a refused build, an empty or bad
answer), it reads that snapshot, else the confs, else refuses; each fallback
prints a line on stderr. The confs are repos.common.conf plus
repos.<profile>.conf, in <config>/repo-sync/ (<config> is
$SYSTEM_TOOLS_CONFIG, default ~/.config/system): the bootstrap for a machine
that never reached the hub. Copy the committed repos.<profile>.conf.example
there. Format is "<subdir> <owner/repo>", with # comments and blank lines
ignored; subdir "." means the repo sits directly in the root.

The terra profile reads repos.terra.conf alone and skips repos.common.conf,
because a terra machine deliberately carries only the system repo.

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
  SD_PACK_ROOT        the command pack whose bin/sd-db reads the list
                      (default: ~/repos/platypeeps/sd-ai-command-pack)
  XDG_STATE_HOME      where the snapshot lives (default: ~/.local/state)
  SYSTEM_TOOLS_CONFIG shared config root (default: ~/.config/system)
  MACHINE_SETUP_STATE where to look for the recorded profile
                      (default: ~/.config/machine-setup)
  REPO_SYNC_SD_DB     the sd workflow database hygiene reads, read-only,
                      to list branches named for a done item, and whose
                      schema version refresh reads (default:
                      ~/.local/share/sd/sd.db; absent means none listed
                      and nothing to migrate)
  JEV_REPO_SYNC_HYGIENE
                      0, off, false, no or disabled switches off the Jev
                      shadow ordering of nightly's hygiene report; unset
                      means on. local-jev asks whether each listed line is
                      live, abandoned or superseded and records the answer;
                      the report is mailed unchanged. Jev must also be on
                      and keyed (`jev enabled`)
  REPO_SYNC_DRAIN_WAIT
                      seconds refresh waits for the lane locks and an idle
                      gate together before it refuses (default 2700,
                      45 minutes)
  REPO_SYNC_LANES_HELD
                      set to 1 only by refresh_drain.py for its child: the
                      lanes are already held, so refresh or follow moves
                      checkouts
  REPO_SYNC_LANE_NAMES
                      set only by refresh and follow for refresh_drain.py:
                      the conf's checkout names, whose lane locks it makes
                      and holds
  REPO_SYNC_JEV       local-jev's entrypoint (default: the local-jev
                      folder of this checkout)
HELPEOF
    exit 0
    ;;
  *)
    echo "usage: $(basename "$0") sync|check|list|reconcile|hygiene|nightly|refresh|follow|test" >&2
    exit 1
    ;;
esac

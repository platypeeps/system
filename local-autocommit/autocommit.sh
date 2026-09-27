#!/bin/sh
# Commit and push one scoped path, unattended, on behalf of a job that just
# rewrote it.
#
# Jobs in this repository generate tracked files and used to leave them
# dirty: local-ai-apps' inventory and local-repo-sync's repo list. A generated file nobody commits is worse than
# no file -- `git status` is permanently noisy, so a real edit hides in the
# noise, and the next person to switch branches carries the generated churn
# onto the branch with them.
#
# The logic here was extracted from a profile-capture script that committed
# and pushed its rosters correctly and alone. Its comments are
# kept where they explain a trap rather than a roster, because each one names a
# way an unattended push reports success having done nothing.
#
# Two verbs, and a job calls both:
#
#   check   BEFORE it writes. Refuses when someone else's work would be swept
#           in, or when the commit would land somewhere nobody is looking.
#   commit  AFTER it writes. Stages the scope, proves nothing else came with
#           it, commits, pushes.
#
# They are separate because the interesting refusal is only answerable before
# the job runs: once a generator has overwritten its file, a hand edit that was
# sitting there is already gone, and no check after the fact can see it.
#
# Both verbs fast-forward the checkout onto the remote before they do anything
# else. A commit written on a checkout that is behind cannot be pushed, and
# `sync_with_remote` below says how that stranded one for good.
#
# Exit codes, as convention 6 in CLAUDE.md spells them:
#   0  committed and pushed, or nothing to do
#   3  deferred on purpose -- wrong branch, scope already dirty, behind the
#      remote with a local edit blocking the fast-forward
#   1  broken -- the commit exists and the remote does not have it
#
# local-health-check reads these codes.
set -eu

DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$DIR/.." && pwd)"

ME="autocommit"

# The deploy key the ruleset on main exempts from its required checks
# (sd:1417). The jobs push to main directly, and with `route` and
# `system-native` required, a push as the operator's own key is refused: its
# commit has no checks. The key is one per machine, has no passphrase because
# launchd cannot type one, and is a deploy key, so it reaches this repository
# alone and no API. The `main integrity` ruleset, which exempts nobody, still
# refuses it a force push.
KEY="${AUTOCOMMIT_SSH_KEY:-$HOME/.ssh/system_autocommit}"
AGENTS="${AUTOCOMMIT_LAUNCH_AGENTS:-$HOME/Library/LaunchAgents}"
# The jobs that push to main through this tool or beside it. A machine that
# loads none of them has no use for the key.
# AUTOCOMMIT_PUSHING_JOBS replaces the list, space-separated, for a machine
# whose own jobs (for example ones kept in CRON_JOBS_EXTRA_DIRS) push too.
PUSHING_JOBS="${AUTOCOMMIT_PUSHING_JOBS:-repo-sync-nightly ai-apps-nightly}"
# launchd labels are <prefix>.cron.<job>, as local-cron-jobs writes them.
LABEL_PREFIX="${SYSTEM_TOOLS_LABEL_PREFIX:-local.system-tools}"
# Asks GitHub whether the org still allows deploy keys. The switch belongs to
# the enterprise, not to this repository, and a flip refuses every key at once.
GH="${AUTOCOMMIT_GH:-gh}"

usage() {
  cat <<'USAGE'
autocommit.sh — commit and push one scoped path on behalf of a generator job.

Usage:
  autocommit.sh check  --scope PATH [--scope PATH ...] [--author NAME]
  autocommit.sh commit --scope PATH [--scope PATH ...] --message SUBJECT
                       [--body TEXT] [--author NAME] [--empty-ok]
  autocommit.sh status
  autocommit.sh key path|ssh-command|create
  autocommit.sh test [-v]
  autocommit.sh help

Verbs:
  check   Run before the job writes. Verifies the checkout sits on the default
          branch, fast-forwards it onto the remote, verifies the scope is
          clean, and verifies no earlier run of this author left a commit
          unpushed. A job that gets a non-zero answer must not write.
  commit  Run after the job writes. Fast-forwards onto the remote first, so
          the new commit is one the remote can take. Stages --scope only,
          refuses if anything outside it is staged, commits as --author, then
          `pull --ff-only` and `push`, each checked on its own.
  status  Report every scope this repository auto-commits, and whether each is
          clean, and whether this machine holds the deploy key its installed
          jobs push with. Exit 0 healthy, 3 nothing to check, 1 a commit under
          one of those scopes that the remote does not have, or a pushing job
          installed with the key missing or readable by others, or with the
          org disallowing deploy keys (asked with `gh`; AUTOCOMMIT_GH
          overrides it, and an unreadable answer is a note, not a fault).
          local-health-check reads these codes.
  key     The per-machine deploy key the ruleset on main exempts from its
          required checks. `path` prints where it lives, `ssh-command` prints
          the GIT_SSH_COMMAND that uses it (exit 3 when it is missing), and
          `create` makes it and prints the command that registers it.
          AUTOCOMMIT_SSH_KEY overrides the path.

Options:
  --scope PATH     Repo-relative file or directory. The only paths the commit
                   may touch. Required, and repeatable: a generator that
                   rewrites more than one tracked file names each one, and
                   they land in a single commit. A file left out of the list
                   is a file left dirty for good.
  --message TEXT   Commit subject. Required for `commit`.
  --body TEXT      Commit body. Optional.
  --author NAME    Identity for the commit and for unpushed detection.
                   Default: autocommit. Commits as NAME <NAME@localhost>.
  --empty-ok       `commit` exits 0 rather than 3 when the scope has no change.
                   Generators that usually change nothing want this.

Exit codes: 0 done or nothing to do, 3 deferred (wrong branch, scope dirty),
1 broken (a commit exists that the remote does not have).
USAGE
}

die() { echo "$ME: $*" >&2; exit 1; }
defer() { echo "$ME: $*" >&2; exit 3; }

# The branch a commit here is allowed to land on. Asked of the remote rather
# than assumed: a checkout whose default is not `main` would otherwise have
# every nightly commit refused with a message naming the wrong branch.
default_branch() {
  ref=$(git symbolic-ref --quiet refs/remotes/origin/HEAD 2>/dev/null || true)
  if [ -n "$ref" ]; then
    basename "$ref"
    return 0
  fi
  # No origin/HEAD on this clone. `git remote set-head origin -a` writes it;
  # until someone does, fall back rather than refuse every run.
  if git show-ref --verify --quiet refs/heads/main; then echo main; else echo master; fi
}

# A commit this author made and could not push. Without this, the run that
# failed to push reports it once and every later run says "no changes" and
# exits 0 -- the commit sits unpushed indefinitely and the log looks healthy.
# Narrowed to this job's own author so an unrelated local commit, which is a
# normal state for a person's checkout, does not block the job.
unpushed_for() { # author
  git rev-parse --abbrev-ref '@{u}' >/dev/null 2>&1 || return 0
  git log '@{u}..HEAD' --author="$1@localhost" --oneline 2>/dev/null || true
}

# The same question for `status`, asked of the write set rather than of one
# identity: any commit the remote does not have that touches a scope this
# repository auto-commits. `cmd_status` says why that is the right question.
unpushed_under_scopes() {
  git rev-parse --abbrev-ref '@{u}' >/dev/null 2>&1 || return 0
  old_ifs=$IFS; IFS=$NL
  set --
  for scope in $(autocommitted_scopes); do IFS=$old_ifs; set -- "$@" "$scope"; IFS=$NL; done
  IFS=$old_ifs
  [ $# -gt 0 ] || return 0
  git log '@{u}..HEAD' --format='%h %an  %s' -- "$@" 2>/dev/null || true
}

# Newline-separated, because a scope may contain spaces. Every consumer
# iterates it with IFS=$NL, or turns it into positional parameters with
# `scope_args`, and never by bare word splitting.
NL='
'
SCOPES_GIVEN=""; MESSAGE=""; BODY=""; AUTHOR="autocommit"; EMPTY_OK=0
parse_options() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --scope)    [ $# -ge 2 ] || die "--scope needs a value"
                  SCOPES_GIVEN="${SCOPES_GIVEN:+$SCOPES_GIVEN$NL}$2"; shift 2 ;;
      --message)  [ $# -ge 2 ] || die "--message needs a value"; MESSAGE="$2"; shift 2 ;;
      --body)     [ $# -ge 2 ] || die "--body needs a value"; BODY="$2"; shift 2 ;;
      --author)   [ $# -ge 2 ] || die "--author needs a value"; AUTHOR="$2"; shift 2 ;;
      --empty-ok) EMPTY_OK=1; shift ;;
      *) die "unknown option: $1" ;;
    esac
  done
  [ -n "$SCOPES_GIVEN" ] || die "--scope is required"
  # Each one checked on its own. An absolute or climbing scope would take the
  # blast radius outside the repository, and every check below is written
  # against a repo-relative path.
  cleaned=""
  old_ifs=$IFS; IFS=$NL
  for scope in $SCOPES_GIVEN; do
    IFS=$old_ifs
    case "$scope" in
      /*)   die "--scope must be repo-relative, not absolute: $scope" ;;
      *..*) die "--scope must not climb out of the repository: $scope" ;;
    esac
    scope="${scope%/}"
    [ -e "$ROOT/$scope" ] || die "--scope does not exist in this repository: $scope"
    cleaned="${cleaned:+$cleaned$NL}$scope"
    IFS=$NL
  done
  IFS=$old_ifs
  SCOPES_GIVEN="$cleaned"
}

# The scopes as positional parameters, for a `git ... -- "$@"` pathspec. A
# POSIX shell has no arrays, and word splitting on a path with a space in it
# would hand git two pathspecs that match nothing.
scope_args() {
  old_ifs=$IFS; IFS=$NL
  set --
  for scope in $SCOPES_GIVEN; do IFS=$old_ifs; set -- "$@" "$scope"; IFS=$NL; done
  IFS=$old_ifs
  printf '%s\0' "$@"
}

# One argument, quoted for the shell that git runs GIT_SSH_COMMAND through. A
# double-quoted path would let a `"` or a `$(...)` in it change the command.
shquote() {
  printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}

# The deploy key and nothing else. IdentitiesOnly still offers the config's
# own IdentityFile entries (`Host *` names the operator's key), and ssh offers
# agent-held keys before file-only ones, so the agent is switched off: with it
# on, GitHub can take the operator's key first and the ruleset sees the wrong
# actor. BatchMode stops a prompt nobody is there to answer. The config is
# still read, because `Host github.com` routes through ssh.github.com:443.
ssh_command() {
  printf 'ssh -i %s -o IdentitiesOnly=yes -o IdentityAgent=none -o BatchMode=yes' "$(shquote "$KEY")"
}

# Point git's transport at the deploy key for the fetch, the pull and the push.
# Without it the push goes out as the default identity and the required checks
# on main refuse it; naming the cause here is what the push error would not do.
use_deploy_key() {
  if [ -f "$KEY" ]; then
    GIT_SSH_COMMAND=$(ssh_command)
    export GIT_SSH_COMMAND
  else
    echo "$ME: MISSING deploy key $KEY; pushing as the default identity, which" >&2
    echo "  the required checks on main refuse. Create it: autocommit.sh key create" >&2
  fi
}

# `git status --porcelain` over every scope at once.
scopes_status() {
  old_ifs=$IFS; IFS=$NL
  set --
  for scope in $SCOPES_GIVEN; do IFS=$old_ifs; set -- "$@" "$scope"; IFS=$NL; done
  IFS=$old_ifs
  git status --porcelain -- "$@"
}

# The scopes as one readable phrase for a message a person reads.
scopes_phrase() {
  printf '%s' "$SCOPES_GIVEN" | tr '\n' ' ' | sed 's/ $//'
}

# Catch up with the remote BEFORE writing a commit, never only after.
#
# The commit used to be made on whatever the checkout happened to hold, with
# `pull --ff-only` run afterwards. Pull requests land in this repository all
# day and nothing pulls `main` on a timer, so the checkout is routinely a
# commit or two behind by the time a nightly job fires. The new commit and the
# remote's were then siblings, the fast-forward was no longer possible, and
# the commit was stranded locally for good -- while `check` on every later
# night read that same commit as "an earlier run did not push" and refused.
#
# That is 2026-09-21 exactly: 81c9b93 landed on the remote at 03:42, this
# checkout still held its parent ad82388, ai-apps committed 20d2bbf on
# ad82388 at 04:30, and the fast-forward that followed answered "Not possible
# to fast-forward, aborting". One commit behind was enough.
#
# Fast-forward only, and only when the checkout is behind. Resolving a
# divergence unattended is the one thing this tool must not do: behind is the
# state worth fixing, diverged is the state worth reporting, and the check
# below still reports it.
sync_with_remote() {
  git rev-parse --abbrev-ref '@{u}' >/dev/null 2>&1 || return 0
  if ! git fetch -q; then
    echo "$ME: fetch failed; working from what the checkout already has." >&2
    return 0
  fi
  # Ahead or diverged is not this function's business. `unpushed_for` reports
  # a stranded commit, and the push reports a divergence nobody can flatten.
  git merge-base --is-ancestor HEAD '@{u}' 2>/dev/null || return 0
  if ! git merge -q --ff-only '@{u}'; then
    defer "the checkout is behind $(git rev-parse --abbrev-ref '@{u}') and the
  fast-forward did not apply; nothing committed. A local edit is in the way of
  a file the remote changed, and a person needs to reconcile it."
  fi
}

# Shared by both verbs: the commit must land on the default branch, the
# checkout must be level with the remote, and the remote must already have
# everything this author committed earlier.
assert_landable() {
  branch=$(git rev-parse --abbrev-ref HEAD)
  want=$(default_branch)
  if [ "$branch" != "$want" ]; then
    defer "the checkout is on '$branch', not '$want'; nothing committed.
  A generated file committed onto someone's feature branch rides into their
  pull request. Re-run once the checkout is back on '$want'."
  fi
  sync_with_remote
  unpushed=$(unpushed_for "$AUTHOR")
  if [ -n "$unpushed" ]; then
    echo "$ME: an earlier run committed as '$AUTHOR' but did not push:" >&2
    printf '%s\n' "$unpushed" | sed 's/^/  /' >&2
    echo "  The commit is safe on disk. A person needs to push or reconcile it." >&2
    exit 1
  fi
}

cmd_check() {
  parse_options "$@"
  cd "$ROOT"
  use_deploy_key
  assert_landable
  # Any one dirty scope defers the whole job. The generator writes all of them
  # or none, so clearing a subset would leave the rest to be swept in later.
  if [ -n "$(scopes_status)" ]; then
    echo "$ME: $(scopes_phrase) is already dirty before the job ran:" >&2
    scopes_status | sed 's/^/  /' >&2
    defer "someone is mid-edit, or an earlier run left it uncommitted.
  Refusing before the generator overwrites it -- a hand edit swept into an
  unattended commit is one nobody reviewed, and one the generator would
  otherwise destroy silently."
  fi
  echo "$ME: $(scopes_phrase) is clean on $(git rev-parse --abbrev-ref HEAD); safe to write."
}

cmd_commit() {
  parse_options "$@"
  [ -n "$MESSAGE" ] || die "--message is required for commit"
  cd "$ROOT"
  use_deploy_key
  assert_landable

  if [ -z "$(scopes_status)" ]; then
    echo "$ME: no changes under $(scopes_phrase)."
    [ "$EMPTY_OK" -eq 1 ] && exit 0
    exit 3
  fi

  # Explicit pathspec, never `git add -A`. A generator touches files outside
  # its own scope, and `-A` would sweep in untracked ones nobody looked at --
  # which is how a profile-capture job's author committed two unreviewed files
  # by hand while building it.
  old_ifs=$IFS; IFS=$NL
  set --
  for scope in $SCOPES_GIVEN; do IFS=$old_ifs; set -- "$@" "$scope"; IFS=$NL; done
  IFS=$old_ifs
  git add -- "$@"

  # Prove it. A staged path outside the scopes means the pathspec did not hold,
  # and an unattended push is the wrong place to find that out. Both forms are
  # checked for each scope because a scope may be a single file, where
  # "$scope/" never matches, or a directory, where the bare name never does.
  outside=$(git diff --cached --name-only)
  old_ifs=$IFS; IFS=$NL
  for scope in $SCOPES_GIVEN; do
    IFS=$old_ifs
    outside=$(printf '%s' "$outside" | grep -v -e "^$scope\$" -e "^$scope/" || true)
    IFS=$NL
  done
  IFS=$old_ifs
  if [ -n "$outside" ]; then
    echo "$ME: staged paths outside $(scopes_phrase); refusing to commit." >&2
    printf '%s\n' "$outside" | sed 's/^/  /' >&2
    git reset -q
    exit 1
  fi

  if [ -n "$BODY" ]; then
    git -c user.name="$AUTHOR" -c user.email="$AUTHOR@localhost" \
      commit -q -m "$MESSAGE" -m "$BODY"
  else
    git -c user.name="$AUTHOR" -c user.email="$AUTHOR@localhost" \
      commit -q -m "$MESSAGE"
  fi

  # Checked one at a time, and not as `pull && push`. `set -e` exempts a
  # failing command inside an AND-OR list, so the list can fail whole and
  # execution carries on to the success message below:
  #
  #   $ sh -c 'set -eu; git -C /nonexistent pull -q --ff-only && git push -q
  #            echo "reached the success line"'
  #   reached the success line
  #   script exit=0
  #
  # Which is this tool's own failure mode in miniature -- reporting success
  # having not done the thing -- in the one place where nobody is watching.
  if ! git pull -q --ff-only; then
    echo "$ME: pull --ff-only failed; the commit is local only." >&2
    echo "  The remote advanced and this cannot fast-forward, or authentication" >&2
    echo "  failed. The commit is safe on disk; a person needs to reconcile." >&2
    exit 1
  fi
  if ! git push -q; then
    echo "$ME: push failed; the commit is local only." >&2
    exit 1
  fi
  echo "$ME: committed and pushed $(scopes_phrase) — $MESSAGE"
}

# The scopes this repository auto-commits, enumerated from the filesystem
# rather than listed. A written list drifts the moment a generator gains a
# file: this one named `repos.personal.conf` alone while `repos.common.conf`,
# `repos.work.conf` and `repos.laptop.conf` sat beside it, so `status` reported
# clean on three files it never looked at. Each job still passes its own
# --scope; this decides nothing and is a report.
autocommitted_scopes() {
  for candidate in \
    "$ROOT"/local-ai-apps/profiles \
    "$ROOT"/local-repo-sync/repos.*.conf
  do
    [ -e "$candidate" ] || continue
    printf '%s\n' "${candidate#"$ROOT"/}"
  done
}

# owner/repo from origin: the last two path segments, whatever the URL's
# shape -- scp-like, or ssh:// with a port, where a prefix strip leaves `443/`
# in front.
origin_slug() {
  git -C "$ROOT" remote get-url origin | sed -e 's#\.git$##' -e 's#/*$##' \
    | awk -F/ '{ print $(NF-1) "/" $NF }' | sed 's#^.*:##'
}

# What is wrong with this machine's deploy key, printed, or nothing. Only a
# machine with a pushing job installed can be wrong: one with none has no use
# for the key, and a report there would be a finding nobody can act on.
# Installed means a plist in the LaunchAgents folder, which is what
# `cron-jobs.sh install` writes and `uninstall` removes.
deploy_key_problem() {
  installed=""
  for job in $PUSHING_JOBS; do
    [ -f "$AGENTS/$LABEL_PREFIX.cron.$job.plist" ] && installed="$installed $job"
  done
  [ -n "$installed" ] || return 0
  if [ ! -f "$KEY" ]; then
    echo "MISSING deploy key $KEY, which these installed jobs push with:$installed"
    return 0
  fi
  # Read from `ls`, not `stat`: BSD and GNU `stat` take different flags, and
  # GNU's `-f` is a filesystem report that prints before it fails.
  mode=$(ls -ld "$KEY" | cut -c1-10)
  case "$mode" in
    -??-------) ;;
    *) echo "deploy key $KEY is mode $mode; ssh refuses a private key others can read"
       return 0 ;;
  esac
  # A healthy key is refused all the same when the org disallows deploy keys,
  # and the enterprise owns that setting (2026-09-23: the org's own PATCH
  # answers 422). Asked here, a flip is named the night it happens and not
  # first seen as a refused push. An answer that cannot be read -- no `gh`, no
  # login under launchd, no network -- is a note and not a fault: it says
  # nothing about the setting, and a finding raised on it could not be acted on.
  owner=$(origin_slug | cut -d/ -f1)
  if enabled=$("$GH" api "orgs/$owner" --jq .deploy_keys_enabled_for_repositories 2>/dev/null); then
    case "$enabled" in
      true) ;;
      false) echo "DISABLED deploy keys in org $owner, which these installed jobs push with:$installed. An enterprise or org policy turned them off; every push will be refused." ;;
      *) echo "$ME: note: org $owner answered '$enabled' for deploy keys; not checked" >&2 ;;
    esac
  else
    echo "$ME: note: could not read org $owner's deploy-key setting with $GH; not checked" >&2
  fi
}

cmd_key() {
  case "${1:-}" in
    path) echo "$KEY" ;;
    ssh-command)
      [ -f "$KEY" ] || { echo "$ME: MISSING deploy key $KEY" >&2; exit 3; }
      ssh_command; echo ;;
    create)
      [ ! -e "$KEY" ] || die "$KEY already exists; refusing to replace a key that may be registered."
      slug=$(origin_slug)
      host=$(hostname -s)
      mkdir -p "$(dirname "$KEY")"
      ssh-keygen -q -t ed25519 -N "" -C "autocommit@$host" -f "$KEY"
      chmod 600 "$KEY"
      echo "created $KEY. Register it on $slug as a deploy key with write access:"
      echo "  gh api repos/$slug/keys -f title=$(shquote "autocommit $host") -F key=@$(shquote "$KEY.pub") -F read_only=false"
      ;;
    *) die "key wants path, ssh-command or create" ;;
  esac
}

cmd_status() {
  cd "$ROOT"
  # First, because a broken key outranks "nothing to check": a machine whose
  # pushing job is installed without its key fails every night whatever the
  # scopes look like.
  problem=$(deploy_key_problem)
  if [ -n "$problem" ]; then
    echo "$ME: $problem"
    exit 1
  fi
  present=0; dirty=0
  old_ifs=$IFS; IFS=$NL
  for scope in $(autocommitted_scopes); do
    IFS=$old_ifs
    present=$((present + 1))
    if [ -n "$(git status --porcelain -- "$scope")" ]; then
      echo "DIRTY   $scope"
      dirty=$((dirty + 1))
    else
      echo "clean   $scope"
    fi
    IFS=$NL
  done
  IFS=$old_ifs
  [ "$present" -eq 0 ] && { echo "$ME: no auto-committed scopes on this machine."; exit 3; }
  # Asked of the scopes, not of one author. This read `unpushed_for autocommit`
  # -- the default identity no caller passes: ai-apps commits as `ai-apps` and
  # repo-sync as `repo-sync`, so the single state this report exists to raise
  # was the one state it could not see. On 2026-09-21 an `ai-apps` commit sat
  # unpushed for nine hours while this printed "nothing unpushed" and exited 0,
  # and local-health-check read that 0. An author list would drift the next
  # time a generator is added; the scopes above are already enumerated from the
  # filesystem, and a generated file the remote does not have is a fault
  # whoever wrote it.
  stuck=$(unpushed_under_scopes)
  if [ -n "$stuck" ]; then
    echo "$ME: commits the remote does not have, under an auto-committed scope:"
    printf '%s\n' "$stuck" | sed 's/^/  /'
    exit 1
  fi
  # A dirty scope on its own is the ordinary state between a generator writing
  # and its own commit landing, and it is what the jobs exist to clear. It is
  # only a fault when a commit is also stuck, which is checked above.
  echo "$ME: $present scope(s), $dirty dirty, nothing unpushed."
  exit 0
}

case "${1:-}" in
  check)  shift; cmd_check "$@" ;;
  commit) shift; cmd_commit "$@" ;;
  status) shift; cmd_status "$@" ;;
  key)    shift; cmd_key "$@" ;;
  test)   shift; exec "${PYTHON:-python3}" -m unittest discover -s "$DIR/tests" -t "$DIR" "$@" ;;
  -h|--help|help) usage; exit 0 ;;
  "") usage >&2; exit 1 ;;
  *) echo "$ME: unknown command: $1" >&2; usage >&2; exit 1 ;;
esac

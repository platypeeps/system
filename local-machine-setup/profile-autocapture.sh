#!/bin/sh
# Commit additive profile-roster changes, unattended. Nothing else.
#
# `machine-setup.sh capture` is a mirror: it rebuilds each roster from what is
# installed right now and replaces the file wholesale. That is safe in one
# direction and not the other. Installing a cron job and having the roster
# record it is a faithful log of a deliberate act. Losing an entry is a silent
# deletion from the declared configuration, and the causes are rarely
# deliberate -- a job uninstalled to debug something, a plist that failed to
# load after an upgrade, an agent unloaded by hand. Rebuild the machine and it
# is simply not there.
#
# So this runs `capture --additive`, which writes additions and refuses
# removals, and commits only what that produced. What makes an unattended
# commit safe here is not care, it is that the destructive direction is one
# the tool will not go.
#
# Exit 0 when it committed or had nothing to do; non-zero only when a person
# is genuinely needed. Removals are rare, which is exactly what makes them
# worth a notification -- the reverse of the daily drift banner this replaces.
set -eu

# Same idiom as machine-setup.sh, deliberately, so the two agree.
DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$DIR/.." && pwd)"
# The profile directory machine-setup.sh reads, resolved the same way. The
# rosters live outside this repository, in the operator's config; that
# config is a git checkout of its own when it is versioned at all.
SYSTEM_TOOLS_CONFIG="${SYSTEM_TOOLS_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/system}"
PROFILE_DIR="${MACHINE_SETUP_PROFILE_DIR:-$SYSTEM_TOOLS_CONFIG/machine-setup/profiles}"
if [ ! -d "$PROFILE_DIR" ]; then
  echo "profile-autocapture: no profile directory at $PROFILE_DIR; nothing to capture." >&2
  exit 1
fi
PROFILE_DIR="$(cd "$PROFILE_DIR" && pwd -P)"

# Unversioned config: capture still records additions, and there is nothing
# to commit. Capture's own exit (0, or 3 for a held removal) is the answer.
if ! REPO=$(git -C "$PROFILE_DIR" rev-parse --show-toplevel 2>/dev/null); then
  rc=0
  sh "$DIR/machine-setup.sh" capture --apply --additive || rc=$?
  echo "profile-autocapture: $PROFILE_DIR is not under version control; nothing to commit."
  exit "$rc"
fi
REPO="$(cd "$REPO" && pwd -P)"
# The only path this job may ever touch. Everything below is written against
# this one variable so that widening the blast radius takes a deliberate edit.
SCOPE="${PROFILE_DIR#"$REPO"/}"
[ "$SCOPE" != "$PROFILE_DIR" ] || SCOPE="."

cd "$REPO"

# The deploy key a ruleset on the default branch exempts. autocommit.sh
# owns where it lives, and exits 3 with a MISSING line on stderr when it is
# absent; the push then goes out as the default identity and names why it was
# refused. Any other exit is a broken helper, and pushing on regardless would
# be the silent fallback this job exists to avoid.
key_rc=0
ssh_cmd=$(sh "$ROOT/local-autocommit/autocommit.sh" key ssh-command) || key_rc=$?
case "$key_rc" in
  0) GIT_SSH_COMMAND=$ssh_cmd; export GIT_SSH_COMMAND ;;
  3) ;;
  *) echo "profile-autocapture: autocommit.sh key ssh-command failed ($key_rc); nothing written." >&2
     exit 1 ;;
esac

if [ -n "$(git status --porcelain -- "$SCOPE")" ]; then
  echo "profile-autocapture: $SCOPE is already dirty before capture; refusing."
  echo "  Someone is mid-edit, or a previous run left it uncommitted. Resolve"
  echo "  by hand -- committing on top would bundle work nobody reviewed."
  git status --short -- "$SCOPE"
  exit 1
fi

# Catch up with the remote BEFORE capture, not only after the commit.
#
# `capture --apply` refuses outright on a checkout that is behind: the
# manifests it rewrites wholesale are the ones the missing commits may have
# changed (check_repo_current in machine-setup.sh). Nothing pulls the
# checkout on a timer, and commits land on the remote from other machines.
# Behind by one commit was enough.
#
# That was the case in the wild: the checkout sat one commit behind, capture
# answered "STALE checkout is 1 commit(s) behind origin/main / refusing to
# write", and the remedy capture names is a pull this job never ran.
#
# Only when the incoming commits leave the rosters alone. Read the refusal
# capture prints literally: "those commits may be what changed them." A
# commit that removes a roster entry is the case it means, and pulling it and
# capturing on top would undo it -- the entry is still installed here, so an
# additive capture reads the removal as an addition, writes it back, and
# pushes. The removal a person made on one machine would be reverted overnight
# by another. Commits that touch nothing under $SCOPE cannot do that, and they
# are what the checkout is usually behind by.
#
# Fast-forward only. A diverged checkout stays a report, never an unattended
# merge. A fetch that cannot reach the remote is not fatal -- the comparison
# falls back to the ref on disk, as capture's own check does, and the same
# ConnectTimeout bounds it: a job nobody is watching must not hang on a
# machine that cannot reach GitHub.
if git rev-parse --abbrev-ref '@{u}' >/dev/null 2>&1; then
  GIT_SSH_COMMAND="${GIT_SSH_COMMAND:-ssh} -o ConnectTimeout=5" git fetch -q 2>/dev/null \
    || echo "profile-autocapture: fetch failed; comparing against the ref on disk." >&2
  if [ "$(git rev-list --count 'HEAD..@{u}')" -gt 0 ]; then
    if [ -n "$(git diff --name-only 'HEAD..@{u}' -- "$SCOPE")" ]; then
      echo "profile-autocapture: incoming commits change $SCOPE; refusing." >&2
      git log --oneline 'HEAD..@{u}' -- "$SCOPE" | sed 's/^/  /' >&2
      echo "  A roster this machine rebuilds changed on the remote. An entry" >&2
      echo "  removed there may still be installed here, and an additive" >&2
      echo "  capture would push it straight back. Pull and reconcile by hand." >&2
      exit 1
    fi
    if ! git merge -q --ff-only '@{u}'; then
      echo "profile-autocapture: cannot fast-forward onto the remote; nothing written." >&2
      echo "  The checkout has diverged. Capture refuses to rewrite the rosters" >&2
      echo "  from a stale checkout, so a person needs to reconcile." >&2
      exit 1
    fi
  fi
fi

rc=0
sh "$DIR/machine-setup.sh" capture --apply --additive || rc=$?

if [ "$rc" -ne 0 ] && [ "$rc" -ne 3 ]; then
  echo "profile-autocapture: capture failed (rc=$rc); nothing committed."
  exit "$rc"
fi

# A commit this job made and could not push. Without this, the run that failed
# to push reports it once and every later run says "no roster changes" and
# exits 0 -- the commit sits unpushed indefinitely and the log looks healthy.
# Narrowed to this job's own author so an unrelated local commit, which is a
# normal state for a person's checkout, does not block the job.
if git rev-parse --abbrev-ref '@{u}' >/dev/null 2>&1; then
  unpushed=$(git log '@{u}..HEAD' --author=machine-setup@localhost --oneline || true)
  if [ -n "$unpushed" ]; then
    echo "profile-autocapture: an earlier run committed but did not push:" >&2
    printf '%s\n' "$unpushed" | sed 's/^/  /' >&2
    exit 1
  fi
fi

if [ -z "$(git status --porcelain -- "$SCOPE")" ]; then
  echo "profile-autocapture: no roster changes."
  [ "$rc" -eq 3 ] && exit 3
  exit 0
fi

# Explicit pathspec, never `git add -A`. Capture also touches files outside
# $SCOPE, and `-A` would sweep in untracked ones nobody looked at -- which is
# how this exact job's author committed two unreviewed files by hand while
# building it.
git add -- "$SCOPE"

# Prove it. A staged path outside $SCOPE means the pathspec did not hold, and
# an unattended push is the wrong place to find that out.
if [ "$SCOPE" = . ]; then
  outside=""
else
  outside=$(git diff --cached --name-only | grep -v "^$SCOPE/" || true)
fi
if [ -n "$outside" ]; then
  echo "profile-autocapture: staged paths outside $SCOPE; refusing to commit."
  printf '%s\n' "$outside" | sed 's/^/  /'
  git reset -q
  exit 1
fi

added=$(git diff --cached -U0 -- "$SCOPE" | grep '^+[^+]' | sed 's/^+//' | grep -v '^#' | grep -v '^$' || true)
summary=$(printf '%s' "$added" | tr '\n' ' ' | sed 's/ *$//')

git -c user.name="machine-setup" -c user.email="machine-setup@localhost" \
  commit -q -m "chore(machine-setup): roster gained ${summary:-entries}" \
  -m "Additive capture. Entries that appeared on this machine, recorded in the
profile rosters. Removals are never auto-committed: capture --additive
refuses to write a roster that lost an entry, so a job uninstalled to debug
something cannot quietly leave the declared configuration.

Committed by local-machine-setup/profile-autocapture.sh, scoped to
$SCOPE and nothing else."

# Checked one at a time, and not as `pull && push`. `set -e` exempts a failing
# command inside an AND-OR list, so the list can fail whole and execution
# carries on to the success message below:
#
#   $ sh -c 'set -eu; git -C /nonexistent pull -q --ff-only && git push -q
#            echo "reached the success line"'
#   reached the success line
#   script exit=0
#
# Which is this job's own failure mode in miniature -- reporting success having
# not done the thing -- in the one place where nobody is watching to notice.
if ! git pull -q --ff-only; then
  echo "profile-autocapture: pull --ff-only failed; the commit is local only." >&2
  echo "  The remote advanced and this cannot fast-forward, or authentication" >&2
  echo "  failed. The commit is safe on disk; a person needs to reconcile." >&2
  exit 1
fi
if ! git push -q; then
  echo "profile-autocapture: push failed; the commit is local only." >&2
  exit 1
fi
echo "profile-autocapture: committed and pushed — ${summary:-entries}"
[ "$rc" -eq 3 ] && exit 3
exit 0

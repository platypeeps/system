#!/bin/sh
# Nightly brew health: run brew doctor, fix what is safe to fix without a
# human (broken symlinks, stale downloads, unlinked kegs that still have a
# formula), and email a summary of anything that needs a decision
# (deprecated formulae/casks, untrusted taps, orphaned kegs, ...).
# Usage: brew-doctor.sh run
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
NOTIFY="$DIR/../local-notify/notify.sh"

case "${1:-}" in
  run)
    ;;
  -h|--help|help)
    cat <<'HELPEOF'
usage: brew-doctor.sh run

  run   brew doctor, then:
          auto-fix (never touches trust or installs):
            - broken symlinks / stale files  -> brew cleanup --prune=all
            - unlinked kegs with a formula   -> brew link (never --overwrite)
          report-only, emailed as a summary when present:
            - deprecated/disabled formulae and casks
            - untrusted taps (trusting is a security decision)
            - kegs with no formula, and anything else brew doctor raises
        Exits 0 when clean or when the email went out; exits 1 when the
        summary could not be delivered, so the cron failure push fires.

The email goes through local-notify's email channel (NOTIFY_EMAIL_TO/EMAIL_FROM in
local-notify/.env or the environment).
HELPEOF
    exit 0
    ;;
  *)
    echo "usage: $(basename "$0") run" >&2
    exit 1
    ;;
esac

command -v brew >/dev/null 2>&1 || { echo "brew missing" >&2; exit 1; }

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT INT TERM

brew doctor > "$tmp/doctor1" 2>&1 || true
if ! grep -q '^Warning:' "$tmp/doctor1"; then
  echo "brew doctor: clean"
  exit 0
fi

fixed=""

# --- auto-fixes (no security decisions, no installs, no removals) ---------

if grep -q 'Broken symlinks were found\|stale' "$tmp/doctor1"; then
  echo "autofix: brew cleanup --prune=all"
  brew cleanup --prune=all >/dev/null 2>&1 || true
  fixed="$fixed- brew cleanup --prune=all (broken symlinks / stale files)
"
fi

# Unlinked kegs: only link ones brew still has a formula for, and never
# --overwrite — an orphaned formula keg once clobbered a cask's binary here.
if grep -q 'You have unlinked kegs' "$tmp/doctor1"; then
  sed -n '/You have unlinked kegs/,/^$/p' "$tmp/doctor1" \
    | grep '^  [a-z0-9]' | tr -d ' ' > "$tmp/unlinked" || true
  while IFS= read -r keg; do
    [ -n "$keg" ] || continue
    if brew info --formula "$keg" >/dev/null 2>&1; then
      if brew link "$keg" >/dev/null 2>&1; then
        echo "autofix: brew link $keg"
        fixed="$fixed- brew link $keg
"
      else
        echo "skip: brew link $keg failed (conflict — needs a human)"
      fi
    else
      echo "skip: $keg has no formula (orphan — reported)"
    fi
  done < "$tmp/unlinked"
fi

# --- re-check and report ---------------------------------------------------

brew doctor > "$tmp/doctor2" 2>&1 || true
if ! grep -q '^Warning:' "$tmp/doctor2"; then
  echo "brew doctor: clean after auto-fixes"
  exit 0
fi

n=$(grep -c '^Warning:' "$tmp/doctor2")
echo "remaining: $n warning(s) — emailing summary"

subject="brew doctor: $n warning(s) on $(hostname -s)"
{
  echo "brew doctor nightly report — $(date '+%Y-%m-%d %H:%M') on $(hostname -s)"
  echo
  if [ -n "$fixed" ]; then
    echo "Auto-fixed:"
    printf '%s' "$fixed"
    echo
  fi
  echo "Needs a decision:"
  echo
  cat "$tmp/doctor2"
} > "$tmp/body"

if sh "$NOTIFY" -t "$subject" -k status -c ntfy,email -b "$(cat "$tmp/body")"; then
  echo "email sent"
  exit 0
else
  echo "email FAILED — exiting 1 so the cron failure push fires" >&2
  cat "$tmp/body" >&2
  exit 1
fi

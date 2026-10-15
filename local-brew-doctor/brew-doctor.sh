#!/bin/sh
# Nightly brew health: run brew doctor, fix what is safe to fix without a
# human (broken symlinks, stale downloads, unlinked kegs that still have a
# formula), and print a summary of anything that needs a decision
# (deprecated formulae/casks, untrusted taps, orphaned kegs, ...) with an
# SD_REPORT_ATTENTION line, so the run's report reaches Reports and Today.
# Usage: brew-doctor.sh run
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
. "$DIR/../lib/bounded.sh"

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
          report-only, printed as a summary when present, with an
          SD_REPORT_ATTENTION line so the run's report reaches Reports and
          Today:
            - deprecated/disabled formulae and casks
            - untrusted taps (trusting is a security decision)
            - kegs with no formula, and anything else brew doctor raises
        Exits 0; nothing is mailed.
HELPEOF
    exit 0
    ;;
  *)
    echo "usage: $(basename "$0") run" >&2
    exit 1
    ;;
esac

command -v brew >/dev/null 2>&1 || { echo "brew missing" >&2; exit 1; }

# Each brew call runs under a bound: on a night macOS stops answering
# permission checks, brew hangs (sd:2660, sd:3536). Its step line goes to fd 3,
# the job log, and not into the output this reads.
exec 3>&2
# shellcheck disable=SC2034 # read by st_step in lib/bounded.sh
ST_STEP_FD3=1

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT INT TERM

# brew doctor exits 1 on a warning. Any other failure, a timeout (124) or a
# signal among them, is no answer even after a partial warning, and a failure
# with no warning is no clean bill: neither may drive a fix (sd:3536).
doctor_answered() { # rc, output
  [ "$1" -eq 0 ] || { [ "$1" -eq 1 ] && grep -q '^Warning:' "$2"; }
}

d_rc=0
st_step 600 brew doctor < /dev/null > "$tmp/raw" 2>&1 || d_rc=$?
# A failed step names itself on stderr too, so in this capture; it is not brew's.
grep -v '^\[step ' "$tmp/raw" > "$tmp/doctor1" || :
if ! doctor_answered "$d_rc" "$tmp/doctor1"; then
  cat "$tmp/doctor1"
  echo "brew doctor FAILED (exit $d_rc)" >&2
  exit 1
fi
if ! grep -q '^Warning:' "$tmp/doctor1"; then
  echo "brew doctor: clean"
  exit 0
fi

fixed=""

# --- auto-fixes (no security decisions, no installs, no removals) ---------

if grep -q 'Broken symlinks were found\|stale' "$tmp/doctor1"; then
  echo "autofix: brew cleanup --prune=all"
  st_step 1800 brew cleanup --prune=all < /dev/null >/dev/null 2>&1 || true
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
    # 1 is no formula; a timeout or a signal is no answer, not an orphan.
    i_rc=0
    st_step 120 brew info --formula "$keg" < /dev/null >/dev/null 2>&1 || i_rc=$?
    if [ "$i_rc" -eq 124 ] || [ "$i_rc" -gt 128 ]; then
      echo "skip: brew info $keg did not answer (exit $i_rc)"
    elif [ "$i_rc" -eq 0 ]; then
      if st_step 300 brew link "$keg" < /dev/null >/dev/null 2>&1; then
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

d_rc=0
st_step 600 brew doctor < /dev/null > "$tmp/raw" 2>&1 || d_rc=$?
grep -v '^\[step ' "$tmp/raw" > "$tmp/doctor2" || :
if ! doctor_answered "$d_rc" "$tmp/doctor2"; then
  cat "$tmp/doctor2"
  echo "brew doctor re-check FAILED (exit $d_rc)" >&2
  exit 1
fi
if ! grep -q '^Warning:' "$tmp/doctor2"; then
  echo "brew doctor: clean after auto-fixes"
  exit 0
fi

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
# One line per warning: its first line and a checksum of its whole text, not
# the date, so a warning whose list changed is a new finding. The ingest's
# reason is the run's set of lines: the same set folds into the open report, a
# smaller one folds into it too (pack sd:4658), and a new warning opens its own
# report and closes the older one on this host, with the exceptions
# local-cron-jobs/README.md names (pack sd:4641). The host stays in each line,
# as every host files under the one job name.
awk -v d="$tmp" '/^Warning:/ { n++ } n { print > (d "/warning." n) }' "$tmp/doctor2"
i=1
while [ -f "$tmp/warning.$i" ]; do
  echo "SD_REPORT_ATTENTION: brew doctor on $(hostname -s): $(head -1 "$tmp/warning.$i") [$(cksum < "$tmp/warning.$i" | cut -d' ' -f1)]"
  i=$((i + 1))
done

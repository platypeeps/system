#!/bin/sh
# Notice files arriving in the mounted Google Drives and say what they are.
# Usage: drive-intake.sh fetch|peek|report|stamp|status|test
#
# Exit codes follow the repo convention and local-cron-jobs depends on them:
#   0  something to act on   3  nothing to do   1  a real error
#
# 3 rather than 1 for "nothing new" matters: a nightly job that exits 1 on a
# quiet day banners a failure every quiet day, and then nobody reads the
# banner. Note that local-cron-jobs itself treats ANY non-zero rc as a failure,
# so a job wrapping these verbs must map 3 to 0 before returning.
set -e

SELF="$0"
while [ -L "$SELF" ]; do
  link=$(readlink "$SELF")
  case "$link" in
    /*) SELF="$link" ;;
    *)  SELF="$(dirname "$SELF")/$link" ;;
  esac
done
DIR="$(cd "$(dirname "$SELF")" && pwd)"

case "$1" in
  test)
    shift
    exec "${PYTHON:-python3}" -m unittest discover -s "$DIR/tests" -t "$DIR" "$@"
    ;;
  fetch|peek|report|stamp|status)
    exec "${PYTHON:-python3}" "$DIR/drive_intake.py" "$@"
    ;;
  -h|--help|help)
    cat <<'HELPEOF'
usage: drive-intake.sh fetch|peek|report|stamp|status|test

  fetch     walk every configured root, compare each file against the state on
            path, size and modification time, and APPEND what is new, modified
            or moved to the arrivals log. The first run records a baseline and
            logs nothing, because 2,892 files being new is not news.
            exit 0 changed, 3 unchanged, 1 error
  peek      say what a fetch would find, and change nothing. This is the verb
            to type when you only want to look, because fetch advances state.
            exit 0 something to find, 3 nothing, 1 error
  report    print undelivered arrivals grouped by route. Reading never marks
            anything delivered. --all includes rows already delivered.
            exit 0 something to show, 3 nothing, 1 no log yet
  stamp     mark arrivals delivered, up to --through TIMESTAMP, default now.
            The digest calls this after the mail is actually sent.
            exit 0 stamped, 3 nothing to stamp, 1 no log
  status    config soundness, each root's presence, undelivered count, and how
            old the state is
            exit 0 fresh, 3 stale or never run, 1 unusable
  test      run the unittest suite in tests/

Routes: data-export, document, correspondence, noise. Rules live in
<config>/drive-intake/drive-intake.conf, first match wins, anything unmatched
is noise. Start from drive-intake.conf.example beside this script.

A path no rule matches is named by Jev instead of being dumped in noise. It
runs unless JEV_DRIVE_INTAKE is set to 0, off, false, no or disabled -- unset means on -- and
unless Jev cannot answer on this machine; the rules run first and unchanged
either way, and every failure routes the path to noise and says why on
stderr. Only the
relative path is sent, never a file's contents.

The arrivals log is append-only and carries a reported_at column, so a hand-run
fetch cannot eat the rows the morning digest has not yet mailed.

A move is detected by pairing a vanished file with an appeared one on basename,
size and modification time, and only when that pairing is unique on both sides.
An ambiguous pair is left as a new file rather than guessed at.

This never writes to a Drive. It opens a Drive path only with os.scandir and
never with a mode that can modify; the test suite proves it by hashing a
fixture tree before and after a fetch.

environment:
  DRIVE_INTAKE_CONFIG        conf to read (default <config>/drive-intake/drive-intake.conf;
                             <config> is $SYSTEM_TOOLS_CONFIG, default ~/.config/system)
  DRIVE_INTAKE_STATE         state directory (default ~/.local/share/drive-intake)
  DRIVE_INTAKE_STALE_HOURS   how old the state may be before status says STALE (default 36)
  JEV_DRIVE_INTAKE           0, off, false, no or disabled stops Jev routing what no rule
                             matched; unset means it routes
  DRIVE_INTAKE_JEV           path to the jev entrypoint (default ../local-jev/jev.sh)
HELPEOF
    exit 0
    ;;
  *)
    echo "usage: $(basename "$0") fetch|peek|report|stamp|status|test" >&2
    exit 1
    ;;
esac

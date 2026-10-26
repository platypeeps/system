#!/bin/sh
# Record additive profile-roster changes, unattended. Nothing else.
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
# removals. The rosters live in the config folder and keep no git history:
# the job writes them and commits nothing. Exit is capture's own: 0 when it
# wrote or had nothing to do, 3 when a removal was held. Removals are rare,
# which is exactly what makes them worth a notification -- the reverse of the
# daily drift banner this replaces. Any other exit is a failed capture.
#
# A held removal alerts once: while the same held set stands, later nights
# print it and exit 0, so one removal no one has ruled on fails no night after
# the first (sd:6307). A changed set alerts again, and so does one that comes
# back after a night with nothing held. A held exit whose output names nothing,
# or a record the job cannot write, alerts every night: an unknown is no repeat.
#
# It takes no arguments.
set -eu

# An argument it does not know is refused, not run: a mistyped `--help` used
# to write the profiles (sd:3546).
case "${1:-}" in
  -h|--help|help)
    echo "usage: profile-autocapture.sh   (takes no arguments)"
    echo "Record additive profile-roster changes: machine-setup.sh capture --apply --additive."
    echo "Exit 0 when it wrote or had nothing to do, 3 when a removal was held;"
    echo "the same held removal again exits 0 and says it is still held."
    exit 0 ;;
  "") ;;
  *)
    echo "usage: profile-autocapture.sh   (takes no arguments; help prints what it does)" >&2
    exit 1 ;;
esac

# Same idiom as machine-setup.sh, deliberately, so the two agree.
DIR="$(cd "$(dirname "$0")" && pwd)"
# The profile directory machine-setup.sh reads, resolved the same way.
SYSTEM_TOOLS_CONFIG="${SYSTEM_TOOLS_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/system}"
PROFILE_DIR="${MACHINE_SETUP_PROFILE_DIR:-$SYSTEM_TOOLS_CONFIG/machine-setup/profiles}"
if [ ! -d "$PROFILE_DIR" ]; then
  echo "profile-autocapture: no profile directory at $PROFILE_DIR; nothing to capture." >&2
  exit 1
fi

# The held set last alerted on, in machine-setup.sh's state folder.
STATE_DIR="${MACHINE_SETUP_STATE:-$HOME/.config/machine-setup}"
HELD_RECORD="$STATE_DIR/autocapture-held"

out=$(mktemp "${TMPDIR:-/tmp}/profile-autocapture.XXXXXX")
trap 'rm -f "$out"' EXIT
rc=0
sh "$DIR/machine-setup.sh" capture --apply --additive > "$out" || rc=$?
cat "$out"
if [ "$rc" -eq 0 ]; then
  rm -f "$HELD_RECORD"
elif [ "$rc" -eq 3 ]; then
  # The held manifests and their entries, as capture names them.
  held=$(grep -E '^    (HELD|REFUSED) |^              - ' "$out" || :)
  if [ -n "$held" ] && [ "$held" = "$(cat "$HELD_RECORD" 2>/dev/null || :)" ]; then
    echo "profile-autocapture: this removal is still held, reported before; not alerting again."
    rc=0
  elif [ -n "$held" ]; then
    { mkdir -p "$STATE_DIR" && printf '%s\n' "$held" > "$HELD_RECORD"; } 2>/dev/null \
      || echo "profile-autocapture: cannot record the held removal in $HELD_RECORD; it alerts again." >&2
  fi
else
  echo "profile-autocapture: capture failed (rc=$rc)." >&2
fi
exit "$rc"

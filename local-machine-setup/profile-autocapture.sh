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
# It takes no arguments.
set -eu

# Same idiom as machine-setup.sh, deliberately, so the two agree.
DIR="$(cd "$(dirname "$0")" && pwd)"
# The profile directory machine-setup.sh reads, resolved the same way.
SYSTEM_TOOLS_CONFIG="${SYSTEM_TOOLS_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/system}"
PROFILE_DIR="${MACHINE_SETUP_PROFILE_DIR:-$SYSTEM_TOOLS_CONFIG/machine-setup/profiles}"
if [ ! -d "$PROFILE_DIR" ]; then
  echo "profile-autocapture: no profile directory at $PROFILE_DIR; nothing to capture." >&2
  exit 1
fi

rc=0
sh "$DIR/machine-setup.sh" capture --apply --additive || rc=$?
if [ "$rc" -ne 0 ] && [ "$rc" -ne 3 ]; then
  echo "profile-autocapture: capture failed (rc=$rc)." >&2
fi
exit "$rc"

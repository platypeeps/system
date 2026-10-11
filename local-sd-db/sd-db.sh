#!/bin/sh
# Forwarder (sd:3281). The workflow database's entrypoint is the command pack's
# bin/sd-db now. A plist or cron job installed before the move still names this
# path, so it runs the pack's copy and warns; `machine-setup.sh doctor` lists
# each config that still names it. Delete this file once the doctor is clean.
set -e
PACK="${SD_PACK_ROOT:-$HOME/repos/platypeeps/sd-ai-command-pack}"
echo "local-sd-db/sd-db.sh is retired: running $PACK/bin/sd-db; run machine-setup.sh doctor to find the config that names this path" >&2
exec "$PACK/bin/sd-db" "$@"

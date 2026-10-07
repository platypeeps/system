#!/bin/sh
# The hub's alarm for satellite claims that go quiet (sd:2918).
# Usage: satellite-stale.sh status|check|run|test|help
#
# A satellite session claims an item with `sd-db.sh claim`; this asks
# `sd-db.sh satellite-stale` on the hub which claims show no progress for
# SD_SATELLITE_STALE_HOURS. The logic lives in local-sd-db
# (`sd_db.satellite_stale`); this folder gives it convention 1's entrypoint,
# convention 6's `status`, and the notifier for `run`.
# Record: docs/work/2026-10-07-satellite-staleness-alarm/.
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
. "$DIR/../lib/config.sh"

TOOL=satellite-stale

usage() {
  cat <<'USAGE'
satellite-stale.sh — alert when a satellite's claimed item shows no progress.

Usage: satellite-stale.sh <command>

  status   One line. Exits 0 when no open claim is stale, 1 when one is,
           3 when there is nothing to check (no database, no open claim, or
           this machine is a satellite) -- local-health-check reads these
           codes. Reads branch refs as the last `run` fetched them.
  check    One line per open claim: fresh, stale, quiet or skipped. Sends
           nothing. Hub only.
  run      The cron job's verb: fetch each claimed branch, then send one
           alert per stale episode through local-notify, inside the alert
           window. Exits 1 when an alert was not delivered. Hub only.
  test     Run this folder's tests (extra args go to unittest).
  help     This text.

Claims are written on a satellite:
  sd-db.sh claim ITEM [--branch B] [--quiet-until ISO]   sd-db.sh unclaim ITEM

Configuration: $SYSTEM_TOOLS_CONFIG/satellite-stale/.env or exported (copy
.env.example). Every value has a default.
  SD_SATELLITE_STALE_HOURS     hours without progress before an alert (3)
  SD_SATELLITE_STALE_WINDOW    local hours that may alert, START-END (7-22)
  SD_SATELLITE_STALE_CHANNELS  local-notify channels (ntfy,email)
USAGE
}

case "${1:-}" in
  -h|--help|help) usage; exit 0 ;;
  "") usage >&2; exit 1 ;;
esac

st_source_env "$TOOL"
SD_DB=${SATELLITE_STALE_SD_DB:-$DIR/../local-sd-db/sd-db.sh}

case "$1" in
  status)
    shift
    exec sh "$SD_DB" satellite-stale status "$@"
    ;;
  check)
    shift
    exec sh "$SD_DB" satellite-stale "$@"
    ;;
  run)
    shift
    SD_NOTIFY=${SD_NOTIFY:-$DIR/../local-notify/notify.sh}
    export SD_NOTIFY
    exec sh "$SD_DB" satellite-stale --notify "$@"
    ;;
  test)
    shift
    exec "${PYTHON:-python3}" -m unittest discover -s "$DIR/tests" -t "$DIR" "$@"
    ;;
  *)
    echo "satellite-stale.sh: unknown command $1" >&2
    usage >&2
    exit 1
    ;;
esac

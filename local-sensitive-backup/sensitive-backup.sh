#!/bin/sh
# Monthly backup of sensitive files to the 1Password vault Backups.
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
. "$DIR/../lib/config.sh"

case "${1:-}" in
  run|plan)
    exec "${PYTHON:-python3}" "$DIR/sensitive_backup.py" "$1"
    ;;
  test)
    shift
    exec "${PYTHON:-python3}" -m unittest discover -s "$DIR/tests" -t "$DIR" "$@"
    ;;
  -h|--help|help)
    cat <<'HELPEOF'
usage: sensitive-backup.sh run|plan|test

  plan   list the files a run would store; needs no 1Password sign-in
  run    store them as one document in the vault Backups, titled
         "<host> sensitive files <YYYY-MM>", and keep the newest 12 months
  test   run this folder's tests (extra args go to unittest)

The files come from $SYSTEM_TOOLS_CONFIG/sensitive-backup/targets (see
targets.example). run needs OP_SERVICE_ACCOUNT_TOKEN, a service account
that can write only to Backups; it never prints a file or a token.
HELPEOF
    exit 0
    ;;
  *)
    echo "usage: $(basename "$0") run|plan|test" >&2
    exit 1
    ;;
esac

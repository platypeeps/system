#!/bin/sh
# Links into the shared public-read S3 bucket (sd:5346): a presigned POST that
# lets someone outside AWS upload files under one key prefix, and the public
# URL of an uploaded key. Signing reads the uploader profile's keys through
# `aws configure export-credentials`; nothing here writes to AWS.
# Usage: shared-upload.sh link <key-prefix> [--expires 168h] [--max-mb N]|url <key>|test|help
set -e
# A link in ~/bin points here: walk it to the real file, so lib/ and the
# Python signer beside this script are found.
SELF="$0"
while [ -L "$SELF" ]; do
  link=$(readlink "$SELF")
  case "$link" in
    /*) SELF="$link" ;;
    *)  SELF="$(dirname "$SELF")/$link" ;;
  esac
done
DIR="$(cd "$(dirname "$SELF")" && pwd)"
. "$DIR/../lib/config.sh"

usage() {
  cat <<'HELPEOF'
usage: shared-upload.sh link <key-prefix> [--expires 168h] [--max-mb N] | url <key> | test | help

  link   print a presigned POST for uploads under <key-prefix>/: the form's
         url and fields, a ready curl line, and the public URL each file
         gets. One link takes many files; S3 names each after its file.
         --expires is a count and a unit (m, h or d), 168h at most, the
         Signature Version 4 limit; --max-mb caps each file's size.
         Keys signed by temporary credentials (SSO, a role) stop the link
         when they expire, and stderr says so.
  url    print the public GET URL of <key>.
  test   run the unittest suite (tests/) against a fake `aws` on PATH;
         extra arguments go to unittest (-v).

config: <config>/shared-upload/.env (<config> is $SYSTEM_TOOLS_CONFIG,
default ~/.config/system); copy local-shared-upload/.env.example there.
An exported value wins over the file.
  SHARED_UPLOAD_BUCKET    the bucket (link, url)
  SHARED_UPLOAD_REGION    its region (link, url)
  SHARED_UPLOAD_PROFILE   the AWS profile whose keys sign links (link)
  SHARED_UPLOAD_EXPIRES   a link's default lifetime (default 168h)
  SHARED_UPLOAD_MAX_MB    a file's default size cap in MiB (default 500)
  PYTHON                  interpreter for the signer (default python3)
HELPEOF
}

# need <name>...: each name set, from the environment or the config file.
need() {
  for name in "$@"; do
    eval "value=\${$name:-}"
    if [ -z "$value" ]; then
      st_missing "$name" shared-upload .env
      exit 1
    fi
  done
}

case "${1:-}" in
  link|url)
    st_source_env shared-upload
    need SHARED_UPLOAD_BUCKET SHARED_UPLOAD_REGION
    [ "$1" = url ] || need SHARED_UPLOAD_PROFILE
    exec "${PYTHON:-python3}" "$DIR/shared_upload.py" "$@"
    ;;
  test)
    shift
    exec "${PYTHON:-python3}" -m unittest discover -s "$DIR/tests" -t "$DIR" "$@"
    ;;
  -h|--help|help)
    usage
    exit 0
    ;;
  *)
    usage >&2
    exit 1
    ;;
esac

#!/bin/sh
# Mezmo pipeline control plane: the operational-state variable and the webhook
# source config, against api.mezmo.com's v3 API.
#
# Replaces mezmo-pipeline-state/ and mezmo-webhook-source/, which shared a
# service key, a pipeline id and a byte-identical placeholder guard across two
# folders and two .env files while disagreeing about the API host.
#
# Credentials and IDs come from <config>/mezmo-pipeline/.env (../lib/config.sh;
# see .env.example) or from the environment — the file is optional when the values are already exported.
# Usage: pipeline.sh state <verb> | source webhook | status | test
set -e
MZ_DIR="$(cd "$(dirname "$0")" && pwd)"

usage() {
  cat <<'HELPEOF'
usage: pipeline.sh state <verb> | source webhook | status | test

  state get            print the pipeline's current operational state
  state normal         set operational_state normal
  state incident       set operational_state incident
  state test-mode-on   incident + test_mode on
  state test-mode-off  incident + test_mode off

  source webhook       turn capture_metadata on and auto_parse off on the
                       webhook source, by reading the source, changing those
                       two fields and writing it back

  status               report API reachability and the current state without
                       writing anything. Exits 0 when healthy, 3 when the
                       folder is not configured, 1 when configured and
                       failing — local-health-check reads those codes.
  test                 run the offline test suite (makes no network calls)

Every write is followed by a read-back that prints the value the API now
holds, because these commands mutate a live account.

environment (<config>/mezmo-pipeline/.env or exported; <config> is
SYSTEM_TOOLS_CONFIG, default ~/.config/system; see .env.example):
  MEZMO_API_BASE             API host (default https://api.mezmo.com)
  MEZMO_PIPELINE_SERVICE_KEY required by everything
  MEZMO_PIPELINE_ID          required by everything
  MEZMO_PIPELINE_STATE_ID    required by `state`
  MEZMO_SOURCE_ID            required by `source`
  MEZMO_ACCOUNT_ID           required by `source`
HELPEOF
}

# Help must work without any configuration at all, so it is answered before
# anything reads .env or checks a variable.
case "${1:-}" in
  -h|--help|help) usage; exit 0 ;;
esac

# `test` runs before the env checks too: the suite is offline and must be
# runnable on a machine that has no Mezmo credentials at all (CI, for one).
if [ "${1:-}" = "test" ]; then
  shift
  exec sh "$MZ_DIR/tests/run.sh" "$@"
fi

# shellcheck disable=SC1091
. "$MZ_DIR/lib.sh"
mz_load_env "$MZ_DIR"

STATE_PATH="/v3/pipeline/state-variable"
SOURCE_PATH="/v3/pipeline/$MEZMO_PIPELINE_ID/source/$MEZMO_SOURCE_ID"

need_jq() {
  command -v jq >/dev/null 2>&1 || {
    echo "jq is required for $1 — brew install jq" >&2
    exit 1
  }
}

# Read the state back out of the API and print it. Used both by `status` and
# as the read-back after every `state` write.
read_state() {
  # The old script sent pipeline_id in a body on a GET. That is unusual — a
  # server is entitled to ignore a GET body — but it is what this deployment
  # answers today, so it is preserved verbatim rather than "fixed" blind
  # against a live work account. See the README gotcha.
  mz_api GET "$STATE_PATH" "{\"pipeline_id\": \"$MEZMO_PIPELINE_ID\"}"
}

state_cmd() {
  mz_require MEZMO_PIPELINE_SERVICE_KEY MEZMO_PIPELINE_ID MEZMO_PIPELINE_STATE_ID
  need_jq "state"
  case "$1" in
    get)
      read_state
      printf '%s' "$MZ_BODY" | jq '.data[0].state'
      ;;
    normal|incident|test-mode-on|test-mode-off)
      case "$1" in
        normal)        body=$(mz_state_body normal) ;;
        incident)      body=$(mz_state_body incident) ;;
        test-mode-on)  body=$(mz_state_body incident on) ;;
        test-mode-off) body=$(mz_state_body incident off) ;;
      esac
      mz_api PUT "$STATE_PATH/$MEZMO_PIPELINE_STATE_ID" "$body"
      echo "wrote $1 (HTTP $MZ_CODE); reading it back:"
      read_state
      printf '%s' "$MZ_BODY" | jq '.data[0].state'
      ;;
    ''|*)
      echo "usage: $(basename "$0") state get|normal|incident|test-mode-on|test-mode-off" >&2
      exit 1
      ;;
  esac
}

# Read, change two fields, write back.
#
# The old webhook-source.sh built a whole source object from scratch and PUT
# it, with "access_keys": [] and "signing_keys": [] hardcoded — against an API
# the folder's own README documented as replace-not-patch. Any key configured
# on that source was silently destroyed every time the script ran. Starting
# from what the server currently holds is the fix: fields this tool does not
# care about survive untouched because they are never retyped.
source_webhook() {
  mz_require MEZMO_PIPELINE_SERVICE_KEY MEZMO_PIPELINE_ID \
             MEZMO_SOURCE_ID MEZMO_ACCOUNT_ID
  need_jq "source webhook"

  if ! mz_api GET "$SOURCE_PATH"; then
    echo "refusing to write: could not read the current source config first." >&2
    echo "A blind PUT here replaces the whole object and would drop any" >&2
    echo "access_keys/signing_keys the source holds." >&2
    exit 1
  fi

  # The v3 API wraps single objects in {"data": ...}; unwrap when it does so
  # the same code handles both shapes.
  current=$(printf '%s' "$MZ_BODY" | jq -e 'if has("data") then .data else . end') || {
    echo "could not parse the source config the API returned:" >&2
    echo "$MZ_BODY" >&2
    exit 1
  }

  patched=$(printf '%s' "$current" | jq -e '
      .user_config.capture_metadata = true
    | .user_config.auto_parse.enabled = false
    | if (.gateway_route? // null) != null
        then .gateway_route.capture_metadata = true
        else . end
  ') || {
    echo "could not patch the source config" >&2
    exit 1
  }

  if [ "$current" = "$patched" ]; then
    echo "source already has capture_metadata on and auto_parse off — nothing to write."
  else
    mz_api PUT "$SOURCE_PATH" "$patched"
    echo "wrote source config (HTTP $MZ_CODE)"
  fi

  echo "reading it back:"
  mz_api GET "$SOURCE_PATH"
  printf '%s' "$MZ_BODY" \
    | jq 'if has("data") then .data else . end
          | {capture_metadata: .user_config.capture_metadata,
             auto_parse: .user_config.auto_parse.enabled,
             access_keys: (.gateway_route.access_keys | length),
             signing_keys: (.gateway_route.signing_keys | length)}'
}

# Exit 3 = not configured. local-health-check treats it as "skip", so a
# machine that has no Mezmo credentials does not report a nightly finding it
# could never act on.
status_cmd() {
  if ! mz_require MEZMO_PIPELINE_SERVICE_KEY MEZMO_PIPELINE_ID \
                  MEZMO_PIPELINE_STATE_ID 2>/dev/null; then
    echo "mezmo-pipeline: SKIP — not configured on this machine"
    exit 3
  fi
  if ! read_state; then
    # The code says what to do, and this tool is the one that knows: the
    # health check used to carry these two hints for every tool it swept,
    # which was right here and nonsense anywhere else.
    case "${MZ_CODE:-}" in
      401) why="the service key was rotated — replace MEZMO_PIPELINE_SERVICE_KEY" ;;
      404) why="the pipeline or state id moved — check MEZMO_PIPELINE_ID and MEZMO_PIPELINE_STATE_ID" ;;
      '')  why="network, DNS or TLS — curl could not complete the request" ;;
      *)   why="unexpected status from the API" ;;
    esac
    echo "mezmo-pipeline: FAIL — $MEZMO_API_BASE unreachable or rejecting (HTTP ${MZ_CODE:-none}): $why"
    exit 1
  fi
  if command -v jq >/dev/null 2>&1; then
    st=$(printf '%s' "$MZ_BODY" | jq -c '.data[0].state' 2>/dev/null || echo '?')
  else
    st='(jq not installed)'
  fi
  echo "mezmo-pipeline: OK — HTTP $MZ_CODE, state=$st"
}

cmd="${1:-}"
[ $# -gt 0 ] && shift
case "$cmd" in
  state)  state_cmd "${1:-}" ;;
  source)
    case "${1:-}" in
      webhook) source_webhook ;;
      *) echo "usage: $(basename "$0") source webhook" >&2; exit 1 ;;
    esac
    ;;
  status) status_cmd ;;
  *)
    usage >&2
    exit 1
    ;;
esac

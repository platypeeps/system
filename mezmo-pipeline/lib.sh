# Shared helpers for pipeline.sh. Sourced, never executed — no shebang, no
# `set -e` here; the entrypoint owns both.
#
# This file exists because mezmo-pipeline-state and mezmo-webhook-source each
# carried a byte-identical copy of the env loading and the placeholder guard,
# and the two drifted apart on the API host without anyone noticing (one
# talked to api.mezmo.com, the other to the legacy api.logdna.com for the same
# /v3/pipeline surface). One copy, one host.

# The API host. api.logdna.com is the legacy name for the same v3 surface and
# is no longer used anywhere here; override only to point at a test double.
MEZMO_API_BASE="${MEZMO_API_BASE:-https://api.mezmo.com}"

# The .env lives outside the checkout, at <config>/mezmo-pipeline/.env
# (../lib/config.sh resolves <config>). MZ_SKIP_ENV_FILE lets the offline
# suite run identically on a machine that has that file and one that does not. Without it the
# tests would pass or fail depending on whether the operator had configured
# the tool, which is exactly the kind of environment-dependent suite that
# reads green everywhere except CI.
mz_load_env() {
  # shellcheck source=../lib/config.sh
  . "$1/../lib/config.sh"
  if [ -z "${MZ_SKIP_ENV_FILE:-}" ]; then
    st_source_env mezmo-pipeline
  fi
  # Re-resolve after .env, so a base set there wins over the default above but
  # never over one already exported.
  MEZMO_API_BASE="${MEZMO_API_BASE:-https://api.mezmo.com}"
}

# A value that is present but still the .env.example placeholder is worse than
# an absent one: a bare -n test passes, and the script goes on to send an
# authenticated request carrying a null UUID — so the failure comes back as a
# remote error about an id that does not exist, instead of a local line naming
# the variable to fill in. The patterns match what machine-setup's doctor
# reports on, so the two agree about what "still a placeholder" means.
# Substring rather than prefix for change-me so `sts_change-me` is caught too;
# a real credential containing that string is not a thing.
mz_is_placeholder() {
  case "$1" in
    *change-me*|*changeme*|*CHANGE_ME*|your-*|xxx*|XXX*|*placeholder*) return 0 ;;
    00000000-0000-0000-0000-000000000000) return 0 ;;
  esac
  return 1
}

# Only the variables the chosen subcommand actually needs are required, so
# `state get` does not demand the source ids and vice versa.
mz_require() {
  _missing=""
  _placeholder=""
  for _v in "$@"; do
    eval "_val=\${$_v:-}"
    if [ -z "$_val" ]; then
      _missing="$_missing $_v"
    elif mz_is_placeholder "$_val"; then
      _placeholder="$_placeholder $_v"
    fi
  done
  # Separate `if`s, not `[ -n ... ] && echo`: an AND-list that fails is the
  # block's exit status, and under set -e a script whose only problem was a
  # placeholder would die on the missing-values line instead of printing both.
  if [ -n "$_missing" ] || [ -n "$_placeholder" ]; then
    for _v in $_missing; do
      st_missing "$_v" mezmo-pipeline .env mezmo-pipeline
    done
    if [ -n "$_placeholder" ]; then
      echo "still at .env.example placeholder(s):$_placeholder" >&2
      echo "  fill them in at $(st_config_dir mezmo-pipeline)/.env, or export real values" >&2
    fi
    return 1
  fi
  return 0
}

# Every call goes through here so that no request can succeed silently at the
# shell level while failing at the HTTP level. The old scripts used `curl -i`
# and `curl -s` with no status check at all: a 401 or a 404 printed and exited
# 0, `set -e` never fired, and a state flip that never happened was
# indistinguishable from one that did.
#
# Sets MZ_BODY to the response body and MZ_CODE to the status. Returns
# non-zero for a transport failure or any status outside 2xx.
mz_api() {
  _method=$1
  _path=$2
  _body=${3:-}
  _url="$MEZMO_API_BASE$_path"
  _out=$(mktemp)
  if [ -n "$_body" ]; then
    MZ_CODE=$(curl -sS -o "$_out" -w '%{http_code}' \
      --request "$_method" --url "$_url" \
      -H "Authorization: Token $MEZMO_PIPELINE_SERVICE_KEY" \
      -H 'Content-Type: application/json' \
      --data "$_body") || MZ_CODE=""
  else
    MZ_CODE=$(curl -sS -o "$_out" -w '%{http_code}' \
      --request "$_method" --url "$_url" \
      -H "Authorization: Token $MEZMO_PIPELINE_SERVICE_KEY" \
      -H 'Content-Type: application/json') || MZ_CODE=""
  fi
  MZ_BODY=$(cat "$_out")
  rm -f "$_out"
  if [ -z "$MZ_CODE" ]; then
    echo "$_method $_url: curl could not complete the request (network/DNS/TLS)" >&2
    return 1
  fi
  case "$MZ_CODE" in
    2??) return 0 ;;
  esac
  echo "$_method $_url: HTTP $MZ_CODE" >&2
  [ -n "$MZ_BODY" ] && echo "$MZ_BODY" >&2
  return 1
}

# Body builders are separate functions so the offline suite can assert on the
# exact JSON without a network call.
mz_state_body() {
  # $1 operational_state, $2 optional test_mode
  if [ -n "${2:-}" ]; then
    printf '{"pipeline_id": "%s", "state": {"operational_state": "%s", "test_mode": "%s"}}' \
      "$MEZMO_PIPELINE_ID" "$1" "$2"
  else
    printf '{"pipeline_id": "%s", "state": {"operational_state": "%s"}}' \
      "$MEZMO_PIPELINE_ID" "$1"
  fi
}

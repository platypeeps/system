#!/bin/sh
# Offline test suite for pipeline.sh. Makes no network calls: `curl` is
# shadowed on PATH by a recording stub, so every test asserts on the exact
# method, URL and body the tool would have sent.
#
# Run it with `./pipeline.sh test`.
set -u

DIR="$(cd "$(dirname "$0")" && pwd)"
FOLDER="$(cd "$DIR/.." && pwd)"
PIPELINE="$FOLDER/pipeline.sh"

command -v jq >/dev/null 2>&1 || {
  echo "jq is required by this suite and by pipeline.sh itself — brew install jq" >&2
  exit 1
}

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT INT TERM
BIN="$TMP/bin"
mkdir -p "$BIN"
cp "$DIR/curl-stub" "$BIN/curl"
chmod +x "$BIN/curl"

PASS=0
FAIL=0
pass() { PASS=$((PASS + 1)); printf '  ok    %s\n' "$1"; }
fail() {
  FAIL=$((FAIL + 1))
  printf '  FAIL  %s\n' "$1"
  [ $# -gt 1 ] && printf '        %s\n' "$2"
  return 0
}

# newstub is always called as $(newstub) — a subshell — so it must not depend
# on any state in the parent. An earlier version incremented a counter here and
# every test silently shared one directory, complete with a stale request
# counter, which made the stub answer call 1 of test 7 with test 1's response.
newstub() {
  s=$(mktemp -d "$TMP/stub.XXXXXX")
  : > "$s/calls"
  echo "$s"
}

# Defaults are overridable per-test by setting E_* before calling.
runp() {
  sd=$1
  shift
  env -i PATH="$BIN:/usr/bin:/bin:/usr/sbin:/sbin" HOME="$TMP" TMPDIR="$TMP" \
    MZ_SKIP_ENV_FILE=1 MZ_STUB_DIR="$sd" \
    MZ_STUB_DEFAULT_CODE="${E_CODE:-200}" \
    MEZMO_PIPELINE_SERVICE_KEY="${E_KEY-sts_realkey}" \
    MEZMO_PIPELINE_ID="${E_PID-11111111-1111-1111-1111-111111111111}" \
    MEZMO_PIPELINE_STATE_ID="${E_STID-22222222-2222-2222-2222-222222222222}" \
    MEZMO_SOURCE_ID="${E_SRC-33333333-3333-3333-3333-333333333333}" \
    MEZMO_ACCOUNT_ID="${E_ACC-44444444-4444-4444-4444-444444444444}" \
    MEZMO_API_BASE="${E_BASE-https://api.mezmo.com}" \
    sh "$PIPELINE" "$@"
}

# A state-variable GET body the read-back paths can parse.
STATE_RESP='{"data":[{"state":{"operational_state":"normal"}}]}'

echo "mezmo-pipeline offline suite"
echo

# --- the command-line contract -------------------------------------------
echo "command-line contract"

out=$(sh "$PIPELINE" --help 2>&1); rc=$?
[ $rc -eq 0 ] && pass "--help exits 0" || fail "--help exits 0" "got $rc"
for word in "state get" "source webhook" "status" "test"; do
  case "$out" in
    *"$word"*) pass "--help documents '$word'" ;;
    *)         fail "--help documents '$word'" ;;
  esac
done

sh "$PIPELINE" >/dev/null 2>&1; rc=$?
[ $rc -eq 1 ] && pass "no arguments exits 1" || fail "no arguments exits 1" "got $rc"

sh "$PIPELINE" bogus >/dev/null 2>&1; rc=$?
[ $rc -eq 1 ] && pass "unknown command exits 1" || fail "unknown command exits 1" "got $rc"

for args in "state" "state bogus" "source" "source bogus"; do
  # shellcheck disable=SC2086
  runp "$(newstub)" $args >/dev/null 2>&1; rc=$?
  [ $rc -eq 1 ] && pass "'$args' exits 1" || fail "'$args' exits 1" "got $rc"
done

# --- the placeholder guard -------------------------------------------------
echo
echo "placeholder guard"

guard() {
  env -i PATH="/usr/bin:/bin" sh -c '
    MZ_SKIP_ENV_FILE=1
    . "$1/lib.sh"
    if mz_is_placeholder "$2"; then echo yes; else echo no; fi
  ' _ "$FOLDER" "$1"
}
for bad in "change-me" "sts_change-me" "changeme" "CHANGE_ME" "your-key" \
           "xxx" "XXX-nope" "placeholder" "00000000-0000-0000-0000-000000000000"; do
  [ "$(guard "$bad")" = "yes" ] && pass "rejects placeholder '$bad'" \
    || fail "rejects placeholder '$bad'"
done
for good in "sts_9f3a1c" "11111111-1111-1111-1111-111111111111" "realvalue"; do
  [ "$(guard "$good")" = "no" ] && pass "accepts real value '$good'" \
    || fail "accepts real value '$good'"
done

# --- required variables ----------------------------------------------------
echo
echo "required variables"

out=$(E_KEY=""; runp "$(newstub)" state get 2>&1); rc=$?
[ $rc -ne 0 ] && pass "missing key fails" || fail "missing key fails"
case "$out" in
  *MEZMO_PIPELINE_SERVICE_KEY*) pass "missing key is named in the error" ;;
  *) fail "missing key is named in the error" "got: $out" ;;
esac

out=$(E_STID="00000000-0000-0000-0000-000000000000"; runp "$(newstub)" state get 2>&1); rc=$?
case "$out" in
  *placeholder*MEZMO_PIPELINE_STATE_ID*) pass "placeholder value is named as a placeholder" ;;
  *) fail "placeholder value is named as a placeholder" "got: $out" ;;
esac

# `state` must not demand the source-only ids, and vice versa.
sd=$(newstub)
printf '%s' "$STATE_RESP" > "$sd/resp.1.body"
out=$(E_SRC=""; E_ACC=""; runp "$sd" state get 2>&1); rc=$?
[ $rc -eq 0 ] && pass "'state' does not require the source ids" \
  || fail "'state' does not require the source ids" "got $rc: $out"

# --- the config directory ---------------------------------------------------
echo
echo "config directory"

# The .env lives at <SYSTEM_TOOLS_CONFIG>/mezmo-pipeline/.env, never in this
# folder. These two run without MZ_SKIP_ENV_FILE against a temp config dir.
runc() {
  sd=$1
  shift
  env -i PATH="$BIN:/usr/bin:/bin:/usr/sbin:/sbin" HOME="$TMP" TMPDIR="$TMP" \
    SYSTEM_TOOLS_CONFIG="$TMP/config" MZ_STUB_DIR="$sd" MZ_STUB_DEFAULT_CODE=200 \
    MEZMO_API_BASE="https://api.mezmo.com" \
    sh "$PIPELINE" "$@"
}

out=$(runc "$(newstub)" state get 2>&1); rc=$?
case "$rc:$out" in
  [!0]*"$TMP/config/mezmo-pipeline/.env"*) pass "missing value names the config .env path" ;;
  *) fail "missing value names the config .env path" "got $rc: $out" ;;
esac

mkdir -p "$TMP/config/mezmo-pipeline"
cat > "$TMP/config/mezmo-pipeline/.env" <<'ENVEOF'
MEZMO_PIPELINE_SERVICE_KEY=sts_fromconfig
MEZMO_PIPELINE_ID=11111111-1111-1111-1111-111111111111
MEZMO_PIPELINE_STATE_ID=22222222-2222-2222-2222-222222222222
ENVEOF
sd=$(newstub); printf '%s' "$STATE_RESP" > "$sd/resp.1.body"
out=$(runc "$sd" state get 2>&1); rc=$?
[ $rc -eq 0 ] && pass "values are read from the config .env" \
  || fail "values are read from the config .env" "got $rc: $out"
rm -rf "$TMP/config"

# --- request bodies ---------------------------------------------------------
echo
echo "request bodies"

body_for() {
  env -i PATH="/usr/bin:/bin" sh -c '
    . "$1/lib.sh"
    MEZMO_PIPELINE_ID=PID
    mz_state_body "$2" "${3:-}"
  ' _ "$FOLDER" "$1" "${2:-}"
}
b=$(body_for normal)
echo "$b" | jq -e '.state.operational_state == "normal"' >/dev/null 2>&1 \
  && pass "normal body sets operational_state" || fail "normal body sets operational_state" "$b"
echo "$b" | jq -e 'has("state") and (.state | has("test_mode") | not)' >/dev/null 2>&1 \
  && pass "normal body omits test_mode" || fail "normal body omits test_mode" "$b"
b=$(body_for incident on)
echo "$b" | jq -e '.state.operational_state == "incident" and .state.test_mode == "on"' >/dev/null 2>&1 \
  && pass "test-mode body sets incident + test_mode" || fail "test-mode body sets incident + test_mode" "$b"

# --- the API host -----------------------------------------------------------
echo
echo "API host"

base=$(env -i PATH="/usr/bin:/bin" sh -c '. "$1/lib.sh"; echo "$MEZMO_API_BASE"' _ "$FOLDER")
[ "$base" = "https://api.mezmo.com" ] && pass "default host is api.mezmo.com" \
  || fail "default host is api.mezmo.com" "got $base"

# The invariant is that no CODE still targets the legacy host. The README and
# .env.example mention it on purpose, to say why it is gone — prose explaining
# a migration is not a regression, so only the shell sources are checked, and
# comments in those are excluded too.
if grep -n "api\.logdna\.com" "$FOLDER"/*.sh 2>/dev/null \
     | grep -v '^[^:]*:[0-9]*:[[:space:]]*#' | grep -q .; then
  fail "no legacy api.logdna.com left in the shell sources"
else
  pass "no legacy api.logdna.com left in the shell sources"
fi

sd=$(newstub); printf '%s' "$STATE_RESP" > "$sd/resp.1.body"
( E_BASE="https://example.invalid"; runp "$sd" state get ) >/dev/null 2>&1
case "$(cat "$sd/calls")" in
  *https://example.invalid/v3/pipeline/state-variable*) pass "MEZMO_API_BASE override is honoured" ;;
  *) fail "MEZMO_API_BASE override is honoured" "calls: $(cat "$sd/calls")" ;;
esac

# --- HTTP status handling (the silent-failure regression) -------------------
echo
echo "HTTP status handling"

sd=$(newstub); printf '%s' "$STATE_RESP" > "$sd/resp.1.body"
runp "$sd" state get >/dev/null 2>&1; rc=$?
[ $rc -eq 0 ] && pass "200 succeeds" || fail "200 succeeds" "got $rc"

for code in 401 404 500; do
  sd=$(newstub); echo "$code" > "$sd/resp.1.code"
  runp "$sd" state get >/dev/null 2>&1; rc=$?
  [ $rc -ne 0 ] && pass "HTTP $code exits non-zero" \
    || fail "HTTP $code exits non-zero" "exited 0 — this is the bug the old scripts had"
done

sd=$(newstub); echo "TRANSPORT_FAIL" > "$sd/resp.1.code"
runp "$sd" state get >/dev/null 2>&1; rc=$?
[ $rc -ne 0 ] && pass "curl transport failure exits non-zero" \
  || fail "curl transport failure exits non-zero"

# --- verify-after-write -----------------------------------------------------
echo
echo "verify after write"

sd=$(newstub)
echo "200" > "$sd/resp.1.code"; printf '%s' '{}' > "$sd/resp.1.body"
printf '%s' "$STATE_RESP" > "$sd/resp.2.body"
runp "$sd" state normal >/dev/null 2>&1; rc=$?
calls=$(cat "$sd/calls")
[ "$(echo "$calls" | wc -l | tr -d ' ')" = "2" ] \
  && pass "'state normal' makes two calls (write then read-back)" \
  || fail "'state normal' makes two calls (write then read-back)" "calls: $calls"
case "$(echo "$calls" | head -1)" in
  "PUT "*state-variable/22222222*) pass "the write is a PUT to the state id" ;;
  *) fail "the write is a PUT to the state id" "got: $(echo "$calls" | head -1)" ;;
esac
case "$(echo "$calls" | tail -1)" in
  "GET "*state-variable*) pass "the read-back is a GET" ;;
  *) fail "the read-back is a GET" "got: $(echo "$calls" | tail -1)" ;;
esac
jq -e '.state.operational_state == "normal"' < "$sd/body.1" >/dev/null 2>&1 \
  && pass "the PUT body carries operational_state normal" \
  || fail "the PUT body carries operational_state normal" "$(cat "$sd/body.1")"

# --- the webhook source read-modify-write ------------------------------------
echo
echo "webhook source: read, modify, write"

# What the server currently holds: keys configured, and the two fields wrong.
SRC_CURRENT='{"data":{"id":"33333333-3333-3333-3333-333333333333","title":"Web Hook","user_config":{"capture_metadata":false,"auto_parse":{"enabled":true}},"gateway_route":{"id":"33333333-3333-3333-3333-333333333333","access_keys":["ak_live_keep_me"],"signing_keys":["sk_live_keep_me"],"capture_metadata":false}}}'

sd=$(newstub)
printf '%s' "$SRC_CURRENT" > "$sd/resp.1.body"   # GET
printf '%s' '{}'           > "$sd/resp.2.body"   # PUT
printf '%s' "$SRC_CURRENT" > "$sd/resp.3.body"   # read-back GET
runp "$sd" source webhook >/dev/null 2>&1; rc=$?
calls=$(cat "$sd/calls")
case "$(echo "$calls" | head -1)" in
  "GET "*/source/33333333*) pass "reads the source before writing" ;;
  *) fail "reads the source before writing" "got: $(echo "$calls" | head -1)" ;;
esac
case "$(echo "$calls" | sed -n 2p)" in
  "PUT "*/source/33333333*) pass "then PUTs the source" ;;
  *) fail "then PUTs the source" "got: $(echo "$calls" | sed -n 2p)" ;;
esac
if [ -f "$sd/body.2" ]; then
  jq -e '.gateway_route.access_keys == ["ak_live_keep_me"]' < "$sd/body.2" >/dev/null 2>&1 \
    && pass "the PUT preserves access_keys (was wiped by the old script)" \
    || fail "the PUT preserves access_keys (was wiped by the old script)" "$(cat "$sd/body.2")"
  jq -e '.gateway_route.signing_keys == ["sk_live_keep_me"]' < "$sd/body.2" >/dev/null 2>&1 \
    && pass "the PUT preserves signing_keys (was wiped by the old script)" \
    || fail "the PUT preserves signing_keys (was wiped by the old script)" "$(cat "$sd/body.2")"
  jq -e '.user_config.capture_metadata == true' < "$sd/body.2" >/dev/null 2>&1 \
    && pass "the PUT turns capture_metadata on" || fail "the PUT turns capture_metadata on"
  jq -e '.user_config.auto_parse.enabled == false' < "$sd/body.2" >/dev/null 2>&1 \
    && pass "the PUT turns auto_parse off" || fail "the PUT turns auto_parse off"
else
  fail "the PUT preserves access_keys (was wiped by the old script)" "no PUT was made"
  fail "the PUT preserves signing_keys (was wiped by the old script)" "no PUT was made"
  fail "the PUT turns capture_metadata on" "no PUT was made"
  fail "the PUT turns auto_parse off" "no PUT was made"
fi

# A failed read must abort. A blind PUT here is what destroyed the keys.
sd=$(newstub); echo "404" > "$sd/resp.1.code"
runp "$sd" source webhook >/dev/null 2>&1; rc=$?
[ $rc -ne 0 ] && pass "a failed read exits non-zero" || fail "a failed read exits non-zero"
case "$(cat "$sd/calls")" in
  *PUT*) fail "a failed read makes no PUT" "it wrote anyway: $(cat "$sd/calls")" ;;
  *)     pass "a failed read makes no PUT" ;;
esac

# Already correct: nothing to write.
SRC_OK='{"data":{"user_config":{"capture_metadata":true,"auto_parse":{"enabled":false}},"gateway_route":{"access_keys":[],"signing_keys":[],"capture_metadata":true}}}'
sd=$(newstub)
printf '%s' "$SRC_OK" > "$sd/resp.1.body"
printf '%s' "$SRC_OK" > "$sd/resp.2.body"
runp "$sd" source webhook >/dev/null 2>&1
case "$(cat "$sd/calls")" in
  *PUT*) fail "an already-correct source is not rewritten" "it wrote: $(cat "$sd/calls")" ;;
  *)     pass "an already-correct source is not rewritten" ;;
esac

# --- status exit codes ------------------------------------------------------
echo
echo "status exit codes"

out=$(E_KEY=""; E_PID=""; E_STID=""; runp "$(newstub)" status 2>&1); rc=$?
[ $rc -eq 3 ] && pass "unconfigured status exits 3 (health-check skips it)" \
  || fail "unconfigured status exits 3 (health-check skips it)" "got $rc: $out"
case "$out" in *SKIP*) pass "unconfigured status says SKIP" ;; *) fail "unconfigured status says SKIP" "got: $out" ;; esac

sd=$(newstub); printf '%s' "$STATE_RESP" > "$sd/resp.1.body"
out=$(runp "$sd" status 2>&1); rc=$?
[ $rc -eq 0 ] && pass "healthy status exits 0" || fail "healthy status exits 0" "got $rc: $out"
case "$out" in *OK*) pass "healthy status says OK" ;; *) fail "healthy status says OK" "got: $out" ;; esac

sd=$(newstub); echo "500" > "$sd/resp.1.code"
out=$(runp "$sd" status 2>&1); rc=$?
[ $rc -eq 1 ] && pass "failing status exits 1" || fail "failing status exits 1" "got $rc: $out"
case "$out" in *FAIL*) pass "failing status says FAIL" ;; *) fail "failing status says FAIL" "got: $out" ;; esac

# The remedy is keyed on the code and lives here, not in the health check
# that used to print it for every tool it swept.
sd=$(newstub); echo "401" > "$sd/resp.1.code"
out=$(runp "$sd" status 2>/dev/null); rc=$?
case "$rc:$out" in 1:*"HTTP 401"*"service key was rotated"*) pass "401 status names the rotated service key" ;;
  *) fail "401 status names the rotated service key" "got $rc: $out" ;; esac
sd=$(newstub); echo "404" > "$sd/resp.1.code"
out=$(runp "$sd" status 2>/dev/null); rc=$?
case "$rc:$out" in 1:*"HTTP 404"*"pipeline or state id moved"*) pass "404 status names the moved id" ;;
  *) fail "404 status names the moved id" "got $rc: $out" ;; esac

# --- the suite really is offline --------------------------------------------
echo
echo "offline guarantee"

resolved=$(env -i PATH="$BIN:/usr/bin:/bin" sh -c 'command -v curl')
[ "$resolved" = "$BIN/curl" ] \
  && pass "curl resolves to the stub inside the sandbox, not the real binary" \
  || fail "curl resolves to the stub inside the sandbox, not the real binary" "got $resolved"

# --- result -----------------------------------------------------------------
echo
echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ] || exit 1

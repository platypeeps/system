#!/bin/sh
# What profile-autocapture.sh does with capture's result. Run by hand:
#
#     sh local-machine-setup/additive-capture-test.sh
#
# Scope, stated up front: this covers the **wrapper** -- that it runs an
# additive capture, passes capture's exit code on, and touches no git -- and
# does **not** cover the `capture --additive` removal guard inside
# machine-setup.sh. That guard needs a live machine to enumerate, and a test
# that reimplemented it would pass while the real one rotted. It was verified
# by hand against a constructed removal; this file does not pretend otherwise.
#
# Everything runs in a temp dir. It never touches the real profiles.
set -eu

DIR="$(cd "$(dirname "$0")" && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

pass=0; fail=0
ok()    { pass=$((pass + 1)); echo "  ok      $1"; }
bad()   { fail=$((fail + 1)); echo "  FAIL    $1"; }
check() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (want '$3', got '$2')"; fi; }

# A config folder (standing in for $SYSTEM_TOOLS_CONFIG) that is a git
# checkout, so the test can show the job leaves its history alone, and a tools
# folder holding the wrapper. `capture` is stubbed per-case.
SYSTEM_TOOLS_CONFIG="$TMP/work"
export SYSTEM_TOOLS_CONFIG
mkdir -p "$TMP/tools/local-machine-setup" "$TMP/work/machine-setup/profiles"
cp "$DIR/profile-autocapture.sh" "$TMP/tools/local-machine-setup/"
cd "$TMP/work"
git init -q . && git config user.email t@t && git config user.name t
printf 'kept-job\n' > machine-setup/profiles/t.cron
git add -A && git commit -qm init

# capture's stand-in: appends $1 to the roster, exits $2.
stub_capture() {
  cat > "$TMP/tools/local-machine-setup/machine-setup.sh" <<STUB
#!/bin/sh
[ -n "$1" ] && printf '%s\n' "$1" >> "$TMP/work/machine-setup/profiles/t.cron"
exit $2
STUB
}

run() { rc=0; out=$(sh "$TMP/tools/local-machine-setup/profile-autocapture.sh" 2>&1) || rc=$?; }

echo "=== exit codes"

stub_capture "" 9
run
check "a failing capture propagates"   "$rc" "9"
stub_capture "" 3
run
check "a held removal exits 3"         "$rc" "3"

echo
echo "=== no git history"

stub_capture "new-job" 0
run
check "an addition exits 0"            "$rc" "0"
check "the roster has it" \
  "$(tail -1 machine-setup/profiles/t.cron)" "new-job"
check "no commit is made"              "$(git rev-list --count HEAD)" "1"
check "nothing is staged"              "$(git diff --cached --name-only)" ""

echo
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]

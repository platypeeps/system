#!/bin/sh
# What profile-autocapture.sh refuses to do. Run by hand:
#
#     sh local-machine-setup/additive-capture-test.sh
#
# Scope, stated up front: this covers the **wrapper's** git behaviour -- the
# refusals and the failure signalling -- and does **not** cover the
# `capture --additive` removal guard inside machine-setup.sh. That guard needs
# a live machine to enumerate, and a test that reimplemented it would pass
# while the real one rotted. It was verified by hand against a constructed
# removal; this file does not pretend otherwise.
#
# It exists because the wrapper has already been wrong once in a way nothing
# would have caught. `git pull -q --ff-only && git push -q` reported success
# on failure: `set -e` exempts a failing command inside an AND-OR list, so the
# list failed whole and execution carried on to the success message. In an
# unattended job that is the whole failure mode -- reporting success having
# not done the thing -- in the one place nobody is watching.
#
# Everything runs in a temp dir against a bare remote. It never touches the
# real repository, the real profiles, or the real remote.
set -eu

DIR="$(cd "$(dirname "$0")" && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

pass=0; fail=0
ok()    { pass=$((pass + 1)); echo "  ok      $1"; }
bad()   { fail=$((fail + 1)); echo "  FAIL    $1"; }
check() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (want '$3', got '$2')"; fi; }
says()  { case "$2" in *"$3"*) ok "$1" ;; *) bad "$1 (no '$3' in output)" ;; esac; }

# A config checkout (standing in for $SYSTEM_TOOLS_CONFIG) holding just a
# machine-setup/profiles/ directory, wired to a bare remote, and a tools
# folder holding the wrapper and autocommit's key helper. `capture` is stubbed
# per-case: these are tests of what the wrapper does with capture's result,
# not of capture.
SYSTEM_TOOLS_CONFIG="$TMP/work"
AUTOCOMMIT_SSH_KEY="$TMP/no-key"
export SYSTEM_TOOLS_CONFIG AUTOCOMMIT_SSH_KEY
mkdir -p "$TMP/tools/local-machine-setup" "$TMP/tools/local-autocommit"
cp "$DIR/profile-autocapture.sh" "$TMP/tools/local-machine-setup/"
cp "$DIR/../local-autocommit/autocommit.sh" "$TMP/tools/local-autocommit/"

new_repo() {
  rm -rf "$TMP/work" "$TMP/remote.git"
  git init -q --bare "$TMP/remote.git"
  mkdir -p "$TMP/work/machine-setup/profiles"
  cd "$TMP/work"
  git init -q . && git config user.email t@t && git config user.name t
  printf 'kept-job\n' > machine-setup/profiles/t.cron
  git add -A && git commit -qm init
  git remote add origin "$TMP/remote.git"
  git push -q -u origin HEAD:refs/heads/main 2>/dev/null
  git branch -q --set-upstream-to=origin/main 2>/dev/null || true
}

# capture's stand-in: appends $1 to the roster, exits $2.
stub_capture() {
  cat > "$TMP/tools/local-machine-setup/machine-setup.sh" <<STUB
#!/bin/sh
[ -n "$1" ] && printf '%s\n' "$1" >> "$TMP/work/machine-setup/profiles/t.cron"
exit $2
STUB
}

run() { rc=0; out=$(sh "$TMP/tools/local-machine-setup/profile-autocapture.sh" 2>&1) || rc=$?; }

echo "=== refusals"

new_repo; stub_capture "" 0
printf 'someone-was-editing\n' >> machine-setup/profiles/t.cron
run
check "dirty scope refuses"            "$rc" "1"
says  "dirty scope says why"           "$out" "already dirty"
check "dirty scope commits nothing"    "$(git rev-list --count HEAD)" "1"

new_repo; stub_capture "" 9
run
check "a failing capture propagates"   "$rc" "9"
check "a failing capture commits nothing" "$(git rev-list --count HEAD)" "1"

echo
echo "=== push failure is not success"

# The regression this file was written for. The remote is made unreachable
# after the commit is prepared, so the push cannot succeed.
new_repo; stub_capture "new-job" 0
git remote set-url origin "$TMP/does-not-exist.git"
run
check "an unpushable commit exits non-zero" "$rc" "1"
case "$out" in
  *"committed and pushed"*) bad "it claimed success anyway" ;;
  *) ok "it does not claim success" ;;
esac
check "the commit is still made locally" "$(git rev-list --count HEAD)" "2"

# ...and the next run must not go quiet about it.
stub_capture "" 0
run
check "a later run reports the unpushed commit" "$rc" "1"
says  "and names it"                    "$out" "did not push"

echo
echo "=== the happy path still works"

new_repo; stub_capture "new-job" 0
run
check "an addition is committed and pushed" "$rc" "0"
says  "and says so"                     "$out" "committed and pushed"
check "the remote has it" \
  "$(git ls-remote origin refs/heads/main | cut -f1)" "$(git rev-parse HEAD)"
check "one file changed, scoped" \
  "$(git show --name-only --format= HEAD | tr -d '\n')" \
  "machine-setup/profiles/t.cron"

echo
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]

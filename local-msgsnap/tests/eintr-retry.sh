#!/bin/sh
# Prove that msgsnap retries open(2) on EINTR and that only TCC exits 2.
#
# There is no unit harness for the Swift source, so this runs the real
# binary end to end with open(2) stood in by interpose_open.c
# (DYLD_INSERT_LIBRARIES); tests/run-macos-only.sh runs it on a Mac through
# `msgsnap.sh test` (tests/test_eintr_retry.py, sd:946). Each case sets what
# the source open does — fail with EINTR n times, then succeed or fail with a
# chosen errno — and checks the exit code, the line printed, and how many
# opens the binary made. No Full Disk Access is needed: a successful open is
# redirected to a scratch file, and every failing case exits before the
# snapshot starts.
#
# It compiles its own copy of the binary into a temp dir and never touches
# bin/msgsnap: rebuilding that one changes its cdhash and kills the FDA grant
# (README.md, "Gotchas").
#
# Usage: sh tests/eintr-retry.sh        (from local-msgsnap, or anywhere)
# Exit 0 when every case passes, 1 otherwise.
set -u

DIR="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$DIR/src/msgsnap.swift"
SHIM="$DIR/tests/interpose_open.c"

TMP="$(mktemp -d -t msgsnap-eintr)"
trap 'rm -rf "$TMP"' EXIT INT TERM

# Same flags as msgsnap.sh build, minus the signing: an ad-hoc signature is
# not needed for dyld to honour DYLD_INSERT_LIBRARIES on a non-hardened binary.
if ! swiftc -O -o "$TMP/msgsnap" "$SRC" > "$TMP/swiftc.log" 2>&1; then
    echo "eintr-retry: swiftc failed:" >&2; cat "$TMP/swiftc.log" >&2; exit 1
fi
# Through xcrun: a bare clang here can pick a Command Line Tools SDK whose
# libSystem.tbd the Xcode linker rejects, while swiftc above links fine.
if ! xcrun --sdk macosx clang -Wall -Wextra -dynamiclib -o "$TMP/shim.dylib" "$SHIM" > "$TMP/clang.log" 2>&1; then
    echo "eintr-retry: clang failed:" >&2; cat "$TMP/clang.log" >&2; exit 1
fi
: > "$TMP/redirect.db"

ran=0
failed=0

# run NAME EINTR ERRNO EXPECT_RC EXPECT_TRIES EXPECT_LINE [ARGS...]
# EXPECT_LINE is matched as a prefix of what the binary printed, stdout and
# stderr together, since --check prints to one and a snapshot run to the other.
run() {
    name="$1"; eintr="$2"; errno="$3"; want_rc="$4"; want_tries="$5"; want_line="$6"
    shift 6
    ran=$((ran + 1))
    rm -f "$TMP/count"
    MSGSNAP_TEST_EINTR="$eintr" MSGSNAP_TEST_ERRNO="$errno" \
    MSGSNAP_TEST_REDIRECT="$TMP/redirect.db" MSGSNAP_TEST_COUNT="$TMP/count" \
    DYLD_INSERT_LIBRARIES="$TMP/shim.dylib" \
        "$TMP/msgsnap" "$@" > "$TMP/out" 2>&1
    rc=$?
    line="$(head -n 1 "$TMP/out")"
    tries="$(cat "$TMP/count" 2>/dev/null || echo 0)"
    case "$line" in "$want_line"*) line_ok=1 ;; *) line_ok=0 ;; esac
    if [ "$rc" -eq "$want_rc" ] && [ "$tries" -eq "$want_tries" ] && [ "$line_ok" -eq 1 ]; then
        echo "ok   $name"
    else
        failed=$((failed + 1))
        echo "FAIL $name"
        echo "     want rc=$want_rc tries=$want_tries line='$want_line'..."
        echo "     got  rc=$rc tries=$tries line='$line'"
    fi
}

# The control: with nothing injected the redirect alone yields "ok". If this
# fails, the shim is not in the process and no case below means anything.
run "control: the shim redirects a clean open" \
    0 0 0 1 "ok: readable" --check

# sd:935 — three nights of msgsnap-nightly died on one EINTR with rc 2.
run "EINTR three times, then the open succeeds: retried, ok" \
    3 0 0 4 "ok: readable" --check
run "EINTR on every attempt: gives up at the bound, exits 4, not 2" \
    -1 0 4 10 "blocked: open failed after 10 attempts: Interrupted system call (errno 4)" --check
run "EINTR on every attempt, snapshot run: exits 4 before touching the source" \
    -1 0 4 10 "msgsnap: open failed after 10 attempts: Interrupted system call (errno 4)"

# The exit codes the job comment recites. Only TCC exits 2.
run "EPERM (TCC): exits 2 on the first attempt, no retry" \
    0 1 2 1 "blocked: denied by TCC" --check
run "EACCES (mode bits or an ACL, not TCC): exits 4, no retry" \
    0 13 4 1 "blocked: open failed: Permission denied (errno 13)" --check
run "ENOENT (no database): exits 4, no retry" \
    0 2 4 1 "blocked: no database at " --check
run "EIO (neither TCC nor absent): exits 4, no retry" \
    0 5 4 1 "blocked: open failed: Input/output error (errno 5)" --check

echo "Ran $ran tests, $failed failed"
[ "$failed" -eq 0 ]

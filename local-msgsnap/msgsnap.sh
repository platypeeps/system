#!/bin/sh
# msgsnap — build, grant and run the one binary on this machine that holds
# Full Disk Access. See README.md for why the grant cannot be narrower.
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
SRC="$DIR/src/msgsnap.swift"
BIN="$DIR/bin/msgsnap"
OUTDIR="$HOME/.local/state/msgsnap"
LABEL="local.msgsnap.run"

usage() {
    cat <<EOF
msgsnap.sh — snapshot the Messages database with a minimal FDA holder

Usage: msgsnap.sh <subcommand>

  build    compile src/msgsnap.swift to bin/msgsnap and ad-hoc sign it.
           NOTE: the FDA grant is keyed to the binary's cdhash, so every
           rebuild invalidates it and you must re-grant. Build rarely.
  grant    open the Full Disk Access pane and print the path to add
  check    run the access probe directly from this shell. Tells you about
           this shell's context, not about the binary — see 'verify'.
  verify   the honest test: run the probe from a one-shot launchd job,
           where this binary is its own responsible process
  snap     take a snapshot into \$HOME/.local/state/msgsnap. Runs direct
           if that works, otherwise reruns itself via launchd.
  status   show what is built, granted and snapshotted
  clean    remove the built binary (does NOT touch snapshots)
  test     run tests/ as a unittest suite: tests/eintr-retry.sh, the end-to-end
           open(2) retry and exit-code check, on a copy of the binary built
           into a temp dir. Never touches bin/msgsnap or the grant. This is
           what the system-native CI tools leg runs.

The binary reads one compiled-in path and copies it. It takes no source
path argument, by design.
EOF
}

need_bin() {
    [ -x "$BIN" ] || { echo "msgsnap.sh: not built — run: $0 build" >&2; exit 1; }
}

cmd_build() {
    mkdir -p "$DIR/bin"
    swiftc -O -o "$BIN" "$SRC"
    codesign -f -s - "$BIN"
    echo "built: $BIN"
    codesign -d --verbose=4 "$BIN" 2>&1 | grep '^CDHash=' || true
    cat >&2 <<EOF

The cdhash just changed. If Full Disk Access was already granted to a previous
build, macOS no longer honours it: remove the stale entry and add this one.
  $0 grant
EOF
}

cmd_grant() {
    need_bin
    echo "Add this binary to Full Disk Access:"
    echo
    echo "  $BIN"
    echo
    echo "In the pane that opens: click +, press Cmd+Shift+G, paste the path above."
    echo "Remove any older msgsnap entry first — a stale cdhash entry is dead weight."
    open "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"
}

# Run the binary as its own launchd job and echo what it printed.
#
# This is not belt-and-braces, it is the only reliable way to invoke it. TCC
# attributes a file access to the *responsible process*, which for a spawned
# child is normally the parent that started it. Under launchd the binary is its
# own responsible process and its FDA grant applies; started from a shell whose
# parent has no grant, the same binary is denied. .claude/rules/macos-tcc.md documents
# the leak in the other direction — a terminal's own FDA making an ungranted
# binary look granted — and this is the same mechanism seen from the other side.
#
# Returns 0 if the job produced output, 1 if it timed out.
launchd_run() {
    for a in "$@"; do
        case "$a" in
            *'<'*|*'>'*|*'&'*|*'"'*|*"'"*)
                echo "msgsnap.sh: refusing to embed '$a' in a plist" >&2; exit 1 ;;
        esac
    done

    PLIST="$(mktemp -t msgsnap-plist).plist"
    OUT="$(mktemp -t msgsnap-out)"

    {
        printf '%s\n' '<?xml version="1.0" encoding="UTF-8"?>'
        printf '%s\n' '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">'
        printf '%s\n' '<plist version="1.0"><dict>'
        printf '  <key>Label</key><string>%s</string>\n' "$LABEL"
        printf '%s\n' '  <key>ProgramArguments</key><array>'
        printf '    <string>%s</string>\n' "$BIN"
        for a in "$@"; do printf '    <string>%s</string>\n' "$a"; done
        printf '%s\n' '  </array>'
        printf '%s\n' '  <key>RunAtLoad</key><true/>'
        printf '  <key>StandardOutPath</key><string>%s</string>\n' "$OUT"
        printf '  <key>StandardErrorPath</key><string>%s</string>\n' "$OUT"
        printf '%s\n' '</dict></plist>'
    } > "$PLIST"

    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$PLIST"

    i=0
    while [ "$i" -lt 30 ]; do
        [ -s "$OUT" ] && break
        sleep 1
        i=$((i + 1))
    done

    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"

    if [ -s "$OUT" ]; then
        cat "$OUT"
        rm -f "$OUT"
        return 0
    fi
    # Not a timeout to shrug at: an ungranted TCC read under launchd blocks
    # rather than failing, which is how a job here once hung for 27 minutes.
    rm -f "$OUT"
    echo "msgsnap.sh: no output after 30s — treat as NOT granted, see ../.claude/rules/macos-tcc.md" >&2
    return 1
}

cmd_check() {
    need_bin
    echo "(direct from this shell — reflects this shell's TCC context, not the binary's)"
    "$BIN" --check || true
}

cmd_verify() {
    need_bin
    echo "launchd probe said:"
    launchd_run --check | sed 's/^/  /'
}

cmd_snap() {
    need_bin
    # Try direct first: when the caller does hold FDA this avoids a launchd
    # round-trip entirely. Exit 2 is specifically "TCC said no", which for a
    # binary that verify says is granted means the caller is the problem.
    set +e
    OUTPUT="$("$BIN" "$@" 2>&1)"
    RC=$?
    set -e
    if [ "$RC" -eq 0 ]; then
        printf '%s\n' "$OUTPUT"
        return 0
    fi
    if [ "$RC" -ne 2 ]; then
        printf '%s\n' "$OUTPUT" >&2
        return "$RC"
    fi
    echo "direct run denied by TCC — rerunning via launchd, where the binary is" >&2
    echo "its own responsible process. (Run 'verify' if this also fails.)" >&2
    launchd_run "$@"
}

cmd_status() {
    if [ -x "$BIN" ]; then
        echo "binary:    $BIN"
        codesign -d --verbose=4 "$BIN" 2>&1 | grep '^CDHash=' | sed 's/^/           /' || true
    else
        echo "binary:    not built"
    fi
    if [ -d "$OUTDIR" ]; then
        echo "snapshots: $OUTDIR"
        ls -lh "$OUTDIR" 2>/dev/null | tail -n +2 | sed 's/^/           /'
    else
        echo "snapshots: none yet"
    fi
}

cmd_clean() {
    rm -f "$BIN"
    echo "removed $BIN (snapshots in $OUTDIR left alone)"
}

case "${1:-}" in
    -h|--help|help) usage; exit 0 ;;
    build)  cmd_build ;;
    grant)  cmd_grant ;;
    check)  cmd_check ;;
    verify) cmd_verify ;;
    snap)   shift; cmd_snap "$@" ;;
    status) cmd_status ;;
    clean)  cmd_clean ;;
    test)   shift
            # A unittest module, not the shell suite direct: the CI wrapper
            # asserts a unittest summary and refuses skips (CLAUDE.md), and
            # the unwired-suite guard keys on tests/test_*.py.
            exec "${PYTHON:-python3}" -m unittest discover -s "$DIR/tests" -t "$DIR" "$@" ;;
    "")     usage >&2; exit 1 ;;
    *)      echo "msgsnap.sh: unknown subcommand '$1'" >&2; usage >&2; exit 1 ;;
esac

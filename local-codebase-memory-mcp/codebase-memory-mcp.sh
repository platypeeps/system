#!/bin/sh
# codebase-memory-mcp daemon wrapper (UI on :9749), driven by launchd.
# Usage: codebase-memory-mcp.sh run
set -e

case "${1:-}" in
  run)
    ;;
  -h|--help|help)
    cat <<'HELPEOF'
usage: codebase-memory-mcp.sh run

  run   run the codebase-memory-mcp daemon (UI on :9749) in the foreground
        with a FIFO stdin ($XDG_STATE_HOME/system/codebase-memory-mcp/,
        default ~/.local/state); stdout/stderr replace codebase-memory-mcp.log.
        This is what launchd invokes (<prefix>.codebase-memory-mcp) —
        the daemon is normally already running, and a second copy exits when
        the port is taken.
HELPEOF
    exit 0
    ;;
  *)
    echo "usage: $(basename "$0") run" >&2
    exit 1
    ;;
esac

# SQLite reads this store with pread() rather than mapping it. Every crash
# report this service left carried `EXC_BAD_ACCESS (SIGBUS)` with the subtype
# `FS pagein error: 22`, faulting inside the mapping in `getPageMMap` ->
# `readDbPage` -> `unixRead`. No database was corrupt: all 40 of them pass
# `integrity_check` and match their own headers. Fourteen instances share
# `~/.cache/codebase-memory-mcp/`, and one of them renames a database to
# `.db.corrupt` and rebuilds it while another holds the old inode mapped --
# a mapped page whose file is gone is a bus error, not a read error.
#
# Measured, not assumed: with this unset the process maps the database itself
# (`vmmap` shows one region for `<project>.db`); with it set to 0 that region
# is gone and only the WAL's `-shm` segment remains, which is required by WAL
# mode and is not the pager path that faulted. The trade is speed for a read
# that returns `SQLITE_IOERR` instead of killing the process.
CBM_SQLITE_MMAP_SIZE=0
export CBM_SQLITE_MMAP_SIZE

DIR="$(cd "$(dirname "$0")" && pwd)"
LOG="$DIR/codebase-memory-mcp.log"
# The FIFO lives outside the checkout: in ~/repos it blocked `grep -r` for hours.
STATE="${XDG_STATE_HOME:-$HOME/.local/state}/system/codebase-memory-mcp"
FIFO="$STATE/stdin.fifo"

exec > "$LOG" 2>&1

rm -f "$DIR/.stdin.fifo"
mkdir -p "$STATE"
[ -p "$FIFO" ] || mkfifo "$FIFO"
exec 3<>"$FIFO"

exec "$HOME/.local/bin/codebase-memory-mcp" --ui=true --port=9749 <&3

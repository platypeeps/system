# codebase-memory-mcp daemon

Runs `codebase-memory-mcp` as a background service via macOS `launchd`. Starts automatically at login, restarts on crash, logs to `codebase-memory-mcp.log` (truncated on every start).

- Script: `codebase-memory-mcp.sh` (launchd invokes `codebase-memory-mcp.sh run`)
- LaunchAgent: `~/Library/LaunchAgents/$LABEL.plist`, where `LABEL` is
  `${SYSTEM_TOOLS_LABEL_PREFIX:-local.system-tools}.codebase-memory-mcp`.
  This folder ships no plist; install one that runs `codebase-memory-mcp.sh run`.
- UI: http://127.0.0.1:9749
- Log: `codebase-memory-mcp.log` (this folder)

The daemon only starts through the explicit `run` subcommand; running the
script with no arguments prints usage and exits without touching the daemon
or its log.

## Control

```bash
LABEL="${SYSTEM_TOOLS_LABEL_PREFIX:-local.system-tools}.codebase-memory-mcp"

# stop (until next login)
launchctl bootout gui/$UID/$LABEL

# start
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/$LABEL.plist

# restart
launchctl kickstart -k gui/$UID/$LABEL

# status (PID + last exit code)
launchctl list | grep codebase-memory-mcp

# disable permanently (remove from login items)
launchctl bootout gui/$UID/$LABEL
rm ~/Library/LaunchAgents/$LABEL.plist
```

## Why this service does not memory-map its store

`codebase-memory-mcp.sh` exports `CBM_SQLITE_MMAP_SIZE=0`, and the same
variable belongs on every stdio instance an MCP client spawns -- those are the
ones that crashed. Twenty-five crash reports carried `EXC_BAD_ACCESS (SIGBUS)`
with the subtype `FS pagein error: 22`, faulting inside the mapping in
SQLite's pager. No database was corrupt: all forty pass `integrity_check` and
match their own headers. Fourteen instances share `~/.cache/codebase-memory-mcp/`,
and one renames a database to `.db.corrupt` and rebuilds it while another
holds the old inode mapped.

The variable's effect was measured rather than assumed. With it unset, `vmmap`
shows the process mapping `<project>.db` itself; with it set to 0 that region
is gone and only the WAL `-shm` segment remains, which WAL mode requires and
which is not the pager path that faulted.

Setting it on the daemon alone does not stop the crashes. A client config that
spawns its own instance needs `"env": {"CBM_SQLITE_MMAP_SIZE": "0"}` beside the
command.

## Logs

```bash
tail -f codebase-memory-mcp.log
```

Log clears on every start/restart — only holds output since last boot of the service.

## Config

Port/UI flags live in `codebase-memory-mcp.sh`. Edit, then restart (`launchctl kickstart -k gui/$UID/$LABEL`, `LABEL` as above) to apply.

## Notes

- Binary is an MCP stdio server — it exits immediately if stdin hits EOF. The script keeps stdin open via a FIFO (`.stdin.fifo`) so the process stays alive as a standalone daemon.
- If port 9749 is already in use by another manually-started instance, kill that process first (`lsof -i :9749`) before loading the agent.

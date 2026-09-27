# local-maintenance

Nightly machine upkeep in one sweep: pending macOS / App Store updates,
docker prune (Sundays), backup freshness, certificate expiry, log rotation,
disk space. Findings that need a human are emailed through local-notify's
email channel; safe fixes (log rotation, docker prune) happen automatically.

## Usage

```sh
./maintenance.sh check   # read-only: print findings and would-be fixes
./maintenance.sh run     # apply safe fixes, email findings (cron entrypoint)
./maintenance.sh uv-prune  # prune uv's cache (the login agent's entrypoint)
./maintenance.sh uv-prune-plist  # print that login agent's plist
./maintenance.sh test    # run the unit tests
```

`run` exits 1 only when the findings email could not be delivered, so the
cron failure notification covers a lost report, not mere findings — the
email itself is the report.

## Checks

Configured in `maintenance.conf` (gitignored; start from
`cp maintenance.conf.example maintenance.conf`). Pipe-separated, `~/` expands
to `$HOME`:

| Directive | Meaning |
| --- | --- |
| `disk\|<path>\|<pct>` | filesystem of `<path>` has at least `<pct>`% free |
| `cert\|<pem>\|<days>` | x509 cert not expiring within `<days>` |
| `mount\|<path>` | volume is mounted |
| `process\|<name>\|<label>` | process running (`pgrep -x`) |
| `age\|<file>\|<days>\|<label>` | file modified within `<days>` |
| `logs\|<dir-or-file>\|<mb>` | rotate `.log` files above `<mb>` |

Checks only one machine wants go in `maintenance.<profile>.conf`, read after
the common file. The profile is whatever machine-setup recorded for this
machine (`~/.config/machine-setup/profile`); `MAINTENANCE_PROFILE` overrides
it, and an absent file is not an error. Profile files are gitignored too.

Built in, no conf needed: `softwareupdate -l`, `mas outdated`, AI session
logs past their retention (`scan-for-secrets.sh prune`, which owns the
retention and the directory list — 30 days for the transcripts, 7 for the
codex shell-snapshot cache, and `~/.claude/projects` deliberately not in the
list because Claude Code deletes its own), and on Sundays `docker system prune -f` (containers
and dangling images only — volumes are never pruned, several local services
keep state in them).

## uv cache prune at login

The uv-cache-prune LaunchAgent runs `uv-prune` once at login. Its label is
`$SYSTEM_TOOLS_LABEL_PREFIX.uv-cache-prune` (prefix default
`local.system-tools`). `uv-prune-plist` prints it, filled from
`uv-cache-prune.plist.template` for this checkout and `$HOME`:

```sh
label="${SYSTEM_TOOLS_LABEL_PREFIX:-local.system-tools}.uv-cache-prune"
./maintenance.sh uv-prune-plist > ~/Library/LaunchAgents/$label.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/$label.plist
```

`uv cache prune` needs uv's cache lock, and a long-lived uv server holds the
lock while it runs. A prune later in the day therefore waits and gives up.

At login only one such server runs: the Google Workspace MCP, a KeepAlive
agent. `uv-prune` does these steps:

1. Pause each loaded agent in `UV_PRUNE_PAUSE` (default
   `$SYSTEM_TOOLS_LABEL_PREFIX.google-workspace-mcp`; the rendered plist
   carries the value) with `launchctl bootout`.
2. Wait up to `UV_PRUNE_WAIT` seconds (20) for its uv processes to exit.
3. Skip the prune, exit 0, when any `uv` or `uvx` process is still left.
4. Run `uv cache prune` with `UV_LOCK_TIMEOUT` of `UV_PRUNE_LOCK_TIMEOUT` (60).
   It never passes `--force`: the lock is what keeps a concurrent uv safe.
5. Start the paused agents again with `launchctl bootstrap`, also on failure.
   It retries three times, and an agent already loaded counts as started.

An unloaded agent is an outage KeepAlive cannot end, because launchd no
longer knows it. So each label goes into `~/.local/state/uv-prune/paused`
before its bootout, and leaves it only once the agent is loaded again.
A run that finds a label there restores it first and prunes nothing.

It prints the cache size before and after and the prune's exit status.
It exits 1 only while an agent stays down. The plist's `KeepAlive`
(`SuccessfulExit` false, every 60 s) then runs it again until the agent
is back. A failed prune exits 0, so launchd does not pause the MCP again.
The agent logs to `/tmp/<label>.{out,err}`.

## Gotchas

- Log rotation truncates in place after copying the tail to `<name>.1`, so
  a service holding the file open keeps writing without a restart.
- Milvus's `volumes/.../rdb_data*/*.log` are RocksDB **data** files, not
  logs. Never add those paths to the conf.
- The `maintenance-nightly` cron job (local-cron-jobs) runs `run` at 03:45.

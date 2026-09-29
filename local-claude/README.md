# local-claude

Tooling for the live Claude Desktop / Claude Code configs: a versioned record
of the MCP servers they run, and upkeep for the claude-mem plugin.

The record itself is private and lives outside the checkout, in
`<config>/claude/`. `<config>` is `$SYSTEM_TOOLS_CONFIG`, default
`~/.config/system`. This folder holds the scripts, tests and README only.

## Files

- `claude.sh` — `capture|status|restore` for the MCP server list. Dry run by
  default, `--apply` writes.
- `<config>/claude/mcp/common/`, `<config>/claude/mcp/<profile>/` — the
  sanitized snapshots, one `claude-code.json` and `claude-desktop.json` per
  layer. Every credential is a `${VARNAME}` placeholder naming the
  `~/.config/shell/env.sh` export that holds it; every path under `$HOME` is
  `${HOME}`. `capture` refuses to write a file where any value still looks like
  a live key. Copy the folder to a new machine before `restore`.

  Layered the way `machine-setup` layers its manifests: `common/` holds the
  servers every machine runs, `<profile>/` the rest, and the profile file wins
  where both define a server. The profile is the one `machine-setup` recorded
  in `~/.config/machine-setup/profile`; `MACHINE_SETUP_PROFILE` overrides it.

  **`capture` writes the profile file only and never touches `common/`.** It
  was flat until 2026-08-24, which made it belong to whichever machine ran
  `capture` last: the first snapshot was taken on one machine, so running
  `capture` on another would have quietly deleted the two servers only the
  first runs from the shared record. A server
  in `common/` that this machine does not run is reported and left alone, since
  one machine not running it is not evidence the others don't.
- `claude.sh prune-mem-logs [--apply]` — log retention claude-mem lacks.
  The worker writes one `~/.claude-mem/logs/claude-mem-YYYY-MM-DD.log` a day,
  16-56 MB each, and deletes none; 13.25.3 has `CLAUDE_MEM_LOG_LEVEL` and no
  retention setting (41 files, 997M on 2026-09-25). This deletes the dated
  files older than `CLAUDE_MEM_LOG_KEEP_DAYS` (14) days. Today's
  and yesterday's files always stay: the name carries the worker's UTC date,
  so in a western evening the worker already writes tomorrow's. Other names
  and symlinks are never touched. Dry run by default; prints the bytes
  reclaimed. Run daily at 04:37 by the `claude-mem-log-prune` cron job.
  In `claude-mem.db` it deletes `tool_uses` rows older than
  `CLAUDE_MEM_TOOL_USES_KEEP_DAYS` (7, at least 1) days. The worker inserts one
  row per tool call, raw input and response, and deletes none. In 13.25.3 only
  two HTTP lookups read the table, cloud sync does not cover it, and no trigger
  or foreign key names it. No other table is touched, and there is no full
  `VACUUM` and no worker restart. After the delete it runs
  `PRAGMA incremental_vacuum` when `auto_vacuum` is 2 (INCREMENTAL, set on the
  live file on 2026-09-25); otherwise it says the freed pages are reused inside
  the file. A dry run opens the file read-only and prints rows and approximate
  bytes; `--apply` prints rows deleted and file size before and after. It
  deletes in batches of 2000 rows, each its own short transaction, under a
  busy timeout of `CLAUDE_MEM_DB_BUSY_TIMEOUT` (30) seconds. A lock held past
  that is a logged skip with exit 0, because `cron-jobs.sh` reads non-zero as
  FAILED and one locked night is caught up the next. A missing database, a
  database that is a symlink, or a missing table is skipped the same way; an invalid setting or any other database
  error exits non-zero.
- `claude.sh test` — the unittest suite in `local-claude/tests/`, run by the
  `tools` leg of `tests/ci-native.sh` under `make check`. It runs against a scratch
  `CLAUDE_MEM_DATA_DIR`, so it reads and writes nothing under `~/.claude-mem`.
- **Do not set `CLAUDE_MEM_OBSERVER_MAX_CONVERSATION_CHARS` below the
  plugin's 400000 default** in `~/.claude-mem/settings.json`, and do not
  treat it as a cost lever — the model/tier settings are. Set to 150000 on
  2026-09-12 during a cost pass, it stalled every observer session with big
  tool outputs: the worker's `drain()` feeds queued messages to the CLI with
  no backpressure, the budget check counts *unanswered* messages, and a
  budget recycle aborts the answer in flight, so a session that fills the
  budget before its first answer makes zero progress; after 2 such recycles
  it sleeps 10 minutes (`Observer conversation still does not fit after 2
  recycles`). 8 of those in 40 minutes, 0 the day before; queue 2689 -> 2876
  and climbing. Restored to 400000 at 09:00 and the queue turned around
  within 10 minutes. The same mechanism still caps throughput at 400000 on
  heavy sessions — that is a plugin (13.24.23) limit, not a setting. The
  queue is in-memory, so a worker restart discards everything pending.

## Why a snapshot

The live configs carry API tokens inline, so they can never be committed. That
left the server set itself recorded nowhere: adding, removing or repointing an
MCP server was a decision made once, on one machine, and lost on the next.
`capture` records the decision without the credentials.

```sh
./claude.sh status              # what drifted since the last capture
./claude.sh capture --apply     # record the current server set
./claude.sh restore --apply     # put the recorded set back on a new machine
```

## Placeholders are not only for git

Claude Code expands `${VARNAME}` out of its own process environment, in `env`
blocks and in `headers` alike, so `restore` writes the placeholders through
untouched — the file Claude Code reads then holds no credential at all. Claude
Desktop is a GUI app with no login shell behind it, so its values are expanded
before writing.

A variable that is not exported would resolve to nothing and break that
server, so `restore` keeps whatever the live config had for it and says which
variable to export.

Quit the app before `restore --apply`: both rewrite their config on exit and
will clobber the write.

Rotations go the other way — `scan-for-secrets refresh --apply` pushes current
`~/.config/shell/env.sh` values into both live configs (they are in
`REFRESH_JSON_TARGETS`). Placeholders are left alone by design.

Profile snapshots of the full live configs once lived in this folder, and
embedded GitHub PATs in MCP server env blocks. They were deleted 2026-08-21.
Keep any such copy in `<config>/claude/`, never in the checkout; `mcp/` is the
sanctioned record.

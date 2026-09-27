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
- `claude.sh mem-pro-watchdog [--apply]` — moves the claude-mem observer
  along an ordered provider chain by toggling `CLAUDE_MEM_PROVIDER` in
  `~/.claude-mem/settings.json` (the worker re-reads it per generator start).
  Run every 15 minutes by the `claude-mem-pro-watchdog` cron job. Exists
  because the plugin's own fallback never fires on a 402 from cmem.ai: that
  error takes the worker's pause-and-preserve path, which re-probes every 30
  minutes forever and stalls the queue (0 fallback log lines across the
  2026-09-10..12 exhaustion). State: `~/.claude-mem/pro-watchdog.json`;
  log: `~/.claude-mem/logs/pro-watchdog.log`. Every switch posts a `notify`.

  **The chain** is `CLAUDE_MEM_PROVIDER_CHAIN` in `settings.json`, highest
  priority first, for example `"openrouter,gemini,claude"`: the cmem Pro
  gateway, then Gemini, then the Claude subscription. Absent, it is
  `"openrouter,claude"`, the two-provider toggle this was before the chain.
  A provider counts only when it also holds what the worker needs:

  | Provider | Needs |
  |---|---|
  | `openrouter` | a `cm_pro_` `CLAUDE_MEM_CLOUD_SYNC_TOKEN`, the gateway as `CLAUDE_MEM_OPENROUTER_BASE_URL`, and the same token as `CLAUDE_MEM_OPENROUTER_API_KEY` |
  | `gemini` | `CLAUDE_MEM_GEMINI_API_KEY` in `settings.json`, or `GEMINI_API_KEY` in `~/.claude-mem/.env` |
  | `claude` | nothing |

  claude-mem 13.25.3 reads the Gemini key from those two places only, never
  from its process environment. With `CLAUDE_MEM_PROVIDER=gemini` and no key
  there it runs the Claude subscription without saying so. The watchdog reads
  the same two places, and never writes the key anywhere.

  **Down** needs the worker's own signal (a `quota-cooldown.json` entry, or
  `observer-health.json` reporting `quota_exhausted` after the last success)
  *and* a confirming probe: a 1-token completion for the gateway with each
  of the first three models in `CLAUDE_MEM_OPENROUTER_MODEL`, in the order
  the worker falls back through them (`allowance_exhausted` or a 402 is
  exhausted, and `key_invalid`, `subscription_inactive` or a 401 is
  inactive, at the first model that says so; a success with any model is
  ok; a 403 without one of those codes tries the next model), a 1-token `generateContent` with the configured model for
  Gemini (429 or `RESOURCE_EXHAUSTED` is exhausted, a rejected key is
  inactive). The Claude subscription has no cheap probe, so a cooldown
  entry for `claude` is its only answer. It counts for 30 minutes from
  arming, the window in which the worker refuses the provider; after that
  the worker admits a probe, and only a success with `claude` removes the
  entry, which never happens while another provider runs. The same
  30-minute window rules out any candidate: a provider the worker still
  refuses is passed over without a probe. The observer moves to the
  next provider down the chain that answers. When none does, a provider above
  that answers now takes it at once, since waiting would stall the queue.
  When nothing does, it stays and
  notifies once for that episode, not every run. Any failed check ends that
  provider's success streak. **Up**, while the current provider works, probes
  every enabled
  provider above it each run, and returns to the highest that
  has answered for `CLAUDE_MEM_PRO_RETURN_DELAY_MIN` (60) minutes running,
  since neither cmem.ai nor Google publishes a reset time. A probe that gets
  no clear answer never moves anything. `CLAUDE_MEM_PRO_GATEWAY` and
  `CLAUDE_MEM_GEMINI_ENDPOINT` override the two endpoints, for the tests.

  **A configuration it does not manage exits 0, not 1.** Three states an
  operator chose each log one line and exit 0: no `settings.json` at all, a
  `CLAUDE_MEM_PROVIDER` the chain does not list, and a chain that leaves
  fewer than two usable providers (for example, a
  `CLAUDE_MEM_CLOUD_SYNC_TOKEN` that is not a `cm_pro_` key under the default
  chain). Two are faults nobody chooses, and they keep a non-zero exit: an
  `CLAUDE_MEM_OPENROUTER_API_KEY` that differs from the Pro token, and a
  `settings.json` that is there but unreadable — which includes a `settings.json`
  that is a symlink to nothing, since a link that stopped resolving is a broken
  configuration and not an absent one. The
  reason is the schedule: `cron-jobs.sh` reads any non-zero exit as FAILED and
  its run report opens an item, so on `*/15` a deliberate setting became about
  ninety-six report items a day (sd:880). Setting `CLAUDE_MEM_PROVIDER` to
  `gemini` on 2026-09-14 did exactly that; with `gemini` in the chain it is
  now managed.
- `claude.sh prune-mem-logs [--apply]` — log retention claude-mem lacks.
  The worker writes one `~/.claude-mem/logs/claude-mem-YYYY-MM-DD.log` a day,
  16-56 MB each, and deletes none; 13.25.3 has `CLAUDE_MEM_LOG_LEVEL` and no
  retention setting (41 files, 997M on 2026-09-25). This deletes the dated
  files older than `CLAUDE_MEM_LOG_KEEP_DAYS` (14) days and trims
  `pro-watchdog.log` to its last 5000 lines, by temp file and rename under an
  exclusive `flock` the watchdog's appends also take, so no line is lost; a
  writer or second trim that waited on the lock reopens the new file. Today's
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
  `tools` leg of `system-native`. It drives `mem-pro-watchdog` against a
  scratch `CLAUDE_MEM_DATA_DIR` and a stub server on `127.0.0.1`, so it reads
  and writes nothing under `~/.claude-mem` and asks no remote anything.
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

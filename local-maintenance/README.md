# local-maintenance

Nightly machine upkeep in one sweep: install pending macOS / App Store updates,
docker prune (Sundays), exited containers and the brew, npm and uv caches
(nightly), idle cargo build output in checkouts, backup freshness,
certificate expiry, log rotation, disk space. Findings that need a human go
to the run's report, which Reports and Today show; safe fixes (log
rotation, the prunes, the build output) happen automatically.

## Usage

```sh
./maintenance.sh check   # read-only: print findings and would-be fixes
./maintenance.sh run     # apply safe fixes, report findings (cron entrypoint)
./maintenance.sh uv-prune  # prune uv's cache (the login agent's entrypoint)
./maintenance.sh uv-prune-plist  # print that login agent's plist
./maintenance.sh move <path> [folder]  # put a path on the bulk SSD, then link it
./maintenance.sh test    # run the unit tests
```

## Move a path to the bulk storage root

`move <path> [folder]` copies a file or folder to
`<bulk root>/<folder>/<path inside its checkout>`, checks every file by
size, content and extended attributes, then replaces the source with a symlink to the copy
(sd:8797). `<folder>` defaults to the checkout's name; outside a checkout
name it, and the copy goes to `<bulk root>/<folder>/<basename>`. The root
is the workflow database's `sd.bulk_storage_root`. On macOS the copy is
Apple's `ditto`, which keeps resource forks and Finder tags. A mismatch removes the
copy and keeps the source.

Writers are quiesced as machine-setup's bulkmove stage does it (sd:8242):
the move holds the flock of each loaded cron job that starts pack runs
(`build-dispatch`, `lane-run`, `night-build`), so a slot that fires
meanwhile skips, and reads the open pack runs (`lib/open_runs.py`) before
the copy and again before the link. A run in progress, an open run or an
unreadable database keeps the source; one found after the copy removes
the copy. Other writers (an editor, an intake job) are not locked out,
so `lsof` and a full compare run again just before the link, with the
source still in place; a hit, or an `lsof` that fails, removes the
copy. Once linked, the copy is the live data and nothing rolls back:
the original, set aside as `<path>.bulk-move-<pid>`, is deleted only
when `lsof` and a compare find it unchanged. Otherwise it stays, the
move exits 1 and names it: compare the two before deleting it. It refuses, changing nothing: a path git
tracks or a folder holding one, a checkout, build output (`target/`,
`node_modules/`), a path a process holds open (`lsof`; an `lsof` that
fails or exits nonzero refuses too), a relative symlink that leads out
of the folder, a missing or full bulk root, an existing destination, and
a destination outside the bulk root or inside the source (a `..` or
absolute folder, a bulk root inside the moved folder). It also refuses a
copy with any path under `<bulk root>/*/lane/`: repo-sync's lane sweep
deletes files there 14 days old, and the copy keeps the source's dates.

With findings, `run` prints one line per finding, `SD_REPORT_ATTENTION:
maintenance on <host>: <finding>`, and one per pending update under its finding,
and exits 0. A finding leaves out a measure that drifts (the free-space
percentage), so the same findings each night stay one open report, fewer of
them fold into it too (pack sd:4658), and a new or changed one opens its own and closes
the older one on that host, with the exceptions
[`local-cron-jobs`](../local-cron-jobs/README.md) names. The cron
run's ingest maps that line to a report that
needs attention, so Reports and Today carry the findings until the report is
acknowledged; nothing is mailed (sd:3454).

## Checks

Configured in `<config>/maintenance/maintenance.conf`, outside the checkout
(`<config>` is `$SYSTEM_TOOLS_CONFIG`, default `~/.config/system`; start from
`cp maintenance.conf.example ~/.config/system/maintenance/maintenance.conf`). Pipe-separated, `~/` expands
to `$HOME`:

| Directive | Meaning |
| --- | --- |
| `disk\|<path>\|<pct>` | filesystem of `<path>` has at least `<pct>`% free |
| `cert\|<pem>\|<days>` | x509 cert not expiring within `<days>` |
| `mount\|<path>` | volume is mounted |
| `process\|<name>\|<label>` | process running (`pgrep -x`) |
| `age\|<file>\|<days>\|<label>` | file modified within `<days>` |
| `logs\|<dir-or-file>\|<mb>` | rotate `.log` files above `<mb>` |

Checks only one machine wants go in `<config>/maintenance/maintenance.<profile>.conf`, read after
the common file. The profile is whatever machine-setup recorded for this
machine (`~/.config/machine-setup/profile`); `MAINTENANCE_PROFILE` overrides
it, and an absent file is not an error. Profile files are gitignored too.

Built in, no conf needed: `softwareupdate -l`, `mas outdated`, AI session
logs past their retention (`scan-for-secrets.sh prune`, which owns the
retention and the directory list — 30 days for the transcripts, 7 for the
codex shell-snapshot cache, and `~/.claude/projects` deliberately not in the
list because Claude Code deletes its own), on Sundays `docker system prune -f` (containers
and dangling images only — volumes are never pruned, several local services
keep state in them), and idle build output in checkouts (sd:5994).

Every night `run` also clears caches no other job cleans (sd:8746):
exited docker containers (`docker rm`; created ones stay), `brew cleanup
-s`, `npm cache clean --force` (skipped while npm runs), and `uv-prune`
below. A uv prune skipped three nights in a row is a finding, naming
the uv commands that hold the cache: on an always-on machine another uv
server may never let go of it. Each later night of the streak raises
the same finding, which folds into the open report, so the alert stays
open until a prune succeeds.

`run` installs the pending updates it finds (sd:6927):
`sudo -A softwareupdate -i <label>` for each macOS update that needs no
restart, and `sudo -A mas upgrade <id>` for each App Store app, one at a
time under a 600 s bound, so one stuck download holds up no other app
(sd:7188). sudo reads the password through
`local-machine-setup/sudo-askpass.sh`, from the login-Keychain item that
machine-setup's "Unattended sudo" step makes. Without the item sudo
refuses, and the update stays a finding with sudo's reason. A macOS
update that needs a restart stays a finding: the restart is yours.
`check` installs nothing.

`run` deletes each cargo `target/` folder under `~/worktrees` or
`~/repos` (main checkouts, sd:8779), at depth 2 or 3, that nothing
wrote or ran for 3 days; `check` names them. An exec sets a file's access time on APFS, so a file accessed within 3 days keeps
the folder: a trial ran a binary there each evening that nothing rebuilt
(sd:6128). A folder's access time does not count, as the walk sets it.
A folder that `lsof` shows a process using (a running binary, an open
file, a working directory) stays too, however old: a paid run lost the
binary it ran (sd:8240); an `lsof` that fails keeps every folder.
Only a folder holding cargo's `CACHEDIR.TAG` counts, and no link is
followed; the checkout and its commits stay. While a `cargo` or `rustc`
process runs, the run prunes nothing more, with no finding: a cargo
waiting on a lock would get the moved copy. An old time is no proof cargo is idle,
so the move uses cargo's own lock: it takes `flock(2)` on every
`.cargo-lock` in the folder without waiting, and while it holds them,
one rename(2) moves `target/` into
`~/.local/state/maintenance/prune-trash/`. A busy lock is a running
build, and the folder stays. A build can lock a new profile after that
pass and before the rename, so after the rename `prune_move.py` locks
every `.cargo-lock` in the moved copy too. A busy or unreadable one moves
`target/` back; if `target/` exists again, the copy stays in the trash,
with a finding. After the rename no path leads into the trash, so no
build can lock there later. The delete runs in the trash: a cargo that
starts during the hold or the delete makes a fresh `target/`. A rename that
fails (another volume) keeps the folder; nothing is copied. A run
stopped during a delete leaves its entry in the trash, and the next run
deletes entries there, never a path in a checkout, and none whose
lock a build still holds. A probe that fails is a finding, and the folder
stays.
The walks and the delete run under `lib/bounded.sh`'s `st_step`.

## uv cache prune at login

`run` takes `uv-prune` each night too; at login the
uv-cache-prune LaunchAgent runs it once. Its label is
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
Claude sessions use a cache of their own: machine-setup's settings
baseline sets `UV_CACHE_DIR` to `~/.cache/uv-claude`. Each session's
`uvx` MCP proxy then holds that cache's lock, not the main one.

At login only one such server runs: the Google Workspace MCP, a KeepAlive
agent. `uv-prune` does these steps:

1. Pause each loaded agent in `UV_PRUNE_PAUSE` (default
   `$SYSTEM_TOOLS_LABEL_PREFIX.google-workspace-mcp`; the rendered plist
   carries the value) with `launchctl bootout`. kev is not paused: it
   serves from its venv's python, so no uv process of its runs.
   A `uvx` run, such as a Claude session's MCP proxy, is never
   signalled: nothing proves its client is gone. While one holds the
   lock the prune skips; the login prune after a reboot cleans the cache.
2. Wait up to `UV_PRUNE_WAIT` seconds (20) for the holders of the
   cache's lock to exit. `lsof -t <cache>/.lock` lists them, so a uv
   on another cache does not count.
3. Skip the prune, exit 0, when a holder is still left.
4. Run `uv cache prune` with `UV_LOCK_TIMEOUT` of `UV_PRUNE_LOCK_TIMEOUT` (60).
   At login it then prunes the Claude sessions' cache,
   `UV_PRUNE_SESSION_CACHE` (`~/.cache/uv-claude`), the same way.
   The nightly `run` prunes the main cache only.
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
- A cron job's log rotates only under the job's `.<job>.flock`, and not
  while `.<job>.unreported` or a `.<job>.reporting.<run>` exists: each
  defers it to a later night.
  The `maintenance-nightly` log itself holds its lock during the run, so
  it always defers.
- Milvus's `volumes/.../rdb_data*/*.log` are RocksDB **data** files, not
  logs. Never add those paths to the conf.
- The `maintenance-nightly` cron job (local-cron-jobs) runs `run` at 03:45.

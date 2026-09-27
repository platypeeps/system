# local-cswap — `cswap auto` as a launchd daemon

Runs `cswap auto` (the foreground account-switching poll loop) as a per-user
macOS LaunchAgent that starts at login, restarts if it dies, and writes all
output to a log file in this directory.

## Layout

| Path | Purpose |
| --- | --- |
| `cswap.sh` | Single entrypoint: `run` (what launchd executes) plus `start`/`stop`/`status` |
| `cswap-auto.plist.template` | LaunchAgent template; `start` fills `@LABEL@`, `@DIR@`, `@HOME@` and writes it to `~/Library/LaunchAgents` |
| `cswap-auto.conf` | Flags for `cswap auto` and the path to the binary |
| `logs/cswap-auto.log` | stdout |
| `logs/cswap-auto.err.log` | stderr |

## Usage

```bash
./cswap.sh start      # install, start, run at every login
./cswap.sh status     # is it alive? plus last 15 log lines
./cswap.sh status 50  # ...last 50 log lines
./cswap.sh stop       # stop and unload (stays stopped across logins)

./cswap.sh stop --keep-installed  # kill the process only; KeepAlive/login restarts it
tail -f logs/cswap-auto.log            # follow live
```

`status` exits `0` running, `1` loaded but not running, `2` not loaded —
usable in scripts.

## Changing flags

Edit `cswap-auto.conf`, then restart:

```bash
./cswap.sh stop && ./cswap.sh start
```

Example — poll every 5 minutes, switch at 85%, watch the Opus weekly window:

```bash
: "${CSWAP_AUTO_ARGS:=--interval 300 --threshold 85 --model Opus}"
```

The `:=` form means precedence is **environment > `cswap-auto.conf` > built-in
default**, so you can override for a one-off foreground run without editing the
file: `CSWAP_AUTO_ARGS="--dry-run" ./cswap.sh run`.

`start` re-renders the plist from the template and re-bootstraps every run, so
it is also the way to apply template edits.

The launchd label is `$SYSTEM_TOOLS_LABEL_PREFIX.cswap-auto`; the prefix
defaults to `local.system-tools`. Stop the agent before changing the prefix,
or the old label stays loaded.

## Notes

- The agent is a **LaunchAgent** (`gui/$UID`), not a system daemon: it runs as
  your user with your keychain and `~/.claude` state, which is what `cswap`
  needs. It does not run while you are logged out.
- `PYTHONUNBUFFERED=1` is set in the plist so the log updates line by line
  instead of in 8 KB blocks.
- `ThrottleInterval` is 30 s, so a crash loop respawns at most twice a minute.
  A missing/non-executable `cswap` exits `78`, which launchd does not hot-loop.
- The logs are not rotated. If they grow, truncate with
  `: > logs/cswap-auto.log` (truncate rather than delete — launchd holds the
  file descriptor open, so `rm` would leave writes going to a deleted inode).

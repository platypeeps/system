# local-cswap — one Claude account on every machine

Remote Control sees only the sessions of the active Claude account.
When machines rotate their accounts on their own, they drift apart, and the hub stops seeing the satellites.
`cswap.sh sync` keeps every machine on the account the hub picks (sd:7323).

## How it works

A local-cron-jobs job (`local-cron-jobs/examples/cswap-sync.job`) runs `cswap.sh sync` every minute on every machine.
The machine's role decides what sync does:

- **The hub** is the machine whose `sd-db fence` reads `holder` (exit 0).
  It runs one `cswap auto --once --strategy consume-first` tick, then publishes the active account's `organizationUuid` as the workflow database setting `sys.cswap_account`.
  It writes the row only when the value changes.
- **Every other machine** reads `sys.cswap_account`, finds its own account with that `organizationUuid` in `cswap list --json`, and runs `cswap switch <n>` only when that account is not active.
  Account numbers differ between machines; the id does not.

A promoted satellite reads `holder`, so it leads by itself after a failover.
A fence that fails or prints anything else follows: an unknown answer must not make a second leader.

Sync switches nothing and exits 1 when the hub cannot be read, the row is unset or is no UUID, or no single account here has that id.
It then prints `SD_REPORT_ATTENTION: data safe, one step failed: cswap sync <step>`, so Today lists the failure and nothing pushes to the phone.
A hub that refuses this machine's `sd_db` build is not yet such a failure: the hub moved its pins, and `repo-sync.sh follow` catches up within its 5-minute job and 600 s bound.
Sync then prints a `skipped:` line, switches nothing and exits 0; it counts these runs in `state/build-refusals` and fails from the 30th in a row (system sd:7407).
A hub this machine cannot reach (`HubUnreachable`, or Tailscale cannot name this node) counts the same way: a tailnet drop of a minute or two opened a report on each satellite (sd:7650).
It reads the row with the pack's `SD_FOLLOWED` marker set, so `sd` starts no follow of its own that the 60 s bound would cut part way.
Each `cswap`, `sd` and `sd-db` call runs under `lib/bounded.sh`.

A switch drops each session's Remote Control link, and `/rc` in a dropped session reconnects it under the new account (sd:9427).
So a sync that switches the account, on the hub or a satellite, writes `state/rc-pending` before any later step can fail.
Once `herdr agent list` answers with a list it can parse, each claude pane it names is owed in `state/rc-owed`, keyed by herdr session and pane.
A sync reads and writes only its own herdr session's rows (`HERDR_SESSION`, default `sd`), so a pane owed in another session waits for a sync there.
Each herdr session lists its panes once per switch, within 30 minutes of it: `state/rc-listed` names the switch and each session that listed for it, written only under the reconnect lock. A pane keeps its `state/rc-owed` row for the switch, marked `sent` before its `/rc` goes; a listing retried for a pane with no session id owes only panes with no row, so a sent pane gets no second `/rc`, and a failed write loses one `/rc` at worst. Only a switch writes `state/rc-pending`, and no sync deletes it.
One reconnect pass runs at a time on the machine (a lock on `state/rc-lock`): a sync that finds it held skips the pass, and its owed panes wait for the next sync.
Every sync then submits `/rc` (`herdr agent prompt`) to each owed pane that is idle or done, whose latest Remote Control banner, as Claude prints it at line start, is `⏺ Remote Control disconnected — `, not `  /remote-control is active · `, and whose input box (the rules around the last `❯` line) holds no typed text and is the last thing on screen, with no dialog below it; dim text there is a prompt suggestion.
Empty means the line is exactly `❯` and its no-break space, plus at most dim text: blanks of any kind, or a control sequence other than colour, read as typed text.
Only a drop banner and box rules in Claude's own colour count: text typed into a draft is plain, so a draft that types a banner or a box is never read as an empty box.
A pane herdr names no session id for gets no keys: at the listing it is not owed yet, and the next sync lists again; an owed pane keeps its row until herdr names its session again. Any other pane gets no keys, as a key can answer a permission dialog or join a half-typed prompt; it stays owed, and the next sync tries again.
An active banner stays owed too: it may be the old account's, printed before the drop.
Right before the keys, a fresh screen read, `herdr agent get` and a last screen read check the pane again: a session that takes the pane during the first read gets no keys, and a draft typed during the get moves the screen, which must read the same twice; from the first read to the end of the send must take at most 1000 ms (`CSWAP_RC_GAP_MS`), or the pane stays owed, as a draft typed during a slow check or a slow delivery would take the `/rc`; the send is bounded by the time left, to the millisecond (herdr takes `--timeout` only with `--wait`), and a send that failed or ran late counts as sent, as it may have landed, so it is not sent twice.
The account sync runs first, then one reconnect pass of at most 60 seconds (`CSWAP_RC_BUDGET`): each herdr call is bounded by what is left of it, and a pane it does not reach stays owed.
A pane leaves the list once sent, gone, running another session, or once its switch is no longer the current one: a newer switch, or 30 minutes after it. Only rows of the current switch take a `/rc`, checked again right before the keys, so a newer switch's listing never sends a pane a second one.
A sync that switches nothing writes no list, so a connected session is never toggled off.
Cron's PATH may lack herdr, so sync also looks in `~/.local/bin`, `/opt/homebrew/bin` and `/usr/local/bin` (`CSWAP_HERDR_DIRS`).
No herdr, or no herdr server answering, prints one `reconnect: skipped:` line and leaves the exit code alone.
Each herdr call runs under `st_step`.
Each herdr call targets the session `local-herdr` starts: `sd`, or `HERDR_SESSION` when set; an inherited `HERDR_SOCKET_PATH` is dropped.

`sys.cswap_account` is declared in the repository's `sd-plugin.json`, so `sd config get|set` reads and writes it as a database row, over the tailnet on a satellite.
The `sys` plugin must be registered on every machine: machine-setup's `repos` stage registers it, and `sd plugin list` names it.

## Usage

```bash
./cswap.sh sync     # what the job runs; safe to run by hand
./cswap.sh status   # same account as the hub?
sd config get sys.cswap_account   # the account the hub published
```

`status` exits `0` on the hub's account, `1` on another account, with the hub unreadable, or with the retired agent still installed, and `3` with nothing to check (no `cswap`, or nothing published yet).

## Changing flags

`cswap-auto.conf` holds the path to `cswap` and the flags for the hub's `cswap auto` tick, such as `--threshold 85`.
Sync adds `--strategy consume-first` after them.
The `:=` form means precedence is **environment > `cswap-auto.conf` > built-in default**.

## The retired agent

Until sd:7323, `cswap.sh start` installed an always-on `cswap auto` LaunchAgent, `<prefix>.cswap-auto`.
Sync boots out a loaded one and deletes its plist from `~/Library/LaunchAgents`, so it cannot rotate the machine on its own again.
The machine-setup agents stage never installs a profile's `*.cswap-auto` line; it reports it `DIFFERS` until the line and its config plist are gone.
`logs/` holds that agent's old logs; nothing writes there now.

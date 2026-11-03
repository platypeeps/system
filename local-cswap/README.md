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
Each `cswap`, `sd` and `sd-db` call runs under `lib/bounded.sh`.

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

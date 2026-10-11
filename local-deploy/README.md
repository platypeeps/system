# local-deploy

After a merge to this checkout, restart only the sd services the merge changed (sd:2725).
Each restart uses its safe form, and each is checked before the next one starts.
A LaunchAgent change is reported, never applied.

```sh
local-deploy/deploy.sh plan <from-sha> <to-sha>    # print the actions
local-deploy/deploy.sh apply <from-sha> <to-sha>   # run them
local-deploy/deploy.sh upgrade [--from SHA]        # pull main, apply, record
```

## What a change maps to

`plan` reads `git diff --name-only from..to` in this checkout.
Changes under a `tests/` folder and to `*.md` files restart nothing.

| Changed | Action |
| --- | --- |
| `local-sd-db/` | restart sd-serve |
| `local-project-dashboard/` | restart dashboard |
| a `*.plist` or `*.plist.template`, or `.py`/`.sh` lines that name launchd keys or the plist environment | report `needs <folder> install`; the service still restarts |

The install check greps changed lines, because the dashboard writes its plist in code.
It skips `local-deploy/`, which names those keys but writes no plist.
A plist value computed outside those lines goes unseen.

`sd_db` and its schema live in the command pack's `lib/` (sd:3279).
The dashboard and sd-serve run that `lib/`, and `local-repo-sync/repo-sync.sh refresh` moves it.
So no change in this checkout installs `sd_db` or needs a migration.
System's frozen `local-sd-db/sd_db` is no service's library.

## How `apply` restarts each service

`apply` prints the reports, then refuses with exit 1 before any restart when `lsof` cannot answer for port 8769.
It is missing, or exits other than 1 with no output (its "no match").
The dashboard checks its own library when it starts: the pack's `lib/sd_db` and no other copy.

Then it restarts each service:

| Service | Restart | Check |
| --- | --- | --- |
| sd-serve | `launchctl kickstart -k gui/<uid>/<prefix>.sd-serve` | a new listener on port 8769 within `DEPLOY_WAIT` seconds (default 30) |
| dashboard | `launchctl kickstart -k gui/<uid>/<prefix>.sd-dashboard` | `dashboard.sh health --wait`, up to `DEPLOY_WAIT` seconds |

- The label prefix is `SYSTEM_TOOLS_LABEL_PREFIX`, default `local.system-tools`.
- A service whose agent launchd does not hold is skipped, so a satellite skips sd-serve.
- sd-serve is skipped with a report while any TCP connection on port 8769 is established.
  One satellite session is one connection (`sd_db/serve.py` in the pack's `lib/`).
  Run `apply` again once the session closes.
- `apply` stops at the first failed check and exits 1 naming it.

Limit of the session probe: `lsof` sees the moment it runs.
A satellite can open a session between the probe and the kickstart; that session drops, as on any hub restart.

## Upgrading the hub in one command

`upgrade` does the whole hub upgrade in one line (sd:2812):

```sh
sh ~/repos/system/local-deploy/deploy.sh upgrade
```

1. It refuses while another upgrade holds `upgrade.lock` beside the record: an `flock` the kernel drops when its upgrade ends.
   It refuses unless the checkout is on `main` with no uncommitted or untracked files.
2. It runs `git fetch origin`, then fast-forwards to `origin/main`; it refuses when they diverged.
3. It reads the last deployed sha from `${XDG_STATE_HOME:-$HOME/.local/state}/system/deploy/deployed`.
   The first run has no record: give `--from SHA`, the sha the services run now. `--from` always overrides the record.
4. An empty plan records the new sha and exits 0.
5. An open sd-serve session refuses before any restart.
6. It runs `apply`. Inside upgrade, `apply` prints no `skip` line for an agent listed in `stopped`.
7. It starts each agent listed in the file `stopped` beside the record: sd-serve (a new listener), then the dashboard (`health --wait`).
   This version stops no agent. An older one stopped them to install `sd_db`; the list lets a rerun finish that upgrade.
   An agent whose wait fails after its bootstrap is booted out again and stays listed.
   It exits 1 and prints the rerun that finishes: `sh <checkout>/local-deploy/deploy.sh upgrade --from <from>`.
   The rerun bootstraps only a listed agent that launchd does not hold; a held one still passes its wait.
8. It records the new sha only when every step passed and no session held sd-serve back.
   A `needs <folder> install` report also holds the record: install the agent, then record with `upgrade --from <new sha>`.
   After a failure the old record stays, so a rerun replays the same range; the restarts are idempotent.

The pull runs before the steps, but the steps run the `deploy.sh` read before the pull.
A change to `deploy.sh` itself takes effect on the next upgrade.

## Wiring it into the merge lane

The operator adds this line to the lane after a system merge, once this checkout stands on the new main:

```sh
~/repos/system/local-deploy/deploy.sh apply <old main sha> <new main sha>
```

The lane needs allow rules for the two `launchctl kickstart -k` labels.

## Tests

`deploy.sh test` runs `tests/test_deploy.py`.
It stubs `launchctl`, `lsof` and `dashboard.sh`, so it runs on Linux.

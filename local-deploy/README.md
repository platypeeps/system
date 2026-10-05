# local-deploy

After a merge to this checkout, restart only the sd services the merge changed (sd:2725).
Each restart uses its safe form, and each is checked before the next one starts.
A LaunchAgent change or a schema change is reported, never applied.

```sh
local-deploy/deploy.sh plan <from-sha> <to-sha>    # print the actions
local-deploy/deploy.sh apply <from-sha> <to-sha>   # run them
```

## What a change maps to

`plan` reads `git diff --name-only from..to` in this checkout.
Changes under a `tests/` folder and to `*.md` files restart nothing.

| Changed | Action |
| --- | --- |
| `local-sd-db/` | restart sd-serve |
| `local-project-dashboard/` | restart dashboard |
| `local-sd-runner/` | restart runner |
| a `*.plist` or `*.plist.template`, or `.py`/`.sh` lines that name launchd keys or the plist environment | report `needs <folder> install`; the service still restarts |
| `local-sd-db/sd_db/schema.py` or `local-sd-db/sd_db/schema/` | report `needs migration; restart after migrate`; nothing restarts, since new code may read a schema not yet there |

The install check greps changed lines, because the dashboard and the runner write their plists in code.
A plist value computed outside those lines goes unseen.

## How `apply` restarts each service

| Service | Restart | Check |
| --- | --- | --- |
| sd-serve | `launchctl kickstart -k gui/<uid>/<prefix>.sd-serve` | a new listener on port 8769 within `DEPLOY_WAIT` seconds (default 30) |
| dashboard | `launchctl kickstart -k gui/<uid>/<prefix>.sd-dashboard` | `dashboard.sh health` |
| runner | `runner.sh restart`, which drains first; never a kickstart | its exit code |

- The label prefix is `SYSTEM_TOOLS_LABEL_PREFIX`, default `local.system-tools`.
- A service whose agent launchd does not hold is skipped, so a satellite skips sd-serve.
- sd-serve is skipped with a report while any TCP connection on port 8769 is established.
  One satellite session is one connection (`local-sd-db/sd_db/serve.py`).
  Run `apply` again once the session closes.
- `apply` stops at the first failed check and exits 1 naming it.

Limit of the session probe: `lsof` sees the moment it runs.
A satellite can open a session between the probe and the kickstart; that session drops, as on any hub restart.

## Wiring it into the merge lane

The operator adds this line to the lane after a system merge, once this checkout stands on the new main:

```sh
~/repos/system/local-deploy/deploy.sh apply <old main sha> <new main sha>
```

The lane needs allow rules for the two `launchctl kickstart -k` labels.

## Tests

`deploy.sh test` runs `tests/test_deploy.py`.
It stubs `launchctl`, `lsof`, `dashboard.sh` and `runner.sh`, so it runs on Linux.

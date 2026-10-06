# local-deploy

After a merge to this checkout, restart only the sd services the merge changed (sd:2725).
Each restart uses its safe form, and each is checked before the next one starts.
A LaunchAgent change or a schema change is reported, never applied.

```sh
local-deploy/deploy.sh plan <from-sha> <to-sha>    # print the actions
local-deploy/deploy.sh apply <from-sha> <to-sha>   # run them
local-deploy/deploy.sh upgrade [--from SHA]        # pull main, install, apply, record
```

## What a change maps to

`plan` reads `git diff --name-only from..to` in this checkout.
Changes under a `tests/` folder and to `*.md` files restart nothing.

| Changed | Action |
| --- | --- |
| `local-sd-db/` | restart sd-serve |
| `local-project-dashboard/` | restart dashboard |
| `local-sd-runner/` | restart runner |
| `local-sd-db/sd_db/` | report `needs sd_db install`, then restart sd-serve, dashboard and runner |
| a `*.plist` or `*.plist.template`, or `.py`/`.sh` lines that name launchd keys or the plist environment | report `needs <folder> install`; the service still restarts |
| `local-sd-db/sd_db/schema.py` or `local-sd-db/sd_db/schema/` | report `needs migration; restart after migrate`; nothing restarts, since new code may read a schema not yet there |

The install check greps changed lines, because the dashboard and the runner write their plists in code.
It skips `local-deploy/`, which names those keys but writes no plist.
A plist value computed outside those lines goes unseen.

The dashboard and the runner import an installed `sd_db`, not this checkout's.
A library change reaches them only after `local-sd-db/sd-db.sh install <venv>` for each one's interpreter.

## How `apply` restarts each service

`apply` prints the reports, then refuses with exit 1 before any restart in three cases:

- `lsof` cannot answer for port 8769: it is missing, or exits other than 1 with no output (its "no match").
- The dashboard's or the runner's installed `sd_db` differs in content from the checkout's `local-sd-db/sd_db`.
  The check runs `source:local-project-dashboard/sd_dashboard/runtime.py::_build_manifest` on both and compares them file by file.
  It does not use `_library_lag`: that compares commits, and a wheel from `sd-db.sh install` records none.
  It runs under the interpreter the agent's plist names (`SD_DASHBOARD_PYTHON`, `SD_RUNNER_PYTHON`).
  The refusal names the `sd-db.sh install <venv>` that provisions it.
- The plan restarts the runner, and `runner.sh restart --dry-run` refuses.
  The dry run applies the runner's own agent and load checks (`max_load`, default the core count) and kicks nothing.

Then it restarts each service:

| Service | Restart | Check |
| --- | --- | --- |
| sd-serve | `launchctl kickstart -k gui/<uid>/<prefix>.sd-serve` | a new listener on port 8769 within `DEPLOY_WAIT` seconds (default 30) |
| dashboard | `launchctl kickstart -k gui/<uid>/<prefix>.sd-dashboard` | `dashboard.sh health --wait`, up to `DEPLOY_WAIT` seconds |
| runner | `runner.sh restart`, which drains first; never a kickstart | its exit code |

- The label prefix is `SYSTEM_TOOLS_LABEL_PREFIX`, default `local.system-tools`.
- A service whose agent launchd does not hold is skipped, so a satellite skips sd-serve.
- sd-serve is skipped with a report while any TCP connection on port 8769 is established.
  One satellite session is one connection (`local-sd-db/sd_db/serve.py`).
  Run `apply` again once the session closes.
- `apply` stops at the first failed check and exits 1 naming it.

Limit of the session probe: `lsof` sees the moment it runs.
A satellite can open a session between the probe and the kickstart; that session drops, as on any hub restart.

## Upgrading the hub in one command

`upgrade` does the whole hub upgrade in one line (sd:2812):

```sh
sh ~/repos/system/local-deploy/deploy.sh upgrade
```

1. It refuses unless the checkout is on `main` with no uncommitted or untracked files.
2. It runs `git fetch origin`, then fast-forwards to `origin/main`; it refuses when they diverged.
3. It reads the last deployed sha from `${XDG_STATE_HOME:-$HOME/.local/state}/system/deploy/deployed`.
   The first run has no record: give `--from SHA`, the sha the services run now. `--from` always overrides the record.
4. An empty plan records the new sha and exits 0.
5. A plan that needs a migration refuses. Migrate and restart by hand, then record with `upgrade --from <new sha>`.
6. A `needs sd_db install` plan runs `local-sd-db/sd-db.sh install <venv>` once per distinct venv.
   The venvs come from the interpreters the dashboard's and the runner's plists name. A failed install stops.
7. It runs `apply` over the range.
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
It stubs `launchctl`, `lsof`, `dashboard.sh` and `runner.sh`, so it runs on Linux.

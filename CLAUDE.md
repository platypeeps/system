# system — repo guide for Claude

Small scripts and wrappers that run local infrastructure on macOS workstations.
Every top-level folder is one independent tool with a `README.md`. There is no repository-wide build.
One operator runs these tools on their own machines; the repository is public so others can read and reuse them.
`README.md` covers setup and configuration for a human reader; this file holds the rules for changing the code.

This file holds rules. Record incidents and findings in `docs/`, not here.
Area rules load from `.claude/rules/` when you touch matching files (index at the end).

## Terms

- **Operator**: the person who runs these tools on their machines; their values never enter this repository.
- **Config folder**: the per-machine folder outside the checkout that holds private values (convention 3).
- **Command pack**: the public repository `platypeeps/sd-ai-command-pack`; it ships the `sd` workflow commands (`sd`, `sd-ship`, `sd-docs-lint`).
- **Workflow database**: the SQLite store behind `sd`, built by `local-sd-db`; a work item in it is written `sd:<n>`.
- **Jev**: an optional hosted model from TypeSafe, wrapped by `local-jev`; see the Jev section.

## This repository is public

- Never commit personal values: names, email addresses, hostnames, IP addresses, account ids, private repository names.
- Put them in the config folder (convention 3), and commit a `.env.example` or `<name>.conf.example` with `change-me` values.
- Use `example.test` names and TEST-NET addresses (`192.0.2.x`, `198.51.100.x`, `203.0.113.x`) in tests and docs.
- Run `local-scan-for-secrets/scan-for-secrets.sh` from the root before a commit that adds configuration.
- Install the pre-push leak guard once per clone: `sh local-leak-guard/leak-guard.sh install`.
  - It reads `<config>/privacy-patterns`, one ERE per line; never commit that file or its contents.
  - Test with synthetic patterns; no suite may need the real ones.

## Tests and CI

- Merges gate on `make check` through the local gate (`repo.ci=local`): `sd-ship merge` runs it via `sd-check` and posts `sd/local-gate`, the one required check.
- `make check` runs `tests/check.sh`: the preflight and all four legs of `tests/ci-native.sh`, plus `tests/run-macos-only.sh` suites on a Mac.
- Actions CI is off on purpose; add no workflow. Who, when, revert: `docs/local-ci-rollout.md`.
- A test that needs macOS goes in a suite named in `tests/macos-only-suites.txt`, never behind a skip; Linux cannot run it.
- Do not count suites in prose; the `run_suite` lines in `tests/ci-native.sh` are the enumeration, and prose counts go stale.
- Write tests as Python `unittest`, wired into a `run_suite` line or `tests/macos-only-suites.txt`; never skip one.
  `tests/ci-native.sh` fails an unwired folder, a skip or a missing summary; `tests/check.sh` fails a real `launchctl` call.

## Structure

- `local-sd-db` is a Python package (`sd_db`); its test entrypoint is `sd-db.sh test`.
- `sd-db.sh` database verbs run an installed copy when it is built for a newer schema than the checkout; `SD_DB_LIBRARY` forces a side.
- `local-sd-db` keeps its build backend in `_build.py`, so nothing is fetched to build it.
- One hub holds the workflow database; a satellite reaches it over the tailnet; setup and refusals: `local-sd-db/README.md`.
  - A satellite runs no hub-only agent: `SD_HUB_ONLY_AGENTS` in `local-machine-setup/machine-setup.sh` is the one list.
  - A verb that needs a lock or a directory beside the database refuses with `HubOnly` off the hub; do not add a remote path for it.
  - Roll out `repo.satellite_gate = accept` one repository at a time: `local-sd-db/README.md`, "Satellite gate offload".
- `local-project-dashboard` runs the workflow dashboard; `dashboard.sh test` runs its suite, and `preflight`, `install`, `health` manage the server.
- Read `local-project-dashboard/RUNTIME.md` before changing the service.
- `local-*` folders run local services or tools; `network-testing` is iperf3/ping tooling.
- `mezmo-*` folders are helpers for the Mezmo API and its test data; only they may name that product.
  - `tests/test_product_name.py` enforces it in the `make check` preflight; its `ALLOWED` entries carry their reasons.
- `lib/` holds the shared config resolver (`lib/config.sh` for shell, `lib/system_tools_config.py` for Python) and `lib/bounded.sh`, the bounded step for unattended jobs (`st_bounded`, `st_step`).

## Conventions — keep these when adding or changing anything

1. **One entrypoint per runnable folder**, named after the folder minus `local-`/`mezmo-` (`local-redis/redis.sh`).
   - An unprefixed folder keeps its full name (`network-testing/network-testing.sh`).
   - Multi-action tools take subcommands (`start|stop|update` for docker services); do not add `run-X.sh`/`stop-X.sh`.
   - No-arg invocation prints usage to stderr and exits 1.
   - Every script answers `-h|--help|help` with its subcommands and options, and exits 0.
2. **POSIX sh** (`#!/bin/sh`, `set -e`); resolve the folder with `DIR="$(cd "$(dirname "$0")" && pwd)"`, never a hardcoded path.
   - A script that `local-bin-links` links must walk `$0` to its real path first (`while [ -L "$SELF" ]` loop, with a comment); through the symlink, sibling files are missing.
3. **Secrets and personal values never go into tracked files.** They live in the config folder, outside the checkout.
   - The root is `SYSTEM_TOOLS_CONFIG`, default `${XDG_CONFIG_HOME:-$HOME/.config}/system`.
   - Each tool reads `<root>/<tool>/`; `<tool>` is the folder minus `local-`, and a `mezmo-*` folder keeps its full name.
   - Resolve it with `lib/config.sh` (`st_config_dir`, `st_source_env`, `st_missing`) or `lib/system_tools_config.py`; do not repeat the rule.
   - Commit `<folder>/.env.example` or `<file>.example` with `change-me` values.
   - Source `.env` only if it exists, then check the needed variables; a missing `.env` is fine when the values are exported.
   - On a missing variable, fail with its name and both remedies: export it, or copy the `.example` to the config path.
   - Keep the root `.gitignore` lines for in-folder config files as a safety net.
   - Cron jobs are config too: `local-cron-jobs` reads `<root>/cron-jobs/jobs/`; `local-cron-jobs/examples/` is a catalogue only.
   - A job for one machine goes in `<root>/cron-jobs/jobs/<host>/`, `<host>` being the lower-cased `hostname -s`.
4. **Data and log folders** (`storage/`, `volumes/`, `logs/`, `falkordb_data/`, `mcp_logs/`) are gitignored contents-only, kept by `.gitkeep`; use the same pattern for new stateful services.
5. Docker services use `--rm` and named containers; `update` removes images by IMAGE ID (`awk '{print $3}'` on `docker images`; `$2` is the tag).
6. **A `status` subcommand answers with an exit code**: `0` healthy, `3` nothing to check (not configured or not running), `1` up and broken.
   - `local-health-check` runs `status` only for tools that declare it, and reports only `1`, so unconfigured machines stay silent.
   - Declare it with one sentence in the tool's `help`, next to its `status` line: "local-health-check reads these codes".
   - The sweep probes each folder's entrypoint `help` output, bounded by `HELP_BOUND`; there is no tool list to edit.
   - Do not list the declaring tools in prose; lists go stale.
7. **launchd labels** use the prefix in `SYSTEM_TOOLS_LABEL_PREFIX` (default `local.system-tools`); never hardcode a personal prefix.

Deliberate deviations: `local-scan-for-secrets` scans the cwd on no-arg; `local-cswap` and `tests/ci-native.sh` are bash; `local-gito` sources `~/.gito/.env`; `local-cron-jobs` uses `local`; `local-genai-traces` uses compose with `restart: unless-stopped`, not `--rm`, to stay always on.

## Planned work lives in `docs/work/`

- A change with a shape worth agreeing on first gets a folder under `docs/work/` with `prd.md`, `design.md` and `implement.md`.
- Check with the command pack's `sd-docs-lint` from the repository root, with no `--work-dir`; an absolute value reads zero references and passes silently.
- Never add `.github/sd-docs-lint.json`; that opt-in sends `docs/work` prose to a third party.

## Nothing may depend on Jev

`local-jev` puts TypeSafe's Jev model on `PATH`. It is experimental, needs an API key, and calls a hosted service.

- Every caller keeps its old mechanism; a machine runs the same without Jev as with it.
- Use one of two shapes; a caller that uses neither is wrong:

      if jev enabled JEV_MY_STAGE; then verdict=$(jev noul ...); else verdict=$(old_way); fi
      verdict=$(jev choice ... --fallback "$(old_way)")

- Use Jev for ordering, routing and triage only; it does not approve, merge, send or delete.
- Pipe nothing sensitive into Jev; every call leaves the machine.
- `tests/test_jev_contract.py` enforces callers and shapes; switch, exit codes and test pins: `.claude/rules/jev.md`.

## Area rules

These files load when you touch matching paths.

| Topic | File |
| --- | --- |
| Services, ports, launchd plists, wrappers | `.claude/rules/services.md` |
| `local-machine-setup` stages that remediate | `.claude/rules/machine-setup.md` |
| `local-bin-links` and the research kit | `.claude/rules/bin-links.md` |
| macOS TCC under launchd | `.claude/rules/macos-tcc.md` |
| The 02:xx cron slot needs a scheduled wake | `.claude/rules/cron-wake.md` |
| Jev switch, exit codes, test pins | `.claude/rules/jev.md` |

# system

Small scripts, wrappers, and mini apps that run local infrastructure on a
macOS workstation. Each top-level folder is one self-contained tool with its
own README. There is no repository-wide build.

## Layout

| Prefix | Meaning |
|---|---|
| `local-*` | Run a service or tool locally (mostly docker or launchd) |
| `mezmo-*` | Helpers and test data for the Mezmo pipeline API |
| `network-testing` | iperf3/ping testing between machines on a LAN |
| `lib` | The shared config resolver the tools source |
| `docs` | Design documents and planned work (`docs/work/`) |

The larger pieces are `local-sd-db` (a Python package holding the workflow
database), `local-project-dashboard` (the dashboard over it) and
`local-cron-jobs` (launchd-scheduled jobs). They pair
with the public command pack
[`platypeeps/sd-ai-command-pack`](https://github.com/platypeeps/sd-ai-command-pack).

## Conventions

- One entrypoint per folder, named after the folder minus `local-`/`mezmo-`:
  `local-redis/redis.sh`. Multi-action tools use subcommands
  (`start|stop|update`, etc.); run without arguments for usage (exit 1);
  `-h|--help|help` answers and exits 0.
- Secrets and personal values never live in tracked files. They live in a
  config folder outside the checkout (see below); each folder commits a
  `.env.example` (or `<name>.conf.example`) with `change-me` values.
- Data and log directories (`storage/`, `volumes/`, `logs/`, …) are gitignored
  but kept in the tree via `.gitkeep`.
- Docker `update` subcommands delete images by IMAGE ID (`awk '{print $3}'`),
  with the grep anchored to the real image name.
- launchd labels share one prefix, read from `SYSTEM_TOOLS_LABEL_PREFIX`
  (default `local.system-tools`).

## Getting started

```sh
git clone https://github.com/platypeeps/system ~/repos/system
cd ~/repos/system
sh local-bin-links/bin-links.sh help      # put the tools on PATH
sh local-redis/redis.sh help              # every entrypoint answers help
```

## Configuration

Private and per-machine values live in one folder outside the checkout:

```sh
SYSTEM_TOOLS_CONFIG="${SYSTEM_TOOLS_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/system}"
```

Each tool reads `$SYSTEM_TOOLS_CONFIG/<tool>/`, where `<tool>` is the folder
name minus `local-` (a `mezmo-*` folder keeps its full name). For example:

```sh
mkdir -p ~/.config/system/notify
cp local-notify/.env.example ~/.config/system/notify/.env   # then fill it in
```

A tool that misses a value names the variable and both remedies: export it, or
copy the `.example` file to the config path it prints.

Scheduled jobs are configuration too. `local-cron-jobs` installs the jobs in
`$SYSTEM_TOOLS_CONFIG/cron-jobs/jobs/`; `local-cron-jobs/examples/` holds
examples to copy from, and none is installed by default. A job for one machine
goes in `$SYSTEM_TOOLS_CONFIG/cron-jobs/jobs/<host>/`, where `<host>` is the
lower-cased `hostname -s`; other hosts' folders are ignored, so one config
folder can serve several machines.

Before pushing, install the leak guard once per clone:
`sh local-leak-guard/leak-guard.sh install`. It refuses a push that adds a line
matching a pattern in `$SYSTEM_TOOLS_CONFIG/privacy-patterns`.

## Ports

No two `local-*` services collide; each can move with a variable listed by its
script's `-h`.

| Port | Kept by | Moved |
| --- | --- | --- |
| `6379` | local-redis | falkordb `6380`, graphiti's bundled falkordb `6381` |
| `3003` | local-falkordb browser UI | graphiti UI `3004` |
| `4317/4318` (OTLP) | local-opentelemetry-collector | jaeger `4327/4328` |
| `8001` | local-redis RedisInsight | clickhouse MCP `8002` |
| `6006` | local-phoenix | local-genai-traces OTLP `4337/4338`, Phoenix UI `6016`, health `13137` |

Other fixed ports:

- `8080-8083`: jaeger's HotROD demo.
- `8084`: local-llama-cpp (override with `PORT`); graphiti MCP HTTP is on 8085.
- `8009`: local-kev on loopback, Kev's documented port (override with `KEV_PORT`).
- `8766`: local-task-actions (override with `TASK_ACTIONS_PORT`).
- `8767`: the workflow dashboard in `local-project-dashboard` (override with
  `dashboard.sh serve --port`).
- `5434`: local-postgres (override with `POSTGRES_HOST_PORT`).
- `6337`: local-qdrant (override with `QDRANT_PORT`).

## Tests

`make check` runs every suite, and merges gate on it through the local gate
(`repo.ci=local`): `sd-ship merge` runs it via `sd-check` in a clean worktree of
the pull request's head and posts `sd/local-gate`, the one required check. It
needs `python3.14` on `PATH` and fetches the command pack at its pinned SHA
into `.ci/`.

`tests/ci-native.sh` holds the preflight and the `run_suite` lines, which are
the list of suites; the preflight fails on a `*/tests/test_*.py` folder that no
line names. No GitHub workflow runs the suites. The pack pin is one full commit
sha in `.sd-pack-rev`; bump it deliberately.

Some tests need macOS: the Swift build and dyld shim in `local-msgsnap`.
Linux cannot run them, and a skipped test fails the check, so they live in
separate suites named in `tests/macos-only-suites.txt`. `make check` runs them
on a Mac; to run only them:

    tests/run-macos-only.sh all

## License

MIT; see `LICENSE`.

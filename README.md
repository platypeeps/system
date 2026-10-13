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

The larger pieces are `local-project-dashboard` (the dashboard over the
workflow database) and `local-cron-jobs` (launchd-scheduled jobs). They pair
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
- Docker `update` subcommands delete images by ID, listed with
  `docker images -aq --filter "reference=<repo>"`.
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

machine-setup's `repos` stage installs the leak guard's pre-push hook in every
public managed clone; in any other clone run
`sh local-leak-guard/leak-guard.sh install`. It refuses a push that adds a line
matching a pattern in `$SYSTEM_TOOLS_CONFIG/privacy-patterns`.

## Ports

No two tools bind the same port, and `tests/test_ports.py` (part of the
`make check` preflight) fails when two do or when this table differs from what
the scripts and compose files bind. Each port moves with a variable listed by
its script's `-h`. Keep one row per folder, its ports as backticked numbers in
the second column; the third column is free text.

| Folder | Ports | What |
| --- | --- | --- |
| local-clickhouse | `8123`, `9000`, `8002` | ClickHouse HTTP, native, MCP server |
| local-codebase-memory-mcp | `9749` | daemon UI |
| local-deploy | `8769` | sd-serve, which the upgrade checks (the pack runs it) |
| local-falkordb | `6380`, `3003` | FalkorDB, browser UI |
| local-genai-traces | `4337`, `4338`, `6016`, `13137` | OTLP gRPC, OTLP HTTP, Phoenix UI, health |
| local-graphiti-mcp | `6381`, `3004`, `8085` | bundled FalkorDB, its UI, MCP HTTP |
| local-kev | `8009` | Kev's documented port, on loopback |
| local-llama-cpp | `8084`, `50052` | server (override with `PORT`), RPC |
| local-machine-setup | `8443` | Tailscale HTTPS front for the dashboard |
| local-milvus | `19530`, `9091`, `2379`, `8000` | gRPC, metrics, etcd, Attu UI |
| local-mock-mcp | `9992` | mock MCP server |
| local-opentelemetry-collector | `4317`, `4318`, `55679` | OTLP gRPC, OTLP HTTP, zPages |
| local-opentelemetry-demo | `8080`, `18080`, `18081` | demo frontend, Kubernetes and Prometheus MCP forwards |
| local-postgres | `5434` | PostgreSQL (`POSTGRES_HOST_PORT`) |
| local-project-dashboard | `8767` | the workflow dashboard (`dashboard.sh serve --port`) |
| local-redis | `6379`, `8001` | Redis Stack, RedisInsight |
| local-task-actions | `8766` | signed task actions (`TASK_ACTIONS_PORT`) |
| mezmo-aura | `3033`, `3101`, `3102` | Aura, experiment orchestrator, single-agent |

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

To run one suite as the gate does, name it by its `run_suite` or
`tests/macos-only-suites.txt` name: `make test SUITE=repo-sync`. It runs the
preflight, then that suite, against the pinned pack in `.ci/pack`, not the
live pack checkout.

Some tests need macOS: the Swift build and dyld shim in `local-msgsnap`.
Linux cannot run them, and a skipped test fails the check, so they live in
separate suites named in `tests/macos-only-suites.txt`. `make check` runs them
on a Mac; to run only them, against the pack `make check` or `make test`
fetched into `.ci/pack`:

    tests/run-macos-only.sh all

## License

MIT; see `LICENSE`.

# mezmo-aura

Run and poke Mezmo aura locally: web server on `:3033` (`aura webserver`,
installed or run from a source checkout), CLI, and quick curl smoke tests.
`config.toml` is the default server config. It references API keys via
`{{ env.* }}` templates only, no literals.

## Usage

```sh
./aura.sh install            # build aura from $AURA_REPO and link ~/.local/bin/aura
./aura.sh server             # aura webserver with config.toml
./aura.sh server-repo        # cargo run from $AURA_REPO (tees aura-output.txt)
./aura.sh cli [args]         # aura CLI against the local server
./aura.sh health             # GET /health
./aura.sh models             # GET /v1/models
./aura.sh prompt "text"      # POST a chat completion
./aura.sh image              # build aura-local:instrumented from $AURA_REPO
./aura.sh experiment start [--overlay servers.toml] [--target dev]   # math MCP + two Aura servers from that image
./aura.sh experiment run [--prompts prompts.txt]      # the six math requests, or your prompts
./aura.sh experiment inspect [summary|tree]
./aura.sh experiment stop
```

## Install

`install` builds `aura` from the `$AURA_REPO` checkout, not from Homebrew:
the `mezmo/tap` formula lags the nightlies.
It runs `cargo build --release --bin aura` and copies the binary to
`~/.local/opt/aura/<version>/`, where `<version>` is `git describe --tags`
(with `-dirty` for uncommitted changes).
A `SOURCE` file there names the version and the full commit.
`~/.local/bin/aura` then links to that copy; older versions stay, so the
link can go back.
No separate server binary is installed: AURA deprecated `aura-web-server`
in `v0.2.18-nightly.16`, and `aura webserver` takes the same flags and
environment variables.

## Traces

Every Aura run here exports its traces to `local-genai-traces`
(`127.0.0.1:4337`, Phoenix on `:6016`).
`server` and `server-repo` set `OTEL_EXPORTER_OTLP_ENDPOINT` and
`OTEL_SERVICE_NAME=aura` unless they are already exported.
`AURA_TRACES=0` turns that default off for one run.
They do not record prompts and answers: a server may carry real work, and
the collector may forward it. Export `OTEL_RECORD_CONTENT=true` to record.
The experiment records them, since its requests are test prompts.
That includes every line of a `--prompts` file, so keep real data out of it.

The experiment runs only `$AURA_IMAGE` (default `aura-local:instrumented`),
a local build of the `$AURA_REPO` checkout, never the published image.
`image` builds it and labels it `aura.source=<branch>@<commit>`, with
`+dirty` when tracked files have changes. `experiment start` prints that
label, and refuses an image without it: the tag alone may name any image.
To change what the experiment measures, change the checkout and rebuild.

`experiment start` refuses to run without the image or without a healthy
`local-genai-traces`, so no experiment runs untraced. It writes its two
server configs into `experiment/state/` from the checkout's
`examples/quickstart-orchestration-math/config.toml`: the model comes from
`LLM_*`, and the single-agent copy turns orchestration off. The servers
listen on `127.0.0.1:3101` (orchestration) and `:3102` (single agent);
`AURA_ORCH_PORT` and `AURA_SINGLE_PORT` move them. Answers go to
`experiment/state/responses.jsonl`. `experiment run` sends every request,
then exits 1 when any one failed or came back without an answer. Aura
answers a model provider's failure with HTTP 200 and the error as the
reply, so such a reply counts as failed too: stderr names the case and the
error line. The servers run with `AURA_DEBUG_PROVIDER_ERRORS=true`, so that
line carries the provider's own words, such as "No endpoints found".

To point the experiment at other MCP servers, such as the Mezmo dev
endpoint, give `experiment start --overlay <file.toml>`. Each
`[mcp.servers.<name>]` table in the file replaces the example's server of
that name, sub-tables included, or adds a new one; both configs get it.
Worker tables may join them (below). The file may hold nothing else, and
start refuses an overlay it cannot merge cleanly. Write the key as a
template, never as a value:

```toml
[mcp.servers.mezmo-dev]
transport = "http_streamable"
url = "https://mcp.example.test/mcp"

[mcp.servers.mezmo-dev.headers]
Authorization = "Bearer {{ env.MEZMO_SERVICE_KEY }}"
```

The orchestration server hands tools to its workers, and each
`[orchestration.worker.<name>]` table's `mcp_filter` decides which tools that
worker sees. An overlay server alone therefore reaches the single agent but
not the workers. The overlay may also hold worker tables, which replace the
example's worker of that name, sub-tables included, or add one:

```toml
[orchestration.worker.traces]
description = "Reads traces through the dev MCP server"
turn_depth = 5
mcp_filter = ["list_failed_traces", "get_trace_spans"]
preamble = "You read traces. Report what the tools return."
```

Compose passes `MEZMO_SERVICE_KEY` into both servers by name, so its value
stays in the environment; start refuses an overlay that holds the value.
Without `--overlay`, the configs are the math example as before.

The experiment picks its own Mezmo target and ignores the shell's
`MEZMO_TARGET`, whose default may be production. The target is
`--target <name>` on `start` or `run`, else `AURA_MEZMO_TARGET` from `.env`,
else `dev`. Its values come from `AURA_MEZMO_<NAME>_SERVICE_KEY` and
`AURA_MEZMO_<NAME>_MCP_URL`, the name upper-cased with `-` as `_`. The
experiment drops every exported `MEZMO_*` value, then passes only the
target's values to the servers, as `MEZMO_TARGET`, `MEZMO_SERVICE_KEY` and
`MEZMO_MCP_URL`. An overlay may read the URL as `{{ env.MEZMO_MCP_URL }}`.
Start refuses an overlay that reads a value the target lacks, and a target
name holding "prod" unless `--allow-prod` is given. Start records the target
in `experiment/state/target`. `run` refuses another target or none, and each
answer in `responses.jsonl` names the target.

`experiment run --prompts <file>` sends each line of the file to both
servers, as cases `single-p<n>` and `orch-p<n>`; blank lines and lines
starting with `#` are skipped, and a file with no prompt is refused.
Without `--prompts`, it sends the six math requests.

## Configuration

Private values live outside the checkout, in `<config>/mezmo-aura/`.
`<config>` is `$SYSTEM_TOOLS_CONFIG`, default `~/.config/system`.

| File | Holds |
|---|---|
| `.env` | `OPENAI_API_KEY`, `MEZMO_SERVICE_KEY`, `AURA_REPO`; `LLM_PROVIDER`, `LLM_MODEL`, `LLM_API_KEY`, `AURA_MEZMO_TARGET` and each target's `AURA_MEZMO_<NAME>_SERVICE_KEY` and `AURA_MEZMO_<NAME>_MCP_URL` for the experiment. Copy `.env.example`. |
| `config.toml` | Optional. Replaces `config.toml` beside the script. |

Values already exported win over `.env`.

This folder was `local-aura` until sd:2539, and it read `<config>/aura/`.
Convention 3 keeps a `mezmo-*` folder's full name, so the config folder moved too.
With only `<config>/aura/` present, every verb except `help` and `test` stops.
The message names both paths and the `mv` that moves the folder; the script moves nothing.
With both folders present, the script reads the new one and names the old one on stderr.
The experiment's compose project is now `mezmo-aura-experiment`.
Stop an experiment started before the rename with `docker compose -p local-aura-experiment down`.
`server` and `server-repo` stop and name the missing variable when a key is unset.

## Gotchas

- `PORT`/`HOST` env vars override the defaults.
- `CONFIG_PATH` names a server config for one run.
- The experiment containers use port 3030 inside: the image's own health
  check probes it, and another port leaves the containers unhealthy.
- `experiment run` spends model tokens: six requests cost about $0.27 with
  `anthropic/claude-sonnet-4` on OpenRouter on 2026-10-01.

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
./aura.sh experiment start   # math MCP + two Aura servers from that image
./aura.sh experiment run     # send the fixed scenario requests
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
The experiment records them, since its requests are fixed test prompts.

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
then exits 1 when any one failed or came back without an answer.

## Configuration

Private values live outside the checkout, in `<config>/mezmo-aura/`.
`<config>` is `$SYSTEM_TOOLS_CONFIG`, default `~/.config/system`.

| File | Holds |
|---|---|
| `.env` | `OPENAI_API_KEY`, `MEZMO_API_KEY`, `AURA_REPO`; `LLM_PROVIDER`, `LLM_MODEL`, `LLM_API_KEY` for the experiment. Copy `.env.example`. |
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

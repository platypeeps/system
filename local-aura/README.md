# local-aura

Run and poke Mezmo aura locally: web server on `:3033` (brew install or a
source checkout), CLI, and quick curl smoke tests.
`config.toml` is the default server config. It references API keys via
`{{ env.* }}` templates only, no literals.

## Usage

```sh
./aura.sh install            # brew install aura + aura-web-server
./aura.sh server             # brew binary with config.toml
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

## Traces

Every Aura run here exports its traces to `local-genai-traces`
(`127.0.0.1:4337`, Phoenix on `:6016`), with content recorded.
`server` and `server-repo` set `OTEL_EXPORTER_OTLP_ENDPOINT`,
`OTEL_RECORD_CONTENT=true` and `OTEL_SERVICE_NAME=aura` unless they are
already exported. `AURA_TRACES=0` turns that default off for one run.

The experiment runs only `$AURA_IMAGE` (default `aura-local:instrumented`),
a local build of the `$AURA_REPO` checkout, never the published image.
`image` builds it and labels it `aura.source=<branch>@<commit>`, with
`+dirty` when tracked files have changes; `experiment start` prints that label.
To change what the experiment measures, change the checkout and rebuild.

`experiment start` refuses to run without the image or without a healthy
`local-genai-traces`, so no experiment runs untraced. It writes its two
server configs into `experiment/state/` from the checkout's
`examples/quickstart-orchestration-math/config.toml`: the model comes from
`LLM_*`, and the single-agent copy turns orchestration off. The servers
listen on `127.0.0.1:3101` (orchestration) and `:3102` (single agent);
`AURA_ORCH_PORT` and `AURA_SINGLE_PORT` move them. Answers go to
`experiment/state/responses.jsonl`.

## Configuration

Private values live outside the checkout, in `<config>/aura/`.
`<config>` is `$SYSTEM_TOOLS_CONFIG`, default `~/.config/system`.

| File | Holds |
|---|---|
| `.env` | `OPENAI_API_KEY`, `MEZMO_API_KEY`, `AURA_REPO`; `LLM_PROVIDER`, `LLM_MODEL`, `LLM_API_KEY` for the experiment. Copy `.env.example`. |
| `config.toml` | Optional. Replaces `config.toml` beside the script. |

Values already exported win over `.env`.
`server` and `server-repo` stop and name the missing variable when a key is unset.

## Gotchas

- `PORT`/`HOST` env vars override the defaults.
- `CONFIG_PATH` names a server config for one run.
- The experiment containers use port 3030 inside: the image's own health
  check probes it, and another port leaves the containers unhealthy.
- `experiment run` spends model tokens: six requests cost about $0.27 with
  `anthropic/claude-sonnet-4` on OpenRouter on 2026-10-01.

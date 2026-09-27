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
```

## Configuration

Private values live outside the checkout, in `<config>/aura/`.
`<config>` is `$SYSTEM_TOOLS_CONFIG`, default `~/.config/system`.

| File | Holds |
|---|---|
| `.env` | `OPENAI_API_KEY`, `MEZMO_API_KEY`, `AURA_REPO`. Copy `.env.example`. |
| `config.toml` | Optional. Replaces `config.toml` beside the script. |

Values already exported win over `.env`.
`server` and `server-repo` stop and name the missing variable when a key is unset.

## Gotchas

- `PORT`/`HOST` env vars override the defaults.
- `CONFIG_PATH` names a server config for one run.

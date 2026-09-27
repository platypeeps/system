# local-gito

Wrapper around `uvx gito.bot` — an LLM code reviewer. Replaces `~/bin/common/gito`.

## Usage

```sh
./gito.sh review        # review the working tree
./gito.sh cr            # shortcut for review
./gito.sh setup         # gito's interactive credential setup
gito review             # symlink in ~/bin/common
```

Anything the wrapper does not recognize is passed straight to `uvx gito.bot`,
so `gito.sh review --help` reaches gito's own help. Bare `gito.sh -h` describes
the wrapper.

## Configuration

Sources `~/.gito/.env` when present (override with `GITO_ENV_FILE`), then fills
in defaults: `LLM_API_TYPE=openai`, `MODEL=gpt-5.5`, and the OpenAI v1 endpoint
as `LLM_API_BASE` for openai-type providers. `OPENAI_API_KEY` is accepted as a
fallback for `LLM_API_KEY`.

If no key is found the wrapper warns and continues anyway, since some provider
configs authenticate elsewhere. `setup` and `version` never warn.

## Gotchas

- Bare `gito` used to default to `review`. It now prints usage and exits 1,
  per the repo convention — type `gito review` or `gito cr`.
- `~/.gito/.env` lives outside this repo, but it no longer holds a literal
  key: every value is a `${VAR}` reference into `~/.config/shell/env.sh`. Keep
  it mode 0600.
- **Braces are required** in that file. gito re-reads it itself with
  `load_dotenv(override=True)`, overriding what this wrapper exported, and
  python-dotenv interpolates `${VAR}` but leaves a bare `$VAR` literal — which
  reached the API verbatim and returned 401.
- The active provider is Moonshot/Kimi (`kimi-k2.7-code`); MiniMax is
  commented out because it inlines `<think>` blocks into `message.content`.

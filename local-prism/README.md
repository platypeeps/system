# local-prism

Wrapper around the `prism` binary in `~/repos/ai/prism` — an LLM code reviewer
with deterministic exit codes. Replaces `~/bin/common/prism`.

## Usage

```sh
./prism.sh review       # review code changes
./prism.sh github <pr>  # review a GitHub pull request
./prism.sh models       # provider and model management
prism review            # symlink in ~/bin/common
```

Everything is passed through to the binary, so `prism.sh review --help` reaches
prism's own help. Bare `prism.sh -h` describes the wrapper; `prism.sh help review`
still passes through.

## Configuration

Exports `ANTHROPIC_API_KEY` from `CUSTOM_ANTHROPIC_API_KEY` when that is set,
then sources `~/.prism/.env` (override with `PRISM_ENV_FILE`).

## Gotchas

- This repo holds only the wrapper. The binary lives in `~/repos/ai/prism`; if
  that checkout is gone, delete this folder.
- The old script exported `ANTHROPIC_API_KEY=$CUSTOM_ANTHROPIC_API_KEY`
  unconditionally, so an unset `CUSTOM_ANTHROPIC_API_KEY` overwrote a working
  key with an empty string. The copy is now guarded on a non-empty value.
- `~/.prism/.env` lives outside this repo, but it no longer holds a literal
  key: every value is a `${VAR}` reference into `~/.config/shell/env.sh`. That
  keeps
  one copy of each credential, lets `scan-for-secrets refresh` skip the file
  instead of overwriting it, and makes the file safe enough to copy between
  machines.
- The active provider is Moonshot/Kimi (`kimi-k2.7-code`). MiniMax is
  commented out and must stay that way: it returns reasoning inline in
  `message.content` as `<think>...</think>`, so prism reads thinking text as
  the answer. Moonshot puts it in a separate `reasoning_content` field.

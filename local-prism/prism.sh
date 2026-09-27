#!/bin/sh
# Wrapper around the prism binary in ~/repos/ai/prism that loads local config.
# Usage: prism.sh <prism-command> [args...]
set -e

PRISM_BIN="${PRISM_BIN:-$HOME/repos/ai/prism/prism}"
PRISM_ENV_FILE="${PRISM_ENV_FILE:-$HOME/.prism/.env}"

usage() {
  cat <<'HELPEOF'
usage: prism.sh <prism-command> [args...]

  review [args...]  review code changes
  github [args...]  review a GitHub pull request
  models | config | cache | hook | version
  <anything else>   passed straight through to the prism binary

For a prism command's own options, ask it directly: prism.sh review --help

environment:
  PRISM_BIN                 prism binary (default ~/repos/ai/prism/prism)
  PRISM_ENV_FILE            env file sourced when present (default ~/.prism/.env)
  CUSTOM_ANTHROPIC_API_KEY  when set, exported as ANTHROPIC_API_KEY
HELPEOF
}

# `-h`/`--help`/`help` alone describe this wrapper; with further arguments they
# belong to prism (e.g. `prism.sh help review`), so they pass through.
if [ "$#" -eq 1 ]; then
  case "$1" in
    -h|--help|help)
      usage
      exit 0
      ;;
  esac
fi

if [ "$#" -eq 0 ]; then
  echo "usage: $(basename "$0") <prism-command> [args...]" >&2
  exit 1
fi

if [ ! -x "$PRISM_BIN" ]; then
  echo "prism.sh: $PRISM_BIN not found or not executable (set PRISM_BIN)" >&2
  exit 1
fi

# Only override ANTHROPIC_API_KEY when there is actually a value to copy —
# exporting it unconditionally replaced a working key with an empty string.
if [ -n "${CUSTOM_ANTHROPIC_API_KEY:-}" ]; then
  ANTHROPIC_API_KEY="$CUSTOM_ANTHROPIC_API_KEY"
  export ANTHROPIC_API_KEY
fi

if [ -f "$PRISM_ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$PRISM_ENV_FILE"
  set +a
fi

exec "$PRISM_BIN" "$@"

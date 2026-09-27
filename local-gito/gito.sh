#!/bin/sh
# Thin wrapper around `uvx gito.bot <command>` that loads local LLM config.
# Usage: gito.sh <gito-command> [args...]
set -e

UVX_BIN="${UVX_BIN:-uvx}"
GITO_UVX_PACKAGE="${GITO_UVX_PACKAGE:-gito.bot}"
GITO_ENV_FILE="${GITO_ENV_FILE:-$HOME/.gito/.env}"

usage() {
  cat <<'HELPEOF'
usage: gito.sh <gito-command> [args...]

  cr [args...]     shortcut for `gito.sh review [args...]`
  review [args...] review the working tree (gito's own main command)
  setup            run gito's interactive credential setup
  <anything else>  passed straight through to `uvx gito.bot`

For a gito command's own options, ask it directly: gito.sh review --help

environment:
  GITO_ENV_FILE       env file sourced when present (default ~/.gito/.env)
  UVX_BIN             uvx binary (default: uvx from PATH)
  GITO_UVX_PACKAGE    package uvx runs (default: gito.bot)
  LLM_API_TYPE        default: openai
  LLM_API_BASE        defaulted to the OpenAI v1 endpoint for openai types
  MODEL               default: gpt-5.5
  LLM_API_KEY         falls back to OPENAI_API_KEY when unset
HELPEOF
}

# `-h`/`--help`/`help` alone describe this wrapper; with further arguments they
# belong to gito, so they fall through to the pass-through path below.
if [ "$#" -eq 1 ]; then
  case "$1" in
    -h|--help|help)
      usage
      exit 0
      ;;
  esac
fi

if [ "$#" -eq 0 ]; then
  echo "usage: $(basename "$0") <gito-command> [args...]" >&2
  exit 1
fi

if ! command -v "$UVX_BIN" >/dev/null 2>&1; then
  echo "gito.sh: $UVX_BIN not found. Install uv first." >&2
  exit 1
fi

# Gito's docs use ~/.gito/.env for LLM_API_TYPE, LLM_API_KEY, MODEL, etc.
if [ -f "$GITO_ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$GITO_ENV_FILE"
  set +a
fi

# Convenience: let the normal OpenAI variable satisfy Gito's LLM_API_KEY.
if [ -z "${LLM_API_KEY:-}" ] && [ -n "${OPENAI_API_KEY:-}" ]; then
  LLM_API_KEY="$OPENAI_API_KEY"
  export LLM_API_KEY
fi

: "${LLM_API_TYPE:=openai}"
if [ -z "${LLM_API_BASE:-}" ]; then
  case "${LLM_API_TYPE}:${LLM_API_PLATFORM:-}" in
    openai:|openai:openai|open_ai:|open_ai:openai)
      LLM_API_BASE="https://api.openai.com/v1/"
      ;;
  esac
fi
: "${MODEL:=gpt-5.5}"

export LLM_API_TYPE
if [ -n "${LLM_API_BASE:-}" ]; then
  export LLM_API_BASE
fi
export MODEL

# Tiny shortcut: `gito.sh cr` == `gito.sh review`.
if [ "$1" = "cr" ]; then
  shift
  set -- review "$@"
fi

# Commands that never reach an LLM should not nag about credentials.
case "$1" in
  setup|version|--help|-h|help) ;;
  *)
    if [ -z "${LLM_API_KEY:-}" ]; then
      cat >&2 <<'MSG'
gito.sh: LLM_API_KEY is not set.

Fix one of these:
  1. Run: gito.sh setup
  2. Export OPENAI_API_KEY or LLM_API_KEY
  3. Put LLM_API_KEY=... in ~/.gito/.env

Continuing anyway in case your provider config handles auth elsewhere.
MSG
    fi
    ;;
esac

exec "$UVX_BIN" "$GITO_UVX_PACKAGE" "$@"

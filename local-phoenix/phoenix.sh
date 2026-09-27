#!/bin/sh
# Arize Phoenix LLM-observability server (UI on :6006) from the local .venv.
# Usage: phoenix.sh start|demo|update
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

# Activate only for subcommands that need python — help must work (and exit 0)
# on a fresh clone where the gitignored .venv does not exist yet.
need_venv() {
  if [ ! -f .venv/bin/activate ]; then
    echo "phoenix.sh: no .venv — run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
    exit 1
  fi
  . .venv/bin/activate
}

case "$1" in
  start)
    need_venv
    phoenix serve
    ;;
  update)
    need_venv
    # Upgrade phoenix + direct deps, pull in security fixes, refreeze the pin file.
    pip install --upgrade arize-phoenix openai
    pip check
    pip freeze > requirements.txt
    echo "updated and refroze requirements.txt; verify with: $(basename "$0") start"
    ;;
  demo)
    # Sends one instrumented OpenAI chat completion (needs OPENAI_API_KEY
    # exported and a running phoenix server).
    need_venv
    python app.py
    ;;
  -h|--help|help)
    cat <<'HELPEOF'
usage: phoenix.sh start|demo|update

  start        serve the Phoenix UI on :6006 from the local .venv
  demo         send one instrumented OpenAI chat completion (needs
               OPENAI_API_KEY exported and a running server)
  update       upgrade arize-phoenix + openai, pip check, refreeze
               requirements.txt
HELPEOF
    exit 0
    ;;
  *)
    echo "usage: $(basename "$0") start|demo|update" >&2
    exit 1
    ;;
esac

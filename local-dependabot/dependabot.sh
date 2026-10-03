#!/bin/sh
# The Dependabot sweep's one entrypoint: the held-bump watcher's mechanical
# pass, and this folder's suite. The daily sweep itself stays prompt-driven
# (`ROUTINE.md`, run by local-cron-jobs); nothing here merges.
# Usage: dependabot.sh holds [--dry-run] [--repo OWNER/NAME]...|test|help
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"

usage() {
    cat >&2 <<'USAGE'
dependabot.sh — the Dependabot sweep's held bumps, watched.

Usage: dependabot.sh <command>

  holds       The watcher's mechanical pass, as the contract in ROUTINE.md
              states it: enumerate the fleet through the daily sweep's two
              gates, find every `sd-hold:` record on a Dependabot pull
              request, evaluate its probes, and on a lift edit the record,
              comment, ask for the rebase, and log it. Prints one line per
              record (HELD, LIFTED, LIFTED-EARLIER, LIFTED (resumed),
              CARRIED, UNKNOWN, MALFORMED, IGNORED) and one per repository
              skipped, naming the gate. Exits 0 unless it cannot run at all.
                --dry-run          read and report; write nothing
                --repo OWNER/NAME  watch this repository instead of the
                                   fleet (repeatable)
  test        Run this folder's tests.
  help        This text.

The daily sweep is not here: it is prompt-driven, and `ROUTINE.md` is its
contract. `holds` is the part of the watcher a test can pin; writing a hold
in the first place is still the agent's judgement.
USAGE
}

# The same interpreter loop as `local-sd-plan/sd-plan.sh`, for the same
# reason: under launchd `python3` is Xcode's 3.9, and `holds.py` is written
# against 3.11 (`str | None` in signatures, `datetime` and dataclass idioms
# the older runtime rejects at import). Pin the provisioned interpreter the
# pack ships, fall back to PATH for CI and machines without the pack, and
# check the version here so a too-old Python is one sentence and not a
# traceback three imports deep. An explicitly set PYTHON is honoured, but
# still checked. Only the commands that run Python pick one: help and usage
# answer on a machine with no 3.11 at all.
python_is_new_enough() {
    command -v "$1" >/dev/null 2>&1 || return 1
    "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null
}

pick_python() {
    if [ -n "${PYTHON:-}" ]; then
        python_is_new_enough "$PYTHON" || {
            echo "dependabot: $PYTHON is too old; holds.py needs Python 3.11 or newer" >&2
            exit 1
        }
    else
        for candidate in \
            "${SD_HOLDS_PYTHON:-}" \
            "$HOME/repos/platypeeps/sd-ai-command-pack/.venv/bin/python" \
            /opt/homebrew/bin/python3 \
            python3
        do
            [ -n "$candidate" ] || continue
            if python_is_new_enough "$candidate"; then
                PYTHON="$candidate"
                break
            fi
        done
        [ -n "${PYTHON:-}" ] || {
            echo "dependabot: no Python 3.11 or newer found; holds.py needs one" >&2
            exit 1
        }
    fi
}

case "${1:-}" in
    holds)
        shift
        pick_python
        exec "$PYTHON" "$DIR/holds.py" "$@"
        ;;
    test)
        shift
        pick_python
        exec "$PYTHON" -m unittest discover -s "$DIR/tests" -t "$DIR" "$@"
        ;;
    -h|--help|help)
        usage
        exit 0
        ;;
    *)
        usage
        exit 1
        ;;
esac

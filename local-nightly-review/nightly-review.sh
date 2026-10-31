#!/bin/sh
# The nightly review job (sd:3638): settle the queue's rulings, ask Sonnet for
# up to five suggestions from the past day, and file them as one review batch
# the operator approves or rejects in the dashboard.
# Usage: nightly-review.sh run|test|help
#
# Design: docs/work/2026-10-12-review-queue/design.md, "Nightly review job".
# The store is the pack's (`sd-db review file|export|apply`); this script
# opens no database. Each step is bounded (lib/bounded.sh), and a step that
# fails stops the run with its exit code: a failed run is unknown, never
# "no suggestions".
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$DIR/.." && pwd)"
. "$ROOT/lib/bounded.sh"

PACK="${SD_PACK_ROOT:-$HOME/repos/platypeeps/sd-ai-command-pack}"
QUEUE=nightly
# Read-only tools only: the model holds no write tool and no `sd` writer.
TOOLS='Read Grep Glob Bash(sd today:*) Bash(sd task show:*) Bash(sd gate status:*) Bash(df:*) Bash(du:*) Bash(ls:*)'

usage() {
  cat <<'USAGE'
nightly-review.sh — the nightly review job: suggestions to approve in the dashboard.

Usage: nightly-review.sh <command>

  run    1. apply: `sd-db review apply nightly` files a task for each approval,
            expires suggestions unruled after 7 days, and closes settled batches;
            `sd-db review priorities` applies priority rulings, closes items
            unranked 30 days, and files one batch of P1-P4 questions.
         2. review: unless today's batch file exists, `claude -p` on Sonnet with
            read-only tools reads the past day and replies with suggestions; the
            reply becomes <bulk root>/system/nightly-review/<date>.json.
         3. file: `sd-db review file` files that batch, at most 5 suggestions,
            dropping a subject rejected in the last 90 days.
         Exits 0 when the batch is filed, already filed, or empty; non-zero
         when any step fails, and then nothing is filed.
  test   Run this folder's tests (extra args go to unittest).
  help   This text.

The bulk root is `sd config get sd.bulk_storage_root`. SD_PACK_ROOT names the
pack (default ~/repos/platypeeps/sd-ai-command-pack); CLAUDE_BIN names the
claude binary (default: claude on PATH).
USAGE
}

run() {
  today=$(date +%F)
  bulk=$("$PACK/bin/sd" config get sd.bulk_storage_root) || {
    echo "nightly-review: sd.bulk_storage_root is not set; set it with: sd config set sd.bulk_storage_root <folder>" >&2
    return 1
  }
  # shellcheck disable=SC2088 # a literal ~ in a value, expanded here
  case "$bulk" in "~"|"~/"*) bulk="$HOME${bulk#"~"}" ;; esac
  [ -d "$bulk" ] || {
    echo "nightly-review: the bulk storage root $bulk is not a folder; is its volume mounted?" >&2
    return 1
  }
  batch="$bulk/system/nightly-review/$today.json"
  mkdir -p "$(dirname "$batch")"

  st_step 120 "$PACK/bin/sd-db" review apply "$QUEUE"
  # The pack's priority sweep (pack sd:6726): rulings, stale closes, questions.
  st_step 120 "$PACK/bin/sd-db" review priorities

  # Once a day: a batch file in place means the model ran, so a rerun only
  # files it. A `.part` is a reply never renamed, and is asked for again.
  if [ -e "$batch" ]; then
    echo "nightly-review: $batch exists; the model does not run again today"
  else
    tmp=$(mktemp -d "${TMPDIR:-/tmp}/nightly-review.XXXXXX")
    trap 'rm -rf "$tmp"' EXIT
    st_step 120 python3 "$DIR/nightly_review.py" rejected "$PACK/bin/sd" "$PACK/bin/sd-db" > "$tmp/rejected"
    {
      cat "$DIR/prompt.md"
      printf '\nDate: %s\nSystem checkout: %s\nBulk storage root: %s\n' "$today" "$ROOT" "$bulk"
      printf '\nSubjects rejected in the last 90 days:\n'
      if [ -s "$tmp/rejected" ]; then cat "$tmp/rejected"; else echo "(none)"; fi
    } > "$tmp/prompt"
    # --allowedTools only adds grants: --restricted drops the inherited settings files and code-running tools,
    # --strict-mcp-config the MCP servers, and --tools names all the model has (sd:3638 review).
    (cd "$ROOT" && st_step 1200 "${CLAUDE_BIN:-claude}" -p "$(cat "$tmp/prompt")" --model sonnet \
      --restricted --strict-mcp-config --tools 'Read,Grep,Glob,Bash' --add-dir "$bulk" "$HOME/.claude/projects" \
      --allowedTools "$TOOLS" --disallowedTools 'Write Edit NotebookEdit' < /dev/null > "$tmp/reply")
    # The raw reply stays beside the batch, so a refused one can be read the next morning (sd:5732).
    cp "$tmp/reply" "${batch%.json}.reply"
    source="system@$(git -C "$ROOT" rev-parse --short=12 HEAD 2>/dev/null || echo unknown)"
    st_step 60 python3 "$DIR/nightly_review.py" batch "$tmp/reply" "$batch" "$today" "$source"
  fi

  count=$(python3 "$DIR/nightly_review.py" check "$batch" "$today")
  if [ "$count" -eq 0 ]; then
    echo "nightly-review: no suggestions today; nothing filed"
    return 0
  fi
  st_step 120 "$PACK/bin/sd-db" review file "$batch"
}

case "${1:-}" in
  run) [ "$#" -eq 1 ] || { usage >&2; exit 1; }; run ;;
  test) shift; exec "${PYTHON:-python3}" -m unittest discover -s "$DIR/tests" -t "$DIR" "$@" ;;
  -h|--help|help) usage ;;
  *) usage >&2; exit 1 ;;
esac

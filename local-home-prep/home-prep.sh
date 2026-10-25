#!/bin/sh
# The home prep job (sd:6029): for each home item due tomorrow, one bounded
# Sonnet run drafts what it can (a Gmail draft, a Drive doc, a checklist) and
# the job writes a `Prepped for <due>:` note with the links. The operator
# sends or does it: the model holds no send or share tool (grill Q54).
# Usage: home-prep.sh run|preview|test|help
#
# Items and notes go through the pack's `sd`; this script opens no database.
# Each step is bounded (lib/bounded.sh), and a step that fails stops the run
# with its exit code: a failed run is unknown, never "nothing due".
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$DIR/.." && pwd)"
. "$ROOT/lib/config.sh"
. "$ROOT/lib/bounded.sh"

PACK="${SD_PACK_ROOT:-$HOME/repos/platypeeps/sd-ai-command-pack}"
SERVER=workspace-mcp
# Read tools, a draft and a private doc only: no send, label, share or permission tool.
TOOLS="Read Grep Glob Bash(sd task show:*)"
for tool in search_gmail_messages get_gmail_message_content get_gmail_thread_content draft_gmail_message \
  search_drive_files get_drive_file_content get_doc_as_markdown create_doc; do
  TOOLS="$TOOLS mcp__${SERVER}__$tool"
done
# Named too, so a settings file that allows them cannot hand them back.
DENIED="Write Edit NotebookEdit mcp__${SERVER}__send_gmail_message mcp__${SERVER}__modify_gmail_message_labels"
DENIED="$DENIED mcp__${SERVER}__manage_drive_access mcp__${SERVER}__set_drive_file_permissions"
DENIED="$DENIED mcp__${SERVER}__get_drive_shareable_link"

usage() {
  cat <<'USAGE'
home-prep.sh — prep tomorrow's home items; the operator acts.

Usage: home-prep.sh <command>

  run      Select the open items due tomorrow with no repo or in a repo named
           in HOME_PREP_REPOS, and not yet prepped for that date. One
           `claude -p` on Sonnet drafts what it can for them: Gmail drafts,
           a private Drive doc, a checklist. It sends nothing. Then one
           `Prepped for <due>:` note per item names the drafts and links.
           Exits 0 when every item is prepped or none is due; 1 when a step
           fails, an item has no prep, or the model was denied a tool.
  preview  List the items `run` would prep; no model, no note.
  test     Run this folder's tests (extra args go to unittest).
  help     This text.

Config: <config>/home-prep/.env (copy local-home-prep/.env.example).
  HOME_PREP_ACCOUNT  the Google account the drafts go in (required for run)
  HOME_PREP_REPOS    space-separated repos whose items count as home, as the
                     database names them (~/repos/<name>); empty: none
SD_PACK_ROOT names the pack (default ~/repos/platypeeps/sd-ai-command-pack);
CLAUDE_BIN names the claude binary; WORKSPACE_MCP_URL the workspace MCP
server (default http://127.0.0.1:8083/mcp).
USAGE
}

select_items() {
  tomorrow=$(python3 "$DIR/home_prep.py" tomorrow)
  st_step 300 python3 "$DIR/home_prep.py" select "$PACK/bin/sd" "$tomorrow" "${HOME_PREP_REPOS:-}" > "$tmp/selected"
  count=$(python3 "$DIR/home_prep.py" show "$tmp/selected" | wc -l | tr -d ' ')
}

run() {
  if [ -z "${HOME_PREP_ACCOUNT:-}" ]; then
    st_missing HOME_PREP_ACCOUNT home-prep .env
    return 1
  fi
  select_items
  if [ "$count" -eq 0 ]; then
    echo "home-prep: nothing to prep for $tomorrow"
    return 0
  fi
  echo "home-prep: $count item(s) due $tomorrow"
  python3 "$DIR/home_prep.py" prompt "$DIR/prompt.md" "$tmp/selected" "$HOME_PREP_ACCOUNT" > "$tmp/prompt"
  mcp=$(printf '{"mcpServers":{"%s":{"type":"http","url":"%s"}}}' "$SERVER" \
    "${WORKSPACE_MCP_URL:-http://127.0.0.1:8083/mcp}")
  dirs=""
  for repo in ${HOME_PREP_REPOS:-}; do
    case "$repo" in "~/"*) repo="$HOME/${repo#"~/"}" ;; esac
    [ -d "$repo" ] && dirs="$dirs $repo"
  done
  # --restricted drops the settings files and code-running tools, --strict-mcp-config
  # every other MCP server, and dontAsk denies what --allowedTools does not name.
  # shellcheck disable=SC2086 # one --add-dir word per repo
  (cd "$tmp" && st_step 1800 "${CLAUDE_BIN:-claude}" -p "$(cat "$tmp/prompt")" --model sonnet \
    --output-format json --max-turns 60 --permission-mode dontAsk \
    --restricted --strict-mcp-config --mcp-config "$mcp" --tools 'Read,Grep,Glob,Bash' \
    ${dirs:+--add-dir $dirs} --allowedTools "$TOOLS" --disallowedTools "$DENIED" \
    < /dev/null > "$tmp/result")
  st_step 300 python3 "$DIR/home_prep.py" apply "$PACK/bin/sd" "$tmp/selected" "$tmp/result" "$tomorrow"
}

preview() {
  select_items
  echo "home-prep: $count item(s) due $tomorrow"
  python3 "$DIR/home_prep.py" show "$tmp/selected"
}

case "${1:-}" in
  run|preview)
    [ "$#" -eq 1 ] || { usage >&2; exit 1; }
    st_source_env home-prep
    tmp=$(mktemp -d "${TMPDIR:-/tmp}/home-prep.XXXXXX")
    trap 'rm -rf "$tmp"' EXIT
    "$1" ;;
  test) shift; exec "${PYTHON:-python3}" -m unittest discover -s "$DIR/tests" -t "$DIR" "$@" ;;
  -h|--help|help) usage ;;
  *) usage >&2; exit 1 ;;
esac

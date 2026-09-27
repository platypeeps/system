#!/bin/sh
# One named persistent herdr session, and the agents in it brought back after
# a restart. herdr 0.9.0 already knows each pane's Claude Code / Codex session
# id (its integrations report it) and resumes them itself when a client
# attaches after a server restart. This wrapper keeps a small state file of
# those ids beside herdr's own, so a session that herdr could not restore --
# stopped and deleted, or a pane closed by hand -- is still resumable from a
# record this repository owns. See README.md for the state file's shape.
# Usage: herdr.sh start|resume|snapshot|record|list|forget|status|test|help
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"

usage() {
  cat <<'HELPEOF'
usage: herdr.sh start|resume|snapshot|record <pane> <agent> <id> [cwd]|list|forget <pane>|status|test|help

  start      attach the one named persistent session (default `sd`,
             HERDR_SESSION overrides), creating it when absent. When the
             session is already running, `resume` runs first; when it is
             not, herdr's own restore resumes the agents as the client
             attaches, and `resume` from inside the session catches any it
             missed.
  resume     for each pane in the state file: skip it when the pane already
             holds that agent session, and leave it alone when it holds
             another agent or command; otherwise start the agent in the pane
             (`claude --resume <id>`, `codex resume <id>`) in the recorded
             directory, creating a workspace at that directory when the pane
             is gone. Exits 1 when any pane could not be resumed.
  snapshot   write every live agent that reports a session id into the
             state file (`herdr agent list`); the thin path, since herdr's
             integrations already know the ids.
  record     what a pane calls as its agent starts, when the snapshot path
             is not enough: record <pane> <claude|codex> <session-id> [cwd];
             cwd defaults to the caller's.
  list       print the state file, one line per pane.
  forget     drop one pane from the state file.
  status     exits 0 when the session runs and every recorded pane holds
             its recorded agent session; 3 when there is nothing to check
             (herdr not installed, or no state file for the session); 1 when
             the state file is present and the session or a pane is gone --
             local-health-check reads those codes.
  test       run the unittest suite (tests/) against a fake `herdr` on
             PATH; extra arguments go to unittest (-v).

env:
  HERDR_SESSION     session name (default sd); also what herdr itself reads
                    to target a named session, so inside that session's
                    panes the wrapper targets it without being told.
  HERDR_STATE_DIR   where <session>.json lives
                    (default $XDG_STATE_HOME/sd-herdr, ~/.local/state/sd-herdr)
  PYTHON            interpreter for the verbs that read JSON (default python3)
HELPEOF
}

case "${1:-}" in
  start|resume|snapshot|record|list|forget|status)
    exec "${PYTHON:-python3}" "$DIR/herdr_wrap.py" "$@"
    ;;
  test)
    shift
    exec "${PYTHON:-python3}" -m unittest discover -s "$DIR/tests" -t "$DIR" "$@"
    ;;
  -h|--help|help)
    usage
    exit 0
    ;;
  *)
    usage >&2
    exit 1
    ;;
esac

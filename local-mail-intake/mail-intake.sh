#!/bin/sh
# Notice what arrives on shared mail aliases and who is waiting on whom.
# Usage: mail-intake.sh fetch|peek|report|stamp|status|test
#
# Exit codes: 0 something to act on, 3 nothing to do, 1 a real error. Note that
# local-cron-jobs treats ANY non-zero rc as a failure, so a job wrapping these
# verbs must map 3 to 0 before returning.
set -e

SELF="$0"
while [ -L "$SELF" ]; do
  link=$(readlink "$SELF")
  case "$link" in
    /*) SELF="$link" ;;
    *)  SELF="$(dirname "$SELF")/$link" ;;
  esac
done
DIR="$(cd "$(dirname "$SELF")" && pwd)"

case "$1" in
  test)
    shift
    exec "${PYTHON:-python3}" -m unittest discover -s "$DIR/tests" -t "$DIR" "$@"
    ;;
  fetch|peek|report|stamp|status)
    exec "${PYTHON:-python3}" "$DIR/mail_intake.py" "$@"
    ;;
  -h|--help|help)
    cat <<'HELPEOF'
usage: mail-intake.sh fetch|peek|report|stamp|status|test

  fetch     search both aliases, group by thread, and APPEND a row for every
            thread whose newest message is one we have not recorded. The first
            run records a baseline and logs nothing.
            exit 0 changed, 3 unchanged, 1 error
  peek      say what a fetch would find, and change nothing
  report    print undelivered threads, those waiting on you first.
            --all includes rows already delivered
  stamp     mark rows delivered, up to --through TIMESTAMP, default now
  status    config, aliases, server reachability, undelivered count
  test      run the unittest suite in tests/

Whose move it is comes from who spoke last and nothing else. No attempt is
made to read the text and judge whether a question was asked; that is the kind
of guess that is wrong quietly.

`report` can optionally ask Jev a separate question — does the newest message
ask its recipients for a decision or an action — and float the threads that do
to the top of the group they are already in. It runs unless JEV_MAIL_INTAKE is
set to 0, off, false, no or disabled — unset means on — and unless `local-jev/jev.sh enabled`
declines. It never changes
waiting_on, never drops a thread and never moves one between groups, and any
failure falls back to today's order, says why on stderr, and leaves the exit
code alone. Only the subject line and the word inbound or outbound leave the
machine: no address, no name, no body, no id.

It reads Gmail from the shell, through the workspace MCP server on loopback,
the same path local-notify already uses to send. So it is a plain command job
and does not depend on a Claude session — thirteen agent-driven cron jobs died
on 2026-09-19 when that session expired.

It never sends, replies, files or labels, and it asks for headers rather than
bodies. Nothing it writes goes in a repository: mail headers carry real names
and addresses, and those stay out of git.

environment:
  MAIL_INTAKE_CONFIG    conf to read (default <config>/mail-intake/mail-intake.conf;
                        <config> is $SYSTEM_TOOLS_CONFIG, default ~/.config/system.
                        Start from mail-intake.conf.example beside this script)
  MAIL_INTAKE_STATE     state directory (default ~/.local/share/mail-intake)
  WORKSPACE_MCP_URL     MCP endpoint (default http://127.0.0.1:8083/mcp)
  JEV_MAIL_INTAKE       0, off, false, no or disabled stops `report` asking Jev what asks
                        for something; unset means it asks
HELPEOF
    exit 0
    ;;
  *)
    echo "usage: $(basename "$0") fetch|peek|report|stamp|status|test" >&2
    exit 1
    ;;
esac

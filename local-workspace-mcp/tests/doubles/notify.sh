#!/bin/sh
# Stub notifier, passed as WORKSPACE_MCP_NOTIFY: journals its arguments and
# exits with $FAKE_DIR/notify_rc (default 0). With $FAKE_DIR/notify_stall it
# stalls like an ntfy push that never returns: a child `sleep` (the curl
# stand-in) whose pid, and this script's, go to notify_pids for the test.
# With $FAKE_DIR/notify_delay it takes that many seconds, then succeeds.
printf 'notify %s\n' "$*" >> "$FAKE_DIR/journal"
[ -e "$FAKE_DIR/notify_delay" ] && /bin/sleep "$(cat "$FAKE_DIR/notify_delay")"
if [ -e "$FAKE_DIR/notify_stall" ]; then
  sleep 30 &
  printf '%s %s\n' "$$" "$!" > "$FAKE_DIR/notify_pids"
  wait
fi
exit "$(cat "$FAKE_DIR/notify_rc" 2>/dev/null || echo 0)"

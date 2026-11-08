#!/bin/sh
# The night triage job (sd:8143): after the day's scheduled jobs, one
# unattended Sonnet run answers "any system, workflow or operational issues on
# this machine?", repairs what it may, files code bugs, and the alerts get
# updated. Usage: night-triage.sh run|test|help
#
# Design: docs/work/2026-11-08-night-triage/design.md. The model holds no
# alert writer: night_triage.py claims this machine's alerts before it runs
# and settles them from its reply. Each step is bounded (lib/bounded.sh); a
# failed step gives every claim back and exits non-zero, so cron's failure
# channel reports it and the alerts stay as they were.
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$DIR/.." && pwd)"
. "$ROOT/lib/bounded.sh"

PACK="${SD_PACK_ROOT:-$HOME/repos/platypeeps/sd-workflow}"
SD="$PACK/bin/sd"
CRON="$ROOT/local-cron-jobs/cron-jobs.sh"
# Repair and file (operator ruling, sd:8143): read, rerun a job, restart a
# service, clear a stale cron log or lock, file an item. No merge, push,
# sudo, scheduled-job or permission edit, mail, Slack or credential tool, and
# no alert writer: `sd task status` and `sd reports acknowledge` are this
# script's. tests/test_night_triage.py pins the list.
# ponytail: `rm -f` is held to the cron logs folder by prefix only; a wrapper
# that proves a file stale goes in if a run ever removes the wrong one.
TOOLS="Read Grep Glob Bash(sd today:*) Bash(sd task show:*) Bash(sd note list:*) \
Bash(sd reports list:*) Bash(sd gate status:*) Bash(sd task add:*) \
Bash($CRON status:*) Bash($CRON list:*) Bash($CRON logs:*) Bash($CRON run:*) \
Bash($ROOT/local-health-check/health-check.sh check:*) \
Bash(launchctl print:*) Bash(launchctl list:*) Bash(launchctl kickstart:*) \
Bash(rm -f $ROOT/local-cron-jobs/logs/:*) \
Bash(df:*) Bash(du:*) Bash(ls:*) Bash(ps:*) Bash(pgrep:*) Bash(tail:*) Bash(git status:*) Bash(git log:*)"
DENY='Write Edit NotebookEdit Bash(sudo:*) Bash(git push:*) Bash(gh:*) Bash(sd-ship:*) Bash(crontab:*)'

usage() {
  cat <<'USAGE'
night-triage.sh — the night triage job: this machine's issues, repaired or filed.

Usage: night-triage.sh <command>

  run    1. release: give back any claim an earlier run left (state/claims).
         2. claim: `sd reports list`; each planning alert whose report names
            this machine (lower-cased `hostname -s`) is claimed with
            `sd task status N in_progress`; an alert already claimed is skipped.
         3. triage: `claude -p` on Sonnet reads the alerts, `sd today`'s
            Operations, this machine, and local status; it may rerun a failed
            job, restart a service, clear a stale cron file and file bugs with
            `sd task add`; it replies with one answer per alert. 1680 s bound.
         4. settle: each alert goes back to planning with one note; a fix with
            evidence for every open followup is then acknowledged with
            `sd reports acknowledge N --resolve-followups`.
         Exits 0 when every alert is settled, non-zero when any step fails;
         a failure before step 4 leaves every alert as it was.
  test   Run this folder's tests (extra args go to unittest).
  help   This text.

SD_PACK_ROOT names the pack (default ~/repos/platypeeps/sd-workflow);
CLAUDE_BIN names the claude binary (default: claude on PATH).
USAGE
}

run() {
  host=$(hostname -s | tr '[:upper:]' '[:lower:]')
  state="$DIR/state"
  mkdir -p "$state"
  claims="$state/claims"
  tmp=$(mktemp -d "${TMPDIR:-/tmp}/night-triage.XXXXXX")
  trap 'rm -rf "$tmp"' EXIT

  st_step 120 python3 "$DIR/night_triage.py" release "$SD" "$claims"
  # Any failure from here gives back what this run claimed.
  rc=0
  count=$(st_step 300 python3 "$DIR/night_triage.py" claim "$SD" "$host" "$claims" "$tmp/alerts.json") || rc=$?
  [ "$rc" -eq 0 ] || { python3 "$DIR/night_triage.py" release "$SD" "$claims" || :; return "$rc"; }

  role=satellite
  fence=$(st_step 60 "$PACK/bin/sd-db" fence 2>/dev/null) || fence=""
  case "$fence" in *"fence: holder:"*) role=hub ;; esac
  st_step 120 "$SD" today --all > "$tmp/today" 2>/dev/null || echo "(sd today failed)" > "$tmp/today"
  {
    cat "$DIR/prompt.md"
    printf '\nMachine: %s (%s)\nSystem checkout: %s\nPack checkout: %s\n' "$host" "$role" "$ROOT" "$PACK"
    printf '\nAlerts you answer for (%s), JSON:\n' "$count"
    cat "$tmp/alerts.json"
    printf '\n\nsd today, Operations, this machine:\n'
    sed -n '/^Operations, this machine/,/^$/p' "$tmp/today"
  } > "$tmp/prompt"

  # As in nightly-review: --restricted drops inherited settings and code
  # tools, --strict-mcp-config every MCP server (no mail, Slack or Drive),
  # --tools names all the model has. The prompt goes in on stdin.
  (cd "$ROOT" && st_step 1680 "${CLAUDE_BIN:-claude}" -p --model sonnet \
    --restricted --strict-mcp-config --tools 'Read,Grep,Glob,Bash' \
    --allowedTools "$TOOLS" --disallowedTools "$DENY" < "$tmp/prompt" > "$tmp/reply") || rc=$?
  [ "$rc" -eq 0 ] || { python3 "$DIR/night_triage.py" release "$SD" "$claims" || :; return "$rc"; }
  cp "$tmp/reply" "$state/$(date +%F).reply"

  st_step 300 python3 "$DIR/night_triage.py" apply "$SD" "$host" "$claims" "$tmp/alerts.json" "$tmp/reply" || rc=$?
  [ "$rc" -eq 0 ] || { python3 "$DIR/night_triage.py" release "$SD" "$claims" || :; return "$rc"; }
}

case "${1:-}" in
  run) [ "$#" -eq 1 ] || { usage >&2; exit 1; }; run ;;
  test) shift; exec "${PYTHON:-python3}" -m unittest discover -s "$DIR/tests" -t "$DIR" "$@" ;;
  -h|--help|help) usage ;;
  *) usage >&2; exit 1 ;;
esac

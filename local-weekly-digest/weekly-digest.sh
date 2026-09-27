#!/bin/sh
# Weekly automation digest: one Sunday email summarizing what the cron
# fleet did — runs and failures per job, drift, pending updates, disk —
# so the nightly jobs can stay quiet unless something needs a human.
# Usage: weekly-digest.sh run|check
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"

NOTIFY="$DIR/../local-notify/notify.sh"
# Seams for a fixture tree, not per-machine settings: on every real machine
# these are this checkout.
CRON_LOGS="${WEEKLY_DIGEST_CRON_LOGS:-$DIR/../local-cron-jobs/logs}"
MSETUP="${WEEKLY_DIGEST_MSETUP:-$DIR/../local-machine-setup/machine-setup.sh}"

case "${1:-}" in
  run)   EMAIL=1 ;;
  check) EMAIL=0 ;;
  test)
    shift
    exec "${PYTHON:-python3}" -m unittest discover -s "$DIR/tests" -t "$DIR" "$@"
    ;;
  -h|--help|help)
    cat <<'HELPEOF'
usage: weekly-digest.sh run|check|test

  run    build the weekly automation summary and email it via local-notify
         (what the weekly-digest cron job runs on Sundays). Always emails —
         the digest IS the report. Exits 1 only when the email could not
         be delivered.
  check  print the same summary to stdout without emailing.
  test   run the unittest suite in tests/; extra arguments go to unittest (-v).

contents: per-job run/failure counts for the last 7 days (from
local-cron-jobs logs), recent failures, machine-setup drift count, pending
brew/mas updates, root disk usage.

order: this week's failure lines are grouped by job, and the jobs ordered
by failures this week, then by the newest failure, then by name; each line
carries its job's weekly count. The order is computed from the failure log
alone and nothing leaves the machine. A line in
any shape but the one local-cron-jobs writes keeps the log's order, with a
note saying so.

environment:
  WEEKLY_DIGEST_CRON_LOGS   cron log folder (default: local-cron-jobs/logs)
  WEEKLY_DIGEST_MSETUP      machine-setup entrypoint whose drift count the
                            digest reads (default: this checkout's
                            local-machine-setup; the line is left out when
                            that file does not exist)
HELPEOF
    exit 0
    ;;
  *)
    echo "usage: $(basename "$0") run|check|test" >&2
    exit 1
    ;;
esac

TMPD=$(mktemp -d)
trap 'rm -rf "$TMPD"' EXIT INT TERM
BODY="$TMPD/body"
FAILURES="$TMPD/failures"
NOTES="$TMPD/notes"
: > "$FAILURES"; : > "$NOTES"

# BSD date takes -v, GNU date takes -d; the CI runner is Linux.
cutoff=$(date -v-7d '+%Y-%m-%d' 2>/dev/null || date -d '7 days ago' '+%Y-%m-%d')

note() { printf -- '%s\n' "$1" >> "$NOTES"; }

if [ -s "$CRON_LOGS/failures.log" ]; then
  awk -v c="$cutoff" 'substr($1, 1, 10) >= c' "$CRON_LOGS/failures.log" > "$FAILURES"
fi

# --- order this week's failures: most frequent first -------------------------

# The failure lines are grouped by job, and the jobs ordered by failures this
# week, then by the newest failure, then by name so a tie cannot shuffle the
# digest from one week to the next. Newest first inside a job. The order reads
# this week's failure lines only, not the job logs: a streak read from logs
# that rotation truncates had no reliable value (sd:1478).
#
# This replaced an order Jev computed (sd:1478). Jev could see only the time,
# the exit code and the weekly count once job names were withheld (sd:1474),
# and that is exactly what this reads, without a request.
#
# It never drops or rewords a line: it reorders lines and appends a label.
# Only the shape `notify_failure` in local-cron-jobs writes carries a job
# name this can trust; a line in any other shape keeps the log's order.
order_failures() {
  _n=$(wc -l < "$FAILURES" | tr -d ' ')
  [ "${_n:-0}" -ge 1 ] || return 0
  _bad=$(awk '
    !($1 ~ /^[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9][-+][0-9][0-9][0-9][0-9]$/ \
      && $2 ~ /^[A-Za-z0-9._-]+$/ && $3 == "FAILED" && $4 ~ /^rc=[0-9]+$/ \
      && NF == 6 && $5 == "(log:" && $6 == "logs/" $2 ".log)") { bad++ }
    END { print bad + 0 }
  ' "$FAILURES")
  if [ "$_bad" -ne 0 ]; then
    note "failure order skipped: $_bad failure line(s) not in the cron failure shape"
    return 0
  fi
  # Recency compares UTC seconds, not the stamp text: across a daylight-saving
  # change 01:15-0700 is later than 01:30-0600. Days come from the civil
  # calendar arithmetic, since BSD awk has no mktime.
  awk -F'\t' '
    function utc(ts,   y, m, d, era, yoe, doy, off) {
      y = substr(ts, 1, 4) + 0; m = substr(ts, 6, 2) + 0; d = substr(ts, 9, 2) + 0
      y -= (m <= 2); era = int(y / 400); yoe = y - era * 400
      doy = int((153 * (m > 2 ? m - 3 : m + 9) + 2) / 5) + d - 1
      off = substr(ts, 21, 2) * 3600 + substr(ts, 23, 2) * 60
      if (substr(ts, 20, 1) == "-") off = -off
      return (era * 146097 + yoe * 365 + int(yoe / 4) - int(yoe / 100) + doy - 719468) * 86400 \
        + substr(ts, 12, 2) * 3600 + substr(ts, 15, 2) * 60 + substr(ts, 18, 2) - off
    }
    { split($0, f, " "); line[NR] = $0; job[NR] = f[2]; stamp[NR] = utc(f[1])
      count[f[2]]++; if (stamp[NR] > newest[f[2]]) newest[f[2]] = stamp[NR] }
    END {
      for (i = 1; i <= NR; i++) {
        j = job[i]
        printf "%d\t%d\t%s\t%d\t%s  [%d this week]\n", count[j], newest[j], j, stamp[i], line[i], count[j]
      }
    }
  ' "$FAILURES" \
    | sort -t'	' -k1,1nr -k2,2nr -k3,3 -k4,4nr \
    | cut -f5- > "$TMPD/failures.ordered"
  cp "$TMPD/failures.ordered" "$FAILURES"
}
order_failures || note "failure order skipped: the ordering itself failed"

{
  printf 'weekly automation digest — %s on %s\n' \
    "$(date '+%Y-%m-%d %H:%M')" "$(hostname -s)"
  echo

  echo "job activity, last 7 days (runs / failures):"
  for log in "$CRON_LOGS"/*.log; do
    [ -e "$log" ] || continue
    job=$(basename "$log" .log)
    [ "$job" = "failures" ] && continue
    # Log lines look like: [job] 2026-08-23T04:41:20-0600 done
    runs=$(awk -v c="$cutoff" '$2 >= c && $3 == "done"' "$log" | wc -l | tr -d ' ')
    fails=$(awk -v c="$cutoff" '$2 >= c && $3 ~ /^FAILED/' "$log" | wc -l | tr -d ' ')
    [ "$runs" = "0" ] && [ "$fails" = "0" ] && continue
    if [ "$fails" != "0" ]; then
      printf '  %-30s %s ok / %s FAILED\n' "$job" "$runs" "$fails"
    else
      printf '  %-30s %s ok\n' "$job" "$runs"
    fi
  done

  if [ -s "$FAILURES" ]; then
    echo
    echo "failures this week:"
    sed 's/^/  /' "$FAILURES"
  fi

  echo
  # machine-setup is a separate tool; without it there is no drift to count.
  if [ -f "$MSETUP" ]; then
    drift=$(sh "$MSETUP" status 2>/dev/null | sed -n 's/^drift *: *//p')
    echo "machine-setup drift : ${drift:-unknown}"
  fi

  if command -v brew >/dev/null 2>&1; then
    bf=$(brew outdated --formula --quiet 2>/dev/null | wc -l | tr -d ' ')
    bc=$(brew outdated --cask --quiet 2>/dev/null | wc -l | tr -d ' ')
    echo "brew outdated       : $bf formulae, $bc casks (Sunday 04:00 sweep handles these)"
  fi
  if command -v mas >/dev/null 2>&1; then
    mo=$(mas outdated 2>/dev/null | wc -l | tr -d ' ')
    echo "App Store outdated  : $mo"
  fi

  dused=$(df -P / | awk 'NR==2 { print $5 }')
  echo "root disk used      : $dused"

  if [ -s "$NOTES" ]; then
    echo
    echo "notes:"
    sed 's/^/  /' "$NOTES"
  fi
} > "$BODY"

cat "$BODY"

if [ "$EMAIL" -eq 1 ]; then
  subject="weekly digest: automation summary for $(hostname -s)"
  if ! sh "$NOTIFY" -t "$subject" -k brief -c ntfy,email -b "$(cat "$BODY")"; then
    echo "email FAILED — exiting 1 so the cron failure push fires" >&2
    exit 1
  fi
fi

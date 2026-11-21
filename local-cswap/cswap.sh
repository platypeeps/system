#!/usr/bin/env bash
# cswap.sh — one Claude account across the fleet (sd:7323).
#
# Remote Control sees only the sessions of the active account, so every
# machine must use the account the hub uses. The hub picks it; satellites
# follow. A local-cron-jobs job runs `sync` every minute on every machine
# (local-cron-jobs/examples/cswap-sync.job).
#
# Usage:
#   cswap.sh sync     lead or follow, by this machine's role
#   cswap.sh status   same account as the hub? (exit 0 yes, 1 no, 3 nothing to check)
#
# The role is the fence's: the machine whose `sd-db fence` reads holder (exit
# 0) leads, so a promoted satellite leads by itself. Any other answer follows.
# The account travels as its organizationUuid in the workflow database row
# sys.cswap_account (sd-plugin.json declares it); account numbers differ
# between machines, so they never travel.
#
# Flags for `cswap auto` live in cswap-auto.conf.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/config.sh
. "$ROOT/../lib/config.sh"
# shellcheck source=../lib/bounded.sh
. "$ROOT/../lib/bounded.sh"

# The always-on `cswap auto` LaunchAgent this script used to install. Sync
# replaces it: a machine that still runs it rotates on its own.
RETIRED_LABEL="$(st_label_prefix).cswap-auto"
RETIRED_SERVICE="gui/$(id -u)/$RETIRED_LABEL"
RETIRED_PLIST="$HOME/Library/LaunchAgents/$RETIRED_LABEL.plist"

PACK="${SD_PACK_ROOT:-$HOME/repos/platypeeps/sd-workflow}"
SETTING="sys.cswap_account"
UUID_RE='^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
# Consecutive sync runs whose read the hub refused for this machine's sd_db
# build, or found the hub unreachable (sd:7650), and the run that fails
# (system sd:7407). The follow job runs every
# 5 minutes and its follow takes up to 10; 30 minute runs cover two of its
# slots, so only a follow that keeps failing fails here too.
REFUSALS="$ROOT/state/build-refusals"
REFUSAL_LIMIT=30

# Remote Control (sd:9427): a switch drops every session's link, and /rc in
# a dropped session reconnects it under the new account. A switch writes
# RC_PENDING; once herdr lists the panes, each claude pane is owed in
# RC_OWED (herdr session, pane, session, epoch). A sync reads and writes
# only the rows of its own herdr session (HERDR_SESSION below): another
# session's panes are not in its list, so they are neither gone nor sent
# (sd:9427 review). Every sync sends /rc to an owed pane that
# is idle or done, whose latest Remote Control banner is the drop, and
# whose input box holds no typed text. Any other pane gets no keys, as a
# key can answer a permission dialog or join a half-typed prompt; it stays
# owed for the next sync, until sent, gone or RC_OWED_FOR seconds old. An
# active banner may be the old account's, printed before the drop, so it
# waits too. Right before the keys, `herdr agent get` and a fresh screen
# read check the pane again; a gap of milliseconds remains, as herdr has
# no check-and-send. Cron's PATH may lack herdr, so HERDR_DIRS is tried
# after it.
RC_OWED="$ROOT/state/rc-owed"
RC_PENDING="$ROOT/state/rc-pending"
# One reconnect pass at a time on the machine, every herdr session's: a
# cron sync and a manual one each rewrote RC_OWED, and the last writer
# lost the other's rows (sd:9427 review). A kernel lock (flock) on fd 9,
# so a killed sync leaves no stale lock.
RC_LOCK="$ROOT/state/rc-lock"
RC_LISTED="$ROOT/state/rc-listed"
RC_OWED_FOR=1800
# One reconnect pass may spend this many seconds: each herdr call is
# bounded by what is left of it, and a pane not reached stays owed
# (sd:9282 lane review, sd:9427). Tests set CSWAP_RC_BUDGET.
RC_BUDGET="${CSWAP_RC_BUDGET:-60}"
# The most milliseconds the last checks before the keys may take, from
# the screen read to the end of the session get (sd:9427 class pass).
RC_GAP_MS="${CSWAP_RC_GAP_MS:-1000}"
RC_DEADLINE=""
HERDR_DIRS="${CSWAP_HERDR_DIRS-$HOME/.local/bin /opt/homebrew/bin /usr/local/bin}"
HERDR=""
# Every herdr call targets the session local-herdr starts (sd unless
# HERDR_SESSION says), not herdr's default: cron found no panes there,
# and its empty list cleared a pending switch. A socket path inherited
# from another session's pane would override the name, so it goes.
export HERDR_SESSION="${HERDR_SESSION:-sd}"
unset HERDR_SOCKET_PATH
AGENTS=""
PANES=""
LISTED=""

# shellcheck source=/dev/null
[[ -f "$ROOT/cswap-auto.conf" ]] && . "$ROOT/cswap-auto.conf"
CSWAP_BIN="${CSWAP_BIN:-$HOME/.local/bin/cswap}"
CSWAP_AUTO_ARGS="${CSWAP_AUTO_ARGS:-}"

# A failed step: one stderr line, one data-safe report line, exit 1. No
# account moves on a failure, so no data is at risk; the step names stay
# fixed so a repeating failure folds into one open report.
fail() { # step, message
  echo "cswap sync: $2" >&2
  echo "SD_REPORT_ATTENTION: data safe, one step failed: cswap sync $1"
  exit 1
}

# Whether this machine leads. The words count only with exit 0: a fence
# that fails is no answer, and an unknown answer must not make a second
# leader.
leads() {
  local out
  out=$(st_bounded 60 "$PACK/bin/sd-db" fence 2>/dev/null) || return 1
  case "$out" in *"fence: holder:"*) return 0 ;; esac
  return 1
}

# `cswap list --json` on stdout, bounded.
accounts() {
  st_bounded 60 "$CSWAP_BIN" list --json
}

# Reads `cswap list --json` on stdin. `active` prints "<number> <uuid>" of
# the active account; `number UUID` prints the number of the one account
# with that organizationUuid, exit 4 when none has it, 5 (and the count)
# when several do.
pick() { # active | number UUID
  python3 -I -c '
import json, sys
data = json.load(sys.stdin)
accounts = data.get("accounts") or []
if sys.argv[1] == "active":
    found = [a for a in accounts if a.get("number") == data.get("activeAccountNumber")]
    if len(found) != 1:
        sys.exit(3)
    print(found[0]["number"], found[0].get("organizationUuid") or "-")
else:
    found = [a["number"] for a in accounts if a.get("organizationUuid") == sys.argv[2]]
    if not found:
        sys.exit(4)
    if len(found) > 1:
        print(len(found))
        sys.exit(5)
    print(found[0])
' "$@"
}

# The published row's value on stdout. Exit 1 with sd's own words on stderr
# when it cannot be read, is unset or is no UUID: each is no answer.
# SD_FOLLOWED is the pack's marker (lib/sd_db/follow.py): with it, a hub
# that refuses this sd_db build is refused at once, instead of sd running
# `repo-sync.sh follow` for up to 600 s, which the 60 s bound cut part way.
published() {
  local value
  value=$(SD_FOLLOWED=1 st_bounded 60 "$PACK/bin/sd" config get "$SETTING") || return 1
  if [[ ! "$value" =~ $UUID_RE ]]; then
    echo "$SETTING holds '$value', which is no organizationUuid" >&2
    return 1
  fi
  printf '%s\n' "$value"
}

# Boot out the retired agent and delete its plist. launchd loads a plist
# left behind again at the next login, so the file goes even when launchd
# no longer holds the label.
retire_agent() {
  if launchctl print "$RETIRED_SERVICE" >/dev/null 2>&1; then
    launchctl bootout "$RETIRED_SERVICE" ||
      fail retire "could not boot out the retired $RETIRED_LABEL agent; run: launchctl bootout $RETIRED_SERVICE"
    echo "retired: booted out $RETIRED_LABEL"
  fi
  if [[ -e "$RETIRED_PLIST" ]]; then
    rm -f "$RETIRED_PLIST" || fail retire "could not delete $RETIRED_PLIST"
    echo "retired: deleted $RETIRED_PLIST"
  fi
}

# st_step for one herdr call: at most $1 seconds and at most what is left
# of the pass's budget; none at all once it is spent (exit 124).
rc_step() {
  local left=$((RC_DEADLINE - $(date +%s))) bound=$1
  shift
  ((left > 0)) || return 124
  ((left < bound)) && bound=$left
  # No herdr process may inherit the pass's lock.
  st_step "$bound" "$@" 9>&-
}

# The herdr binary on stdout: PATH first, then HERDR_DIRS.
herdr_bin() {
  local d
  command -v herdr 2>/dev/null && return 0
  for d in $HERDR_DIRS; do
    [[ -x "$d/herdr" ]] && { echo "$d/herdr"; return 0; }
  done
  return 1
}

# Sets HERDR, and PANES to "pane session status" lines, one per claude pane
# `herdr agent list` names; session "?" when herdr names none. Else one skip line and exit 1: no herdr (2), no
# herdr server answering, or an answer that is not the agent list (3).
herdr_panes() {
  [[ -z "$LISTED" ]] || return 0
  if ! HERDR=$(herdr_bin); then
    echo "reconnect: skipped: no herdr on PATH${HERDR_DIRS:+ or in $HERDR_DIRS}"
    return 2
  fi
  AGENTS=$(rc_step 20 "$HERDR" agent list) ||
    { echo "reconnect: skipped: herdr agent list did not answer within the budget; is herdr running?"; return 1; }
  PANES=$(rc_read panes <<<"$AGENTS") ||
    { echo "reconnect: skipped: herdr agent list gave no agent list this can parse"; return 3; }
  LISTED=1
}

# herdr's answers, read by python. `panes`: `herdr agent list` JSON on stdin
# to "pane session status" lines for claude panes; exit 3 when it has not
# that shape (null, malformed, an agent that is no object); `pane`: one
# `herdr agent get` answer, the same way, one line or none. `screen`: a
# pane's screen on stdin to send or wait <why>. The later of
# Claude's two Remote Control banners decides, as the screen keeps old ones
# above, and only one empty input box below it (rule, ❯, rule; any other
# shape waits) takes keys; dim text there is a prompt suggestion.
rc_read() {
  python3 -I -c '
import json, sys
text = sys.stdin.read()
if sys.argv[1] in ("panes", "pane"):
    try:
        found = json.loads(text)["result"]
        agents = found["agents"] if sys.argv[1] == "panes" else [found["agent"]]
        if not isinstance(agents, list):
            raise TypeError
        for a in agents:
            if a["agent"] != "claude":
                continue
            # No session id, none sent or a false one, is no pane to owe or
            # send to: a new session in the pane could pass for the old one.
            # The list names the pane with session "?", which no owed row
            # holds: the pane is there, its session unknown (sd:9427
            # review); a get names nothing.
            session = a.get("agent_session") or {}
            if not session.get("value"):
                if sys.argv[1] == "pane":
                    continue
                session = {"value": "?"}
            words = [a["pane_id"], session["value"], a.get("agent_status") or "-"]
            if not all(isinstance(w, str) and w and w.split() == [w] for w in words):
                raise TypeError
            print(*words)
    except (ValueError, KeyError, TypeError, AttributeError):
        sys.exit(3)
else:
    import re
    # The screen comes with its ANSI codes (--format ansi). The own
    # banners of Claude are matched on the text with every escape removed, anchored
    # at line start in the form Claude prints them, so prose that quotes
    # them is no banner; a line that holds both texts is neither.
    sgr = re.compile(r"\x1b\[([0-9;:]*)m")
    other = re.compile(r"\x1b(\[[0-9;:?]*[ -/]*[@-~]|\][^\x07\x1b]*(\x07|\x1b\\)|.)")
    DROP, ACTIVE = "Remote Control disconnected", "/remote-control is active"
    def plain(line):
        return other.sub("", sgr.sub("", line))
    def banner(line):
        if DROP in line and ACTIVE in line:
            return None
        if line.startswith("⏺ " + DROP + " — "):
            return "drop"
        if line.startswith("  " + ACTIVE + " · "):
            return "active"
        return None
    # SGR state across a run of lines: text inside a dim (SGR 2) run is a
    # prompt suggestion, not typed input, and is dropped. A parameter with
    # colons (38:2::r:g:b) is one parameter; 38/48/58 with ; take their
    # colour words with them, so a 2 there is no dim.
    def undimmed(line, dim):
        out, pos = [], 0
        for m in sgr.finditer(line):
            if not dim:
                out.append(line[pos:m.start()])
            params, i = (m.group(1) or "0").split(";"), 0
            while i < len(params):
                code = params[i].split(":")[0]
                if code in ("38", "48", "58") and ":" not in params[i]:
                    i += 3 if params[i + 1:i + 2] == ["5"] else 5
                    continue
                if code in ("", "0", "00", "22"):
                    dim = False
                elif code in ("2", "02"):
                    dim = True
                i += 1
            pos = m.end()
        if not dim:
            out.append(line[pos:])
        return plain("".join(out)), dim
    def rule(text):
        return re.fullmatch(r"─+( [^─]* ─+)?", text.strip()) is not None
    raw = text.splitlines()
    lines = [plain(line) for line in raw]
    # Claude draws its drop banner and its box rules in colour; text typed
    # into a draft is plain, so a typed banner or rule is neither (sd:9427
    # review: a draft with a fake banner, a rule and a bare ❯ read as an
    # empty box). A typed active banner still counts: it only waits.
    def coloured(i):
        return raw[i].startswith("\x1b[")
    marks = [(i, banner(line)) for i, line in enumerate(lines)
             if banner(line) == "active" or (banner(line) == "drop" and coloured(i))]
    # Hold on doubt (sd:9282 bundle review): below the last banner, the
    # screen must show one empty input box and nothing like a second one:
    # exactly two rules and, from the top rule down, one ❯ line, adjacent,
    # rule ❯ rule. A draft can hold rule or ❯ lines of its own, so any
    # other shape waits.
    below = range(marks[-1][0] + 1 if marks else 0, len(lines))
    rules = [i for i in below if rule(lines[i]) and coloured(i)]
    # A ❯ line above the top rule is history, the prompt of a finished
    # turn, not input (sd:9427 lane review); from the top rule down is
    # the live box and what is drawn below it.
    prompts = [i for i in below if (not rules or i > rules[0])
               and lines[i].strip().startswith("❯")]
    one_box = len(rules) == 2 and len(prompts) == 1 and rules[0] + 1 == prompts[0] == rules[1] - 1
    # The box is the last thing on screen (sd:9427 review round 5): an
    # old empty box with a permission dialog drawn below it waits. Below
    # the bottom rule only a short footer may follow (status line, mode
    # line): no box-drawing line, numbered option or dialog hint.
    # ponytail: footer read by shape and a cap of 3 lines; a longer
    # custom status line waits, and gets /rc once it fits.
    def footer(text):
        t = text.strip()
        return not ("─" <= t[:1] <= "╿" or re.match(r"(❯|>)?\s*\d+[.)]\s", t)
                    or re.search(r"Esc to|Enter to|Do you want|Would you like", t))
    tail = [lines[i] for i in range(rules[1] + 1, len(lines))] if one_box else []
    tail = [t for t in tail if t.strip()]
    one_box = one_box and len(tail) <= 3 and all(footer(t) for t in tail)
    # Empty is a match, never what is left after deleting (sd:9427 class
    # pass): the line is exactly ❯ and its NBSP, plus at most dim text (a
    # prompt suggestion). No strip, as a draft can be blanks of any kind,
    # and no control sequence but colour, as a cursor move can stand for
    # typed blanks.
    line = raw[prompts[0]] if one_box else ""
    empty = (one_box and other.search(sgr.sub("", line)) is None
             and undimmed(line, False)[0] == "❯\u00a0")
    if marks and marks[-1][1] == "active":
        print("wait: its latest Remote Control banner is active, maybe from the old account")
    elif not marks:
        print("wait: no Remote Control drop on its screen yet")
    elif not rules or not prompts:
        print("wait: no input box on its screen")
    elif not one_box:
        print("wait: its input box is not one empty line")
    elif not empty:
        print("wait: its input box holds text")
    else:
        print("send")
' "$1"
}

# The account just switched: every claude pane owes /rc. The mark, one
# line "<epoch> <pid>" naming this switch, is written at once, before a
# later step can fail, and replaced whole (a new file moved in place).
# Only a switch writes it and nothing deletes it, so no sync can lose a
# newer switch's mark; one older than RC_OWED_FOR owes nothing
# (sd:9427 review). Which herdr session listed for which switch is
# RC_LISTED, written only under RC_LOCK.
owe_rc() {
  { mkdir -p "${RC_PENDING%/*}" && printf '%s %s\n' "$(date +%s)" "$$" >"$RC_PENDING.$$" &&
    mv "$RC_PENDING.$$" "$RC_PENDING"; } 2>/dev/null ||
    echo "reconnect: could not write $RC_PENDING; no pane owes /rc for this switch"
}

# The RC_OWED rows to keep, unchanged: other herdr sessions' rows, and
# with $1 = all this session's too; none if unreadable.
kept_rows() {
  [[ -e "$RC_OWED" ]] || return 0
  awk -v hs="$HERDR_SESSION" -v all="${1:-}" 'NF == 4 && ($1 != hs || all)' "$RC_OWED"
}

# Whether RC_OWED holds a row of this herdr session.
owes_here() {
  awk -v hs="$HERDR_SESSION" 'NF == 4 && $1 == hs { found = 1 } END { exit !found }' "$RC_OWED" 2>/dev/null
}

# A pending switch to this herdr session's owed panes, once herdr lists
# them, once per switch and session: RC_LISTED holds "<mark> <session>"
# for the mark the list was read for, so a switch that lands during the
# list is still unlisted at the next sync. Each listing also records
# "<mark> <session> <pane>" for each pane it owes; one that meets a pane
# with no session id is incomplete, and the next sync lists again.
list_owed() {
  local mark since now rc=0 kept="" listed="" enrolled fresh unknown lines
  mark=$(head -n 1 "$RC_PENDING" 2>/dev/null) || mark=""
  since=${mark%% *}
  now=$(date +%s)
  [[ "$since" =~ ^[0-9]+$ ]] && ((now - since <= RC_OWED_FOR)) || return 0
  grep -Fqx -- "$mark $HERDR_SESSION" "$RC_LISTED" 2>/dev/null && return 0
  herdr_panes || rc=$?
  ((rc == 0)) || return 0
  # A list that does not read is never replaced, and nothing is owed:
  # which panes this switch already owed is unknown (sd:9427 review).
  if [[ -e "$RC_LISTED" ]] && ! listed=$(awk -v m="$mark " 'index($0, m) == 1' "$RC_LISTED" 2>/dev/null); then
    echo "reconnect: could not read $RC_LISTED; the next sync tries again"
    return 0
  fi
  # The first listing for a switch replaces this session's old rows. A
  # retry after an incomplete one keeps them and owes only panes it did
  # not owe yet: a pane already sent is not owed again, as a drop banner
  # it still shows would take a second /rc, which disconnects it
  # (sd:9427 review).
  enrolled=$(awk -v hs="$HERDR_SESSION" 'NF == 4 && $3 == hs { print $4 }' <<<"$listed")
  # A file that does not read is never replaced: its rows would go, and
  # this session stays unlisted, so the next sync tries again (sd:9427
  # review).
  if ! kept=$(kept_rows "$enrolled" 2>/dev/null); then
    echo "reconnect: could not read $RC_OWED; the next sync tries again"
    return 0
  fi
  fresh=$(awk -v hs="$HERDR_SESSION" -v since="$since" -v done="$enrolled" '
    BEGIN { n = split(done, d, "\n"); for (i = 1; i <= n; i++) owed[d[i]] = 1 }
    NF == 3 && $2 != "?" && !($1 in owed) { print hs, $1, $2, since }' <<<"$PANES")
  unknown=$(awk 'NF == 3 && $2 == "?" { printf "%s%s", sep, $1; sep = ", " }' <<<"$PANES")
  # Three writes, each a whole file moved in. The panes about to be owed
  # are recorded first: a failure after it loses their /rc at worst, and
  # never owes a sent pane again. Each write in a group is chained: a
  # group's status is its last command's, and a failed first write
  # would move a partial file in (sd:9427 review).
  if [[ -n "$fresh" ]]; then
    lines=$(awk -v m="$mark" '{ print m, $1, $2 }' <<<"$fresh") || return 0
    if ! { { { [[ -z "$listed" ]] || printf '%s\n' "$listed"; } && printf '%s\n' "$lines"
      } >"$RC_LISTED.new" && mv "$RC_LISTED.new" "$RC_LISTED"; } 2>/dev/null; then
      echo "reconnect: could not write $RC_LISTED; the next sync tries again"
      return 0
    fi
    listed=${listed:+$listed$'\n'}$lines
  fi
  if ! { { { [[ -z "$kept" ]] || printf '%s\n' "$kept"; } &&
      { [[ -z "$fresh" ]] || printf '%s\n' "$fresh"; }
    } >"$RC_OWED.new" && mv "$RC_OWED.new" "$RC_OWED"; } 2>/dev/null; then
    echo "reconnect: could not write $RC_OWED; its panes get no /rc for this switch"
    return 0
  fi
  # Lines of older marks go; this mark's lines stay. A pane with no
  # session id yet is not owed and not lost: the session stays unlisted.
  if [[ -n "$unknown" ]]; then
    echo "reconnect: $unknown names no session yet; $HERDR_SESSION lists again next sync"
  elif ! { { { [[ -z "$listed" ]] || printf '%s\n' "$listed"; } &&
      echo "$mark $HERDR_SESSION"; } >"$RC_LISTED.new" && mv "$RC_LISTED.new" "$RC_LISTED"; } 2>/dev/null; then
    echo "reconnect: could not mark $HERDR_SESSION listed in $RC_LISTED"
  fi
  echo "reconnect: $(grep -c . <<<"$fresh") claude pane(s) owe /rc"
}

# Milliseconds since the epoch; bash 3.2 has no EPOCHREALTIME.
now_ms() { python3 -c 'import time; print(int(time.time() * 1000))'; }

# Right before a send (operator ruling 2026-11-18): a fresh read of the
# pane's ($1) screen says send, the pane still runs the owed session ($2)
# and is idle or done, and a last read shows the same screen; then /rc.
# Else one line, and it stays owed. herdr has no check-and-send, so the
# span from the first read to the end of the send must fit RC_GAP_MS.
send_rc() {
  local now screen last verdict="" start took bound=$(((RC_GAP_MS + 999) / 1000))
  # Screen, get, screen. A session that took the pane during the first
  # read shows a drop and an empty box of its own, so the get follows it
  # (sd:9427 review). A draft typed during the get leaves session and
  # status alone, so the screen is read again and must not have changed
  # (sd:9427 review); a new session redraws the screen. Each check goes
  # stale while the next call runs, so all must fit RC_GAP_MS.
  start=$(now_ms)
  if ! screen=$(rc_step "$bound" "$HERDR" agent read "$1" --format ansi) ||
    ! verdict=$(rc_read screen <<<"$screen") || [[ "$verdict" != send ]]; then
    echo "reconnect: $1 changed before the send (${verdict:-no screen}); still owed"
    return 1
  fi
  now=$(rc_step "$bound" "$HERDR" agent get "$1" | rc_read pane) || now=""
  if [[ "$now" != "$1 $2 idle" && "$now" != "$1 $2 done" ]]; then
    echo "reconnect: $1 changed before the send (${now:-no answer}); still owed"
    return 1
  fi
  if ! last=$(rc_step "$bound" "$HERDR" agent read "$1" --format ansi) || [[ "$last" != "$screen" ]]; then
    echo "reconnect: $1 changed before the send (its screen moved during the check); still owed"
    return 1
  fi
  took=$(($(now_ms) - start))
  if ((took > RC_GAP_MS)); then
    echo "reconnect: $1 took $took ms to check, over $RC_GAP_MS ms; still owed"
    return 1
  fi
  # The send counts too: herdr may take seconds to deliver, and a draft
  # typed meanwhile would take the /rc (sd:9427 review). Its --timeout
  # is the gap's time left. A late /rc that landed anyway shows on the
  # next read as connected, so a pane kept owed gets no second one.
  if ! rc_step "$bound" "$HERDR" agent prompt "$1" /rc --timeout "$((RC_GAP_MS - took))" >/dev/null; then
    echo "reconnect: /rc to $1 failed or ran over $RC_GAP_MS ms; still owed"
    return 1
  fi
  took=$(($(now_ms) - start))
  if ((took > RC_GAP_MS)); then
    echo "reconnect: /rc to $1 ended $took ms after the check began, over $RC_GAP_MS ms; still owed"
    return 1
  fi
}

# One reconnect pass under RC_LOCK; a sync that finds it held skips,
# and every owed row and pending mark stays for the next sync.
reconnect() {
  if ! { mkdir -p "${RC_LOCK%/*}" && exec 9>>"$RC_LOCK"; } 2>/dev/null; then
    echo "reconnect: skipped: could not open $RC_LOCK; the next sync tries again"
    return 0
  fi
  if ! python3 -c 'import fcntl; fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)' 2>/dev/null; then
    exec 9>&-
    echo "reconnect: skipped: another sync is reconnecting; the next sync tries again"
    return 0
  fi
  reconnect_pass
  exec 9>&-
}

# Send /rc to each owed pane that may have it; keep the rest owed.
reconnect_pass() {
  local hs pane session since action screen verdict plan keep="" mine=0
  RC_DEADLINE=$(($(date +%s) + RC_BUDGET))
  list_owed
  owes_here || return 0
  herdr_panes || return 0
  plan=$(awk -v now="$(date +%s)" -v limit="$RC_OWED_FOR" -v hs="$HERDR_SESSION" '
    FILENAME == "-" { if (NF == 3) { status[$1 " " $2] = $3; if ($2 == "?") unknown[$1] = 1 }; next }
    NF != 4 || $4 !~ /^[0-9]+$/ { next }
    $1 != hs { print "other", $0; next }
    !(($2 " " $3) in status) && !($2 in unknown) { print "gone", $0; next }
    now - $4 > limit { print "old", $0; next }
    status[$2 " " $3] == "idle" || status[$2 " " $3] == "done" { print "try", $0; next }
    { print "keep", $0 }' - "$RC_OWED" <<<"$PANES") ||
    { echo "reconnect: skipped: $RC_OWED was not readable"; return 0; }
  while read -r action hs pane session since; do
    case "$action" in
      other) keep+="$hs $pane $session $since"$'\n' ;;
      keep) keep+="$hs $pane $session $since"$'\n'; mine=$((mine + 1)) ;;
      gone) echo "reconnect: $pane is gone or runs another session; not owed" ;;
      old) echo "reconnect: $pane stayed busy or showed no drop for ${RC_OWED_FOR}s; not owed" ;;
      try)
        if (($(date +%s) >= RC_DEADLINE)); then
          echo "reconnect: the ${RC_BUDGET}s budget is spent; $pane stays owed"
          keep+="$hs $pane $session $since"$'\n'; mine=$((mine + 1))
        elif ! screen=$(rc_step 10 "$HERDR" agent read "$pane" --format ansi) ||
          ! verdict=$(rc_read screen <<<"$screen"); then
          keep+="$hs $pane $session $since"$'\n'; mine=$((mine + 1))
        elif [[ "$verdict" != send ]]; then
          echo "reconnect: $pane $verdict"
          keep+="$hs $pane $session $since"$'\n'; mine=$((mine + 1))
        elif send_rc "$pane" "$session"; then
          echo "reconnect: sent /rc to $pane"
        else
          keep+="$hs $pane $session $since"$'\n'; mine=$((mine + 1))
        fi ;;
    esac
  done <<<"$plan"
  if [[ -z "$keep" ]]; then
    rm -f "$RC_OWED" 2>/dev/null || :
  else
    { printf '%s' "$keep" >"$RC_OWED.new" && mv "$RC_OWED.new" "$RC_OWED"; } 2>/dev/null ||
      echo "reconnect: could not update $RC_OWED"
    ((mine == 0)) ||
      echo "reconnect: $mine pane(s) still owe /rc; the next sync tries again"
  fi
}

# The hub: one `cswap auto` tick, then publish the active account. The row
# is written only when it differs (one database row per write), and is
# compared on every run, so a failed write is retried by the next one.
lead() {
  local rc=0 json active row number uuid
  # shellcheck disable=SC2086 # deliberate word splitting of the configured flags
  st_bounded 120 "$CSWAP_BIN" auto --once $CSWAP_AUTO_ARGS --strategy consume-first || rc=$?
  [[ "$rc" != 0 ]] || owe_rc
  json=$(accounts) || fail list "cswap list --json failed"
  active=$(pick active <<<"$json") || fail list "cswap names no single active account"
  read -r number uuid <<<"$active"
  set -- "$number" "$uuid"
  [[ "$2" =~ $UUID_RE ]] || fail list "account $1 has no organizationUuid"
  row=$(published 2>/dev/null) || row=""
  if [[ "$row" != "$2" ]]; then
    st_bounded 60 "$PACK/bin/sd" config set "$SETTING" "$2" >/dev/null ||
      fail publish "could not write $SETTING; satellites keep the last account"
    echo "leads: published account $1 ($2)"
  else
    echo "leads: account $1 ($2), already published"
  fi
  # --once exits 0 switched, 2 no action, 3 blocked (no viable target).
  case "$rc" in
    0 | 2 | 3) ;;
    *) fail auto "cswap auto exited $rc; account $1 stays published" ;;
  esac
}

# A read the hub refused for this machine's sd_db build: the follow job has
# not caught up yet. Or a hub this machine cannot reach: the tailnet dropped
# for a minute or two (sd:7650). Skip the run and count it; fail from the
# REFUSAL_LIMIT-th in a row. A count that cannot be written fails at once:
# skipping without a count would never fail. $1 is build or unreachable.
# The two causes share one count, so the failure names both and this run's
# (sd:7722).
refused_build() {
  local runs now skip
  if [[ "$1" == unreachable ]]; then
    now="the hub is unreachable"
    skip="skipped: the hub is unreachable"
  else
    now="the hub refuses this sd_db build"
    skip="skipped: the hub refuses this sd_db build until repo-sync.sh follow catches up"
  fi
  runs=$(cat "$REFUSALS" 2>/dev/null) || runs=0
  [[ "$runs" =~ ^[0-9]+$ ]] || runs=0
  runs=$((runs + 1))
  { mkdir -p "${REFUSALS%/*}" && printf '%s\n' "$runs" >"$REFUSALS"; } 2>/dev/null ||
    fail read "$now, and $REFUSALS cannot be written; nothing switched"
  if ((runs >= REFUSAL_LIMIT)); then
    fail read "$runs runs in a row could not read the hub's account, each one a refused sd_db build or an unreachable hub; this run: $now; check repo-sync.sh follow, Tailscale and the hub; nothing switched"
  fi
  echo "$skip (run $runs of $REFUSAL_LIMIT); nothing switched"
}

# A satellite: switch to the local account with the published UUID. An
# unreadable hub, an unset row and a UUID no single account here has all
# change nothing.
follow() {
  local want json active number rc=0 err
  err=$(mktemp)
  if ! want=$(published 2>"$err"); then
    cat "$err" >&2
    if grep -q "sd_db build mismatch" "$err"; then
      rm -f "$err"
      refused_build build
      return 0
    fi
    if grep -Eq "HubUnreachable|Tailscale does not name this node" "$err"; then
      rm -f "$err"
      refused_build unreachable
      return 0
    fi
    rm -f "$err"
    fail read "cannot read the hub's account ($SETTING); nothing switched"
  fi
  rm -f "$err"
  # A count left behind only makes the next refusal fail sooner.
  rm -f "$REFUSALS" 2>/dev/null || :
  json=$(accounts) || fail list "cswap list --json failed; nothing switched"
  active=$(pick active <<<"$json") || fail list "cswap names no single active account; nothing switched"
  number=$(pick number "$want" <<<"$json") || rc=$?
  case "$rc" in
    0) ;;
    4) fail map "no account here has organizationUuid $want; add the hub's account with cswap add" ;;
    5) fail map "$number accounts here have organizationUuid $want; nothing switched" ;;
    *) fail map "cswap list --json was not readable; nothing switched" ;;
  esac
  if [[ "${active%% *}" == "$number" ]]; then
    echo "following the hub: account $number already active"
    return 0
  fi
  st_bounded 60 "$CSWAP_BIN" switch "$number" >/dev/null ||
    fail switch "cswap switch $number failed; the next run tries again"
  echo "following the hub: switched to account $number"
  owe_rc
}

cmd_sync() {
  retire_agent
  [[ -x "$CSWAP_BIN" ]] || fail list "cswap not executable at $CSWAP_BIN"
  # The account first, then one reconnect pass for every owed pane, this
  # run's and an earlier run's: slow panes never delay a switch (sd:9282
  # lane review). A sync that fails exits before the pass, and its panes
  # stay owed for the next run.
  if leads; then lead; else follow; fi
  reconnect
}

cmd_status() {
  local json active want number uuid err
  if launchctl print "$RETIRED_SERVICE" >/dev/null 2>&1 || [[ -e "$RETIRED_PLIST" ]]; then
    echo "retired $RETIRED_LABEL agent still installed; the next cswap.sh sync removes it"
    exit 1
  fi
  if [[ ! -x "$CSWAP_BIN" ]]; then
    echo "cswap not installed at $CSWAP_BIN: nothing to check"
    exit 3
  fi
  json=$(accounts) || { echo "cswap list --json failed"; exit 1; }
  active=$(pick active <<<"$json") || { echo "cswap names no single active account"; exit 1; }
  read -r number uuid <<<"$active"
  set -- "$number" "$uuid"
  err=$(mktemp)
  if ! want=$(published 2>"$err"); then
    case "$(cat "$err")" in
      *"is not set"*) rm -f "$err"; echo "no account published in $SETTING yet: nothing to check"; exit 3 ;;
    esac
    echo "cannot read the hub's account ($SETTING): $(cat "$err")"
    rm -f "$err"
    exit 1
  fi
  rm -f "$err"
  if leads; then
    if [[ "$want" == "$2" ]]; then
      echo "leads: account $1, published"
      exit 0
    fi
    echo "leads: account $1 here, but $SETTING names another; the next sync publishes it"
    exit 1
  fi
  if [[ "$want" == "$2" ]]; then
    echo "following the hub, same account ($1)"
    exit 0
  fi
  if number=$(pick number "$want" <<<"$json"); then
    echo "account $1 here, the hub is on account $number"
  else
    echo "no account here has the hub's organizationUuid $want"
  fi
  exit 1
}

case "${1:-}" in
  sync)   cmd_sync ;;
  status) cmd_status ;;
  -h|--help|help)
    cat <<'HELPEOF'
usage: cswap.sh sync|status

  sync     the hub (sd-db fence reads holder) runs one
           `cswap auto --once --strategy consume-first` tick and publishes
           its active account's organizationUuid as sys.cswap_account;
           any other machine switches to its account with that id. An
           unreadable hub or an id no single account here has switches
           nothing and exits 1. A hub that refuses this machine's
           sd_db build skips the run (exit 0) until repo-sync.sh follow
           catches up, and fails from the 30th such run in a row. It
           also boots out the retired cswap-auto LaunchAgent. After a
           switch it sends /rc through herdr to each idle claude pane
           whose Remote Control dropped, and retries a busy one on the
           next sync; no herdr is one skip line.
           local-cron-jobs runs it every minute.
  status   whether this machine uses the hub's account; exits 0 same
           account / 1 another account, hub unreadable or the retired
           agent still installed / 3 nothing to check (no cswap, nothing
           published) — local-health-check reads these codes

environment: CSWAP_BIN, CSWAP_AUTO_ARGS (see cswap-auto.conf),
             SD_PACK_ROOT (default ~/repos/platypeeps/sd-workflow),
             CSWAP_HERDR_DIRS (where to look for herdr after PATH;
             default ~/.local/bin /opt/homebrew/bin /usr/local/bin),
             CSWAP_RC_BUDGET (seconds one reconnect pass may spend;
             default 60)
HELPEOF
    exit 0
    ;;
  *)
    echo "usage: $(basename "$0") sync|status" >&2
    exit 1
    ;;
esac

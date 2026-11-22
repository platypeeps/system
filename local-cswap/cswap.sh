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
#   cswap.sh keys     store one `claude setup-token` key per account (a terminal)
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
# owed for the next sync, until sent, gone or its switch not current. An
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

# Reads `cswap list --json` on stdin. `orgs` prints one
# `<email> <organizationUuid> <accounts in it>` line per account with an email;
# `active` prints "<number> <uuid>" of
# the active account; `number UUID` prints the number of the one account
# with that organizationUuid, exit 4 when none has it, 5 (and the count)
# when several do.
pick() { # active | number UUID
  python3 -I -c '
import json, sys
data = json.load(sys.stdin)
accounts = data.get("accounts") or []
if sys.argv[1] == "orgs":
    # Counted over every account, one with no email too: it still shares.
    orgs = [a.get("organizationUuid") or "-" for a in accounts]
    print("\n".join(a["email"] + " " + o + " " + str(orgs.count(o))
                    for a, o in zip(accounts, orgs) if a.get("email")))
elif sys.argv[1] == "active":
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
# The outcome is kept for the sync: a failed list is asked once and
# skipped once (sd:9427 triage).
herdr_panes() {
  [[ -z "$LISTED" ]] || return "$LISTED"
  herdr_list || LISTED=$?
  [[ -n "$LISTED" ]] || LISTED=0
  return "$LISTED"
}

herdr_list() {
  if ! HERDR=$(herdr_bin); then
    echo "reconnect: skipped: no herdr on PATH${HERDR_DIRS:+ or in $HERDR_DIRS}"
    return 2
  fi
  AGENTS=$(rc_step 20 "$HERDR" agent list) ||
    { echo "reconnect: skipped: herdr agent list did not answer within the budget; is herdr running?"; return 1; }
  PANES=$(rc_read panes <<<"$AGENTS") ||
    { echo "reconnect: skipped: herdr agent list gave no agent list this can parse"; return 3; }
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
    # `typed`, the read between "/ r c" and "enter" (sd:9427 ruling
    # #70122): Claude lists matching slash commands below the box, so
    # no cap on the footer there; a dialog line still waits.
    typed = sys.argv[1] == "typed"
    one_box = one_box and (typed or len(tail) <= 3) and all(footer(t) for t in tail)
    # Empty is a match, never what is left after deleting (sd:9427 class
    # pass): the line is exactly ❯ and its NBSP, plus at most dim text (a
    # prompt suggestion). No strip, as a draft can be blanks of any kind,
    # and no control sequence but colour, as a cursor move can stand for
    # typed blanks.
    line = raw[prompts[0]] if one_box else ""
    text = (undimmed(line, False)[0]
            if one_box and other.search(sgr.sub("", line)) is None else None)
    empty = text == "❯\u00a0"
    shows_rc = text is not None and re.fullmatch("❯[\u00a0 ]/rc[\u00a0 ]*", text) is not None
    if marks and marks[-1][1] == "active":
        print("wait: its latest Remote Control banner is active, maybe from the old account")
    elif not marks:
        print("wait: no Remote Control drop on its screen yet")
    elif not rules or not prompts:
        print("wait: no input box on its screen")
    elif not one_box:
        print("wait: its input box is not one empty line")
    elif typed:
        print("send" if shows_rc else "wait: its input box does not show exactly /rc")
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
# RC_LISTED, written only under RC_LOCK. Owed rows carry the mark's
# epoch, so each mark is later than the last: a second switch within
# one second would keep the first one's sent rows and owe nothing.
owe_rc() {
  local since prev
  since=$(date +%s)
  prev=$(head -n 1 "$RC_PENDING" 2>/dev/null) || prev=""
  prev=${prev%% *}
  [[ "$prev" =~ ^[0-9]+$ ]] && ((prev >= since)) && since=$((prev + 1))
  { mkdir -p "${RC_PENDING%/*}" && printf '%s %s\n' "$since" "$$" >"$RC_PENDING.$$" &&
    mv "$RC_PENDING.$$" "$RC_PENDING"; } 2>/dev/null ||
    echo "reconnect: could not write $RC_PENDING; no pane owes /rc for this switch"
}

# RC_OWED rows are "<herdr session> <pane> <session> <since>", and a pane
# sent /rc for that switch keeps its row with " sent" added, so no
# listing owes it again (sd:9427 review). "sent" is marked before the
# enter; "done" once the enter went, or a box no longer shows the /rc,
# so a sent row's box is read only until then (sd:9427 review).
# "typed" is marked before cswap types /rc into the pane: only such a
# row's box showing exactly /rc is cswap's typing and takes the enter;
# an operator's own /rc in an owed pane is theirs (sd:9427 review).
# The rows to keep, unchanged: other herdr sessions' rows, and this
# session's rows for the switch at $1; none if unreadable.
kept_rows() {
  [[ -e "$RC_OWED" ]] || return 0
  awk -v hs="$HERDR_SESSION" -v since="$1" '(NF == 4 || NF == 5 && ($5 == "typed" || $5 == "sent" || $5 == "done")) && ($1 != hs || $4 == since)' "$RC_OWED"
}

# Whether RC_OWED holds a row of this herdr session: an owed one, or a
# sent one whose /rc may still wait in the box for its enter.
owes_here() {
  awk -v hs="$HERDR_SESSION" '(NF == 4 || NF == 5 && ($5 == "typed" || $5 == "sent")) && $1 == hs { found = 1 } END { exit !found }' "$RC_OWED" 2>/dev/null
}

# A pending switch to this herdr session's owed panes, once herdr lists
# them, once per switch and session: RC_LISTED holds "<mark> <session>"
# for the mark the list was read for, so a switch that lands during the
# list is still unlisted at the next sync. A listing that meets a pane
# with no session id is incomplete, and the next sync lists again.
list_owed() {
  local mark since now rc=0 kept="" listed="" fresh unknown
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
  # This session's rows for an older switch go. Its rows for this one,
  # owed or sent, stay: a retry after an incomplete listing owes only
  # panes with no row, so a sent pane, its drop banner maybe still on
  # screen, gets no second /rc, which would disconnect it. The rows are
  # the one record of what this switch owed: one write, then the mark
  # (sd:9427 review).
  # A file that does not read is never replaced: its rows would go, and
  # this session stays unlisted, so the next sync tries again (sd:9427
  # review).
  if ! kept=$(kept_rows "$since" 2>/dev/null); then
    echo "reconnect: could not read $RC_OWED; the next sync tries again"
    return 0
  fi
  # The rows go by the environment: BSD awk refuses a newline in -v.
  fresh=$(RC_KEPT="$kept" awk -v hs="$HERDR_SESSION" -v since="$since" '
    BEGIN { n = split(ENVIRON["RC_KEPT"], r, "\n"); for (i = 1; i <= n; i++) { split(r[i], w, " "); if (w[1] == hs) owed[w[2]] = 1 } }
    NF == 3 && $2 != "?" && !($1 in owed) { print hs, $1, $2, since }' <<<"$PANES")
  unknown=$(awk 'NF == 3 && $2 == "?" { printf "%s%s", sep, $1; sep = ", " }' <<<"$PANES")
  # Each write in a group is chained: a group's status is its last
  # command's, and a failed first write would move a partial file in
  # (sd:9427 review).
  if ! { { { [[ -z "$kept" ]] || printf '%s\n' "$kept"; } &&
      { [[ -z "$fresh" ]] || printf '%s\n' "$fresh"; }
    } >"$RC_OWED.new" && mv "$RC_OWED.new" "$RC_OWED"; } 2>/dev/null; then
    echo "reconnect: could not write $RC_OWED; the next sync tries again"
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

# One line for the herdr call $1 on pane $2 that exited $3: the pane
# stays owed, and the cause named is the call's, not the pane's.
rc_failed() {
  local why="exit $3"
  (($3 != 124)) || why="exit 124: no answer within the budget or its bound"
  echo "reconnect: herdr agent $1 $2 failed ($why); still owed"
}

# Milliseconds since the epoch; bash 3.2 has no EPOCHREALTIME.
now_ms() { python3 -c 'import time; print(int(time.time() * 1000))'; }

# Right before a send (operator ruling 2026-11-18): a fresh read of the
# pane's ($1) screen says send, its row's switch ($3) is still current,
# the pane still runs the owed session ($2)
# and is idle or done, a last read shows the same screen, and the
# session is asked once more after it; then /rc.
# Else one line, and it stays owed (1); an enter that failed or ran
# late returns 2, as it may have landed. herdr has no check-and-send, so
# the span from the first read to the end of the typing must fit
# RC_GAP_MS. The enter is send_enter's.
send_rc() {
  local now got screen last verdict start took left bound=$(((RC_GAP_MS + 999) / 1000))
  # Screen, get, screen, get. A session that took the pane during a
  # read shows a drop and an empty box of its own, so a get follows
  # each read (sd:9427 review). A draft typed during the get leaves
  # session and status alone, so the screen is read again and must not
  # have changed (sd:9427 review). Each check goes stale while the next
  # call runs, so all must fit RC_GAP_MS.
  start=$(now_ms)
  screen=$(rc_step "$bound" "$HERDR" agent read "$1" --format ansi) ||
    { rc_failed read "$1" $?; return 1; }
  if ! verdict=$(rc_read screen <<<"$screen"); then
    echo "reconnect: $1's screen did not parse; still owed"
    return 1
  fi
  if [[ "$verdict" != send ]]; then
    echo "reconnect: $1 changed before the send ($verdict); still owed"
    return 1
  fi
  got=$(rc_step "$bound" "$HERDR" agent get "$1") ||
    { rc_failed get "$1" $?; return 1; }
  now=$(rc_read pane <<<"$got") || now=""
  if [[ "$now" != "$1 $2 idle" && "$now" != "$1 $2 done" ]]; then
    echo "reconnect: $1 changed before the send (${now:-no claude pane in the answer}); still owed"
    return 1
  fi
  last=$(rc_step "$bound" "$HERDR" agent read "$1" --format ansi) ||
    { rc_failed read "$1" $?; return 1; }
  if [[ "$last" != "$screen" ]]; then
    echo "reconnect: $1 changed before the send (its screen moved during the check); still owed"
    return 1
  fi
  # A new session can draw the same bytes, so the last read proves no
  # session: the session is asked once more after it (sd:9427 review).
  got=$(rc_step "$bound" "$HERDR" agent get "$1") ||
    { rc_failed get "$1" $?; return 1; }
  now=$(rc_read pane <<<"$got") || now=""
  if [[ "$now" != "$1 $2 idle" && "$now" != "$1 $2 done" ]]; then
    echo "reconnect: $1 changed before the send (${now:-no claude pane in the answer}); still owed"
    return 1
  fi
  took=$(($(now_ms) - start))
  if ((took > RC_GAP_MS)); then
    echo "reconnect: $1 took $took ms to check, over $RC_GAP_MS ms; still owed"
    return 1
  fi
  # The send counts too: herdr may take seconds to deliver, and a draft
  # typed meanwhile would take the /rc (sd:9427 review). Its bound is
  # the gap's time left, in milliseconds: herdr refuses --timeout
  # without --wait, and --wait waits for a turn /rc never starts
  # (herdr 0.9.5, sd:9427 review). Once the keys run, the /rc may have gone
  # out: herdr's timeout does not prove no delivery, and a drop banner
  # not yet redrawn would take a second /rc, which disconnects the pane
  # (sd:9427 review). So a failed or late send returns 2: sent, not owed.
  # Only a launched send may have gone out: the budget is checked here,
  # not in rc_step, whose 124 cannot tell an unlaunched call from a
  # timed-out one (sd:9427 triage).
  # A switch that landed during the pass makes this row an older
  # switch's: the newer one lists the pane (sd:9427 review). Typing
  # submits nothing, so a switch that lands after this read is caught
  # before the enter, which reads the switch again.
  if ! now=$(current_since); then
    echo "reconnect: could not read $RC_PENDING before the /rc to $1; still owed"
    return 1
  fi
  if [[ "$now" != "$3" ]]; then
    echo "reconnect: $1 changed before the send (a newer switch); the current one lists it"
    return 1
  fi
  # The gap and the budget are read again after the switch read, and
  # the typing takes only what is left (sd:9427 review).
  took=$(($(now_ms) - start))
  if ((took >= RC_GAP_MS)); then
    echo "reconnect: $1 took $took ms to check, over $RC_GAP_MS ms; still owed"
    return 1
  fi
  left=$((RC_DEADLINE - $(date +%s)))
  if ((left <= 0)); then
    echo "reconnect: the ${RC_BUDGET}s budget is spent before the /rc to $1; still owed"
    return 1
  fi
  # The row is marked typed after every check, right before the keys,
  # so a later pass knows a /rc in this box is cswap's; a mark with no
  # keys after it would let an operator's own /rc take the enter
  # (sd:9427 review). The mark counts in the gap, read once more after
  # it; past it, the mark goes. ponytail: a pass killed between the
  # mark and the keys still leaves one; herdr has no check-and-send.
  if ! mark_typed "$HERDR_SESSION $1 $2 $3"; then
    echo "reconnect: could not write $RC_OWED before typing /rc into $1; still owed"
    return 1
  fi
  RC_TYPED=1
  took=$(($(now_ms) - start))
  left=$((RC_DEADLINE - $(date +%s)))
  if ((took >= RC_GAP_MS || left <= 0)); then
    unmark_typed "$HERDR_SESSION $1 $2 $3"
    echo "reconnect: the gap or budget ran out before typing /rc into $1; still owed"
    return 1
  fi
  # To the millisecond: rounded up to a second, a send could deliver
  # past the gap (sd:9427 review).
  bound=$((RC_GAP_MS - took))
  ((left * 1000 < bound)) && bound=$((left * 1000))
  bound=$(printf '%d.%03d' $((bound / 1000)) $((bound % 1000)))
  # The keys go as two herdr send-keys calls, each written at once:
  # "/ r c", then, once a read shows exactly /rc in the box, "enter"
  # (operator ruling #70122 on sd:9427). agent prompt queued its Enter
  # 300 ms on with no deadline, so a killed client still delivered.
  # Exit 125 with the bound's refusal line means herdr never ran: a
  # denied process group (lib/bounded.sh), so nothing went out and the
  # pane stays owed (sd:9427 review). herdr's own 125 has no such line.
  local rc=0 err
  err=$(st_step "$bound" "$HERDR" agent send-keys "$1" / r c 9>&- 2>&1 >/dev/null) || rc=$?
  [[ -z "$err" ]] || printf '%s\n' "$err" >&2
  if ((rc == 125)) && [[ "$err" == *"st_bounded: process group denied"* ]]; then
    unmark_typed "$HERDR_SESSION $1 $2 $3"
    echo "reconnect: /rc to $1 never started (exit 125); still owed"
    return 1
  fi
  # Typed keys submit nothing: a typing that failed leaves the row
  # owed, and a box that shows exactly /rc on a later pass takes only
  # the enter (send_enter).
  if ((rc != 0)); then
    echo "reconnect: typing /rc into $1 failed or ran over $RC_GAP_MS ms (exit $rc); still owed"
    return 1
  fi
  send_enter "$1" "$2" "$3" mark
}

# The enter for a /rc typed into pane $1 (session $2, switch $3): only
# after a read that shows exactly /rc in the box under a drop banner,
# with session, status, screen and switch unchanged. A draft typed
# meanwhile shows there, and gets no enter. With $4 = mark, the row
# is marked sent first: written beside RC_OWED, the switch read
# again, then moved in right before the enter. A pass stopped before
# the enter leaves a row owed, or sent with /rc in the box; either
# way a later pass sees exactly /rc and sends the enter (sd:9427
# review). Before the mark: 1, still owed; after it: 2, sent.
# The read, the checks after it and the enter fit a gap of their own.
send_enter() {
  local now got screen last verdict start took left bound rc=0 out=1
  [[ "${4-}" == mark ]] || out=2
  start=$(now_ms)
  last=$(rc_step $(((RC_GAP_MS + 999) / 1000)) "$HERDR" agent read "$1" --format ansi) ||
    { echo "reconnect: typed /rc into $1, but its box then showed (no read, exit $?); no enter"; return $out; }
  verdict=$(rc_read typed <<<"$last") || verdict="a screen that did not parse"
  if [[ "$verdict" != send ]]; then
    echo "reconnect: typed /rc into $1, but its box then showed ($verdict); no enter"
    return $out
  fi
  # The read proves no target: a new session in the pane, or a pane
  # turned busy, would take the Enter, which can submit its input or
  # accept a dialog (sd:9427 review). So the session and status are
  # asked again after it, and the switch read again, as before the
  # typing. ponytail: a restart or a keystroke in the milliseconds from
  # the last read to the Enter still races; herdr has no check-and-send.
  got=$(rc_step $(((RC_GAP_MS + 999) / 1000)) "$HERDR" agent get "$1") ||
    { echo "reconnect: typed /rc into $1, but it changed before the enter (no get, exit $?); no enter"; return $out; }
  now=$(rc_read pane <<<"$got") || now=""
  if [[ "$now" != "$1 $2 idle" && "$now" != "$1 $2 done" ]]; then
    echo "reconnect: typed /rc into $1, but it changed before the enter (${now:-no claude pane in the answer}); no enter"
    return $out
  fi
  # A draft typed during that get leaves session and status alone, so
  # the screen is read again and must match the typed read, as before
  # the typing (sd:9427 review).
  screen=$(rc_step $(((RC_GAP_MS + 999) / 1000)) "$HERDR" agent read "$1" --format ansi) ||
    { echo "reconnect: typed /rc into $1, but it changed before the enter (no read, exit $?); no enter"; return $out; }
  if [[ "$screen" != "$last" ]]; then
    echo "reconnect: typed /rc into $1, but it changed before the enter (its screen moved during the check); no enter"
    return $out
  fi
  # A new session can draw the same /rc box, so the last read proves
  # no session: it is asked once more after it, as before the typing
  # (sd:9427 review).
  got=$(rc_step $(((RC_GAP_MS + 999) / 1000)) "$HERDR" agent get "$1") ||
    { echo "reconnect: typed /rc into $1, but it changed before the enter (no get, exit $?); no enter"; return $out; }
  now=$(rc_read pane <<<"$got") || now=""
  if [[ "$now" != "$1 $2 idle" && "$now" != "$1 $2 done" ]]; then
    echo "reconnect: typed /rc into $1, but it changed before the enter (${now:-no claude pane in the answer}); no enter"
    return $out
  fi
  if ! now=$(current_since) || [[ "$now" != "$3" ]]; then
    echo "reconnect: typed /rc into $1, but it changed before the enter (a newer switch, or $RC_PENDING did not read); no enter"
    return $out
  fi
  if [[ "${4-}" == mark ]]; then
    if ! { mark_sent "$HERDR_SESSION $1 $2 $3" && commit_sent; }; then
      echo "reconnect: could not write $RC_OWED before the enter; $1 stays owed"
      return 1
    fi
    # A switch takes no reconnect lock: one that landed during the
    # mark makes this row an older switch's (sd:9427 review). The
    # switch, gap and budget are read after the mark is in: a slow
    # rename used them up, and the enter went on older checks
    # (sd:9427 review).
    if ! now=$(current_since) || [[ "$now" != "$3" ]]; then
      echo "reconnect: typed /rc into $1, but it changed before the enter (a newer switch, or $RC_PENDING did not read); no enter"
      return 1
    fi
  fi
  took=$(($(now_ms) - start))
  left=$((RC_DEADLINE - $(date +%s)))
  bound=$((RC_GAP_MS - took))
  ((left * 1000 < bound)) && bound=$((left * 1000))
  if ((bound <= 0)); then
    echo "reconnect: typed /rc into $1, but the gap or budget ran out before the enter"
    return $out
  fi
  bound=$(printf '%d.%03d' $((bound / 1000)) $((bound % 1000)))
  st_step "$bound" "$HERDR" agent send-keys "$1" enter 9>&- >/dev/null || rc=$?
  if ((rc != 0)); then
    echo "reconnect: the enter for /rc in $1 failed or ran over $RC_GAP_MS ms (exit $rc); a box that still shows /rc takes it next sync"
    return 2
  fi
  mark_done "$HERDR_SESSION $1 $2 $3" || echo "reconnect: could not mark $1 done in $RC_OWED"
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

# The epoch of the current switch mark, or nothing when there is none
# or it is older than RC_OWED_FOR. Only rows of this switch may take a
# /rc: a newer switch lists its own, and a /rc from an older row before
# it would be a second one to a pane whose drop banner is not yet
# redrawn (sd:9427 review). A mark that is there and does not read
# exits 1: it says nothing of which switch is current, and reading it
# as none dropped every row of the current one (sd:9427 review).
current_since() {
  local mark since
  [[ -e "$RC_PENDING" ]] || return 0
  mark=$(head -n 1 "$RC_PENDING" 2>/dev/null) || return 1
  since=${mark%% *}
  [[ "$since" =~ ^[0-9]+$ ]] || return 1
  (($(date +%s) - since <= RC_OWED_FOR)) && echo "$since"
  return 0
}

# Mark the RC_OWED row $1 sent before its /rc goes: a later write that
# fails then loses a /rc at worst, and never owes a sent pane again
# (sd:9427 review). mark_sent writes the marked file beside RC_OWED
# and commit_sent moves it in, before the last switch, gap and budget
# checks. A send that does not happen keeps the row owed in the
# pass's last write.
mark_sent() {
  awk -v row="$1" '$0 == row || $0 == row " typed" { print row, "sent"; next } { print }' "$RC_OWED" >"$RC_OWED.sent" 2>/dev/null
}
# Mark the owed RC_OWED row $1 typed, in one rename; unmark_typed
# takes the mark off, and RC_TYPED says which one ran last.
mark_typed() {
  { awk -v typing="$1" '$0 == typing { print $0, "typed"; next } { print }' "$RC_OWED" >"$RC_OWED.typed" &&
    mv "$RC_OWED.typed" "$RC_OWED"; } 2>/dev/null
}
unmark_typed() {
  RC_TYPED=0
  { awk -v typing="$1" '$0 == typing " typed" { print typing; next } { print }' "$RC_OWED" >"$RC_OWED.typed" &&
    mv "$RC_OWED.typed" "$RC_OWED"; } 2>/dev/null
}
# Mark the sent RC_OWED row $1 done right after its enter went: kept
# sent until the pass's last write, a write that failed let a later
# resume enter an operator's own /rc (sd:9427 review).
mark_done() {
  { awk -v entered="$1" '$0 == entered " sent" { print entered, "done"; next } { print }' "$RC_OWED" >"$RC_OWED.done" &&
    mv "$RC_OWED.done" "$RC_OWED"; } 2>/dev/null
}
commit_sent() { mv "$RC_OWED.sent" "$RC_OWED" 2>/dev/null; }

# Send /rc to each owed pane that may have it; keep the rest owed.
reconnect_pass() {
  local hs pane session since action screen verdict plan cur keep="" mine=0 rc
  RC_DEADLINE=$(($(date +%s) + RC_BUDGET))
  list_owed
  owes_here || return 0
  herdr_panes || return 0
  # Rows of an older switch go unsent: a write of the newer switch's
  # rows that failed, or a switch during this sync's listing, leaves them.
  # A mark older than RC_OWED_FOR is no current switch: its rows go too.
  if ! cur=$(current_since); then
    echo "reconnect: could not read $RC_PENDING; every row stays for the next sync"
    return 0
  fi
  plan=$(awk -v hs="$HERDR_SESSION" -v cur="$cur" '
    FILENAME == "-" { if (NF == 3) { status[$1 " " $2] = $3; if ($2 == "?") unknown[$1] = 1 }; next }
    NF == 5 && ($5 == "sent" || $5 == "done") && $4 ~ /^[0-9]+$/ {
      if ($1 != hs) print "other", $0
      else if ($4 != cur) next
      # A sent pane whose box may still hold the /rc a stopped pass
      # typed gets its enter (sd:9427 review); send_enter checks it.
      else if ($5 == "sent" && (status[$2 " " $3] == "idle" || status[$2 " " $3] == "done")) print "resume", $0
      else print "sent", $0
      next
    }
    !(NF == 4 || NF == 5 && $5 == "typed") || $4 !~ /^[0-9]+$/ { next }
    $1 != hs { print "other", $0; next }
    $4 != cur { print "stale", $0; next }
    !(($2 " " $3) in status) && !($2 in unknown) { print "gone", $0; next }
    status[$2 " " $3] == "idle" || status[$2 " " $3] == "done" { print "try", $0; next }
    { print "keep", $0 }' - "$RC_OWED" <<<"$PANES") ||
    { echo "reconnect: skipped: $RC_OWED was not readable"; return 0; }
  local row
  while read -r action hs pane session since sent; do
    # An owed row as read, its typed mark kept.
    row="$hs $pane $session $since${sent:+ $sent}"
    case "$action" in
      other | sent) keep+="$hs $pane $session $since${sent:+ $sent}"$'\n' ;;
      resume)
        # ponytail: a box hidden by a dialog at this read counts as
        # done; reading the cause apart needs a verdict of its own.
        sent=sent
        if (($(date +%s) < RC_DEADLINE)) &&
          screen=$(rc_step 10 "$HERDR" agent read "$pane" --format ansi 2>/dev/null); then
          if [[ "$(rc_read typed <<<"$screen" 2>/dev/null)" != send ]]; then
            sent="done"
          elif send_enter "$pane" "$session" "$since"; then
            echo "reconnect: sent the enter for the /rc in $pane"
            sent="done"
          fi
        fi
        keep+="$hs $pane $session $since $sent"$'\n' ;;
      keep) keep+="$row"$'\n'; mine=$((mine + 1)) ;;
      gone) echo "reconnect: $pane is gone or runs another session; not owed" ;;
      stale) echo "reconnect: $pane was owed for a switch that is no longer current; not owed" ;;
      try)
        if (($(date +%s) >= RC_DEADLINE)); then
          echo "reconnect: the ${RC_BUDGET}s budget is spent; $pane stays owed"
          keep+="$row"$'\n'; mine=$((mine + 1))
        elif rc=0; screen=$(rc_step 10 "$HERDR" agent read "$pane" --format ansi) || rc=$?; ((rc != 0)); then
          rc_failed read "$pane" "$rc"
          keep+="$row"$'\n'; mine=$((mine + 1))
        elif ! verdict=$(rc_read screen <<<"$screen"); then
          echo "reconnect: $pane's screen did not parse; still owed"
          keep+="$row"$'\n'; mine=$((mine + 1))
        elif [[ "$verdict" != send && ( "$sent" != typed ||
          "$(rc_read typed <<<"$screen" 2>/dev/null)" != send ) ]]; then
          echo "reconnect: $pane $verdict"
          keep+="$row"$'\n'; mine=$((mine + 1))
        else
          # A box that shows exactly /rc, in a row marked typed, holds
          # the typing of a pass that stopped before the enter: it takes
          # only the enter. RC_TYPED says send_rc marked the row typed.
          rc=0 RC_TYPED=0
          if [[ "$verdict" == send ]]; then
            # An empty box holds no typing of cswap's: a typed mark
            # goes before anything else (sd:9427 review).
            if [[ "$sent" == typed ]]; then
              unmark_typed "$hs $pane $session $since"
              sent="" row="$hs $pane $session $since"
            fi
            send_rc "$pane" "$session" "$since" || rc=$?
          else
            send_enter "$pane" "$session" "$since" mark || rc=$?
          fi
          if ((rc == 1)); then
            ((RC_TYPED == 0)) || [[ "$sent" == typed ]] || row+=" typed"
            keep+="$row"$'\n'; mine=$((mine + 1))
          else
            sent=sent
            ((rc != 0)) || { echo "reconnect: sent /rc to $pane"; sent="done"; }
            keep+="$hs $pane $session $since $sent"$'\n'
          fi
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

# The Keychain service of an account's headless claude key, which
# ~/.config/shell/env.sh reads for the account cswap made active (sd:9905).
# An account is its email and its organization, so every item is also
# named by its account field, the organizationUuid: two cswap accounts
# with one email keep two items, and no lookup takes the other's.
key_item() { # email
  printf 'claude-code-oauth-token-%s\n' "$1"
}

# The organization a key belongs to, from the `anthropic-organization-id`
# header of one 1-token API call, then that call's HTTP status; nothing
# when the key does not work. A 429 that names the organization is a key
# that authenticated on an account at its usage limit (sd:9937).
# cswap names each account's organizationUuid, so this is how a key is
# matched to its account. The key reaches curl on stdin, never argv; -q,
# first, keeps ~/.curlrc out, since a trace option there would write the
# Authorization header to a file.
key_org() { # key
  printf 'header = "Authorization: Bearer %s"\n' "$1" |
    curl -q -sS --max-time 60 --config - -o /dev/null -D - \
      -H "anthropic-version: 2023-06-01" -H "anthropic-beta: oauth-2025-04-20" \
      -H "content-type: application/json" -d "$KEY_PROBE" \
      https://api.anthropic.com/v1/messages 2>/dev/null |
    tr -d '\r' | awk 'NR == 1 { code = $2; if (code != 200 && code != 429) exit }
      tolower($1) == "anthropic-organization-id:" { print $2, code; exit }'
}
KEY_PROBE='{"model":"claude-haiku-4-5","max_tokens":1,"system":"You are Claude Code, Anthropic'"'"'s official CLI for Claude.","messages":[{"role":"user","content":"OK"}]}'

# One `claude setup-token` key per cswap account: read hidden from the
# terminal, the first copy of a repeated paste kept, and stored only when
# one API call works and names that account's organization, so a key
# pasted at the wrong prompt overwrites no other account's item. A setup
# token names no user (the profile API refuses it), so accounts that share
# an organization get no key here. The key
# goes in no argv, file or output: curl and `security -i` read it on
# stdin. CSWAP_KEYS_TTY is for tests only.
cmd_keys() {
  local json pairs e want raw rest tok held got code probe works shared
  # A caller's `bash -x` would trace each key; nothing after this needs it.
  set +x
  [[ -x "$CSWAP_BIN" ]] || { echo "cswap not executable at $CSWAP_BIN" >&2; exit 1; }
  json=$(accounts) || { echo "cswap list --json failed" >&2; exit 1; }
  pairs=$(pick orgs <<<"$json") || { echo "cswap list --json was not readable" >&2; exit 1; }
  exec 3<"${CSWAP_KEYS_TTY:-/dev/tty}" 4<<<"$pairs"
  while read -r -u 4 e want shared; do
    if [[ ! "$e" =~ ^[^\"[:space:]]+$ ]]; then
      echo "$e: an email this script cannot quote; skipped"
      continue
    fi
    if [[ ! "$want" =~ $UUID_RE ]]; then
      echo "$e: cswap names no organizationUuid for it; skipped"
      continue
    fi
    # A key names its organization, not its member: in an organization two
    # accounts share, either could own it, so neither is asked for one.
    if ((shared > 1)); then
      echo "$e: $shared accounts share its organization, so a key cannot be matched to one; nothing stored"
      continue
    fi
    printf 'Key for %s (Enter to skip): ' "$e" >&2
    raw=""
    IFS= read -r -s -u 3 raw || :
    echo >&2
    raw="${raw//[[:space:]]/}"
    rest="${raw#*sk-ant-}"
    tok=""
    [[ "$rest" == "$raw" ]] || tok="sk-ant-${rest%%sk-ant-*}"
    unset raw rest
    if [[ -z "$tok" ]]; then
      echo "$e: skipped"
      continue
    fi
    if [[ ! "$tok" =~ ^sk-ant-[A-Za-z0-9_-]+$ ]]; then
      echo "$e: the paste is no key; nothing stored"
      continue
    fi
    # A failed curl is a failed probe, whatever headers it wrote first:
    # the status is checked before a here-string can drop it.
    got="" code="" probe=""
    probe=$(key_org "$tok") || probe=""
    read -r got code <<<"$probe" || :
    works="works"
    [[ "$code" != 429 ]] || works="works (account at its usage limit)"
    if [[ -z "$got" ]]; then
      echo "$e: the key did not work; nothing stored"
    elif [[ "$got" != "$want" ]]; then
      echo "$e: the key belongs to another account; nothing stored"
    else
      # Stored means the item now holds this key: an old item kept by a
      # failed update must not pass, and `security -i` may exit 0 then.
      held=""
      if printf 'add-generic-password -U -a "%s" -s "%s" -w "%s"\n' "$want" "$(key_item "$e")" "$tok" |
          security -i >/dev/null 2>&1; then
        held=$(security find-generic-password -s "$(key_item "$e")" -a "$want" -w 2>/dev/null) || held=""
      fi
      if [[ -n "$held" && "$held" == "$tok" ]]; then
        echo "$e: $works; stored as $(key_item "$e")"
      else
        echo "$e: $works, but the Keychain did not store it"
      fi
      unset held
    fi
    unset tok
  done
  exec 3<&- 4<&-
}

# A WARN line per account with no headless key, the email only. It changes
# no exit code: a missing key leaves that account's headless runs on the
# plain item.
warn_keys() { # cswap list --json
  local e org shared
  command -v security >/dev/null 2>&1 || return 0
  while read -r e org shared; do
    [[ -n "$e" ]] || continue
    security find-generic-password -s "$(key_item "$e")" -a "$org" >/dev/null 2>&1 ||
      echo "WARN no headless claude key for $e; run cswap.sh keys in a terminal"
  done <<<"$(pick orgs <<<"$1" 2>/dev/null)"
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
  warn_keys "$json"
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
  keys)   cmd_keys ;;
  -h|--help|help)
    cat <<'HELPEOF'
usage: cswap.sh sync|status|keys

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
           published) — local-health-check reads these codes. It also
           prints a WARN line per account with no headless claude key.
  keys     in a terminal: for each cswap account, read its
           `claude setup-token` key hidden, keep the first copy of a
           repeated paste, and store it as Keychain item
           claude-code-oauth-token-<email>, account <organizationUuid>,
           only when one API call works
           and names that account's organization (accounts that share
           one get none); ~/.config/shell/env.sh loads the active account's key

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
    echo "usage: $(basename "$0") sync|status|keys" >&2
    exit 1
    ;;
esac

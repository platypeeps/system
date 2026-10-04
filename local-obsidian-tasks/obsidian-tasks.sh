#!/bin/sh
# Nightly digest of due/overdue Obsidian TaskNotes tasks, emailed as HTML
# with per-task buttons: Open (obsidian:// link), Mark done and Postpone
# (signed links served by local-task-actions on this machine).
# A task counts when frontmatter says "status: open" and "scheduled:" is
# today or earlier — recurring tasks work too, since the TaskNotes plugin
# advances "scheduled" to the next occurrence on completion.
# Usage: obsidian-tasks.sh run|list|test
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
. "$DIR/../lib/config.sh"
NOTIFY="$DIR/../local-notify/notify.sh"
ACTIONS="$DIR/../local-task-actions/task-actions.sh"
JEV="$DIR/../local-jev/jev.sh"

# <config>/obsidian-tasks/.env (outside the checkout; <config> is $SYSTEM_TOOLS_CONFIG,
# default ~/.config/system; see .env.example) provides defaults only; an exported
# OBSIDIAN_VAULT wins.
ENV_OBSIDIAN_VAULT="${OBSIDIAN_VAULT:-}"
st_source_env obsidian-tasks
[ -n "$ENV_OBSIDIAN_VAULT" ] && OBSIDIAN_VAULT="$ENV_OBSIDIAN_VAULT"
VAULT="${OBSIDIAN_VAULT:-$HOME/Documents/Obsidian Vault}"
TASKS_SUBDIR="${OBSIDIAN_TASKS_SUBDIR:-TaskNotes/Tasks}"

case "${1:-}" in
  run|list)
    MODE="$1"
    ;;
  test)
    shift
    exec "${PYTHON:-python3}" -m unittest discover -s "$DIR/tests" -t "$DIR" "$@"
    ;;
  -h|--help|help)
    cat <<'HELPEOF'
usage: obsidian-tasks.sh run|list|test

  list  print due/overdue open tasks (scheduled today or earlier) with
        their obsidian:// links; prints nothing when none are due
  run   list, and when tasks are due email an HTML digest through
        local-notify's email channel (what the obsidian-tasks-nightly
        cron job runs); exits 1 only when that email could not be sent.
        Each task gets Open / Mark done / Postpone buttons, served by
        local-task-actions through its Tailscale Funnel URL so they work
        from any device. Action links expire after 7 days.
  test  run the unittest suite in tests/, offline

The digest asks Jev to order it, so the task that matters most is the first
card in the mail instead of the oldest one. It runs unless
JEV_OBSIDIAN_TASKS is set to 0, off, false, no or disabled -- unset means on -- and unless
`jev enabled` declines, it never changes which tasks appear or what their
buttons do, and every failure falls back to today's order with the reason on
stderr. Only each task's title, its
scheduled date and how many days overdue it is leave this machine.

environment:
  OBSIDIAN_VAULT          vault path (default: ~/Documents/Obsidian Vault;
                          also read from <config>/obsidian-tasks/.env,
                          see .env.example)
  OBSIDIAN_TASKS_SUBDIR   task folder inside the vault (default: TaskNotes/Tasks)
  JEV_OBSIDIAN_TASKS      0, off, false, no or disabled switches the digest's Jev ordering
                          off; unset means on
  OBSIDIAN_TASKS_TODAY    YYYY-MM-DD the digest counts due and overdue from
                          (default: today); the test suite pins it
  OBSIDIAN_TASKS_SIGN_TIMEOUT
                          seconds one signed link may take (default: 15); a
                          link that runs out is tried once more, then the
                          digest keeps Open-only links and says why on stderr
HELPEOF
    exit 0
    ;;
  *)
    echo "usage: $(basename "$0") run|list|test" >&2
    exit 1
    ;;
esac

TASKS_DIR="$VAULT/$TASKS_SUBDIR"
[ -d "$TASKS_DIR" ] || { echo "obsidian-tasks.sh: no task folder at $TASKS_DIR" >&2; exit 1; }

TMPD=$(mktemp -d)
trap 'rm -rf "$TMPD"' EXIT INT TERM

body=$(VAULT="$VAULT" TASKS_DIR="$TASKS_DIR" TASKS_SUBDIR="$TASKS_SUBDIR" \
       ACTIONS="$ACTIONS" JEV="$JEV" HTML_OUT="$TMPD/body.html" python3 <<'PYEOF'
import datetime, html, json, os, pathlib, re, subprocess, sys, tempfile, urllib.parse

vault = pathlib.Path(os.environ["VAULT"])
tasks_dir = pathlib.Path(os.environ["TASKS_DIR"])
subdir = os.environ["TASKS_SUBDIR"]
actions = os.environ["ACTIONS"]
# OBSIDIAN_TASKS_TODAY pins the day "due" and "overdue" count from. Unset is
# the real date. The suite pins it because the fixture's scheduled dates and
# this script's "today" come from two processes, and a run that crosses
# midnight would see every task one day more overdue than the golden.
_pinned = os.environ.get("OBSIDIAN_TASKS_TODAY", "")
today = (datetime.date.fromisoformat(_pinned) if _pinned
         else datetime.date.today())
vault_name = vault.name

def field(text, name):
    m = re.search(rf"^{name}:\s*[\"']?(.*?)[\"']?\s*$", text, re.M)
    return m.group(1) if m else ""

# `task-actions.sh url` probes `tailscale funnel status` for its base URL
# unless TASK_ACTIONS_BASE_URL is set, and this digest signs three links a
# card. So the base is discovered once, through `base-url`, and every later
# signature is built on it. A stall on either call is a bounded failure: the
# digest keeps Open-only links for the rest of the run instead of dying on
# `subprocess.TimeoutExpired` (sd:1770; local-obsidian-review did the same
# in sd:1203).
# A signature costs a `sh` and a `python3` start: 0.1 s on an idle machine.
# The nightly slots share the machine with agent jobs, and there it took over
# 5 s: load1 was 131 at 07:28 on 2026-10-03 and the 07:30 review went
# unsigned; the 03:00 task digest went unsigned on 10-01 and 10-03 (sd:2536).
# Nothing in the call waits on a daemon, a keychain or the network once the
# base is known, so the wait is most likely CPU. The bound is 15 s, a call that runs out
# is tried once more, and the fallback says why, with the load averages.
# OBSIDIAN_TASKS_SIGN_TIMEOUT sets the bound in seconds.
try:
    ACTION_URL_TIMEOUT = int(os.environ.get("OBSIDIAN_TASKS_SIGN_TIMEOUT") or 15)
except ValueError:
    ACTION_URL_TIMEOUT = 15
ACTION_URL_TRIES = 2
BASE_URL_TIMEOUT = 10

#: Empty until `signer_base()` runs; ``[""]`` means discovery failed.
_base = []

#: Set by the first signing failure, so a slow signer costs one bounded wait a
#: run, not one a button.
_signer_down = []


def signer_base():
    """The base URL for signed links, resolved once per run; "" on failure."""
    if _base:
        return _base[0]
    why = ""
    try:
        r = subprocess.run(["sh", actions, "base-url"],
                           capture_output=True, text=True,
                           timeout=BASE_URL_TIMEOUT)
        base = r.stdout.strip() if r.returncode == 0 else ""
        if not base:
            why = "base-url exited %d" % r.returncode
    except subprocess.TimeoutExpired:
        base = ""
        why = "base-url did not answer within %ds" % BASE_URL_TIMEOUT
        sys.stderr.write(
            "obsidian-tasks.sh: %s base-url did not answer within %ds\n"
            % (actions, BASE_URL_TIMEOUT))
    except OSError as exc:
        base = ""
        why = "%s could not run: %s" % (actions, exc.strerror or exc)
    if not base:
        sys.stderr.write(
            "obsidian-tasks.sh: action links unsigned -- no base URL could be "
            "discovered (%s, %s); the digest keeps Open-only links for this "
            "run\n" % (why, load_text()))
    _base.append(base)
    return base


def action_url(stem, action, days="3"):
    # Signed by local-task-actions; empty string when that tool is absent or
    # unreachable, so the digest degrades to Open-only buttons instead of failing.
    if _signer_down:
        return ""
    base = signer_base()
    if not base:
        return ""
    env = dict(os.environ, TASK_ACTIONS_BASE_URL=base)
    for _ in range(ACTION_URL_TRIES):
        try:
            r = subprocess.run(["sh", actions, "url", stem, action, days],
                               capture_output=True, text=True, env=env,
                               timeout=ACTION_URL_TIMEOUT)
        except subprocess.TimeoutExpired:
            continue
        except OSError as exc:
            return unsigned("%s could not run: %s" % (actions, exc.strerror or exc))
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
        # The exit code only: the signer's stderr is not copied into a log.
        return unsigned("%s url exited %d" % (actions, r.returncode))
    return unsigned("%s did not sign within %ds, %d times"
                    % (actions, ACTION_URL_TIMEOUT, ACTION_URL_TRIES))


def load_text():
    try:
        return "load averages %.1f %.1f %.1f" % os.getloadavg()
    except OSError:
        return "load averages unknown"


def unsigned(why):
    """Retire the signer for this run, say why once, and give an empty link."""
    _signer_down.append(True)
    sys.stderr.write(
        "obsidian-tasks.sh: action links unsigned -- %s (%s); the digest keeps "
        "Open-only links for this run\n" % (why, load_text()))
    return ""

# Jev orders the digest; it never decides what is in it. It runs unless
# `jev enabled JEV_OBSIDIAN_TASKS` declines -- unkeyed, the fleet switch off,
# or that variable switching this stage off -- so a machine that never heard
# of Jev mails exactly the digest it mailed yesterday. Jev is
# experimental: nothing here may depend on it, so every path out of this
# function is "today's order", and none of them is silent.
URGENCY = ["can wait", "this week", "today", "right now"]


def jev_record_baseline(jev, cause=None):
    """Say the digest's usual order -- the control arm -- is what ran.

    `--outcome ok` always: the old path completed, and that this row exists
    at all is the fact nothing else carries.

    `cause` is the word only when `jev` never wrote its own row: the ask was
    killed at its `timeout=180`, and `jev` flushes after the answer is
    printed and installs no signal handler, so a killed call leaves nothing
    behind. A `jev` that returned -- a non-zero exit, an answer this cannot
    read -- has already written the cause, and repeating it counts one
    decision as two, because `judgment.py`'s DECLINES groups by stage and
    cause across both arms with no deduplication.

    Bookkeeping only: `jev record` sends nothing, needs no key, prints
    nothing and always exits 0, and every failure here is swallowed. A digest
    must never fail over its own measurement.
    """
    try:
        subprocess.run(["sh", jev, "record",
                        "--caller", "local-obsidian-tasks",
                        "--stage", "JEV_OBSIDIAN_TASKS",
                        "--arm", "baseline", "--outcome", "ok"]
                       + (["--decline", cause] if cause else []),
                       capture_output=True, text=True, timeout=30)
    except Exception:
        pass


def jev_ranked(order):
    """Return `order` sorted most-urgent-first, or `order` unchanged.

    `order` is the list the digest would print anyway. Only the title, the
    scheduled date and the days overdue go out with the question -- no file
    path, no note body, no vault name -- because every Jev call leaves this
    machine.
    """
    if not order:
        return order, False
    jev = os.environ.get("JEV", "")
    asked = False
    try:
        # Both halves in one call: Jev can answer here, and JEV_OBSIDIAN_TASKS
        # has not been used to switch this stage off. Unset means on.
        # `--record` so the decline is counted: most nights end at this gate,
        # and the digest's usual order running is the only fact the control
        # arm has.
        probe = subprocess.run(["sh", jev, "enabled", "JEV_OBSIDIAN_TASKS",
                                "--record", "--caller", "local-obsidian-tasks"],
                               capture_output=True,
                               text=True, timeout=30)
        if probe.returncode != 0:
            # No second row: the gate wrote one in the process this call
            # already started, and `judgment.py` counts gate events on their
            # own line. `asked` stays False so the handler below knows.
            raise RuntimeError("jev cannot answer on this machine")
        items, questions = [], {}
        for i, (d, prio, title, link, stem) in enumerate(order):
            qid = "t%d" % i
            items.append({"id": qid, "title": title,
                          "scheduled": d.isoformat(),
                          "days_overdue": (today - d).days})
            questions[qid] = {
                "type": "score",
                "instructions": (
                    'How urgent is the task with id "%s" in items, next to '
                    "the other tasks in the same list?" % qid),
                "criteria": URGENCY,
            }
        # One request, every task a question in it: questions in one `ask`
        # run in parallel, where one call per task would pay the round trip
        # once per line of the mail.
        with tempfile.TemporaryDirectory() as td:
            qf, sf = os.path.join(td, "q.json"), os.path.join(td, "s.json")
            with open(qf, "w") as fh:
                json.dump(questions, fh)
            with open(sf, "w") as fh:
                json.dump({"today": today.isoformat(), "items": items}, fh)
            asked = True
            r = subprocess.run(
                ["sh", jev, "ask", "--questions", qf, "--state", sf,
                 "--state-format", "json",
                 "--caller", "local-obsidian-tasks",
                 "--stage", "JEV_OBSIDIAN_TASKS"],
                capture_output=True, text=True, timeout=180)
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout).strip()[:200]
                               or "jev ask exited %d" % r.returncode)
        answers = json.loads(r.stdout)["answers"]
        # A partial answer is a failure, not a ranking: ordering half the
        # mail by urgency and half by accident is worse than not trying.
        scores = {qid: float(answers[qid]["score"]) for qid in questions}
    except Exception as exc:
        sys.stderr.write("obsidian-tasks.sh: jev did not rank this digest "
                         "(%s); keeping the usual order\n" % exc)
        if asked:
            jev_record_baseline(
                jev,
                "timeout" if isinstance(exc, subprocess.TimeoutExpired)
                else None)
        return order, False
    ranked = sorted(((-scores["t%d" % i], i, item)
                     for i, item in enumerate(order)), key=lambda t: t[:2])
    return [item for _, _, item in ranked], True


due = []
for f in sorted(tasks_dir.glob("*.md")):
    head = f.read_text(errors="replace")
    m = re.match(r"(?s)^---\n(.*?)\n---", head)
    if not m:
        continue
    fm = m.group(1)
    if field(fm, "status") != "open":
        continue
    sched = field(fm, "scheduled")[:10]
    try:
        d = datetime.date.fromisoformat(sched)
    except ValueError:
        continue
    if d > today:
        continue
    title = field(fm, "title") or f.stem
    prio = field(fm, "priority") or "normal"
    link = "obsidian://open?vault=%s&file=%s" % (
        urllib.parse.quote(vault_name),
        urllib.parse.quote(f"{subdir}/{f.stem}"))
    due.append((d, prio, title, link, f.stem))

if not due:
    raise SystemExit(0)

due.sort()
due, by_jev = jev_ranked(due)
lines = []
overdue = sum(1 for d, *_ in due if d < today)
today_n = len(due) - overdue
lines.append(f"{len(due)} open task(s) need attention ({overdue} overdue, {today_n} due today)\n")

BTN = ('<a href="{href}" style="display:inline-block;padding:7px 14px;'
       'margin:0 6px 6px 0;border-radius:6px;text-decoration:none;'
       'font-size:13px;font-weight:600;color:#fff;background:{bg}">{label}</a>')
cards = []
for d, prio, title, link, stem in due:
    age = (today - d).days
    when = "due today" if age == 0 else f"{age} day(s) overdue"
    ptag = "" if prio == "normal" else f" [{prio}]"
    lines.append(f"- {title}{ptag} — scheduled {d}, {when}")
    lines.append(f"  {link}")

    when_color = "#c0392b" if age > 0 else "#2c7a2c"
    # Gmail strips custom-scheme hrefs, so Open goes through the service's
    # https /open redirect when available and falls back to the raw
    # obsidian:// URI otherwise.
    open_url = action_url(stem, "open") or link
    buttons = [BTN.format(href=html.escape(open_url), bg="#7c5cbf", label="Open in Obsidian")]
    done_url = action_url(stem, "done")
    post_url = action_url(stem, "postpone", "3")
    if done_url:
        buttons.append(BTN.format(href=html.escape(done_url), bg="#2c7a2c", label="✓ Mark done"))
    if post_url:
        buttons.append(BTN.format(href=html.escape(post_url), bg="#8a6d1a", label="Postpone 3 days"))
    cards.append(
        '<div style="border:1px solid #ddd;border-radius:8px;padding:14px 16px;'
        'margin:0 0 12px 0;background:#fafafa">'
        f'<div style="font-size:15px;font-weight:600;margin-bottom:2px">'
        f'{html.escape(title)}{html.escape(ptag)}</div>'
        f'<div style="font-size:12px;color:#666;margin-bottom:10px">scheduled '
        f'{d} · <span style="color:{when_color};font-weight:600">{html.escape(when)}</span></div>'
        + "".join(buttons) + '</div>')

if by_jev:
    lines.append("(ordered by Jev, most urgent first)")

doc = (
    '<div style="font-family:-apple-system,Helvetica,Arial,sans-serif;'
    'max-width:560px;margin:0 auto">'
    f'<p style="font-size:14px">{len(due)} open task(s) need attention '
    f'(<b>{overdue} overdue</b>, {today_n} due today)</p>'
    + "".join(cards) +
    '<p style="font-size:11px;color:#999">Buttons work from any device '
    '(Mark done / Postpone ask for one confirming tap) and expire after '
    '7 days.'
    + (' Ordered by Jev, most urgent first.' if by_jev else '')
    + '</p></div>')
open(os.environ["HTML_OUT"], "w").write(doc)
print("\n".join(lines))
PYEOF
)

if [ -z "$body" ]; then
  echo "no due or overdue tasks"
  exit 0
fi

printf '%s\n' "$body"

if [ "$MODE" = "list" ]; then
  exit 0
fi

n=$(printf '%s\n' "$body" | head -1 | awk '{print $1}')
subject="Obsidian tasks: $n due/overdue"
if sh "$NOTIFY" -t "$subject" -k brief -c ntfy,email -b -f html "$(cat "$TMPD/body.html")"; then
  echo "email sent"
  exit 0
fi
echo "email FAILED — exiting 1 so the cron failure push fires" >&2
exit 1

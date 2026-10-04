#!/bin/sh
# Daily digest of vault decision queues (Blog Ideas, Skill Proposals,
# Tips and Tricks, Topics, Market Watch), emailed as HTML with per-note
# buttons: Open plus the decision actions (Accept/Decline, Track/Pause,
# Adopt/Park) served as signed links by local-task-actions. Flipping a
# status here is the input the nightly accept routines act on.
# Usage: obsidian-review.sh run|list|test
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
. "$DIR/../lib/config.sh"
NOTIFY="$DIR/../local-notify/notify.sh"
ACTIONS="$DIR/../local-task-actions/task-actions.sh"
JEV="$DIR/../local-jev/jev.sh"

# <config>/obsidian-review/.env (outside the checkout; <config> is $SYSTEM_TOOLS_CONFIG,
# default ~/.config/system; see .env.example) provides defaults only; an exported
# OBSIDIAN_VAULT wins.
ENV_OBSIDIAN_VAULT="${OBSIDIAN_VAULT:-}"
st_source_env obsidian-review
[ -n "$ENV_OBSIDIAN_VAULT" ] && OBSIDIAN_VAULT="$ENV_OBSIDIAN_VAULT"
VAULT="${OBSIDIAN_VAULT:-$HOME/Documents/Obsidian Vault}"

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
usage: obsidian-review.sh run|list|test

  list  print every note waiting on a decision, per database; prints
        nothing when all queues are empty
  run   list, and when anything is waiting email an HTML digest through
        local-notify's email channel (what the obsidian-review-daily
        cron job runs); exits 1 only when that email could not be sent.
  test  run the unittest suite in tests/, offline

queues and their buttons (each button is one signed status flip served
by local-task-actions; the nightly accept routines do the heavy work):

  Blog Ideas       status inbox      -> Accept / Decline
  Skill Proposals  status proposed   -> Accept / Decline
  Tips and Tricks  status inbox      -> Accept / Decline
                   (tips already `ready` are counted in the footer —
                   the final call on those happens in Obsidian)
  Topics           status candidate  -> Adopt / Park
  Market Watch     status candidate  -> Track / Pause / Decline

The digest asks Jev to order it, so the decision most worth making is the
first card in the mail rather than the highest-scored note of whichever queue
is listed first. It runs unless JEV_OBSIDIAN_REVIEW is set to 0, off, false, no or disabled
-- unset means on -- and unless `jev enabled` declines, it never changes which
notes appear or what their buttons do, and every failure falls back to
today's order with the reason on stderr. Only each note's title, its queue and its
added date leave this machine.

environment:
  OBSIDIAN_VAULT       vault path (default: ~/Documents/Obsidian Vault;
                       also read from <config>/obsidian-review/.env,
                          see .env.example)
  JEV_OBSIDIAN_REVIEW  0, off, false, no or disabled switches the digest's Jev ordering off;
                       unset means on
  OBSIDIAN_REVIEW_TODAY  YYYY-MM-DD the digest counts note ages from
                       (default: today); the test suite pins it
  OBSIDIAN_REVIEW_SIGN_TIMEOUT
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

TMPD=$(mktemp -d)
trap 'rm -rf "$TMPD"' EXIT INT TERM

body=$(VAULT="$VAULT" ACTIONS="$ACTIONS" JEV="$JEV" HTML_OUT="$TMPD/body.html" python3 <<'PYEOF'
import datetime, html, json, os, pathlib, re, subprocess, sys, tempfile, urllib.parse

vault = pathlib.Path(os.environ["VAULT"])
actions = os.environ["ACTIONS"]
# OBSIDIAN_REVIEW_TODAY pins the day ages are counted from. Unset is the
# real date. The suite pins it because the fixture's added dates and this
# script's "today" come from two processes: a run that crosses midnight saw
# every age one day older than the golden (runs 35935415274, 35935854678).
_pinned = os.environ.get("OBSIDIAN_REVIEW_TODAY", "")
today = (datetime.date.fromisoformat(_pinned) if _pinned
         else datetime.date.today())

# db key -> (section title, folder, decide-status, [(action, label, color)])
QUEUES = [
    ("blog", "Blog Ideas", "System/Databases/Blog Ideas", "inbox",
     [("accept", "✓ Accept", "#2c7a2c"), ("decline", "✗ Decline", "#8a3030")]),
    ("skill", "Skill Proposals", "System/Databases/Skill Proposals", "proposed",
     [("accept", "✓ Accept", "#2c7a2c"), ("decline", "✗ Decline", "#8a3030")]),
    ("tip", "Tips and Tricks", "System/Databases/Tips and Tricks", "inbox",
     [("accept", "✓ Accept", "#2c7a2c"), ("decline", "✗ Decline", "#8a3030")]),
    ("topic", "Topics", "System/Databases/Topics", "candidate",
     [("adopt", "✓ Adopt", "#2c7a2c"), ("park", "⏸ Park", "#666666")]),
    ("watch", "Market Watch", "System/Databases/Market Watch", "candidate",
     [("track", "▶ Track", "#2c7a2c"), ("pause", "⏸ Pause", "#8a6d1a"),
      ("decline", "✗ Decline", "#8a3030")]),
]

def field(fm, name):
    m = re.search(rf"^{name}:\s*[\"']?(.*?)[\"']?\s*$", fm, re.M)
    return m.group(1) if m else ""

# `task-actions.sh url` signs a URL locally; it never speaks to the service on
# port 8766, so the service being up says nothing about this call. What it does
# do is shell out to `tailscale funnel status` to learn the public base URL, and
# a stalled tailscaled makes that call unbounded. Until this was bounded, the
# timeout raised `subprocess.TimeoutExpired`, which `except OSError` does not
# catch, and the whole digest died instead of degrading.
#
# Bounding the inner call alone was not enough, and that is what review round 1
# caught. A bounded probe that fails still returns a localhost base URL and
# exit 0, so nothing outside could tell a stalled daemon from a machine with no
# funnel: each of the three or four buttons on each of sixty cards paid the
# timeout again -- around ten minutes -- and every link it produced pointed at
# 127.0.0.1, which works on this machine and nowhere the digest gets read.
#
# So the base URL is discovered exactly once, through `task-actions.sh
# base-url`, and its failure is a failure the caller can see. One answer, one
# probe, and the rest of the run signs against a URL already in hand.
# A signature costs a `sh` and a `python3` start: 0.1 s on an idle machine.
# The nightly slots share the machine with agent jobs, and there it took over
# 5 s: load1 was 131 at 07:28 on 2026-10-03 and the 07:30 review went
# unsigned; the 03:00 task digest went unsigned on 10-01 and 10-03 (sd:2536).
# Nothing in the call waits on a daemon, a keychain or the network once the
# base is known, so the wait is most likely CPU. The bound is 15 s, a call that runs out
# is tried once more, and the fallback says why, with the load averages.
# OBSIDIAN_REVIEW_SIGN_TIMEOUT sets the bound in seconds.
try:
    ACTION_URL_TIMEOUT = int(os.environ.get("OBSIDIAN_REVIEW_SIGN_TIMEOUT") or 15)
except ValueError:
    ACTION_URL_TIMEOUT = 15
ACTION_URL_TRIES = 2
BASE_URL_TIMEOUT = 10

#: Empty until `signer_base()` runs. ``[""]`` means discovery failed and the
#: digest builds Open-only links for the rest of this run; ``["https://..."]``
#: is the base every later signature is built on, passed back down through
#: TASK_ACTIONS_BASE_URL so the daemon is never probed again.
_base = []

#: Set by the first signing failure. Caching discovery answers the daemon
#: stall; it does not answer a signer that is slow for some other reason --
#: each call still starts a Python interpreter and reads the secret file, and
#: a machine under load or a secret on a stalled mount makes that slow too.
#: Without this, 192 buttons times a signing timeout is many minutes of
#: waiting for an answer the first call already gave.
_signer_down = []


def signer_base():
    """The base URL for signed links, resolved once per run.

    Returns "" when discovery could not answer, which is the signal to keep
    Open-only links. A machine with no funnel is not a failure: `base-url`
    answers `http://127.0.0.1:8766` and exits 0, the same as it always did.
    """
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
            "obsidian-review.sh: %s base-url did not answer within %ds\n"
            % (actions, BASE_URL_TIMEOUT))
    except OSError as exc:
        base = ""
        why = "%s could not run: %s" % (actions, exc.strerror or exc)
    if not base:
        sys.stderr.write(
            "obsidian-review.sh: action links unsigned -- no base URL could be "
            "discovered (%s, %s); the digest keeps Open-only links for this "
            "run\n" % (why, load_text()))
    _base.append(base)
    return base


def action_url(db, stem, action):
    # Signed by local-task-actions; empty string when that tool is absent or
    # unreachable, so the digest degrades to Open-only links instead of failing.
    if _signer_down:
        return ""
    base = signer_base()
    if not base:
        return ""
    env = dict(os.environ, TASK_ACTIONS_BASE_URL=base)
    for _ in range(ACTION_URL_TRIES):
        try:
            r = subprocess.run(["sh", actions, "url", "-b", db, stem, action],
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
        "obsidian-review.sh: action links unsigned -- %s (%s); the digest keeps "
        "Open-only links for this run\n" % (why, load_text()))
    return ""

BTN = ('<a href="{href}" style="display:inline-block;padding:7px 14px;'
       'margin:0 6px 6px 0;border-radius:6px;text-decoration:none;'
       'font-size:13px;font-weight:600;color:#fff;background:{bg}">{label}</a>')

# Jev orders the digest; it never decides what is in it. It runs unless
# `jev enabled JEV_OBSIDIAN_REVIEW` declines -- unkeyed, the fleet switch off,
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
                        "--caller", "local-obsidian-review",
                        "--stage", "JEV_OBSIDIAN_REVIEW",
                        "--arm", "baseline", "--outcome", "ok"]
                       + (["--decline", cause] if cause else []),
                       capture_output=True, text=True, timeout=30)
    except Exception:
        pass


def jev_ranked(queues):
    """Reorder the cards the digest already chose, or hand them back as-is.

    It is handed the shown lists *after* the twelve-card cap, so the set of
    notes in the mail is fixed before Jev ever sees it: this can move a card
    up, and cannot put one in or take one out. Only the note title, its
    queue and its added date go out -- no description, no file path, no note
    body, no vault name -- because every Jev call leaves this machine.
    """
    if not queues:
        return queues, False
    jev = os.environ.get("JEV", "")
    asked = False
    try:
        # Both halves in one call: Jev can answer here, and JEV_OBSIDIAN_REVIEW
        # has not been used to switch this stage off. Unset means on.
        # `--record` so the decline is counted: most nights end at this gate,
        # and the digest's usual order running is the only fact the control
        # arm has.
        probe = subprocess.run(["sh", jev, "enabled", "JEV_OBSIDIAN_REVIEW",
                                "--record", "--caller", "local-obsidian-review"],
                               capture_output=True,
                               text=True, timeout=30)
        if probe.returncode != 0:
            # No second row: the gate wrote one in the process this call
            # already started, and `judgment.py` counts gate events on their
            # own line. `asked` stays False so the handler below knows.
            raise RuntimeError("jev cannot answer on this machine")
        items, questions = [], {}
        for qi, q in enumerate(queues):
            for i, (stem, desc, score, created, age) in enumerate(q[5]):
                qid = "n%d_%d" % (qi, i)
                items.append({"id": qid, "queue": q[1], "title": stem,
                              "added": created})
                questions[qid] = {
                    "type": "score",
                    "instructions": (
                        'How much is the decision on the note with id "%s" '
                        "in items worth making today, next to the other "
                        "notes in the same list?" % qid),
                    "criteria": URGENCY,
                }
        if not questions:
            return queues, False
        # One request, every note a question in it: questions in one `ask`
        # run in parallel, where one call per note would pay the round trip
        # once per card in the mail.
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
                 "--caller", "local-obsidian-review",
                 "--stage", "JEV_OBSIDIAN_REVIEW"],
                capture_output=True, text=True, timeout=180)
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout).strip()[:200]
                               or "jev ask exited %d" % r.returncode)
        answers = json.loads(r.stdout)["answers"]
        # A partial answer is a failure, not a ranking: ordering half the
        # mail by urgency and half by accident is worse than not trying.
        scores = {qid: float(answers[qid]["score"]) for qid in questions}
    except Exception as exc:
        sys.stderr.write("obsidian-review.sh: jev did not rank this digest "
                         "(%s); keeping the usual order\n" % exc)
        if asked:
            jev_record_baseline(
                jev,
                "timeout" if isinstance(exc, subprocess.TimeoutExpired)
                else None)
        return queues, False
    out = []
    for qi, q in enumerate(queues):
        picked = [scores["n%d_%d" % (qi, i)] for i in range(len(q[5]))]
        cards = sorted(((-picked[i], i, it) for i, it in enumerate(q[5])),
                       key=lambda t: t[:2])
        q = list(q)
        q[5] = [it for _, _, it in cards]
        # The queue itself rides on its best card, so the first section of
        # the mail is the one holding the decision worth making first.
        out.append((-max(picked), qi, q))
    out.sort(key=lambda t: t[:2])
    return [q for _, _, q in out], True


lines, sections, total = [], [], 0
ready_tips = 0
queues = []
for db, title, sub, decide, acts in QUEUES:
    folder = vault / sub
    items = []
    for f in sorted(folder.glob("*.md")):
        m = re.match(r"(?s)^---\n(.*?)\n---", f.read_text(errors="replace"))
        if not m:
            continue
        fm = m.group(1)
        status = field(fm, "status")
        if db == "tip" and status == "ready":
            ready_tips += 1
        if status != decide:
            continue
        desc = field(fm, "description")
        score = field(fm, "score")
        created = field(fm, "dateCreated")[:10]
        try:
            age = (today - datetime.date.fromisoformat(created)).days
        except ValueError:
            age = None
        items.append((f.stem, desc, score, created, age))
    if not items:
        continue
    total += len(items)
    # Highest-scored first; cap the cards so a backlog (Blog Ideas sits at
    # 50+) doesn't produce an unscrollable email. The count above the cap
    # is stated, never hidden. The cap picks from this order and no other,
    # so the optional Jev ranking below reorders the cards it chose and can
    # never change which twelve they are.
    def rank(it):
        try:
            return -float(it[2])
        except ValueError:
            return 0.0
    items.sort(key=rank)
    shown, rest = items[:12], len(items) - 12
    queues.append((db, title, sub, acts, len(items), shown, rest))

queues, by_jev = jev_ranked(queues)

for db, title, sub, acts, count, shown, rest in queues:
    lines.append(f"{title}: {count} awaiting a decision")
    cards = []
    for stem, desc, score, created, age in shown:
        lines.append(f"- {stem}" + (f" (score {score})" if score else ""))
        link = "obsidian://open?vault=%s&file=%s" % (
            urllib.parse.quote(vault.name), urllib.parse.quote(f"{sub}/{stem}"))
        open_url = action_url(db, stem, "open") or link
        buttons = [BTN.format(href=html.escape(open_url), bg="#7c5cbf", label="Open")]
        for a, label, bg in acts:
            u = action_url(db, stem, a)
            if u:
                buttons.append(BTN.format(href=html.escape(u), bg=bg, label=label))
        meta = " · ".join(x for x in (
            f"score {score}" if score else "",
            (f"added {created}" + (f" ({age}d ago)" if age else ""))
            if created else "") if x)
        cards.append(
            '<div style="border:1px solid #ddd;border-radius:8px;padding:14px 16px;'
            'margin:0 0 12px 0;background:#fafafa">'
            f'<div style="font-size:15px;font-weight:600;margin-bottom:2px">'
            f'{html.escape(stem)}</div>'
            + (f'<div style="font-size:13px;color:#444;margin-bottom:4px">'
               f'{html.escape(desc)}</div>' if desc else "")
            + (f'<div style="font-size:12px;color:#666;margin-bottom:10px">'
               f'{html.escape(meta)}</div>' if meta else "")
            + "".join(buttons) + '</div>')
    if rest > 0:
        cards.append(f'<p style="font-size:12px;color:#888">… and {rest} '
                     'more in Obsidian</p>')
    sections.append(
        f'<h3 style="font-size:16px;margin:18px 0 8px 0">{html.escape(title)}'
        f' <span style="color:#888;font-weight:400">({count})</span></h3>'
        + "".join(cards))

if total == 0:
    raise SystemExit(0)

if by_jev:
    lines.append("(ordered by Jev, most worth deciding first)")

footer = ('<p style="font-size:11px;color:#999">Each decision button asks for '
          'one confirming tap; links work from any device and expire after '
          '7 days. The nightly routines act on what you accept.'
          + (f' {ready_tips} tip(s) are `ready` and wait for your final call '
             'in Obsidian.' if ready_tips else '')
          + (' Ordered by Jev, most worth deciding first.' if by_jev else '')
          + '</p>')
doc = ('<div style="font-family:-apple-system,Helvetica,Arial,sans-serif;'
       'max-width:560px;margin:0 auto">'
       f'<p style="font-size:14px"><b>{total}</b> note(s) across '
       f'{len(sections)} queue(s) wait for a decision</p>'
       + "".join(sections) + footer + '</div>')
open(os.environ["HTML_OUT"], "w").write(doc)
print(f"{total} awaiting decisions")
print("\n".join(lines))
PYEOF
)

if [ -z "$body" ]; then
  echo "all decision queues empty"
  exit 0
fi

printf '%s\n' "$body"

if [ "$MODE" = "list" ]; then
  exit 0
fi

n=$(printf '%s\n' "$body" | head -1 | awk '{print $1}')
subject="Review queue: $n decision(s) waiting"
if sh "$NOTIFY" -t "$subject" -k brief -c ntfy,email -b -f html "$(cat "$TMPD/body.html")"; then
  echo "email sent"
  exit 0
fi
echo "email FAILED — exiting 1 so the cron failure push fires" >&2
exit 1

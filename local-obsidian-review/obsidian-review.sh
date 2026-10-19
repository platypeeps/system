#!/bin/sh
# Daily digest of the decision queues (Blog Ideas, Skill Proposals,
# Tips and Tricks, Topics, Market Watch), filed as a brief on the dashboard's
# Today. It reads the base rows in the workflow database, their source since
# pack sd:4627, never the vault notes (sd:5134). Each card opens the row on the
# dashboard's Tasks page, where its stage moves; the nightly accept routines
# act on that stage.
# Usage: obsidian-review.sh run|list|test
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
. "$DIR/../lib/config.sh"
# The command pack's lib/ holds sd_db, as for `sd-db` (sd:3279).
PACK_LIB="${SD_PACK_ROOT:-$HOME/repos/platypeeps/sd-ai-command-pack}/lib"
JEV="$DIR/../local-jev/jev.sh"
# One id for every jev call this run makes, so the ledger groups them
# (sd:2953). A run that started this one keeps its own: it is the same run.
[ -n "${JEV_RUN:-}" ] || \
  JEV_RUN="obsidian-review-$(date -u +%Y%m%dT%H%M%S)-$(od -An -N2 -tx1 /dev/urandom | tr -d ' \n')"
export JEV_RUN

# <config>/obsidian-review/.env (outside the checkout; <config> is $SYSTEM_TOOLS_CONFIG,
# default ~/.config/system) provides defaults only; an exported value wins.
st_source_env obsidian-review

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

  list  print every row waiting on a decision, per queue; prints
        nothing when all queues are empty
  run   list, and when anything is waiting file the digest as the day's
        `Review Queue` brief (`sd reports brief add`), shown on the
        dashboard's Today (what the obsidian-review-daily cron job runs);
        exits 1 when the base rows cannot be read or the brief could not
        be filed.
  test  run the unittest suite in tests/, offline

queues, read from the workflow database's base rows (`sd_db.records`), and
the stage that waits on a decision; each card's Open link is the row on
the dashboard's Tasks page, where its stage moves:

  Blog Ideas       sdw.blog-idea      stage inbox
  Skill Proposals  sdw.skill-proposal stage proposed
  Tips and Tricks  sdw.tip            stage inbox
                   (tips already `ready` are counted in the footer)
  Topics           sdw.topic          stage candidate
  Market Watch     sdw.market         stage candidate

The digest asks Jev to order it, so the decision most worth making is the
first card in the brief rather than the highest-scored note of whichever queue
is listed first. It runs unless JEV_OBSIDIAN_REVIEW is set to 0, off, false, no or disabled
-- unset means on -- and unless `jev enabled` declines, it never changes which
rows appear or where their links go, and every failure falls back to
today's order with the reason on stderr. Only each row's title, its queue and its
added date leave this machine.

environment:
  SD_PACK_ROOT         the command pack checkout whose lib/ holds sd_db
                       (default: ~/repos/platypeeps/sd-ai-command-pack)
  JEV_OBSIDIAN_REVIEW  0, off, false, no or disabled switches the digest's Jev ordering off;
                       unset means on
  OBSIDIAN_REVIEW_TODAY  YYYY-MM-DD the digest counts row ages from
                       (default: today); the test suite pins it
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

body=$(PACK_LIB="$PACK_LIB" JEV="$JEV" BRIEF_DIR="$TMPD" python3 <<'PYEOF'
import datetime, hashlib, json, os, pathlib, subprocess, sys, tempfile

# OBSIDIAN_REVIEW_TODAY pins the day ages are counted from. Unset is the
# real date. The suite pins it because the fixture's added dates and this
# script's "today" come from two processes: a run that crosses midnight saw
# every age one day older than the golden (runs 35935415274, 35935854678).
_pinned = os.environ.get("OBSIDIAN_REVIEW_TODAY", "")
today = (datetime.date.fromisoformat(_pinned) if _pinned
         else datetime.date.today())

# base kind -> (section title, the stage that waits on a decision). The kind
# is the `sdw` plugin's; its rows are `sd_db.records` rows (pack sd:3973).
QUEUES = [
    ("blog-idea", "Blog Ideas", "inbox"),
    ("skill-proposal", "Skill Proposals", "proposed"),
    ("tip", "Tips and Tricks", "inbox"),
    ("topic", "Topics", "candidate"),
    ("market", "Market Watch", "candidate"),
]


def base_rows():
    """{kind: rows} for every queue, from the workflow database.

    A database or library that cannot answer stops the run: an empty read
    would file "all decision queues empty", a negative the run never saw.
    """
    sys.path.insert(0, os.environ["PACK_LIB"])
    try:
        from sd_db import records
        from sd_db.database import connect
        connection = connect(write=False)
        try:
            return {kind: records.list_rows(connection, kind) for kind, _, _ in QUEUES}
        finally:
            connection.close()
    except Exception as error:
        sys.exit("obsidian-review.sh: the base rows could not be read, so nothing "
                 "is known about the queues: %s" % error)


def text(value):
    return "" if value is None else str(value)


# Jev orders the digest; it never decides what is in it. It runs unless
# `jev enabled JEV_OBSIDIAN_REVIEW` declines -- unkeyed, the fleet switch off,
# or that variable switching this stage off -- so a machine that never heard
# of Jev files exactly the digest it filed yesterday. Jev is
# experimental: nothing here may depend on it, so every path out of this
# function is "today's order", and none of them is silent.
URGENCY = ["can wait", "this week", "today", "right now"]


def jev_subject(stems):
    """The ledger's name for one ordering: `obsidian-review:<16 hex>` (sd:2953).

    No title leaves in it. The key is the first 16 hex of the sha256 of the
    ordered rows' titles, sorted, each followed by a newline. An outcome
    recomputes it from the stems the digest showed, as
    `printf '%s\\n' STEM... | LC_ALL=C sort | shasum -a 256 | cut -c1-16`.
    """
    text = "".join(stem + "\n" for stem in sorted(stems))
    return "obsidian-review:" + hashlib.sha256(text.encode()).hexdigest()[:16]


def jev_record_baseline(jev, cause=None, subject=None):
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
                       + (["--decline", cause] if cause else [])
                       + (["--subject", subject] if subject else []),
                       capture_output=True, text=True, timeout=30)
    except Exception:
        pass


def jev_ranked(queues):
    """Reorder the cards the digest already chose, or hand them back as-is.

    It is handed the shown lists *after* the twelve-card cap, so the set of
    notes in the brief is fixed before Jev ever sees it: this can move a card
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
            for i, (stem, desc, score, created, age, _) in enumerate(q[3]):
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
        subject = jev_subject([it[0] for q in queues for it in q[3]])
        # One request, every note a question in it: questions in one `ask`
        # run in parallel, where one call per note would pay the round trip
        # once per card in the brief.
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
                 "--stage", "JEV_OBSIDIAN_REVIEW",
                 "--subject", subject],
                capture_output=True, text=True, timeout=180)
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout).strip()[:200]
                               or "jev ask exited %d" % r.returncode)
        answers = json.loads(r.stdout)["answers"]
        # A partial answer is a failure, not a ranking: ordering half the
        # brief by urgency and half by accident is worse than not trying.
        scores = {qid: float(answers[qid]["score"]) for qid in questions}
    except Exception as exc:
        sys.stderr.write("obsidian-review.sh: jev did not rank this digest "
                         "(%s); keeping the usual order\n" % exc)
        if asked:
            jev_record_baseline(
                jev,
                "timeout" if isinstance(exc, subprocess.TimeoutExpired)
                else None, subject)
        return queues, False
    out = []
    for qi, q in enumerate(queues):
        picked = [scores["n%d_%d" % (qi, i)] for i in range(len(q[3]))]
        cards = sorted(((-picked[i], i, it) for i, it in enumerate(q[3])),
                       key=lambda t: t[:2])
        q = list(q)
        q[3] = [it for _, _, it in cards]
        # The queue itself rides on its best card, so the first section of
        # the brief is the one holding the decision worth making first.
        out.append((-max(picked), qi, q))
    out.sort(key=lambda t: t[:2])
    return [q for _, _, q in out], True


lines, sections, total = [], [], 0
ready_tips = 0
queues = []
rows = base_rows()
for kind, title, decide in QUEUES:
    items = []
    for row in rows[kind]:
        if kind == "tip" and row["stage"] == "ready":
            ready_tips += 1
        if row["stage"] != decide:
            continue
        fields = row["fields"]
        created = text(fields.get("dateCreated"))[:10]
        try:
            age = (today - datetime.date.fromisoformat(created)).days
        except ValueError:
            age = None
        items.append((row["title"], text(fields.get("description")), text(fields.get("score")),
                      created, age, row["id"]))
    if not items:
        continue
    total += len(items)
    # Highest-scored first; cap the cards so a backlog (Blog Ideas sits at
    # 50+) doesn't produce an unscrollable brief. The count above the cap
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
    queues.append((kind, title, len(items), shown, rest))

queues, by_jev = jev_ranked(queues)

for kind, title, count, shown, rest in queues:
    lines.append(f"{title}: {count} awaiting a decision")
    cards = []
    for stem, desc, score, created, age, item in shown:
        lines.append(f"- {stem}" + (f" (score {score})" if score else ""))
        meta = " · ".join(x for x in (
            f"score {score}" if score else "",
            (f"added {created}" + (f" ({age}d ago)" if age else ""))
            if created else "") if x)
        # The dashboard opens an item at /tasks?row=<id> (sd:3526); its stage moves there.
        cards.append(f"- **{stem}**" + (f" · {desc}" if desc else "")
                     + (f" · {meta}" if meta else "") + f" · [\u2197 Open](/tasks?row={item})")
    if rest > 0:
        cards.append(f"- … and {rest} more on Tasks")
    sections.append(f"## {title} ({count})\n\n" + "\n".join(cards))

if total == 0:
    raise SystemExit(0)

if by_jev:
    lines.append("(ordered by Jev, most worth deciding first)")

footer = ("Open a card to move its stage on Tasks. The nightly routines act on what you accept."
          + (f" {ready_tips} tip(s) are `ready` and wait for your final call." if ready_tips else "")
          + (" Ordered by Jev, most worth deciding first." if by_jev else ""))
# The brief's stem is `<day> - <Series>`, the name `sd reports brief add` files it under (sd:3459).
brief = pathlib.Path(os.environ["BRIEF_DIR"]) / f"{today.isoformat()} - Review Queue.md"
brief.write_text("---\njob: obsidian-review-daily\n---\n\n"
                 f"**{total}** item(s) across {len(sections)} queue(s) wait for a decision.\n\n"
                 + "\n\n".join(sections) + "\n\n" + footer + "\n")
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

# The digest is a brief in the workflow database, shown on the dashboard's
# Today (operator ruling 2026-10-17), not a mail. A rerun the same day
# replaces that day's brief.
for brief in "$TMPD"/*.md; do :; done
if sd reports brief add --belongs-to "$(cd "$DIR/.." && pwd)" --replace "$brief"; then
  echo "brief filed"
  exit 0
fi
echo "brief NOT filed — exiting 1 so the run's report needs attention" >&2
exit 1

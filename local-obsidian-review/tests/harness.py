"""A whole fake repo root in a temp dir, so the suite never touches the vault.

The tool resolves its siblings from its own directory (`$DIR/../local-jev`),
so the cheapest way to stub `local-jev`, `local-notify` and
`local-task-actions` is to copy the entrypoint next to stubs of them. That
keeps the shipped script free of test-only seams: nothing in it knows a test
exists, and nothing here reaches the network. The one input the harness hands
it is the date, in OBSIDIAN_REVIEW_TODAY, and that is an ordinary setting.
"""

from __future__ import annotations

import datetime
import os
import pathlib
import shutil
import subprocess

HERE = pathlib.Path(__file__).resolve().parent
TOOL_DIR = HERE.parent
TOOL = TOOL_DIR / "obsidian-review.sh"
FIXTURES = HERE / "fixtures"

#: The one date a run uses. `build_root` writes the notes' added dates from
#: it, `run` hands it to the script, and `templated` reads it back, so a run
#: that crosses midnight cannot see two different days. It used to be read
#: here and again by the script, and the two disagreed once a CI run crossed
#: 00:00: every "(Nd ago)" came out one day older than the golden.
TODAY = datetime.date.today()

# folder -> notes as (stem, status, score, days since it was added). Only the
# statuses the tool calls "awaiting a decision" may ever reach a card.
QUEUE_NOTES = {
    "Blog Ideas": [("blog-%02d" % i, "inbox", float(i), i) for i in range(1, 15)]
                  + [("blog-accepted", "accepted", 9.0, 4)],
    "Skill Proposals": [("skill-one", "proposed", 5.0, 2),
                        ("skill-two", "proposed", 7.0, 3),
                        ("skill-done", "accepted", 8.0, 9)],
    "Tips and Tricks": [("tip-new", "inbox", 4.0, 1),
                        ("tip-ready-a", "ready", 6.0, 5),
                        ("tip-ready-b", "ready", 6.0, 5)],
    "Topics": [("topic-one", "candidate", 3.0, 6),
               ("topic-two", "candidate", 9.0, 7)],
    "Market Watch": [("watch-one", "candidate", 2.0, 8)],
}
# The queues a digest shows, in the order it shows them today.
EXPECTED_SECTIONS = ["Blog Ideas", "Skill Proposals", "Tips and Tricks",
                     "Topics", "Market Watch"]
# Blog Ideas is over the twelve-card cap on purpose: the cap must pick the
# same twelve whether or not Jev ranked the mail.
EXPECTED_SHOWN = {
    "Blog Ideas": ["blog-%02d" % i for i in range(14, 2, -1)],
    "Skill Proposals": ["skill-two", "skill-one"],
    "Tips and Tricks": ["tip-new"],
    "Topics": ["topic-two", "topic-one"],
    "Market Watch": ["watch-one"],
}
EXPECTED_TOTAL = 20

STUB_NOTIFY = """#!/bin/sh
# Records what the digest would have mailed, and never mails anything.
: > "$NOTIFY_RECORD"
for a in "$@"; do printf '%s\\n' "$a" >> "$NOTIFY_RECORD"; done
exit "${NOTIFY_EXIT:-0}"
"""

# Exits non-zero, which is exactly how the real tool behaves when it is not
# installed: the digest degrades to Open-only links made from obsidian://.
#
# `ACTIONS_STUB_HANG` makes it block instead, which is how the real tool behaves
# when `tailscale funnel status` waits on a daemon that never answers. The
# digest has to bound that and say what it could not reach, not sit there until
# the signer's own timeout kills the run.
#
# `ACTIONS_STUB_RECORD` names a file this stub appends one line to per call:
# the verb, then the base URL it was handed. That is what proves the daemon is
# asked once a run rather than once a button -- the failure review round 1
# found, where every call paid the timeout again and signed a 127.0.0.1 link.
# `ACTIONS_STUB_SIGNS` makes it answer instead of refusing, so the recording
# covers the path a working signer takes, and `ACTIONS_STUB_SIGN_HANG` blocks
# only the `url` verb: discovery answers, signing does not. That is the second
# stall, the one caching the base URL does nothing about.
STUB_ACTIONS = """#!/bin/sh
if [ -n "${ACTIONS_STUB_RECORD:-}" ]; then
  printf '%s %s\\n' "$1" "${TASK_ACTIONS_BASE_URL:-<unset>}" >> "$ACTIONS_STUB_RECORD"
fi
if [ -n "${ACTIONS_STUB_HANG:-}" ]; then sleep "$ACTIONS_STUB_HANG"; fi
if [ "$1" = url ] && [ -n "${ACTIONS_STUB_SIGN_HANG:-}" ]; then
  sleep "$ACTIONS_STUB_SIGN_HANG"
fi
# Only the first `url` call is slow: mkdir succeeds once (sd:2536).
if [ "$1" = url ] && [ -n "${ACTIONS_STUB_FIRST_SIGN_HANG:-}" ] &&
   mkdir "${ACTIONS_STUB_STATE:?}/first-sign" 2>/dev/null; then
  sleep "$ACTIONS_STUB_FIRST_SIGN_HANG"
fi
if [ "$1" = url ] && [ -n "${ACTIONS_STUB_SIGN_EXIT:-}" ]; then exit "$ACTIONS_STUB_SIGN_EXIT"; fi
if [ -n "${ACTIONS_STUB_SIGNS:-}" ]; then
  case "$1" in
    base-url) printf '%s\\n' "$ACTIONS_STUB_SIGNS"; exit 0 ;;
    url)      printf '%s/task?stub=1\\n' "${TASK_ACTIONS_BASE_URL:?url called with no base}"; exit 0 ;;
  esac
fi
exit 1
"""

STUB_JEV = """#!/bin/sh
case "$1" in
  enabled)
    # `jev enabled STAGE` reads the stage variable itself, so the stub has to
    # as well: without this a caller that switched its stage off is ordered.
    if [ -n "${2:-}" ]; then
      eval "word=\\${$2:-}"
      case "$(printf %s "$word" | tr 'A-Z' 'a-z')" in
        0|off|false|no|disabled) exit 3 ;;
      esac
    fi
    exit "${JEV_STUB_ENABLED_EXIT:-0}" ;;
  ask)
    if [ "${JEV_STUB_ASK_FAIL:-0}" = 1 ]; then
      echo "jev: stubbed failure" >&2
      exit 1
    fi
    shift
    exec python3 "$(dirname "$0")/answer.py" "$@"
    ;;
esac
echo "stub jev: unexpected verb $1" >&2
exit 1
"""

# Answers every question with a score, ranking the state's items in exactly
# the reverse of the order they arrived in. A reversal is the clearest thing
# to assert against: if the digest is reordered at all, it is reordered by
# what came back and not by chance.
STUB_ANSWER = """import json, os, sys

args = sys.argv[1:]
opts = {}
while args:
    key = args.pop(0)
    if key.startswith("--"):
        opts[key] = args.pop(0) if args and not args[0].startswith("--") else ""
questions = json.load(open(opts["--questions"]))
state = json.load(open(opts["--state"]))
record = os.environ.get("JEV_STUB_RECORD")
if record:
    json.dump({"questions": questions, "state": state}, open(record, "w"))
items = state["items"]
scores = {it["id"]: float(i + 1) for i, it in enumerate(items)}
answers = {qid: {"score": scores.get(qid, 0.0)} for qid in questions}
json.dump({"answers": answers}, sys.stdout)
"""


def build_root(tmp: str, today: datetime.date = TODAY) -> pathlib.Path:
    """A fake repo root holding the tool, its stubbed siblings and a vault."""
    root = pathlib.Path(tmp)
    (root / "local-obsidian-review").mkdir()
    shutil.copy(TOOL, root / "local-obsidian-review" / TOOL.name)
    (root / "lib").mkdir()
    shutil.copy(TOOL.parents[1] / "lib" / "config.sh", root / "lib" / "config.sh")
    for folder, name, text in (
        ("local-notify", "notify.sh", STUB_NOTIFY),
        ("local-task-actions", "task-actions.sh", STUB_ACTIONS),
        ("local-jev", "jev.sh", STUB_JEV),
    ):
        (root / folder).mkdir()
        path = root / folder / name
        path.write_text(text)
        path.chmod(0o755)
    (root / "local-jev" / "answer.py").write_text(STUB_ANSWER)

    for queue, notes in QUEUE_NOTES.items():
        folder = root / "vault" / "System" / "Databases" / queue
        folder.mkdir(parents=True)
        for stem, status, score, age in notes:
            created = today - datetime.timedelta(days=age)
            (folder / f"{stem}.md").write_text(
                "---\n"
                f"status: {status}\n"
                f"description: what {stem} is about\n"
                f"score: {score}\n"
                f"dateCreated: {created.isoformat()}\n"
                "---\n\n"
                "A body nobody outside this machine may ever see.\n"
            )
        (folder / "not-a-note.md").write_text("no frontmatter here\n")
    return root


def run_proc(root: pathlib.Path, env_extra=None, today: datetime.date = TODAY):
    """Run `obsidian-review.sh run`; return (the finished process, html)."""
    env = dict(os.environ)
    env.update({
        "OBSIDIAN_VAULT": str(root / "vault"),
        "SYSTEM_TOOLS_CONFIG": str(root / "config"),
        "NOTIFY_RECORD": str(root / "notify.args"),
        "OBSIDIAN_REVIEW_TODAY": today.isoformat(),
    })
    env.pop("OBSIDIAN_REVIEW_SIGN_TIMEOUT", None)
    for key in ("JEV_OBSIDIAN_REVIEW", "JEV_STUB_ENABLED_EXIT",
                "JEV_STUB_ASK_FAIL", "JEV_STUB_RECORD",
                "ACTIONS_STUB_HANG", "ACTIONS_STUB_RECORD",
                "ACTIONS_STUB_SIGNS", "ACTIONS_STUB_SIGN_HANG"):
        env.pop(key, None)
    env.update(env_extra or {})
    proc = subprocess.run(
        ["sh", str(root / "local-obsidian-review" / TOOL.name), "run"],
        capture_output=True, text=True, env=env, timeout=120)
    record = root / "notify.args"
    html = record.read_text().splitlines()[-1] if record.exists() else ""
    return proc, html


def run(root: pathlib.Path, env_extra=None, today: datetime.date = TODAY):
    """Run `obsidian-review.sh run`; return (stdout, html, returncode)."""
    proc, html = run_proc(root, env_extra, today)
    return proc.stdout, html, proc.returncode


def templated(text: str, today: datetime.date = TODAY) -> str:
    """Replace every date the fixture can produce with a stable placeholder.

    A golden captured today would otherwise go stale tomorrow, and a suite
    that rots into a skip is a suite this repository treats as a failure.
    """
    for offset in range(-20, 21):
        day = (today + datetime.timedelta(days=offset)).isoformat()
        text = text.replace(day, "{today%+d}" % offset)
    return text

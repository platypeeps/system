"""A whole fake repo root in a temp dir, so the suite never touches a real store.

The tool resolves its siblings from its own directory (`$DIR/../local-jev`),
so the cheapest way to stub `local-jev` is to copy the entrypoint next to a
stub of it; a stub `sd` first on PATH takes the brief. The base rows live in
a fixture workflow database under a temporary HOME, the default path
`sd_db.database.connect` opens, built with the pack's own `sd_db` from
SD_PACK_ROOT. That keeps the shipped script free of test-only seams: nothing
in it knows a test exists, and nothing here reaches the network. The one
input the harness hands it is the date, in OBSIDIAN_REVIEW_TODAY, and that is
an ordinary setting.
"""

from __future__ import annotations

import datetime
import os
import pathlib
import shutil
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
TOOL_DIR = HERE.parent
TOOL = TOOL_DIR / "obsidian-review.sh"
FIXTURES = HERE / "fixtures"
#: The command pack checkout whose lib/ holds sd_db: the gate's pin when it
#: sets SD_PACK_ROOT, else the home clone. Read before any run moves HOME.
PACK_ROOT = pathlib.Path(os.environ.get("SD_PACK_ROOT")
                         or pathlib.Path.home() / "repos/platypeeps/sd-ai-command-pack")
sys.path.insert(0, str(PACK_ROOT / "lib"))

#: The one date a run uses. `build_root` writes the notes' added dates from
#: it, `run` hands it to the script, and `templated` reads it back, so a run
#: that crosses midnight cannot see two different days. It used to be read
#: here and again by the script, and the two disagreed once a CI run crossed
#: 00:00: every "(Nd ago)" came out one day older than the golden.
TODAY = datetime.date.today()

# queue -> (base kind, rows as (title, stage, score, days since it was added)).
# Only the stages the tool calls "awaiting a decision" may ever reach a card.
KINDS = {"Blog Ideas": "blog-idea", "Skill Proposals": "skill-proposal",
         "Tips and Tricks": "tip", "Topics": "topic", "Market Watch": "market"}
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
# same twelve whether or not Jev ranked the digest.
EXPECTED_SHOWN = {
    "Blog Ideas": ["blog-%02d" % i for i in range(14, 2, -1)],
    "Skill Proposals": ["skill-two", "skill-one"],
    "Tips and Tricks": ["tip-new"],
    "Topics": ["topic-two", "topic-one"],
    "Market Watch": ["watch-one"],
}
EXPECTED_TOTAL = 20

# Records the brief the digest would have filed, and files nothing: its
# arguments to $SD_RECORD, the brief file (the last argument) to $SD_RECORD.brief.
STUB_SD = """#!/bin/sh
printf '%s\\n' "$*" > "$SD_RECORD"
for a in "$@"; do last=$a; done
cp "$last" "$SD_RECORD.brief"
exit "${SD_EXIT:-0}"
"""

STUB_JEV = """#!/bin/sh
[ -z "${JEV_STUB_CALLS:-}" ] || printf '%s run=%s\\n' "$*" "${JEV_RUN:-}" >> "$JEV_STUB_CALLS"
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
    """A fake repo root holding the tool, its stubbed siblings and a database of base rows."""
    from sd_db import create_item
    from sd_db.database import connect, local_path
    from sd_db.migrate import initialise

    root = pathlib.Path(tmp)
    (root / "local-obsidian-review").mkdir()
    shutil.copy(TOOL, root / "local-obsidian-review" / TOOL.name)
    (root / "lib").mkdir()
    shutil.copy(TOOL.parents[1] / "lib" / "config.sh", root / "lib" / "config.sh")
    for folder, name, text in (
        ("bin", "sd", STUB_SD),
        ("local-jev", "jev.sh", STUB_JEV),
    ):
        (root / folder).mkdir()
        path = root / folder / name
        path.write_text(text)
        path.chmod(0o755)
    (root / "local-jev" / "answer.py").write_text(STUB_ANSWER)

    (root / "home").mkdir()
    database = local_path(root / "home")
    initialise(database)
    connection = connect(database)
    try:
        for queue, notes in QUEUE_NOTES.items():
            kind = KINDS[queue]
            for stem, status, score, age in notes:
                created = today - datetime.timedelta(days=age)
                path = f"Base/{queue}/{stem}.md"
                create_item(connection, kind="idea", title=stem, status="planning", source="vault",
                            external_id=f"{kind}:{path}", path=path, stage=status,
                            fields={"kind": kind, "description": f"what {stem} is about",
                                    "score": str(score), "dateCreated": created.isoformat()},
                            body={"markdown": "A body nobody outside this machine may ever see.\n"})
    finally:
        connection.close()
    return root


def item_ids(root: pathlib.Path) -> dict:
    """{title: item id} of the fixture's base rows."""
    from sd_db import records
    from sd_db.database import connect, local_path

    connection = connect(local_path(root / "home"), write=False)
    try:
        return {row["title"]: row["id"] for row in records.list_rows(connection)}
    finally:
        connection.close()


def sd_stub(root: pathlib.Path) -> dict:
    """The environment that puts the stub `sd` first on PATH and the fixture database under HOME."""
    return {"PATH": f"{root / 'bin'}:{os.environ['PATH']}", "SD_RECORD": str(root / "sd.args"),
            "HOME": str(root / "home"), "SD_PACK_ROOT": str(PACK_ROOT)}


def run_proc(root: pathlib.Path, env_extra=None, today: datetime.date = TODAY):
    """Run `obsidian-review.sh run`; return (the finished process, the brief filed)."""
    env = dict(os.environ)
    env.update({
        "SYSTEM_TOOLS_CONFIG": str(root / "config"),
        **sd_stub(root),
        "OBSIDIAN_REVIEW_TODAY": today.isoformat(),
    })
    for key in ("JEV_OBSIDIAN_REVIEW", "JEV_STUB_ENABLED_EXIT",
                "JEV_STUB_ASK_FAIL", "JEV_STUB_RECORD", "JEV_STUB_CALLS", "JEV_RUN"):
        env.pop(key, None)
    env.update({k: v for k, v in (env_extra or {}).items() if v is not None})
    for key, value in (env_extra or {}).items():
        if value is None:
            env.pop(key, None)  # None unsets: the tool must find the value elsewhere
    proc = subprocess.run(
        ["sh", str(root / "local-obsidian-review" / TOOL.name), "run"],
        capture_output=True, text=True, env=env, timeout=120)
    record = root / "sd.args.brief"
    brief = record.read_text().rstrip("\n") if record.exists() else ""
    return proc, brief


def run(root: pathlib.Path, env_extra=None, today: datetime.date = TODAY):
    """Run `obsidian-review.sh run`; return (stdout, the brief, returncode)."""
    proc, brief = run_proc(root, env_extra, today)
    return proc.stdout, brief, proc.returncode


def templated(text: str, today: datetime.date = TODAY) -> str:
    """Replace every date the fixture can produce with a stable placeholder.

    A golden captured today would otherwise go stale tomorrow, and a suite
    that rots into a skip is a suite this repository treats as a failure.
    """
    for offset in range(-20, 21):
        day = (today + datetime.timedelta(days=offset)).isoformat()
        text = text.replace(day, "{today%+d}" % offset)
    return text

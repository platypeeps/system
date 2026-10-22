"""The nightly review job's JSON steps (sd:3638); `nightly-review.sh` runs them.

  rejected <sd> <sd-db>                   each subject the nightly queue rejected in
                                          the last 90 days, one `- <subject>` line each
  batch <reply> <out> <date> <source>     the model's reply as a batch file at <out>,
                                          through <out>.part and a rename
  check <file> <date>                     refuse a batch file the job must not file;
                                          print its question count

Stdlib only, and no database: the store is read and written through the
pack's `sd` and `sd-db`. Exit 1 names its reason on stderr and writes nothing.
"""

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

QUEUE = "nightly"
OPTIONS = ["Approve", "Reject"]
#: Every question is the model's own suggestion, so it proposes Approve; the Review page shows that as its recommendation.
PROPOSED = "Proposed label: Approve"
#: The nightly batch's ceiling; `sd-db review file` drops the rest and says so.
LIMIT = 5
#: How long a rejection keeps a subject out, as `sd-db review file` reads it.
SUPPRESS_DAYS = 90
#: A batch is open 7 days at most, so an older one holds no newer ruling.
EXPIRE_DAYS = 7
REF_SOURCE = "task-ref"
REPLY_KEYS = {"questions"}
QUESTION_KEYS = {"subject", "text", "evidence"}


class Refused(Exception):
    """A step the job must not take; the message says why."""


def _run(argv):
    done = subprocess.run(argv, capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise Refused(f"{' '.join(argv)} exited {done.returncode}: {done.stderr.strip()}")
    return done.stdout


def _moment(stamp):
    moment = datetime.fromisoformat(stamp)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def rejected(sd, sd_db):
    """Each rejected subject; a failed read refuses rather than reading as none."""
    since = datetime.now(timezone.utc) - timedelta(days=SUPPRESS_DAYS)
    try:
        items = json.loads(_run([sd, "store", "items", "--kind", "task", "--json"]))
    except ValueError as failure:
        raise Refused(f"sd store items printed no JSON: {failure}") from None
    batches = [row["id"] for row in items
               if row.get("source") == REF_SOURCE and (row.get("external_id") or "").startswith(f"review:{QUEUE}:")
               and _moment(row["created_at"]) >= since - timedelta(days=EXPIRE_DAYS)]
    subjects = []
    with tempfile.TemporaryDirectory() as scratch:
        out = Path(scratch) / "export.jsonl"
        for item in batches:
            _run([sd_db, "review", "export", str(item), "--out", str(out)])
            for line in out.read_text(encoding="utf-8").splitlines()[1:]:
                question = json.loads(line)
                if (question["label"] == "Reject" and _moment(question["ruled_at"]) >= since
                        and question["subject"] not in subjects):
                    subjects.append(question["subject"])
    for subject in subjects:
        print(f"- {subject}")


def _reply(text):
    """The JSON object in the model's reply: the text from its first `{` to its last `}`."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise Refused("the reply holds no JSON object")
    try:
        found = json.loads(text[start:end + 1])
    except ValueError as failure:
        raise Refused(f"the reply is not JSON: {failure}") from None
    if not isinstance(found, dict) or set(found) != REPLY_KEYS or not isinstance(found["questions"], list):
        raise Refused('the reply must be {"questions": [...]} and nothing else')
    # One malformed suggestion drops itself, named on stderr, and the rest file (sd:5732: one bad key lost a night).
    # A reply whose every suggestion is malformed is still refused: that is a broken reply, not "no suggestions".
    questions, dropped = [], []
    for number, question in enumerate(found["questions"], 1):
        evidence = question.get("evidence", []) if isinstance(question, dict) else None
        if (not isinstance(question, dict) or set(question) - QUESTION_KEYS
                or not isinstance(question.get("subject"), str) or not isinstance(question.get("text"), str)
                or not isinstance(evidence, list) or len(evidence) > 5 or not all(isinstance(e, str) for e in evidence)):
            dropped.append(number)
            print(f"nightly-review: suggestion {number} dropped: it must hold subject and text, "
                  "and evidence (at most 5 strings) at most", file=sys.stderr)
            continue
        questions.append({"subject": question["subject"], "text": question["text"],
                          "evidence": [*evidence, PROPOSED], "options": list(OPTIONS)})
    if dropped and not questions:
        raise Refused(f"every suggestion was malformed: {', '.join(map(str, dropped))}")
    return questions


def batch(reply, out, day, source):
    """Write the batch file the job files: every field but the questions is the job's own."""
    questions = _reply(Path(reply).read_text(encoding="utf-8"))
    found = {"schema": 1, "queue": QUEUE, "batch": day, "title": f"Nightly review {day}", "source": source,
             "mode": "open", "limit": LIMIT, "questions": questions}
    out = Path(out)
    part = out.with_name(out.name + ".part")
    part.write_text(json.dumps(found, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(part, out)


def check(path, day):
    """The guard before `sd-db review file`: an open batch of this queue and day only.

    No blind or sealed batch is filed until the dashboard's Review page lands
    (lead ruling on sd:3635, note #16345), and nightly suggestions are open by
    nature, so this job files open batches only.
    """
    try:
        found = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as failure:
        raise Refused(f"cannot read {path}: {failure}") from None
    if not isinstance(found, dict):
        raise Refused(f"{path} is not a batch")
    if found.get("mode") != "open" or "seal" in found:
        raise Refused(f"{path} is a {found.get('mode')} batch; the nightly job files open batches only")
    if found.get("queue") != QUEUE or found.get("batch") != day:
        raise Refused(f"{path} is batch {found.get('batch')} of queue {found.get('queue')}, "
                      f"not {day} of {QUEUE}")
    limit = found.get("limit")
    if type(limit) is not int or not 1 <= limit <= LIMIT:
        raise Refused(f"{path} sets limit {limit}; the nightly batch holds at most {LIMIT}")
    questions = found.get("questions")
    if not isinstance(questions, list) or any(
            not isinstance(question, dict) or question.get("options") != OPTIONS for question in questions):
        raise Refused(f"{path} asks a question without exactly the options {' and '.join(OPTIONS)}")
    print(len(questions))


def main(argv):
    steps = {"rejected": (rejected, 2), "batch": (batch, 4), "check": (check, 2)}
    step = steps.get(argv[0]) if argv else None
    if step is None or len(argv) - 1 != step[1]:
        print(__doc__, file=sys.stderr)
        return 1
    try:
        step[0](*argv[1:])
    except Refused as refusal:
        print(f"nightly-review: {refusal}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

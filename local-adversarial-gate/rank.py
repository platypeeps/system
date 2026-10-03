"""Order an adversarial-gate result by a Jev `noul`, and label each finding.

The gate's own output is ranked by the reviewer's sense of severity. This asks
a second, narrower question per finding -- "is this a real defect in the
document under review, as opposed to a stylistic preference or a restatement of
what the document already says?" -- and uses the answer to ORDER the findings,
so the reader meets the strongest objection first.

**It never drops, hides or filters a finding.** Ordering and a visible label,
nothing else. The whole value of a hostile read is that it says the unwelcome
thing, and a confidence filter that quietly removes one defeats it. The set of
findings in the file after this runs is the set that was in it before; the
suite pins exactly that.

Every failure -- the stage switched off, Jev switched off or unkeyed, a request that fails,
a response that does not cover every finding, an output this cannot parse --
leaves the file byte-for-byte as it was and says why on stderr. Loud, never
silent, and never fatal: the caller's exit code does not change.

What leaves the machine: the text of each finding, truncated, and nothing else.
Not the document under review. A finding quotes a line of that document --
core.md requires it to -- so that line goes too, which is why a caller reviewing
a confidential draft sets JEV_ADVERSARIAL_GATE=0 first. The stage is on unless
that variable switches it off, so that is an action and not an omission.
"""

import argparse
import json
import math
import os
import re
import subprocess
import sys
import tempfile

# core.md makes a confidence tag mandatory on every finding, which is what lets
# a finding be told from a heading that is not one. No tag anywhere in the file
# means this cannot identify findings, and it declines rather than guessing.
CONFIDENCE = re.compile(r"\b(CERTAIN|LIKELY|SPECULATIVE)\b")
HEADING = re.compile(r"^(#{1,6})\s+\S")
FENCE = re.compile(r"^\s*(```|~~~)")
# Written into the file when a run labels it, and read back before a second
# run: ranking an already-ranked file would stack a second label on every
# finding and sort an order that is no longer the reviewer's.
MARKER = "<!-- adversarial-gate: ordered by Jev -->"
NOTE = [
    MARKER,
    "> **Order note.** The findings below are ordered by a Jev `noul`: *is this a",
    "> real defect in the document under review, as opposed to a stylistic",
    "> preference or a restatement of what the document already says?* The number",
    "> under each heading is that probability. **Nothing was removed, filtered or",
    "> hidden** — only the order changed. A low number is a finding to read last,",
    "> not one to skip.",
]
QUESTION = (
    "The state holds the findings of a hostile review, keyed by id. Considering "
    'ONLY the finding with id "{qid}": is it a real defect in the document under '
    "review — something a careful author would have to fix or answer — as opposed "
    "to a stylistic preference, or a restatement of what the document already says?"
)
# How much of one finding is sent. Enough for the quoted line and the objection,
# bounded so a finding that swallowed half the document cannot carry it out.
MAX_FINDING_CHARS = 1500


def warn(message: str) -> None:
    sys.stderr.write(f"adversarial-gate: rank: {message}\n")


def blocks(lines):
    """(level, start, end) per ATX heading, the block holding its subheadings.

    A block runs to the next heading of the same level or shallower, so moving
    one moves what sits under it. Headings inside a fenced code block are text.
    """
    heads = []
    fenced = False
    for i, line in enumerate(lines):
        if FENCE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        m = HEADING.match(line)
        if m:
            heads.append((len(m.group(1)), i))
    out = []
    for n, (level, start) in enumerate(heads):
        end = len(lines)
        for other_level, other_start in heads[n + 1:]:
            if other_level <= level:
                end = other_start
                break
        out.append((level, start, end))
    return out


def finding_blocks(lines):
    """The heading level that carries findings, and the findings at it.

    The level with the most confidence-tagged blocks wins; a tie goes to the
    shallower level, which is the whole finding rather than a section of one.
    """
    tagged = {}
    for level, start, end in blocks(lines):
        if CONFIDENCE.search("\n".join(lines[start:end])):
            tagged.setdefault(level, []).append((start, end))
    if not tagged:
        return []
    level = min(tagged, key=lambda lv: (-len(tagged[lv]), lv))
    return tagged[level]


def runs_of(found):
    """Maximal runs of findings that sit back to back in the file.

    Only a run is reordered. Anything between two findings -- a section header,
    a per-axis verdict, the closing line -- keeps its place, so a reorder can
    never carry a finding out from under the prose that introduces it.
    """
    out = []
    for block in found:
        if out and out[-1][-1][1] == block[0]:
            out[-1].append(block)
        else:
            out.append([block])
    return out


#: Who this is in the judgment ledger, and which decision it is. The stage
#: is the same word `adversarial-gate.sh` hands `jev enabled`, so one name
#: means one thing across the switch, the ledger and the report. Both satisfy
#: the ledger's identifier grammar; a name it refuses is filed under
#: `unknown`, which is the whole reason these are spelled out.
JEV_CALLER = "local-adversarial-gate"
JEV_STAGE = "JEV_ADVERSARIAL_GATE"


def record_baseline(jev, cause=None):
    """Say the reviewer's own order -- the control arm -- is what ran.

    `--outcome ok` always: the old path completed, and that this row exists at
    all is the fact nothing else carries.

    `cause` is the word only when `jev` never wrote its own row: the caller
    killed it on a deadline, or it could not be started. `jev` flushes its
    measurement after the answer is printed and installs no signal handler, so
    a call ended by SIGTERM leaves nothing behind and this row is the only
    record there will be. When `jev` did return -- a non-zero exit, an answer
    this cannot read -- it has already written the cause, and repeating it
    here counts one decision as two: `judgment.py`'s DECLINES groups by stage
    and cause across both arms with no deduplication.

    Bookkeeping only: `jev record` sends nothing, needs no key, prints
    nothing and always exits 0. Every failure is swallowed, because a review
    must never fail over its own measurement.
    """
    try:
        subprocess.run(
            [jev, "record", "--caller", JEV_CALLER, "--stage", JEV_STAGE,
             "--arm", "baseline", "--outcome", "ok"]
            + (["--decline", cause] if cause else []),
            capture_output=True, text=True, timeout=30,
        )
    except Exception:  # noqa: BLE001 - bookkeeping may never fail the caller
        pass


def ask_jev(jev, texts):
    """{id: probability} for every finding, or None and a reason on stderr.

    One request: questions in a single `ask` run in parallel and cannot see
    each other's answers, so N findings cost one round trip.
    """
    questions = {
        qid: {"type": "noul", "instructions": QUESTION.format(qid=qid)}
        for qid in texts
    }
    handle, qpath = tempfile.mkstemp(prefix="adversarial-gate.q.", suffix=".json")
    os.close(handle)
    handle, spath = tempfile.mkstemp(prefix="adversarial-gate.s.", suffix=".json")
    os.close(handle)
    try:
        with open(qpath, "w", encoding="utf-8") as fh:
            json.dump(questions, fh)
        with open(spath, "w", encoding="utf-8") as fh:
            json.dump({"findings": texts}, fh)
        try:
            result = subprocess.run(
                [jev, "ask", "--questions", qpath, "--state", spath,
                 "--state-format", "json",
                 "--caller", JEV_CALLER, "--stage", JEV_STAGE],
                capture_output=True, text=True, timeout=300,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            warn(f"jev could not be run ({exc}); the order is the reviewer's own")
            # `timeout=300` above kills it, and a killed `jev` never reached
            # its flush, so this row is the only record the run will have.
            record_baseline(
                jev,
                "timeout" if isinstance(exc, subprocess.TimeoutExpired)
                else "unavailable")
            return None
    finally:
        for path in (qpath, spath):
            try:
                os.unlink(path)
            except OSError:
                pass
    if result.returncode != 0:
        detail = (result.stderr or "").strip().splitlines()
        warn(f"jev ask exited {result.returncode}"
             f"{': ' + detail[-1] if detail else ''}; the order is the reviewer's own")
        record_baseline(jev)
        return None
    try:
        answers = json.loads(result.stdout)["answers"]
    except (ValueError, KeyError, TypeError):
        warn("jev ask returned no answers block; the order is the reviewer's own")
        record_baseline(jev)
        return None
    scores = {}
    for qid in texts:
        try:
            scores[qid] = float(answers[qid]["noul"])
            # `float` reads NaN and Infinity, and JSON carries both; neither is
            # a probability, and NaN sorts nowhere in particular.
            if not (math.isfinite(scores[qid]) and 0.0 <= scores[qid] <= 1.0):
                raise ValueError(scores[qid])
        except (KeyError, TypeError, ValueError):
            # Partial coverage is refused whole. Sorting the answered findings
            # and leaving the rest wherever they fall is an order no one chose.
            warn(f"jev ask did not answer for {qid}; the order is the reviewer's own")
            record_baseline(jev)
            return None
    return scores


def label_for(score):
    return f"*Jev — real defect: {score:.2f}*"


def rank_text(text, jev):
    """The reordered, labelled document, or None to leave the file alone."""
    if MARKER in text:
        warn("this file was ordered by Jev already; leaving it as it is")
        return None
    lines = text.split("\n")
    found = finding_blocks(lines)
    if len(found) < 2:
        warn(f"{len(found)} finding(s) found with a CERTAIN/LIKELY/SPECULATIVE tag: "
             "nothing to order, leaving the file as it is")
        return None
    texts = {
        f"f{n + 1}": "\n".join(lines[start:end]).strip()[:MAX_FINDING_CHARS]
        for n, (start, end) in enumerate(found)
    }
    scores = ask_jev(jev, texts)
    if scores is None:
        return None

    ids = {block: f"f{n + 1}" for n, block in enumerate(found)}
    ranked = {}
    for run in runs_of(found):
        # Stable and descending: findings Jev scores the same keep the order
        # the reviewer gave them.
        for position, block in zip(run, sorted(run, key=lambda b: -scores[ids[b]])):
            ranked[position] = block

    out = []
    cursor = 0
    for position in found:
        out.extend(lines[cursor:position[0]])
        start, end = ranked[position]
        body = list(lines[start:end])
        label = ["", label_for(scores[ids[(start, end)]])]
        if len(body) > 1 and body[1].strip():
            label.append("")
        out.extend([body[0]] + label + body[1:])
        cursor = position[1]
    out.extend(lines[cursor:])
    return "\n".join(insert_note(out))


def insert_note(lines):
    """The note goes under the document's own title, or at the top if it has none."""
    at = 0
    for i, line in enumerate(lines):
        if line.strip() == "":
            continue
        at = i + 1 if HEADING.match(line) else i
        break
    tail = lines[at:]
    gap = [] if tail and tail[0].strip() == "" else [""]
    return lines[:at] + ([""] if at else []) + NOTE + gap + tail


def replace_text(path, text) -> bool:
    """Write `text` over `path` whole or not at all.

    The gate result is the only copy of the review. Opening it with "w"
    truncated it before the write, so an interruption or a full disk left an
    empty or partial file where the documented fallback promises the
    reviewer's own. A sibling temp file renamed over it keeps one or the
    other, and keeps the original's mode.
    """
    folder = os.path.dirname(os.path.abspath(path))
    try:
        handle, temp = tempfile.mkstemp(prefix=".rank.", dir=folder)
    except OSError as exc:
        warn(f"cannot write beside {path}: {exc}; the order is the reviewer's own")
        return False
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(temp, os.stat(path).st_mode & 0o7777)
        os.replace(temp, path)
    except BaseException as exc:
        try:
            os.unlink(temp)
        except OSError:
            pass
        if not isinstance(exc, OSError):
            raise
        warn(f"cannot write {path}: {exc}; the order is the reviewer's own")
        return False
    return True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(add_help=True, description=__doc__)
    parser.add_argument("--in", dest="path", required=True,
                        help="the gate result to order, edited in place")
    parser.add_argument("--jev", required=True,
                        help="path to local-jev/jev.sh")
    args = parser.parse_args(argv)
    try:
        with open(args.path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        warn(f"cannot read {args.path}: {exc}")
        return 1
    ordered = rank_text(text, args.jev)
    if ordered is None:
        return 0
    if not replace_text(args.path, ordered):
        return 1
    sys.stderr.write(f"adversarial-gate: rank: {args.path} ordered by Jev; "
                     "no finding was removed\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

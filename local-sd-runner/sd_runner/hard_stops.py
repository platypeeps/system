"""The three hard stops item A names, decided here and recorded on the row.

`prd.md` requirement 1 has the runner stop on a failing test, a blocking
review finding open past the cap, or a write outside the repository, and
mark the item `blocked` with the reason. The end run (PR 3) records whatever
outcome it is handed; this module is what decides that the outcome is a
stop, and names it, from three sources:

* **the session's own signal.** A session that hits a stop the runner cannot
  see -- a skill about to write outside the repository, a test it cannot
  make pass -- writes `.git/sd-stop.json` in its clone and exits. The prompt
  tells it so. The runner reads the file whatever the exit code was.
* **the repository's own check**, run by the runner in the clone after an
  author session exits 0 and before anything is shipped. In production that
  is the pack's `sd-check --json`, which asks the repository how it spells
  `check` and `test`; a fixture passes its own argv. A non-zero exit is the
  failing test, named by the checks that failed when the output says which.
* **the ship receipt.** `sd-ship prepare` runs the review and holds the cap;
  when it refuses, the receipt it left says why. A last report with a
  failed check is the failing test; one still `blocking` is the finding,
  past the cap when both automatic passes are spent.

A stop is a `RunnerRefused`, so the ordinary ending carries it: the row's
`detail` names it, `release` transitions the item `blocked` with that
reason and writes the `exec` note, and `record_hard_stop` leaves an open
`followup` the next session starts from. The branch is pushed by the
durability gate like every other ended row's, and left in place.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

from sd_db.runner import HARD_STOPS, RunnerRefused

FAILING_TEST, BLOCKING_FINDING, OUTSIDE_WRITE = HARD_STOPS

SIGNAL_FILE = ".git/sd-stop.json"
SIGNAL_LIMIT = 64 * 1024
EVIDENCE_LIMIT = 4000
#: Automatic review passes `sd-ship` grants an author row before the cap is
#: spent: one code review and one verification of the fix.
REVIEW_CAP = 2


class HardStop(RunnerRefused):
    def __init__(self, kind: str, evidence: str):
        if kind not in HARD_STOPS:
            raise ValueError(f"not a hard stop: {kind}")
        self.kind = kind
        self.evidence = " ".join(str(evidence).split())[:EVIDENCE_LIMIT]
        super().__init__(f"hard stop: {self.kind}: {self.evidence}")


def signal(clone: Path) -> HardStop | None:
    """The stop the session itself wrote, or None. A malformed file is a stop too.

    A session that wrote something here meant to stop; a file the runner
    cannot read must not turn into a `done` row, so it is a stop naming the
    fault rather than nothing. Absence is the only None: a directory, a
    device or a link at the path is an artifact the session left and the
    runner cannot read, and `is_file()` read all three as absence (sd:1221).
    """
    path = Path(clone) / SIGNAL_FILE
    try:
        details = path.lstat()
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as error:
        raise RunnerRefused(f"session wrote an unreadable stop signal at {SIGNAL_FILE}: {error}") from error
    if not stat.S_ISREG(details.st_mode):
        raise RunnerRefused(f"session wrote an unreadable stop signal at {SIGNAL_FILE}: not a regular file")
    try:
        if path.stat().st_size > SIGNAL_LIMIT:
            raise ValueError(f"stop signal exceeds {SIGNAL_LIMIT} bytes")
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("kind") not in HARD_STOPS or not isinstance(value.get("detail"), str):
            raise ValueError("stop signal must be an object with a known kind and a string detail")
    except (OSError, ValueError) as error:
        raise RunnerRefused(f"session wrote an unreadable stop signal at {SIGNAL_FILE}: {error}") from error
    return HardStop(value["kind"], value["detail"] or "the session gave no detail")


def from_check(result: dict) -> HardStop | None:
    """The failing test, from the supervisor's `check` action result.

    The failed checks are named from `checks`, the list the supervisor
    parsed from the whole `sd-check --json` output before it cut `stdout`
    to a 4000-byte tail (#284). Parsing `stdout` here is the fallback for a
    result without that key -- a fixture handing in a raw result -- and it
    is only a fallback: on a repository whose report is longer than the
    tail, the tail is not JSON, and reading it named nothing (sd:235).

    The key's absence selects the fallback, not its value. A result that
    carries `checks` as null parsed the report and found no list; rereading
    the truncated tail behind it can name a check from a JSON fragment the
    supervisor already rejected (sd:1221).
    """
    if result.get("exit_code") == 0:
        return None
    entries = result.get("checks")
    if "checks" not in result:
        try:
            entries = json.loads(result.get("stdout") or "").get("checks")
        except (ValueError, AttributeError):
            entries = None
    failed = []
    for entry in entries if isinstance(entries, list) else []:
        if isinstance(entry, dict) and entry.get("status") == "fail":
            failed.append(f"{entry.get('name')}: {' '.join(map(str, entry.get('command') or []))}".strip(": "))
    tail = (result.get("stderr") or "").strip() or (result.get("stdout") or "").strip()
    named = "; ".join(failed) if failed else "the repository check"
    return HardStop(FAILING_TEST, f"{named} exited {result.get('exit_code')}" + (f": {tail}" if tail else ""))


def from_receipt(receipt: dict, *, head: str | None = None) -> HardStop | None:
    """The stop an `sd-ship prepare` refusal left in its receipt, or None.

    `head` is the commit this run handed to `prepare`. The receipt is keyed
    by repository, branch and item and returns the latest pass, which can
    belong to an earlier run on the same branch; a `prepare` that refuses
    before saving a pass of its own would then be classified by that older
    pass (sd:1221). A caller that names its head gets a stop only from a
    pass that ran against it.
    """
    passes = receipt.get("passes") if isinstance(receipt, dict) else None
    if not isinstance(passes, list) or not passes or not isinstance(passes[-1], dict):
        return None
    if head is not None and passes[-1].get("head") != head:
        return None
    report = passes[-1].get("report")
    if not isinstance(report, dict):
        return None
    check = report.get("check")
    if isinstance(check, dict) and check.get("status") == "fail":
        failed = [f"{entry.get('name')}" for entry in check.get("checks") or [] if isinstance(entry, dict) and entry.get("status") == "fail"]
        return HardStop(FAILING_TEST, f"{', '.join(failed) or 'the repository check'} failed under sd-ship: {check.get('detail') or ''}")
    if report.get("status") == "blocking":
        findings = [row for row in report.get("findings") or [] if isinstance(row, dict) and row.get("disposition") == "blocking"]
        named = "; ".join(f"{row.get('path') or '?'}:{row.get('line') or '?'} {row.get('summary') or row.get('title') or ''}".strip()
                          for row in findings) or "unnamed blocking finding"
        spent = f"{len(passes)} of {REVIEW_CAP} automatic passes spent"
        return HardStop(BLOCKING_FINDING, f"{named} ({spent})")
    return None

"""The alert half of the night triage job (sd:8143): claim, release, apply.

The model never writes an alert. This script claims this machine's open
alerts before the model runs, and after it reads the model's reply and
settles each one: a fix with evidence for every open followup is
acknowledged; a filed bug or an operator action gets one note and goes back
to Today. Every write goes through the pack's `sd`; this opens no database.

The claims file holds one line per alert held: `<id> <status before> <revision
after the claim>`. A release moves an alert back while its last status
change is still this job's claim, so a run that died never takes back an
alert another session claimed since. A row leaves the file only after its
release or a confirmed conflict: an `sd` outage keeps it and exits 1.

Usage:
  night_triage.py claim <sd> <host> <claims> <context.json>
  night_triage.py release <sd> <claims>
  night_triage.py apply <sd> <host> <claims> <context.json> <reply>
"""

import json
import re
import subprocess
import sys
from pathlib import Path

WHO = "night-triage"
CLAIM = f"{WHO} claims it"
ITEM = re.compile(r"^sd:\d+$")


class Refused(Exception):
    """The model's reply cannot be read; every alert goes back as it was."""


def sd(binary, *args):
    done = subprocess.run([binary, *args], capture_output=True, text=True, timeout=120)
    if done.returncode:
        raise RuntimeError(f"sd {' '.join(args[:3])}: exit {done.returncode}: {done.stderr.strip()}")
    return done.stdout


def show(binary, item):
    return json.loads(sd(binary, "task", "show", str(item), "--json"))


def report_fields(report):
    fields = report.get("fields") or "{}"
    fields = json.loads(fields) if isinstance(fields, str) else fields
    return (fields or {}).get("report") or {}


def host_of(report):
    return str(report_fields(report).get("host") or "").lower()


def _claims(path):
    path = Path(path)
    if not path.exists():
        return []
    return [line.split() for line in path.read_text().splitlines() if line.strip()]


def _write_claims(path, rows):
    Path(path).write_text("".join(" ".join(row) + "\n" for row in rows))


def claim(binary, host, claims, context):
    """Claim each unclaimed alert of this host; write what the model reads."""
    held, found = _claims(claims), []
    for report in json.loads(sd(binary, "reports", "list")):
        if report.get("kind") != "report" or host_of(report) != host:
            continue
        if report.get("status") == "in_progress":
            print(f"night-triage: sd:{report['id']} is claimed; skipped", file=sys.stderr)
            continue
        if report.get("status") != "planning":
            continue
        current = show(binary, report["id"])
        if current["item"]["status"] != "planning":
            continue
        try:
            moved = json.loads(sd(binary, "task", "status", str(report["id"]), "in_progress", "--json",
                                  "--if-revision", current["revision"], "--reason", CLAIM))
        except RuntimeError as error:  # another session claimed it first
            print(f"night-triage: sd:{report['id']} not claimed: {error}", file=sys.stderr)
            continue
        held.append([str(report["id"]), "planning", moved["revision"]])
        _write_claims(claims, held)
        fields = report_fields(report)
        found.append({"id": report["id"], "title": report.get("title"), "job": fields.get("job"),
                      "basis": fields.get("attention_basis"), "log": fields.get("source_path"),
                      "followups": [{"id": n["id"], "body": n["body"][:2000]} for n in moved["notes"]
                                    if n["kind"] == "followup" and not n.get("resolved_at")]})
    Path(context).write_text(json.dumps(found, indent=1))
    print(len(found))


def _release_one(binary, row, reason):
    """Give one claim back. True: released; False: another hand holds it now.

    A refused release counts as a conflict only once a re-read shows a new
    revision. Then the alert is still this job's while its last status change
    is this job's claim: a rerun the model started folds into the report and
    moves its revision (sd:8143 review), so it is released at the new one.
    Any other failure, an outage included, raises: the caller keeps the row.
    """
    item, before, revision = row
    try:
        sd(binary, "task", "status", item, before, "--if-revision", revision, "--reason", reason)
        return True
    except RuntimeError as error:
        refused = error
    current = show(binary, item)
    if current["revision"] == revision:
        raise refused
    changes = [n for n in current["notes"] if n["kind"] == "status_change"]
    if current["item"]["status"] != "in_progress" or not changes or CLAIM not in changes[-1]["body"]:
        print(f"night-triage: sd:{item} is another session's now; left to it", file=sys.stderr)
        return False
    sd(binary, "task", "status", item, before, "--if-revision", current["revision"], "--reason", reason)
    return True


def _settle_claims(binary, claims, each):
    """Run `each(row)` over the claims; drop a row only when it returns."""
    rows, failed = _claims(claims), 0
    for row in list(rows):
        try:
            each(row)
        except (RuntimeError, subprocess.TimeoutExpired) as error:
            failed += 1
            print(f"night-triage: sd:{row[0]}: claim kept: {error}", file=sys.stderr)
            continue
        rows.remove(row)
        _write_claims(claims, rows)
    if not rows:
        Path(claims).unlink(missing_ok=True)
    return 1 if failed else 0


def release(binary, claims):
    """Give back every claim still held, as an earlier or this run left it."""
    return _settle_claims(binary, claims,
                          lambda row: _release_one(binary, row, f"{WHO}: release the claim of a run that did not finish"))


def _reply(text):
    decoder, start = json.JSONDecoder(), text.find("{")
    while start >= 0:
        try:
            value, _ = decoder.raw_decode(text, start)
        except ValueError:
            start = text.find("{", start + 1)
            continue
        if isinstance(value, dict) and isinstance(value.get("alerts"), list):
            return {entry.get("id"): entry for entry in value["alerts"] if isinstance(entry, dict)}
        start = text.find("{", start + 1)
    raise Refused("the reply holds no object with an `alerts` list")


def _text(value):
    return value.strip() if isinstance(value, str) else ""


def verdict(alert, entry, host):
    """The note for one alert, and whether to acknowledge it."""
    if not entry:
        return f"{WHO} on {host}: not settled: the run gave no answer for this alert.", False
    outcome, summary = entry.get("outcome"), _text(entry.get("summary"))
    if outcome == "fixed":
        evidence = [_text(line) for line in entry.get("evidence") or [] if _text(line)]
        followups = entry.get("followups") or {}
        missing = [str(f["id"]) for f in alert["followups"] if not _text(followups.get(str(f["id"])))]
        if summary and evidence and not missing:
            lines = [f"{WHO} on {host} fixed it: {summary}", *(f"Evidence: {line}" for line in evidence)]
            lines += [f"Followup #{f['id']}: {_text(followups[str(f['id'])])}" for f in alert["followups"]]
            return "\n".join(lines), True
        why = (f"open followups without evidence: {', '.join('#' + m for m in missing)}" if missing
               else "a fix needs a summary and evidence")
        return f"{WHO} on {host}: not acknowledged: {why}. Reported: {summary or '(none)'}", False
    if outcome == "filed" and ITEM.match(_text(entry.get("item"))) and summary:
        return f"{WHO} on {host}: a code bug, filed as {_text(entry['item'])}: {summary}", False
    if outcome == "operator" and _text(entry.get("action")) and _text(entry.get("why")):
        return (f"{WHO} on {host}: needs the operator: {_text(entry['action'])}\n"
                f"No tool can: {_text(entry['why'])}"), False
    return f"{WHO} on {host}: not settled: the answer was incomplete ({outcome!r}). {summary}", False


def apply(binary, host, claims, context, reply):
    """Settle each claimed alert: release it, note it, acknowledge a proven fix."""
    alerts = {str(a["id"]): a for a in json.loads(Path(context).read_text())}
    answers = _reply(Path(reply).read_text())

    def settle(row):
        released = _release_one(binary, row, f"{WHO} settled its run")
        alert = alerts.get(row[0])
        if alert is None:
            return
        entry = answers.get(alert["id"]) or answers.get(str(alert["id"]))
        note, acknowledge = verdict(alert, entry, host)
        try:
            sd(binary, "task", "note", row[0], note)
            if acknowledge and released:
                sd(binary, "reports", "acknowledge", row[0], "--resolve-followups")
        except RuntimeError as error:  # released already: the row has nothing left to give back
            print(f"night-triage: sd:{row[0]}: {error}", file=sys.stderr)
            failures.append(row[0])

    failures = []
    return _settle_claims(binary, claims, settle) or (1 if failures else 0)


def main(argv):
    verb, args = (argv[1], argv[2:]) if len(argv) > 1 else ("", [])
    try:
        if verb == "claim" and len(args) == 4:
            claim(*args)
        elif verb == "release" and len(args) == 2:
            return release(*args)
        elif verb == "apply" and len(args) == 5:
            return apply(*args)
        else:
            print(__doc__.split("Usage:")[1], file=sys.stderr)
            return 2
    except (Refused, RuntimeError, ValueError, OSError, subprocess.TimeoutExpired) as error:
        print(f"night-triage: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

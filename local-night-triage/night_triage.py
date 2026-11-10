"""The alert half of the night triage job (sd:8143): claim, release, apply.

The model never writes an alert. This script claims this machine's open
alerts before the model runs, and after it reads the model's reply and
settles each one: a fix with evidence for every open followup is
acknowledged; a filed bug or an operator action gets one note and stays on
Today. Every write goes through the pack's `sd`; this opens no database.

A report takes no status through `sd task status`: the pack gives the
`report` kind no status choices and refuses `in_progress` (sd:8143
review). So the claim is a note, `night-triage on <host> claims it`, and
the settle note releases it. An alert is someone else's while the latest
note by anyone but the ingest and this job is a claim; it is skipped.

The claims file holds one line per alert held: `<id> <claim note id>`.
The row goes in before the claim note, with `-` for the id, so a run that
dies after the note but before the row holds its id still leaves a row.
The next release writes a release note on each row whose latest note from
this job is still its claim. A row leaves the file only after that: an
`sd` outage keeps it and exits 1.

Settling writes the note with `--if-revision` of the state it judged, and
acknowledges with `--if-revision` of the state the note left. A writer
that slips in between refuses the write: the job judges again from the
new state, so a followup it added is never resolved unseen. A note from
any session but the ingest after the claim means another writer is on it;
the alert is then left to them, with a note that says so.

An unattended run closes only the ingest's own followups on its word: a
followup a person wrote needs a fix or a passing check that person accepts,
so it leaves the alert open (operator rule, pinned in local-agent-prompt).

Usage:
  night_triage.py claim <sd> <host> <claims> <context.json>
  night_triage.py release <sd> <host> <claims>
  night_triage.py apply <sd> <host> <claims> <context.json> <reply>
"""

import json
import re
import subprocess
import sys
from pathlib import Path

WHO = "night-triage"
ITEM = re.compile(r"^sd:\d+$")
PENDING = "-"  # a claim row written before its claim note landed
TRIES = 3  # settle attempts while other writers keep moving the report
# The session `sd reports ingest` writes its followups under: the pack's
# sd_db.reporting.INGEST_SESSION, pinned by tests/test_night_triage.py.
INGEST_SESSION = "cron"


def mine(host):
    return f"{WHO} on {host}"


def is_ours(note, host):
    return note["body"].startswith(mine(host))


def is_claim(note, host):
    return note["body"].startswith(f"{mine(host)} claims it")


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


def _others(notes, host):
    """Notes by anyone but the ingest and this job, oldest first."""
    return [n for n in notes if n.get("session") != INGEST_SESSION and not is_ours(n, host)]


def held_by_other(notes, host):
    others = _others(notes, host)
    return bool(others) and re.search(r"\bclaim", others[-1]["body"], re.I) is not None


def claim(binary, host, claims, context):
    """Claim each unclaimed alert of this host with a note; write what the model reads."""
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
        if held_by_other(current["notes"], host):
            print(f"night-triage: sd:{report['id']} is claimed; skipped", file=sys.stderr)
            continue
        # Record the intent first: a crash after the note still leaves a row.
        row = [str(report["id"]), PENDING]
        held.append(row)
        _write_claims(claims, held)
        try:
            moved = json.loads(sd(binary, "task", "note", str(report["id"]),
                                  f"{mine(host)} claims it: an unattended triage run; its settle note "
                                  "releases it.", "--json", "--if-revision", current["revision"]))
        except RuntimeError as error:  # lost, or another writer came first
            print(f"night-triage: sd:{report['id']} not claimed: {error}", file=sys.stderr)
            continue  # the pending row is released when this run settles
        row[1] = str(max(n["id"] for n in moved["notes"] if is_claim(n, host)))
        _write_claims(claims, held)
        fields = report_fields(report)
        found.append({"id": report["id"], "title": report.get("title"), "job": fields.get("job"),
                      "basis": fields.get("attention_basis"), "log": fields.get("source_path"),
                      "followups": _open_followups(moved["notes"]),
                      "notes": _history(moved["notes"], host)})
    Path(context).write_text(json.dumps(found, indent=1))
    print(len(found))


def _open_followups(notes):
    return [{"id": n["id"], "body": n["body"][:2000], "session": n.get("session")} for n in notes
            if n["kind"] == "followup" and not n.get("resolved_at")]


def _history(notes, host):
    """Every earlier note but this job's claims and releases, oldest first.

    The model reads them so it does not re-ask a finding a note settled
    (sd:8374).
    """
    return [{"id": n["id"], "kind": n["kind"], "session": n.get("session"), "body": n["body"][:2000],
             "resolved": bool(n.get("resolved_at"))} for n in notes
            if not is_claim(n, host) and "claim released" not in n["body"]]


def _release_one(binary, host, row, reason):
    """Release one claim. True: a release note went in; False: nothing held.

    The claim is still held while this job's latest note on the alert is
    its claim; a pending row whose note never landed holds nothing. An
    outage raises, and the caller keeps the row.
    """
    ours = [n for n in show(binary, row[0])["notes"] if is_ours(n, host)]
    if not ours or not is_claim(ours[-1], host):
        return False
    sd(binary, "task", "note", row[0], f"{mine(host)}: {reason}; claim released.")
    return True


def _settle_claims(binary, claims, each):
    """Run `each(row)` over the claims; drop a row only when it returns."""
    rows, failed = _claims(claims), 0
    for row in list(rows):
        try:
            each(row)
        except (RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
            failed += 1
            print(f"night-triage: sd:{row[0]}: claim kept: {error}", file=sys.stderr)
            continue
        rows.remove(row)
        _write_claims(claims, rows)
    if not rows:
        Path(claims).unlink(missing_ok=True)
    return 1 if failed else 0


def release(binary, host, claims):
    """Release every claim still held, as an earlier or this run left it."""
    return _settle_claims(binary, claims,
                          lambda row: _release_one(binary, host, row, "the run that claimed it did not finish"))


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
        people = [f"#{f['id']} ({f.get('session') or 'unknown'})" for f in alert["followups"]
                  if f.get("session") != INGEST_SESSION]
        if people:
            return (f"{WHO} on {host}: not acknowledged: followups a person wrote need a fix or "
                    f"check they accept: {', '.join(people)}. Reported: {summary or '(none)'}"), False
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


def _writers_since_claim(notes, host):
    """Sessions other than the ingest that wrote after this job's latest claim."""
    claims = [n["id"] for n in notes if is_claim(n, host)]
    after = [n for n in notes if claims and n["id"] > claims[-1]]
    return sorted({str(n.get("session") or "unknown") for n in _others(after, host)})


def apply(binary, host, claims, context, reply):
    """Settle each claimed alert: one note, then acknowledge a proven fix.

    Both writes carry `--if-revision`, so a writer between the read and a
    write refuses it rather than having its followup resolved unseen.
    """
    alerts = {str(a["id"]): a for a in json.loads(Path(context).read_text())}
    answers = _reply(Path(reply).read_text())

    def unchanged_since(item, revision):
        return show(binary, item)["revision"] == revision

    def settle(row):
        item, alert = row[0], alerts.get(row[0])
        if alert is None:  # the claim's answer was lost: give it back
            _release_one(binary, host, row, "the claim did not reach the run")
            return
        entry = answers.get(alert["id"]) or answers.get(str(alert["id"]))
        for _ in range(TRIES):
            current = show(binary, item)
            writers = _writers_since_claim(current["notes"], host)
            if writers:
                note, acknowledge = (f"{mine(host)}: not settled: {', '.join(writers)} wrote on it during "
                                     "the run; left to them; claim released."), False
            else:
                # Judge against the followups open now: a rerun that failed
                # again adds one, and --resolve-followups would close it.
                note, acknowledge = verdict(dict(alert, followups=_open_followups(current["notes"])),
                                            entry, host)
                acknowledge = acknowledge and current["item"]["status"] == "planning"
            try:
                noted = json.loads(sd(binary, "task", "note", item, note, "--json",
                                      "--if-revision", current["revision"]))
                break
            except RuntimeError:
                if unchanged_since(item, current["revision"]):
                    raise  # an outage, not another writer
        else:
            raise RuntimeError(f"the report kept changing; not settled after {TRIES} reads")
        if not acknowledge:
            return
        try:
            sd(binary, "reports", "acknowledge", item, "--if-revision", noted["revision"],
               "--resolve-followups")
        except RuntimeError:
            if unchanged_since(item, noted["revision"]):
                raise
            sd(binary, "task", "note", item, f"{mine(host)}: not acknowledged: the report changed after "
                                             "the note above; the next run settles it.")

    return _settle_claims(binary, claims, settle)


def main(argv):
    verb, args = (argv[1], argv[2:]) if len(argv) > 1 else ("", [])
    try:
        if verb == "claim" and len(args) == 4:
            claim(*args)
        elif verb == "release" and len(args) == 3:
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

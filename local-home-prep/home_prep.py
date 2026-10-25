"""The home prep job's steps (sd:6029); `home-prep.sh` runs them.

  tomorrow                                  tomorrow's local date, YYYY-MM-DD
  select <sd> <due> <repos>                 the open home items due on <due>, as JSON:
                                            no repo, or a repo in <repos> (space separated),
                                            and no `Prepped for <due>:` note yet
  show <selected>                           one line per selected item
  prompt <prompt.md> <selected> <account>   the model's prompt
  apply <sd> <selected> <result> <due>      judge the `claude -p` result object, then write
                                            one `Prepped for <due>:` note per prepped item

Stdlib only, and no database: items and notes go through the pack's `sd`.
Exit 1 names its reason on stderr. A failed read is unknown, never "nothing due".
"""

import json
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

#: The kinds an operator acts on; reports, ideas and work items are not home chores.
KINDS = {"task", "followup", "personal"}
CLOSED = {"done", "cancelled"}
#: Each note starts with this and the due date, so a recurring item's next due date counts anew.
MARK = "Prepped for {due}:"
REPLY_KEYS = {"summary", "checklist", "drafts", "links"}


class Refused(Exception):
    """A step the job must not take; the message says why."""


def _run(argv):
    done = subprocess.run(argv, capture_output=True, text=True, check=False, stdin=subprocess.DEVNULL)
    if done.returncode != 0:
        raise Refused(f"{' '.join(argv[:3])} exited {done.returncode}: {done.stderr.strip()}")
    return done.stdout


def _json(text, what):
    try:
        return json.loads(text)
    except ValueError as failure:
        raise Refused(f"{what} printed no JSON: {failure}") from None


def _body(item):
    """The item body's text: the store keeps it as `{"text": ...}`."""
    raw = item.get("body") or ""
    try:
        found = json.loads(raw)
    except ValueError:
        return raw
    return found.get("text", "") if isinstance(found, dict) else raw


def select(sd, due, repos):
    """The items to prep; each candidate's notes are read, and a failed read refuses."""
    home = set(repos.split())
    items = _json(_run([sd, "store", "items", "--open", "--json"]), "sd store items")
    chosen = []
    for item in items:
        if (item.get("due") != due or item.get("kind") not in KINDS or item.get("status") in CLOSED
                or item.get("parked_at") or (item.get("repo") is not None and item["repo"] not in home)):
            continue
        shown = _json(_run([sd, "task", "show", "--json", str(item["id"])]), f"sd task show {item['id']}")
        if any(note.get("body", "").startswith(MARK.format(due=due)) for note in shown.get("notes", [])):
            continue
        chosen.append({"id": item["id"], "title": item["title"], "kind": item["kind"], "repo": item.get("repo"),
                       "status": item["status"], "due": due, "recurrence": item.get("recurrence"),
                       "body": _body(item)})
    print(json.dumps(chosen, indent=1, ensure_ascii=False))


def show(selected):
    for item in json.loads(Path(selected).read_text(encoding="utf-8")):
        print(f"sd:{item['id']}  {item['repo'] or 'no repo'}  {item['title']}")


def prompt(template, selected, account):
    items = json.loads(Path(selected).read_text(encoding="utf-8"))
    print(Path(template).read_text(encoding="utf-8").rstrip())
    print(f"\nGoogle account (user_google_email): {account}\n\nItems:\n")
    for item in items:
        print(f"- Item sd:{item['id']} ({item['kind']}, {item['repo'] or 'no repo'}, due {item['due']}"
              f"{', repeats ' + item['recurrence'] if item['recurrence'] else ''}): {item['title']}")
        for line in item["body"].strip().splitlines():
            print(f"  {line}")


def _reply(text):
    """The JSON object in the model's reply: the text from its first `{` to its last `}`."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise Refused("the reply holds no JSON object")
    try:
        found = json.loads(text[start:end + 1])
    except ValueError as failure:
        raise Refused(f"the reply is not JSON: {failure}") from None
    if not isinstance(found, dict) or not isinstance(found.get("items"), list):
        raise Refused('the reply must be {"items": [...]}')
    return found["items"]


def _lines(entry, key):
    value = entry.get(key) or []
    return [str(line).strip() for line in value if str(line).strip()] if isinstance(value, list) else []


def _note(entry, due):
    lines = [f"{MARK.format(due=due)} {str(entry.get('summary') or 'nothing to draft').strip()}"]
    lines += [f"- {line}" for line in _lines(entry, "checklist")]
    lines += [f"Draft: {line}" for line in _lines(entry, "drafts")]
    lines += [f"Link: {line}" for line in _lines(entry, "links")]
    return "\n".join(lines)


def apply(sd, selected, result, due):
    """Judge the run by its result object, never the exit code (system sd:6012 probe #28947).

    An error subtype, `is_error`, or no result object writes no note. A
    permission denial still writes the notes of a valid reply, so the
    operator finds the drafts, then fails the run naming the tool.
    """
    wanted = {item["id"] for item in json.loads(Path(selected).read_text(encoding="utf-8"))}
    found = _json(Path(result).read_text(encoding="utf-8"), "claude -p")
    if not isinstance(found, dict) or found.get("type") != "result":
        raise Refused("claude -p printed no result object")
    if found.get("subtype") != "success" or found.get("is_error") is not False:
        raise Refused(f"the model run ended {found.get('subtype')} (is_error {found.get('is_error')}); no note written")
    entries = _reply(str(found.get("result") or ""))
    noted, failures = set(), []
    for entry in entries:
        item = entry.get("id") if isinstance(entry, dict) else None
        if item not in wanted:
            print(f"home-prep: the reply names sd:{item}, which was not selected; no note", file=sys.stderr)
            continue
        if item in noted or set(entry) - REPLY_KEYS - {"id"}:
            print(f"home-prep: the reply's entry for sd:{item} is repeated or holds unknown keys; no note",
                  file=sys.stderr)
            continue
        _run([sd, "task", "note", str(item), _note(entry, due)])
        noted.add(item)
        print(f"home-prep: sd:{item} prepped")
    missing = sorted(wanted - noted)
    if missing:
        failures.append("no prep for " + ", ".join(f"sd:{item}" for item in missing))
    denials = found.get("permission_denials") or []
    if denials:
        tools = sorted({str(denial.get("tool_name")) for denial in denials if isinstance(denial, dict)})
        failures.append("the model was denied " + ", ".join(tools or ["a tool"]))
    if failures:
        raise Refused("; ".join(failures))


def main(argv):
    steps = {"tomorrow": (lambda: print(date.today() + timedelta(days=1)), 0), "select": (select, 3),
             "show": (show, 1), "prompt": (prompt, 3), "apply": (apply, 4)}
    step = steps.get(argv[0]) if argv else None
    if step is None or len(argv) - 1 != step[1]:
        print(__doc__, file=sys.stderr)
        return 1
    try:
        step[0](*argv[1:])
    except Refused as refusal:
        print(f"home-prep: {refusal}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

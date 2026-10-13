"""The runtime catalog, trials and review requests.

`document` is what the v2 Skills page (sd:2123) reads, `/api/skills`: the catalog `skills_catalog.catalog` reads, each
skill's use in the last three Monday-start weeks from `skill_use`, and the use records no catalog skill explains. It
sends no local path: the pack root and the per-file digests stay here, and a skill's source is its path inside the
pack. `review_controls` is what the item page renders; the old screen was retired in sd:3402.
`imported` names the GitHub source of a skill `sd skill intake` installed, which turns on the page's Re-import; the run
itself is `skill_intake`'s, so this module still calls no git (sd:3992).
"""

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sd_db import skills_catalog, workflow

from .controls import form
from .markup import join, tag


def review_controls(connection, row, revision):
    if row["kind"] != "skill-review":
        return ""
    state = workflow.item_state(connection, row["id"])
    proposals = [note for note in state["notes"] if note["kind"] == "proposal" and not note["resolved_at"]]
    if not proposals:
        return tag("p", "Review proposals will appear here. An empty completed review has no changes to apply.", class_="hint")
    choices = []
    import json

    for note in proposals:
        try:
            detail = json.loads(note["body"])
        except ValueError:
            continue
        if not isinstance(detail, dict) or not {"path", "line_start", "line_end", "body"} <= set(detail):
            continue
        identifier = f"proposal-{note['id']}"
        choices.append(tag("label", tag("input", type="checkbox", name="notes", value=str(note["id"]), id=identifier),
            f" {detail['path']}:{detail['line_start']}–{detail['line_end']} · {detail['body']}", for_=identifier, class_="selection-option"))
    return tag("section", tag("h2", "Review proposals"),
        tag("p", "Select the changes you want. Apply queues one assignment for the whole selection.", class_="hint"),
        form(f"/api/skill-reviews/{row['id']}/apply", join(choices), label="Accept and apply selected",
             command=f"sd skill apply {row['id']} NOTE_IDS", revision=revision), class_="control-panel")


#: The weeks a skill's use is counted over, newest last.
WEEKS = 3


def _monday(moment: datetime) -> datetime:
    day = moment.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return day - timedelta(days=day.weekday())


def _stamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _modes(uses) -> dict:
    out: dict = {}
    for entry in uses:
        out[entry["mode"]] = out.get(entry["mode"], 0) + entry["count"]
    return out


def document(connection, *, now: str) -> dict:
    """The page's one document. A catalog that cannot be read raises `WorkflowError`, `OSError` or `ValueError`."""
    inventory = skills_catalog.catalog(connection, now=now)
    starts = [_monday(_stamp(now)) - timedelta(weeks=WEEKS - 1 - i) for i in range(WEEKS)]
    week = lambda stamp: next((i for i in range(WEEKS - 1, -1, -1) if _stamp(stamp) >= starts[i]), None)
    names = {skill["name"] for skill in inventory["skills"]}
    per, last, totals, surfaces, other = {}, {}, [0] * WEEKS, {}, {}
    junk = count = 0
    first = latest = None
    for row in skills_catalog.use_rows(connection):
        count += 1
        first, latest = first or row["timestamp"], row["timestamp"]
        surfaces[row["surface"] or "unknown"] = surfaces.get(row["surface"] or "unknown", 0) + 1
        at = week(row["timestamp"])
        if at is not None:
            totals[at] += 1
        if "/" in row["skill"]:
            junk += 1
            continue
        if row["skill"] not in names:
            other[row["skill"]] = other.get(row["skill"], 0) + 1
            continue
        last[row["skill"]] = row["timestamp"]
        if at is not None:
            per.setdefault(row["skill"], [0] * WEEKS)[at] += 1
    skills = []
    for skill in inventory["skills"]:
        trial = skill["trial"]
        skills.append({
            "name": skill["name"], "status": skill["status"], "paths": skill["paths"],
            "description": skill["description"], "when": skill["when"], "source": skill["path"],
            "installed": sorted({entry["surface"] for entry in skill["installed"] if entry["state"] == "current"}),
            "changed": sorted({entry["surface"] for entry in skill["installed"] if entry["state"] != "current"}),
            "trial": [trial["started"], trial["expires"]] if trial else None,
            "weeks": per.get(skill["name"], [0] * WEEKS),
            "modes": _modes(skill["uses"]),
            "last": last.get(skill["name"]), "revision": skill["revision"],
            "imported": imported(Path(inventory["root"]) / skill["path"]),
        })
    return {
        "read": inventory["observed_at"],
        "paths": {name: value.get("summary", "") for name, value in inventory["paths"].items()},
        "weeks": [start.date().isoformat() for start in starts],
        "totals": totals,
        "skills": skills,
        "junk": junk,
        "surfaces": surfaces,
        "use": {"first": first, "last": latest, "rows": count},
        "other": sorted(([name, n] for name, n in other.items()), key=lambda pair: (-pair[1], pair[0])),
        "attention": _attention(skills, junk),
    }


def _attention(skills, junk) -> dict:
    """The rail badge (sd:3446): each path skill with no use in the weeks shown, and the use-record fault. Never warning."""
    from .v2.pages import attention

    unused = [skill for skill in skills if skill["status"] == "path" and not sum(skill["weeks"])]
    return attention(["caution"] * (len(unused) + (1 if junk else 0)),
                     {"caution": "unused path skills and use-record faults", "ok": "skill flags"})


#: The line `sd skill intake` writes in a skill's trailing Provenance section (pack bin/sd_skill_intake.py
#: PROVENANCE_LINE_RE). The page reads it only to offer Re-import; the verb reads the source again and refuses one it did not write.
INTAKE_LINE = re.compile(r"Adopted by `sd skill intake` on \d{4}-\d{2}-\d{2} from `(https://github\.com/[^`\s]+)` "
                         r"at revision `([0-9a-f]{40}) \(sha256:[0-9a-f]{16}\)`\.")
PROVENANCE = "\n## Provenance\n"


def imported(path: Path) -> dict | None:
    """`{url, commit}` of a skill `sd skill intake` installed, from the last intake line of its one Provenance section."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    start = text.find(PROVENANCE)
    if start == -1 or text.rfind(PROVENANCE) != start:
        return None
    found = [match for line in text[start + len(PROVENANCE):].splitlines() if (match := INTAKE_LINE.fullmatch(line))]
    return {"url": found[-1][1], "commit": found[-1][2]} if found else None


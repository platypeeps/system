"""Briefs: the document behind the default UI's Briefs page (sd:2112, sd:3459).

The page is `v2/briefs.html`; it holds no rows. It reads one JSON document,
`/api/briefs`, built here by `document`.

**Which briefs.** The routine briefs the workflow database holds: one `item`
row of kind `report` and source `brief` each, read through the pack's
`routine_briefs.rows`, newest first and at most `ROWS`, without their text.
A row is one brief: its stem is the subject and the row's `id`, its series
the source, cut at the first ` · ` (an adversarial review's stem carries its
target and time after one, sd:5064), and `item` the item id, whose page
(`/item/<id>`) renders the whole Markdown. A time is given only when the brief was first filed on its
own day; else `at` is null.

**The vault copy.** `sd reports brief render` writes each row's copy into
the vault and records the `brief-copies` heartbeat. A row offers Open in
Obsidian (`open` and `rel`) only when that heartbeat is `ok`, names a folder
for the row's repository, is newer than the row's `updated_at`, and does not
list the stem as refused; else both are null and `copy` says why. Read needs
no copy. `reader.copies` carries the heartbeat's state: `ok`, `failed` with
its reason, `off`, or `unknown` when none was recorded or it was not read.

**Where it was published** (sd:5102). Each row's `pub.copies` names the destinations holding a copy, from the item's
`fields.published` and the vault copy above (`publishing.brief_copies`); `pub.unread` says when the fields were not read.

**Topics** (system sd:5778). `topics` lists the writing plugin's `sdw.topic` rows (`records.list_rows`), active
first: each one's slug, stage, the newest line of its `## Memory` section and how many lines it and `## Sources`
hold, None for a section the body lacks. The hub's brief routines rewrite both sections; nothing here writes them.
A row's `topics` is the brief's `signal_topics`, which pack sd:5798 copies into its record; None until it does, so the
page counts such a brief as not recorded, never as quiet. A failed read is `state: error` with its cause.

**What is not read.** Unread state, follow-up flags, the watchdog's failures
and each job's cadence come from mail and the weekly digest in the design. No
reader supplies them: `watchdog.available` is false and `watchdog.reason`
says so, and the page shows them unknown. Nothing here writes.
"""

from __future__ import annotations

import datetime
import importlib
import re
import sys
import urllib.parse
from pathlib import Path

from . import publishing

__all__ = ["ROWS", "SOURCE", "WATCHDOG_REASON", "document", "rows", "topics"]

#: The most rows the page takes; one more is read to know whether the list was cut.
ROWS = 200
#: The longest lead the page shows, in characters (`routine_briefs.LEAD`).
LEAD = 200
#: Why no brief has a failure, a cadence or an unread state.
WATCHDOG_REASON = "no watchdog reader: the dashboard does not read the failures the weekly digest lists"
SOURCE = "workflow database"

DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
#: The topic stages, in the order the Topics view lists them.
STAGES = ("active", "candidate", "parked", "retired")


def _library():
    """`sd_db.routine_briefs`, looked up here so a library without it is the page's error, never an empty page."""
    try:
        return importlib.import_module("sd_db.routine_briefs")
    except ImportError as missing:
        raise ValueError("the installed sd_db has no routine_briefs; install a pack with it") from missing


def read_rows(connection) -> list[dict]:
    return _library().rows(connection, limit=ROWS + 1)


def read_copies(connection) -> tuple[str, dict] | None:
    return _library().copies_state(connection)


def read_topics(connection) -> list[dict]:
    from sd_db import records

    return records.list_rows(connection, "topic")


def _vault_name() -> str | None:
    """The vault's name for an `obsidian://` link, by the checkout's shared rule; None when there is none."""
    lib = str(Path(__file__).resolve().parents[2] / "lib")
    if lib not in sys.path:
        sys.path.insert(0, lib)
    import system_tools_config

    return system_tools_config.obsidian_vault().name or None


def _instant(value) -> datetime.datetime | None:
    """An ISO instant with its zone, in UTC; None for anything else."""
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment.astimezone(datetime.UTC) if moment.tzinfo else None


def copies(found) -> dict:
    """`reader.copies`: the heartbeat's state, its reason and when it was recorded."""
    if isinstance(found, Exception):
        return {"state": "unknown", "reason": f"the brief-copies heartbeat was not read: {found}", "at": None}
    if found is None:
        return {"state": "unknown", "reason": "no brief render has recorded a heartbeat yet", "at": None}
    stamp, body = found
    state = body.get("state") if isinstance(body, dict) else None
    if state == "ok":
        return {"state": "ok", "reason": "", "at": stamp}
    if state == "off":
        return {"state": "off", "reason": "no repository's obsidian channel is on, so no copy is written", "at": stamp}
    if state == "failed":
        refused = body.get("refused") or []
        why = body.get("reason") or (f"{len(refused)} copies refused" if refused else "the heartbeat says failed")
        return {"state": "failed", "reason": why, "at": stamp}
    return {"state": "unknown", "reason": f"the heartbeat names no known state: {state!r}", "at": stamp}


def _copy(stem: str, repo, updated, found, state: dict, vault: str | None) -> tuple[str | None, str | None, str]:
    """`(rel, open, why)`: the vault copy's path and link when the heartbeat vouches for it, else Nones and why not."""
    if state["state"] != "ok":
        return None, None, f"vault copy {state['state']}: {state['reason']}" if state["reason"] else f"vault copy {state['state']}"
    stamp, body = found
    folder = (body.get("folder") or {}).get(repo) if isinstance(body.get("folder"), dict) else None
    if not isinstance(folder, str) or not folder:
        return None, None, "the last render names no vault folder for this brief's repository"
    rendered, changed = _instant(stamp), _instant(updated)
    if rendered is None or changed is None or rendered <= changed:
        return None, None, "the last render is not newer than this brief, so its copy may be older"
    if stem in (body.get("refused") or []):
        return None, None, "the last render refused this brief's copy"
    if not vault:
        return None, None, "no vault is named, so there is no link"
    rel = f"{folder.rstrip('/')}/{stem}.md"
    return rel, "obsidian://open?vault=%s&file=%s" % (urllib.parse.quote(vault), urllib.parse.quote(rel[:-3])), ""


def _row(raw, found, state: dict, vault: str | None) -> dict | None:
    """One page row from one library row, or None when the row is not one."""
    if not isinstance(raw, dict) or not isinstance(raw.get("brief"), dict):
        return None
    stem, item, brief = raw.get("title"), raw.get("id"), raw["brief"]
    series, day, words, lead = (brief.get(k) for k in ("series", "day", "words", "lead"))
    at = _instant(brief.get("at"))
    if not all(isinstance(v, str) and v for v in (stem, series)) or not isinstance(item, int) or isinstance(item, bool):
        return None
    if not isinstance(day, str) or not DAY.fullmatch(day) or at is None:
        return None
    if not isinstance(words, int) or isinstance(words, bool) or words < 0 or not isinstance(lead, str):
        return None
    try:  # The shape is not a date: 2026-02-30 matches DAY and is no day.
        datetime.date.fromisoformat(day)
    except ValueError:
        return None
    stamp = at.strftime("%Y-%m-%dT%H:%M:%SZ")
    signal = brief.get("signal_topics")
    signal = signal if isinstance(signal, list) and all(isinstance(t, str) and t for t in signal) else None
    rel, link, why = _copy(stem, raw.get("repo"), raw.get("updated_at"), found, state, vault)
    return {"id": stem, "item": item, "subj": stem, "src": series.split(" · ", 1)[0], "day": day, "at": stamp if stamp[:10] == day else None,
            "words": words, "lead": lead[:LEAD], "rel": rel, "open": link, "copy": why, "topics": signal}


def rows(got: list, found, state: dict, vault: str | None) -> tuple[list[dict], int]:
    """The page rows, newest first and at most `ROWS`, and how many library rows were not rows."""
    out, bad, seen = [], 0, set()
    for raw in got[:ROWS]:
        row = _row(raw, found, state, vault)
        if row is None or row["id"] in seen:
            bad += 1
            continue
        seen.add(row["id"])
        out.append(row)
    out.sort(key=lambda r: (r["day"], r["at"] or "", r["item"]), reverse=True)
    return out, bad


def _section(body: str, name: str) -> list[str] | None:
    """The non-blank lines under `## <name>`, list marks dropped, up to the next `## `; None when there is no such heading."""
    lines, found = None, False
    for line in body.split("\n"):
        if line.startswith("## "):
            if found:
                break
            found = line[3:].strip() == name
            lines = [] if found else lines
        elif found and line.strip():
            lines.append(re.sub(r"^\s*[-*]\s+", "", line).strip())
    return lines


def topics(connection, reader=read_topics) -> dict:
    """The Topics view's rows: each `sdw.topic` row's slug, stage, newest `Memory` line and section sizes."""
    try:
        got = reader(connection)
        if not isinstance(got, list):
            raise ValueError("the topic reader returned no list")  # noqa: TRY004 - external document validation
    except Exception as error:  # noqa: BLE001 - a failed read says its cause, never no topics
        return {"state": "error", "reason": str(error) or type(error).__name__, "rows": []}
    out = []
    for raw in got:
        fields, body = raw.get("fields") or {}, raw.get("body") or ""
        memory, sources = _section(body, "Memory"), _section(body, "Sources")
        slug = fields.get("slug") if isinstance(fields, dict) else None
        out.append({"item": raw["id"], "title": raw["title"], "slug": slug if isinstance(slug, str) and slug else None,
                    "stage": raw.get("stage"), "memory": memory[0][:LEAD] if memory else None,
                    "memory_lines": None if memory is None else len(memory),
                    "sources": None if sources is None else len(sources)})
    rank = {stage: i for i, stage in enumerate(STAGES)}
    out.sort(key=lambda row: (rank.get(row["stage"], len(STAGES)), row["title"].casefold()))
    return {"state": "read", "reason": "", "rows": out}


def _publishing(connection, briefs: list[dict], repos: dict) -> None:
    """sd:5102: each row's `pub`: its copies (item `fields.published`, the vault copy, a pending request), its Publish to
    choice (`fields.publish`, ruling note #23223) and what was not read. Unknown is never "published nowhere"."""
    try:
        fields = publishing.item_fields(connection, [brief["item"] for brief in briefs])
        unread = ""
    except Exception as error:  # noqa: BLE001 - fields not read are unknown, never "published nowhere"
        fields, unread = {}, f"where each brief was published was not read: {error}"
    try:
        queue = publishing.kit().QUEUE
    except ImportError as missing:
        queue, unread = None, "; ".join(filter(None, (unread, f"the pack's mirror queue was not read: {missing}")))
    from sd_db import paths

    settings: dict = {}
    for brief in briefs:
        own = fields.get(brief["item"], {})
        repo = repos.get(brief["item"])
        checkout = paths.expand(repo) if isinstance(repo, str) and repo else None
        if checkout is not None and checkout not in settings:
            settings[checkout] = publishing.read_channels(connection, checkout)
        setting, why = settings.get(checkout, (None, "the brief names no repository"))
        queued = publishing.brief_queued(checkout, brief["id"], queue) if checkout is not None else set()
        found = publishing.brief_copies(own.get("published") if isinstance(own.get("published"), dict) else {},
                                        brief["open"], queued)
        brief["pub"] = {**found, "unread": unread, "choice": publishing.brief_choice(own, setting, why)}
        if why and not unread:
            brief["pub"]["choice"]["why"] = why


def document(connection, *, now: str, reader=read_rows, copies_reader=read_copies, vault=_vault_name,
             topic_reader=read_topics) -> dict:
    """The Briefs page's document: the database's brief rows, the vault copy's state, the topics, and what is not read."""
    watchdog = {"available": False, "reason": WATCHDOG_REASON}
    found_topics = topics(connection, topic_reader)
    try:
        found = copies_reader(connection)
    except Exception as error:  # noqa: BLE001 - an unread heartbeat is unknown, never ok
        found = error
    state = copies(found)
    try:
        got = reader(connection)
        if not isinstance(got, list):
            raise ValueError("the brief reader returned no list")  # noqa: TRY004 - external document validation
    except Exception as error:  # noqa: BLE001 - a failed read says its cause, never an empty page
        return {"read": now, "reader": {"state": "error", "reason": str(error) or type(error).__name__, "source": SOURCE,
                                        "total": 0, "shown": 0, "skipped": 0, "capped": False, "copies": state},
                "briefs": [], "watchdog": watchdog, "topics": found_topics}
    try:
        name = vault() if state["state"] == "ok" else None
    except Exception:  # noqa: BLE001 - no vault name is no link; Read still opens the brief
        name = None
    briefs, bad = rows(got, found, state, name)
    for brief in briefs:
        brief["pub"] = None
    _publishing(connection, briefs, {raw.get("id"): raw.get("repo") for raw in got if isinstance(raw, dict)})
    capped = len(got) > ROWS
    return {"read": now,
            "reader": {"state": "partial" if bad else "read", "reason": f"{bad} rows were not briefs" if bad else "",
                       "source": SOURCE, "total": None if capped else len(briefs), "shown": len(briefs), "skipped": bad,
                       "capped": capped, "copies": state},
            "briefs": briefs, "watchdog": watchdog, "topics": found_topics}

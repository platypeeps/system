"""Your mail (sd:4191): on Today, the mail threads waiting on the operator, under the repo whose alias they came to.

A repo names its aliases in its channels (`email`, `sd_db.repos.mail_aliases`); each is a label of `local-mail-intake`'s
conf. The rows are mail-intake's own state, read and never written: no fetch, no stamp, no Jev. Each thread's newest
arrival comes first, since the file keeps every change, and then the filter: an answered thread's newest arrival waits on
them, so filtering first would show it. Unknown is not none: a cause that hides a repo's mail is said under that repo.
"""

from __future__ import annotations

import csv
import os
import sqlite3
from datetime import datetime, timedelta
from urllib.parse import quote

from sd_db import repos
from sd_db.errors import SdDbError

#: An arrival older than this has aged out; nothing marks a thread done (design sd:4191).
WINDOW = timedelta(hours=48)
#: A last fetch older than this leaves the rows unknown: the morning digest fetches daily; this allows one late run.
STALE = timedelta(hours=36)
#: Rows shown; the rest are counted.
SHOWN = 10
#: The arrival columns the rows are chosen by.
NEEDED = ("detected_at", "thread_id", "alias", "waiting_on")


def _when(stamp: str) -> datetime | None:
    try:
        when = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return None
    return when if when.tzinfo else None


def _state(environ, now: datetime):
    """`(arrivals, config, conf_path, reason)`: each thread's newest arrival, the parsed conf, its path, and why none can
    be used or ""."""
    from . import notes_screen

    try:
        module = notes_screen._mail_intake()
    except (OSError, ImportError, SyntaxError, RuntimeError) as error:
        return {}, None, None, f"cannot load local-mail-intake: {error}"
    conf_path, state_dir = module.paths(environ)
    if not state_dir.is_dir():
        return {}, None, conf_path, f"{state_dir} does not exist: mail-intake has not fetched on this machine"
    try:
        text = conf_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, None, conf_path, f"{conf_path} does not exist"
    except (OSError, UnicodeDecodeError) as error:
        return {}, None, conf_path, f"cannot read {conf_path}: {error}"
    config, problems = module.parse_config(text)
    if config is None:
        return {}, None, conf_path, f"{conf_path}: {'; '.join(problems)}"
    path = state_dir / "threads.csv"
    try:
        stamps = [row[3] for row in module.read_thread_rows(path).values()]
    except (OSError, csv.Error, UnicodeDecodeError) as error:
        return {}, None, conf_path, f"cannot read {path}: {error}"
    seen = [(when, stamp) for stamp in stamps if (when := _when(stamp))]
    if not seen:
        return {}, None, conf_path, f"{path} records no fetch: mail-intake has not fetched on this machine"
    last, stamp = max(seen)
    if now - last > STALE:
        return {}, None, conf_path, f"the last fetch was at {stamp}, over 36 hours ago: check the mail-intake fetch job"
    path = state_dir / "mail-arrivals.csv"
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            header = next(csv.reader(handle), [])
        rows = module.read_arrivals(path, unreported_only=False)
    except FileNotFoundError:
        header, rows = list(NEEDED), []  # fetched, and nothing has arrived yet
    except (OSError, csv.Error, UnicodeDecodeError) as error:
        return {}, None, conf_path, f"cannot read {path}: {error}"
    # `read_arrivals` drops a row without a thread, so a file without these columns would read as no mail.
    if missing := [column for column in NEEDED if column not in header]:
        return {}, None, conf_path, f"{path} has no {', '.join(missing)} column: fix or move the file"
    # A fetch stopped mid-append leaves a short line, and mail-intake writes no other `waiting_on` and no row without a
    # time: read as rows, any of these would hide or invent a thread's newest arrival.
    for row in rows:
        if None in row or None in row.values() or row["waiting_on"] not in ("you", "them") or not _when(row["detected_at"]):
            return {}, None, conf_path, f"{path} has a row it cannot read (thread {row['thread_id']}): fix or remove that line"
    return {row["thread_id"]: row for row in rows}, config, conf_path, ""


def document(connection: sqlite3.Connection, *, now: str, environ=None) -> dict:
    """`mail`, the threads shown, newest first; `mail_more`, how many past `SHOWN`; `mail_unread`, one `{repo, reason}` per
    repo whose mail cannot be read; `mail_error`, why no repo's aliases could be read, or "".

    Repos without aliases read nothing, so a machine with no mail-intake state says nothing for them.
    """
    from .screens import _repo_labels

    out = {"mail": [], "mail_more": 0, "mail_unread": [], "mail_error": ""}
    read = getattr(repos, "mail_aliases", None)
    if read is None:
        return {**out, "mail_error": "this sd_db has no repos.mail_aliases; update the command pack"}
    labels, unread = {}, {}
    try:
        for row in repos.registered(connection):
            try:
                if found := read(connection, row["path"]):
                    labels[row["path"]] = found
            except (SdDbError, sqlite3.Error) as failure:
                unread[row["path"]] = str(failure) or "its channels could not be read"
    except (SdDbError, sqlite3.Error) as failure:
        return {**out, "mail_error": str(failure) or "the repos could not be read"}
    label = _repo_labels([*labels, *unread])
    if labels:
        clock = datetime.fromisoformat(now)
        arrivals, config, conf_path, reason = _state(os.environ if environ is None else environ, clock)
        shown = []
        for path, names in labels.items():
            if reason:
                unread[path] = reason
                continue
            if missing := [name for name in names if name not in config.aliases]:
                unread[path] = f"alias {', '.join(missing)} is not in {conf_path}"
                continue
            for arrival in arrivals.values():
                detected = _when(arrival.get("detected_at") or "")
                if (arrival.get("alias") in names and arrival.get("waiting_on") == "you"
                        and detected and clock - detected <= WINDOW):
                    shown.append((_when(arrival.get("sent_at") or "") or detected, path, arrival))
        shown.sort(key=lambda found: found[0], reverse=True)
        out["mail"] = [{
            "repo": label(path), "repo_path": path, "thread": arrival["thread_id"], "subject": arrival.get("subject") or "",
            "who": arrival.get("who") or "", "at": arrival.get("sent_at") or arrival.get("detected_at") or "",
            # The thread in the mailbox mail-intake reads, the account Draft email saves to.
            "url": f"https://mail.google.com/mail/?authuser={quote(config.account)}#all/{quote(arrival['thread_id'], safe='')}",
        } for _, path, arrival in shown[:SHOWN]]
        out["mail_more"] = max(0, len(shown) - SHOWN)
    out["mail_unread"] = [{"repo": label(path), "repo_path": path, "reason": reason} for path, reason in unread.items()]
    return out

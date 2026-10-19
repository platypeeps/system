"""Notes: what happened each day, behind the default UI's Notes page (sd:2120).

The page is `v2/notes.html`; it holds no rows. It reads one JSON document,
`/api/notes`, built here for the last `DAYS` days, each a day in this
machine's own time zone, from records the library already keeps:

- **merges**: the delivery notes `sd-ship` writes when it lands a pull
  request, as the Activity page reads them (`activity_screen.merges`). The
  design walked each checkout's first-parent history; a delivery note is the
  same merge, recorded once, with no git walk.
- **done** and **opened**: the `status_change` notes of the day
  (`reads.status_change_notes`): a move to `done`, and `opened as`.
- **runs**: runner assignments that started or ended that day, as Activity
  reads them (`activity_screen.runs`).

- **daily**: each day's daily note text (sd:3426), from the Obsidian vault
  named by `OBSIDIAN_VAULT` when exported, else from `<config>/shared.env`, else its
  default `~/Documents/Obsidian Vault`. The note's name is the one Obsidian's
  daily-notes settings give it (`.obsidian/daily-notes.json`: `folder` and
  the moment `format`, default `YYYY-MM-DD`); a day folder that holds the
  note of the same name (`<folder>/<day>/<day>.md`) counts too. A day with no
  note says where it looked, and a vault that is not set says so: neither is
  shown as text.

Mail (sd:3425) is its own read, `mail`, behind `/api/notes/mail`, so a slow
mail server never holds up the days: the newest `MAIL_ROWS` inbox messages of
the last `MAIL_DAYS` days, sender, subject and time, from Gmail through
workspace-mcp. It reaches the server with `local-mail-intake`'s client, at
`WORKSPACE_MCP_URL` and as `EMAIL_FROM`, the account `local-notify` sends
from: each exported, else from `<config>/notify/.env`. It asks for headers,
never bodies, and closes its session. The whole read is bounded by
`MAIL_SECONDS`; a server that is down or slower is `down`, an account not set
is `unconfigured`, and a tool error such as a lapsed grant is `error`, each
with the reason, never unknown.

A source that raises is named in `sources` with its reason and adds no rows,
so a failed read never looks like a quiet day. The document writes nothing.

Quick notes (sd:2549) are `sdw.quick-note` items in the vault, a kind
the writing pack declares (sd:2196). `quick_notes` lists them through `sd store
list`, and `quick_add` keeps one through `sd store add`, each `sd` from PATH
with an argv list and no shell. sd does not enforce the kind's rules, so
`quick_text` does (operator ruling on sd:2549): non-empty, one line, no other
control character, no double quote, at most `QUICK_MAX` characters. The title
is the local time, `YYYY-MM-DD HHMMSS`; sd refuses a title taken anywhere in
the vault, and the next try adds ` 2`, ` 3` and so on.
"""

from __future__ import annotations

import functools
import importlib.util
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import unicodedata
import urllib.request
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from sd_db import reads
from sd_db.errors import SdDbError

from . import activity_screen

__all__ = ["DAILY_MAX", "DAYS", "MAIL_DAYS", "MAIL_ROWS", "MAIL_SECONDS", "QUICK_KIND", "QUICK_MAX", "KindMissing", "daily_notes",
           "document", "mail", "quick_add", "quick_notes", "quick_text"]

#: Days the document carries, today included.
DAYS = 7

#: The inbox the page lists: the newest `MAIL_ROWS` messages of the last `MAIL_DAYS` days.
MAIL_ROWS = 15
MAIL_DAYS = 2
#: The whole mail read's ceiling, in seconds: session, search, header fetch and close.
MAIL_SECONDS = 8
#: workspace-mcp's address when neither the environment nor local-notify's config names one, as local-notify has it.
MAIL_URL = "http://127.0.0.1:8083/mcp"

#: The default vault when nothing names one, as the obsidian tools have it.
VAULT_DEFAULT = "~/Documents/Obsidian Vault"
#: Obsidian's own daily-note format when its settings name none.
DAILY_FORMAT = "YYYY-MM-DD"
#: The most daily note text one day carries, in characters; the page says when a note was cut.
DAILY_MAX = 20000
_MOMENT = re.compile(r"\[([^\]]*)\]|YYYY|YY|MMMM|MMM|MM|M|DD|D|dddd|ddd|[A-Za-z]")
_FRONT = re.compile(r"\A---\r?\n.*?\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)

_MOVE = re.compile(r"^(\S+) -> (\S+) by (.+?)(?::|$)")
FAILURES = (SdDbError, sqlite3.Error, OSError, ValueError)

#: The store kind a quick note is kept as.
QUICK_KIND = "sdw.quick-note"
#: The longest quick note, in characters, once newlines are spaces: a thought, a link or a name, not a page.
QUICK_MAX = 1000
#: One `sd store` call's ceiling; `add` walks the whole vault for a title collision.
SD_SECONDS = 30
#: Titles one keep tries: the timestamp, then the suffixes ` 2` to ` 9` for notes kept in the same second.
QUICK_TRIES = 9
#: What sd says when the plugin or its kind is not installed here.
_NO_KIND = re.compile(r"declares no kind|no registered plugin has prefix")


class KindMissing(ValueError):
    """`sdw.quick-note` is not installed on this machine; the page says so instead of failing."""


def _sd(*arguments: str) -> str:
    """`sd store <arguments>`'s stdout; a failure is a ValueError carrying sd's own last line."""
    try:
        done = subprocess.run(["sd", "store", *arguments], capture_output=True, text=True, timeout=SD_SECONDS, check=False)
    except FileNotFoundError:
        raise ValueError("sd is not on the dashboard's PATH, so quick notes cannot be read or kept") from None
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError(f"sd store could not finish: {error}") from None
    if done.returncode:
        said = (done.stderr.strip().splitlines() or [f"sd store exited {done.returncode}"])[-1]
        if _NO_KIND.search(said):
            raise KindMissing(f"{QUICK_KIND} is not installed on this machine: {said}")
        raise ValueError(said)
    return done.stdout


def quick_text(raw) -> str:
    """The note as `sd store add` gets it: newlines flattened to spaces and the ends trimmed; ValueError says which rule failed."""
    if not isinstance(raw, str):
        raise ValueError("A quick note is text.")
    text = re.sub(r"\r\n|[\r\n\u2028\u2029]", " ", raw).strip()
    if not text:
        raise ValueError("The quick note is empty.")
    if any(unicodedata.category(c) == "Cc" for c in text):
        raise ValueError("A quick note holds no control character, such as a tab.")
    if '"' in text:
        raise ValueError('A quick note holds no double quote (").')
    if len(text) > QUICK_MAX:
        raise ValueError(f"A quick note holds at most {QUICK_MAX} characters; this one has {len(text)}.")
    return text


def quick_notes() -> list[dict]:
    """Every `sdw.quick-note`, newest title first; KindMissing when the kind is not installed."""
    try:
        out = _sd("list", QUICK_KIND, "--json")
    except KindMissing:
        raise
    except ValueError as error:
        # `sd store add` makes the kind's folder; until the first note is kept, `list` refuses the folder as missing.
        if "the vault does not hold this kind" in str(error):
            return []
        raise
    rows = json.loads(out)
    if not isinstance(rows, list):
        raise ValueError(f"sd store list {QUICK_KIND} did not return a list")
    notes = [{"text": str(row.get("text") or ""), "title": str(row.get("title") or "")} for row in rows if isinstance(row, dict)]
    return sorted(notes, key=lambda note: note["title"], reverse=True)


def quick_add(text: str) -> dict:
    """Keep `text`, already passed through `quick_text`, under the first free timestamp title; returns the title and text."""
    stamp = datetime.now().strftime("%Y-%m-%d %H%M%S")
    for n in range(1, QUICK_TRIES + 1):
        title = stamp if n == 1 else f"{stamp} {n}"
        try:
            _sd("add", QUICK_KIND, title, "--field", f"text={text}")
        except KindMissing:
            raise
        except ValueError as error:
            if "already exists" in str(error):
                continue
            raise
        return {"title": title, "text": text}
    raise ValueError(f"Every title from {stamp} to {stamp} {QUICK_TRIES} is taken; keep the note again.")


def _vault(environ) -> Path:
    """The vault the obsidian tools read; ValueError says why there is none, `.unconfigured` set when nothing names one."""
    lib = str(Path(__file__).resolve().parents[2] / "lib")
    if lib not in sys.path:
        sys.path.insert(0, lib)
    import system_tools_config

    named = system_tools_config.shared("OBSIDIAN_VAULT", environ)
    source = "OBSIDIAN_VAULT"
    if not named:
        default = Path(os.path.expanduser(VAULT_DEFAULT))
        if default.is_dir():
            return default
        problem = ValueError(system_tools_config.missing("OBSIDIAN_VAULT", "", "shared.env", "local-config-check", environ=environ)
                             + f" The default vault, {default}, is not a folder either.")
        problem.unconfigured = True
        raise problem
    vault = Path(os.path.expanduser(named))
    if not vault.is_dir():
        raise ValueError(f"The vault {source} names, {vault}, is not a folder.")
    return vault


def _moment(fmt: str, day: date) -> str:
    """`day` written in the moment.js `fmt` Obsidian names daily notes with; ValueError on a token this page does not read."""
    tokens = {"YYYY": f"{day:%Y}", "YY": f"{day:%y}", "MMMM": f"{day:%B}", "MMM": f"{day:%b}", "MM": f"{day:%m}",
              "M": str(day.month), "DD": f"{day:%d}", "D": str(day.day), "dddd": f"{day:%A}", "ddd": f"{day:%a}"}

    def one(match):
        if match.group(1) is not None:
            return match.group(1)
        if match.group(0) not in tokens:
            raise ValueError(f"The daily-note format {fmt!r} uses {match.group(0)!r}, which this page does not read.")
        return tokens[match.group(0)]

    return _MOMENT.sub(one, fmt)


def daily_notes(days, environ=None) -> dict:
    """Each day's daily note: `ok` with its text, `none` naming where it looked, `unconfigured` or `error` with the reason."""
    environ = os.environ if environ is None else environ
    try:
        vault = _vault(environ)
        settings = vault / ".obsidian" / "daily-notes.json"
        named = json.loads(settings.read_text(encoding="utf-8")) if settings.is_file() else {}
        if not isinstance(named, dict):
            raise ValueError(f"{settings} is not a JSON object.")
        folder = str(named.get("folder") or "").strip("/")
        fmt = str(named.get("format") or "") or DAILY_FORMAT
        names = {day: _moment(fmt, date.fromisoformat(day)) for day in days}
    except (OSError, ValueError) as error:
        state = "unconfigured" if getattr(error, "unconfigured", False) else "error"
        return {day: {"state": state, "why": str(error)} for day in days}
    root, out = vault.resolve(), {}
    for day, name in names.items():
        base = f"{folder}/{name}" if folder else name
        tried = [f"{base}.md", f"{base}/{name.rsplit('/', 1)[-1]}.md"]
        out[day] = {"state": "none", "why": f"No daily note for {day} in the vault: looked for {tried[0]} and {tried[1]}."}
        for relative in tried:
            path = vault / relative
            try:
                if not path.resolve().is_relative_to(root):
                    out[day] = {"state": "error", "why": f"The daily-note folder {folder!r} leaves the vault."}
                    break
                if not path.is_file():
                    continue
                text = _FRONT.sub("", path.read_text(encoding="utf-8", errors="replace")).strip()
            except OSError as error:
                out[day] = {"state": "error", "why": f"{relative} could not be read: {error.__class__.__name__}: {error}"}
                break
            out[day] = {"state": "ok", "file": relative, "text": text[:DAILY_MAX], "cut": len(text) > DAILY_MAX}
            break
    return out


@functools.lru_cache(maxsize=1)
def _mail_intake():
    # `local-mail-intake`'s workspace-mcp client and parsers, loaded by path: the file sits beside this folder in the
    # checkout, as `collectors.py` does for the screens that load it. Once per process (sd:3532): a load per read
    # recompiled it and rebound `sys.modules` under a read in another tab. A change to the file is a build change,
    # which restarts the server.
    path = Path(__file__).resolve().parents[2] / "local-mail-intake" / "mail_intake.py"
    spec = importlib.util.spec_from_file_location("sd_dashboard_mail_intake", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # its dataclasses look their module up by name
    spec.loader.exec_module(module)
    return module


def close_session(url: str, client) -> None:
    """Close a workspace-mcp session the client opened, so each read or draft (sd:3463) leaves none behind."""
    if client.session:
        try:
            urllib.request.urlopen(urllib.request.Request(url, method="DELETE", headers={"mcp-session-id": client.session}),
                                   timeout=3).close()
        except OSError:
            pass  # a session the server cannot close expires on its own; the caller already has its answer


def workspace(environ=None) -> tuple[str, str, str]:
    """`(url, account, why)`: workspace-mcp's URL and Gmail account, each exported (an empty value too) or from `<config>/notify/.env`.

    `why` names the remedies when the account is not set, and is empty when it is. Mail reads with it, and Draft email
    (sd:3463, `email_draft`) saves with it.
    """
    environ = os.environ if environ is None else environ
    lib = str(Path(__file__).resolve().parents[2] / "lib")
    if lib not in sys.path:
        sys.path.insert(0, lib)
    import system_tools_config

    saved = system_tools_config.read_env("notify", environ)
    url = system_tools_config.setting("WORKSPACE_MCP_URL", saved, environ) or MAIL_URL
    account = system_tools_config.setting("EMAIL_FROM", saved, environ) or ""
    why = "" if account else (system_tools_config.missing("EMAIL_FROM", "notify", ".env", environ=environ)
                              + " It is the Google account workspace-mcp reads mail for.")
    return url, account, why


def _inbox(mi, url: str, account: str, budget: float) -> dict:
    """One bounded read: a session, the inbox search, the found messages' headers, then the session closed."""
    client = mi.Mcp(url, timeout=budget)
    try:
        client.connect()
        found = client.call("search_gmail_messages", {"query": f"in:inbox newer_than:{MAIL_DAYS}d",
                                                       "user_google_email": account, "page_size": MAIL_ROWS})
        ids = list(dict.fromkeys(mid for mid, _ in mi.parse_search(found)))[:MAIL_ROWS]
        blocks = []
        for start in range(0, len(ids), mi.BATCH):
            blocks += mi.parse_metadata(client.call("get_gmail_messages_content_batch", {
                "message_ids": ids[start:start + mi.BATCH], "user_google_email": account, "format": "metadata"}))
    finally:
        close_session(url, client)
    rows = []
    for fields in blocks:
        sent = mi.when(fields.get("Date", ""))
        rows.append({"id": fields["Message ID"], "from": mi.display_name(fields.get("From", "")),
                     "subject": fields.get("Subject") or "(no subject)", "at": _iso(sent.astimezone(timezone.utc)) if sent else None})
    rows.sort(key=lambda row: row["at"] or "", reverse=True)
    return {"rows": rows, "missing": len(ids) - len(rows)}


def mail(*, now: str, environ=None) -> dict:
    """The newest inbox mail: `ok` with rows, or `unconfigured`, `down` or `error` with the reason; never raises."""
    url, account, why = workspace(environ)
    if not account:
        return {"state": "unconfigured", "why": why}
    seconds = MAIL_SECONDS
    box = {}

    def read():
        try:
            mi = _mail_intake()
            try:
                box["answer"] = {"state": "ok", **_inbox(mi, url, account, seconds)}
            except mi.McpError as error:
                down = str(error).startswith("cannot reach")
                box["answer"] = {"state": "down" if down else "error",
                                 "why": f"workspace-mcp at {url}: {error}" if down else f"workspace-mcp could not read the inbox: {error}"}
        except (OSError, ValueError, RuntimeError) as error:  # a socket timeout mid-reply is an OSError, not a McpError
            box["answer"] = {"state": "down", "why": f"workspace-mcp at {url} stopped answering: {error.__class__.__name__}: {error}"}
        except Exception as error:  # noqa: BLE001 - the page names any failure rather than lose the read
            box["answer"] = {"state": "error", "why": f"The mail read failed: {error.__class__.__name__}: {error}"}

    worker = threading.Thread(target=read, name="notes-mail", daemon=True)
    worker.start()
    worker.join(seconds)
    answer = box.get("answer") or {"state": "down", "why": f"workspace-mcp did not answer at {url} within {seconds} s."}
    return {**answer, "read": now, "days": MAIL_DAYS}


def _utc(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _local_day(stamp: str) -> str:
    return _utc(stamp).astimezone().date().isoformat()


def _midnight(day: date) -> datetime:
    """The start of a local day, in UTC; the OS's zone rules give each day its own offset, so DST reads right."""
    return datetime.combine(day, time()).astimezone().astimezone(timezone.utc)


def _changes(connection, start, end):
    done, opened = [], {}
    for row in reads.status_change_notes(connection, since=_iso(start), until=_iso(end)):
        day = _local_day(row["timestamp"])
        if row["body"].startswith("opened as "):
            opened[day] = opened.get(day, 0) + 1
            continue
        move = _MOVE.match(row["body"])
        if move and move.group(2) == "done":
            done.append({"id": f"done:{row['id']}", "day": day, "at": _iso(_utc(row["timestamp"])), "item": row["item"],
                         "title": row["title"], "kind": row["kind"], "repo": activity_screen._repo_label(row["repo"]),
                         "from": move.group(1), "by": move.group(3)})
    return done, opened


def document(connection: sqlite3.Connection, *, now: str) -> dict:
    """The Notes page's document: the last `DAYS` local days, each with its daily note, merges, done items, opened count and runs."""
    today = _utc(now).astimezone().date()
    first = today - timedelta(days=DAYS - 1)
    start, end = _midnight(first), _midnight(today + timedelta(days=1))
    notes = daily_notes([(first + timedelta(days=n)).isoformat() for n in range(DAYS)])
    days = {day: {"merges": [], "done": [], "opened": 0, "runs": [], "daily": note} for day, note in notes.items()}
    sources = {}

    def read(name, fn):
        try:
            return fn()
        except FAILURES as error:
            sources[name] = f"{error.__class__.__name__}: {error}"
            return None

    for row in read("merges", lambda: activity_screen.merges(connection, start, end)) or []:
        days.get(_local_day(row["at"]), {}).setdefault("merges", []).append(
            {k: row[k] for k in ("id", "at", "repo", "what", "ref", "url", "pr", "item", "commit")})
    changes = read("changes", lambda: _changes(connection, start, end))
    if changes:
        for row in changes[0]:
            days.get(row.pop("day"), {}).setdefault("done", []).append(row)
        for day, count in changes[1].items():
            if day in days:
                days[day]["opened"] = count
    for row in read("runs", lambda: activity_screen.runs(connection, start, end)) or []:
        days.get(_local_day(row["at"]), {}).setdefault("runs", []).append(
            {k: row[k] for k in ("id", "at", "s", "repo", "what", "ref", "n", "item", "role", "provider", "status", "started", "ended")})
    return {
        "read": now,
        "today": today.isoformat(),
        "tz": datetime.combine(today, time(12)).astimezone().tzname() or "local",
        "days": days,
        "sources": sources,
    }

"""Import to Slides (pack sd:5525, #25693 option A): an experiment's built deck, imported into Google Slides.

An Experiments row posts `/api/research/slides` with `{"repo": "<key>"}`. `start` runs in the POST, one import at a
time (`IMPORTING`):

1. The deck: the checkout's `docs/dashboard/deck.pptx`, as `research_deck.built` finds it. None is a 400.
2. The work Google account: `google.accounts.work.email`, as the realm sweep reads it (`realm_sweep.account`).
3. The record, `<state>/slides.json` beside the deck runs, one entry per checkout: the Slides file id and link, the
   deck's sha256 and the account. An unchanged deck on the same account reuses that file and calls nothing.
4. A changed deck on the same account replaces that file's content in place (sd:5761): workspace-mcp's
   `update_drive_file`, Drive's files.update, keeps the file's id, name, folder, link and sharing, so the brief's Deck
   row and every link to it stay current. First the record loses its digest, so a deck Drive may now hold is never
   taken for the recorded one; a record that cannot be written sends nothing. The tool takes no bytes, only a path the
   server may read: the deck is copied into the server's attachment folder (`attachments`) for the call and removed
   after it. A tool error may come after Drive took the upload, so a refused or unanswered update leaves the file's
   content unknown, and a second press sends the deck to the same file. A file Drive no longer has (`GONE`) is made
   again, as in step 5.
5. No record, or one for another account: workspace-mcp's `import_to_google_slides` makes the file, through
   `local-mail-intake`'s client, with the deck as base64 and its sha256. A refused call made nothing; a call with no
   answer may have made a file.
6. The record, written atomically. A record that cannot be written after a call answers 503 naming the link.

`status` answers `GET /api/research/slides/status`: whether workspace-mcp answers an `initialize` and the account is
set, and the records, so the page turns the action off with the reason.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import json
import re
import threading
from pathlib import Path

from . import notes_screen, realm_sweep, research_deck
from .publish_drain import _iso, _write

__all__ = ["GONE", "PROBE_TIMEOUT", "SLIDES_TIMEOUT", "TOOL", "REPLACE", "attachments", "request", "start", "status"]

#: The tool that makes a Slides file, for an experiment that has none: an import, never a share or a delete.
TOOL = "import_to_google_slides"
#: The tool that replaces a file's content in place, for an experiment that has one: the only other call this makes.
REPLACE = "update_drive_file"
#: What Drive answers, through workspace-mcp, for a file id it no longer has.
GONE = "File not found"
SLIDES_TIMEOUT = 120
PROBE_TIMEOUT = 5
IMPORTING = threading.Lock()


def store():
    return research_deck.state_dir() / "slides.json"


def records() -> dict:
    """`{key: record}`; `{}` before the first import. A record that cannot be read raises: it is unknown, not empty."""
    try:
        found = json.loads(store().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(found, dict):
        raise ValueError(f"{store()} is not an object")
    return found


def public(record: dict) -> dict:
    return {key: record[key] for key in ("id", "link", "built", "imported") if key in record}


def _account() -> tuple[str, str]:
    rows, why = realm_sweep.held("work")
    return ("", why) if why else realm_sweep.account("work", rows)


def attachments() -> Path:
    """The folder workspace-mcp reads a `file_path` from: its attachment folder, at its default `WORKSPACE_ATTACHMENT_DIR`."""
    return Path.home() / ".workspace-mcp" / "attachments"


def _client(url: str, timeout: int):
    return notes_screen._mail_intake().Mcp(url, timeout=timeout)


def status():
    """`(200, {"ready", "why", "imports"})`: `why` names what keeps Import to Slides off, empty when nothing does."""
    url, _, _ = notes_screen.workspace()
    try:
        imports = {key: public(record) for key, record in records().items()}
    except (OSError, ValueError) as problem:
        return 503, {"error": f"The Slides record could not be read: {problem}"}
    account, why = _account()
    if not why:
        client = _client(url, PROBE_TIMEOUT)
        try:
            client.connect()
        except Exception as error:  # noqa: BLE001 - whatever broke, the server did not answer
            why = f"workspace-mcp at {url} did not answer: {error}"
        finally:
            notes_screen.close_session(url, client)
    else:
        why = f"no work Google account: {why}"
    return 200, {"ready": not why, "why": why, "imports": imports}


def _call(url: str, tool: str, arguments: dict, what: str) -> str:
    """The tool's answer; `ConnectionError` when it did nothing, `LookupError` when whether it did is not known."""
    mi = notes_screen._mail_intake()
    client = _client(url, SLIDES_TIMEOUT)
    try:
        try:
            client.connect()
        except Exception as error:  # noqa: BLE001 - nothing was sent, whatever broke
            raise ConnectionError(f"workspace-mcp at {url}: {error}") from None
        try:
            return client.call(tool, arguments)
        except mi.McpError as error:
            if str(error).startswith(f"{tool}: ") and "unparseable" not in str(error):
                raise ConnectionError(f"workspace-mcp refused {what}: {error}") from None
            raise LookupError(str(error)) from None
        except Exception as error:  # noqa: BLE001 - the call left; its answer did not come back
            raise LookupError(f"{error.__class__.__name__}: {error}") from None
    finally:
        notes_screen.close_session(url, client)


def _import(url: str, account: str, name: str, data: bytes, digest: str) -> tuple[str, str]:
    """`(file id, link)` of the Slides file made from the deck."""
    said = _call(url, TOOL, {"user_google_email": account, "file_name": name, "source_format": "pptx",
                             "base64_content": base64.b64encode(data).decode("ascii"), "base64_sha256": digest}, "the import")
    found = re.search(r"Presentation ID:\s*([\w-]+)", said)
    if not found:
        raise LookupError(f"workspace-mcp answered without a presentation id: {said[:200]}")
    link = re.search(r"Link:\s*(https://\S+)", said)
    return found[1], link[1] if link else f"https://docs.google.com/presentation/d/{found[1]}/edit"


def _update(url: str, account: str, file_id: str, data: bytes, digest: str) -> None:
    """Replaces the content of the Slides file `file_id` with the deck, in place, through a copy the server may read.

    `OSError` when the copy cannot be written, before anything is sent."""
    folder = attachments()
    folder.mkdir(parents=True, exist_ok=True)
    staged = folder / f"sd-research-deck-{digest[:16]}.pptx"
    try:
        staged.write_bytes(data)
        said = _call(url, REPLACE, {"user_google_email": account, "file_id": file_id, "file_path": str(staged),
                                   "source_format": "pptx"}, "the update")
    finally:
        staged.unlink(missing_ok=True)
    found = re.search(r"File ID:\s*([\w-]+)", said)
    if not found or found[1] != file_id:
        raise LookupError(f"workspace-mcp answered without the file id {file_id}: {said[:200]}")


def start(key: str, root=None):
    """Steps 1 to 6 above: `(200, {"slides", "reused"})`, or `(status, {"error"})` naming what is left."""
    with IMPORTING:
        deck = research_deck.built(key, root)
        if deck is None:
            raise ValueError(f"{key} has no built deck: press Build deck first.")
        account, why = _account()
        if why:
            return 503, {"error": f"Import to Slides needs the work Google account: {why}"}
        try:
            known = records()
            data = deck.read_bytes()
            stamp = datetime.datetime.fromtimestamp(deck.stat().st_mtime, datetime.timezone.utc)
        except (OSError, ValueError) as problem:
            return 503, {"error": f"Nothing was imported: {problem}"}
        digest = hashlib.sha256(data).hexdigest()
        earlier = known.get(key)
        mine = earlier if earlier and earlier.get("id") and earlier.get("account") == account else None
        if mine and mine.get("sha256") == digest:
            return 200, {"slides": public(mine), "reused": True}
        url, _, _ = notes_screen.workspace()
        updated = False
        if mine:
            try:
                known = {**known, key: {**mine, "sha256": ""}}
                _write(store(), known)
            except OSError as problem:
                return 503, {"error": f"Nothing was sent: the Slides record could not be written before the update: {problem}"}
            try:
                _update(url, account, mine["id"], data, digest)
                file_id, link, updated = mine["id"], mine["link"], True
            except ConnectionError as error:  # before OSError, its base: the update was refused, or never sent
                if GONE not in str(error):
                    return 503, {"error": f"{error}. Press Import to Slides again to send this deck to the same file, {mine['link']}."}
            except LookupError as error:
                return 504, {"error": f"workspace-mcp gave no clear answer ({error}), so whether {mine['link']} holds this "
                                      "deck is not known. Press Import to Slides again: it updates the same file."}
            except OSError as problem:
                return 503, {"error": f"Nothing was imported: the deck was not staged for workspace-mcp: {problem}"}
        if not updated:
            try:
                file_id, link = _import(url, account, f"{key.rsplit('/', 1)[-1]}: status deck", data, digest)
            except ConnectionError as error:
                return 503, {"error": f"{error}. No Slides file was made."}
            except LookupError as error:
                return 504, {"error": f"workspace-mcp gave no clear answer ({error}), so whether a Slides file was made is "
                                      "not known. Look in Drive before you press Import to Slides again."}
        record = {"id": file_id, "link": link, "sha256": digest, "account": account,
                  "built": _iso(stamp), "imported": _iso(datetime.datetime.now(datetime.timezone.utc))}
        try:
            store().parent.mkdir(parents=True, exist_ok=True)
            _write(store(), {**known, key: record})
        except OSError as problem:
            again = "A second press updates the same file again." if updated else "A second press makes another Slides file."
            return 503, {"error": f"The deck was imported as {link}, but its record was not written: {problem}. {again}"}
        return 200, {"slides": public(record), "reused": False, "updated": updated,
                     "replaces": public(earlier) if earlier and not updated else None}


def request(values: dict):
    """The POST's checks: one field, `repo`, a checkout key; the import as a write."""
    if set(values) != {"repo"} or not isinstance(values["repo"], str):
        raise ValueError('Send {"repo": "<checkout key>"}: the experiment whose deck to import.')
    return lambda connection: start(values["repo"])

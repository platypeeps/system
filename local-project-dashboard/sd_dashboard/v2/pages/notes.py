"""Notes (sd:2120): the last week, a day at a time: merges and items done and opened, from the records Activity reads,
and each day's daily note from the Obsidian vault (sd:3426). The inbox mail and the runner runs left the page (sd:5289).
Quick notes (sd:2549) are `sdw.quick-note` store items, listed and kept through `sd store` (notes_screen)."""

from __future__ import annotations

from . import Api, Page


def _read(read):
    from ... import notes_screen

    return notes_screen.document(read.connection, now=read.now)


def _quick(read):
    from ... import notes_screen

    try:
        return {"notes": notes_screen.quick_notes()}
    except notes_screen.KindMissing as problem:
        return {"notes": [], "unknown": str(problem)}
    except ValueError as problem:
        return 503, {"error": str(problem)}


def _keep(payload, principal):
    from ... import notes_screen

    # The rules are checked here, before the server opens the database, so a refused note never starts sd.
    if set(payload) != {"text"}:
        raise ValueError("Send the quick note's text only.")
    text = notes_screen.quick_text(payload["text"])
    return lambda connection: notes_screen.quick_add(text)


PAGE = Page(
    section="Notes",
    routes=("/notes",),
    html="notes.html",
    item="sd:2120",
    api=(Api("/api/notes", _read), Api("/api/notes/quick", _quick), Api("/api/notes/quick/add", write=_keep)),
)

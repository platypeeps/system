"""Writing pieces: `render` is the old screen at /classic/writing; `document` feeds /api/writing (sd:2125).

`document` is each piece's stage, gates and next moves as `writing.piece_state` gives them, its files' sizes, and the
opening of its draft. The repository goes out by its folder name only, so no local path leaves.
"""

from pathlib import Path

from sd_db import writing
from sd_db.workflow import WorkflowError

from .controls import select
from .listing import Column, Listing
from .markup import tag
from .pages import command_reference, page
from .screens import readable_time


def render(connection, *, now, parameters):
    rows = writing.list_pieces(connection, include_parked=True)
    stage = (parameters.get("stage") or [""])[0]
    readiness = (parameters.get("readiness") or [""])[0]
    parked = (parameters.get("parked") or ["active"])[0]
    query, page_number, selected = Listing.read_query(parameters)
    selected_rows = [row for row in rows
        if (not stage or row["stage"] == stage)
        and (readiness not in ("recorded", "missing")
             or bool(row["ready_digest"]) == (readiness == "recorded"))
        and (parked == "all" or bool(row["parked_at"]) == (parked == "parked"))]
    extra = {key: value for key, value in
             (("stage", stage), ("readiness", readiness), ("parked", parked)) if value}
    listing = Listing(name="pieces", path="/classic/writing", rows=selected_rows,
        query=query, page_number=page_number, selected=selected, extra=extra,
        row_id=lambda row: str(row["id"]), row_href=lambda row: f"/item/{row['id']}",
        columns=[Column("title", "Piece", lambda row: row["title"]),
            Column("stage", "Stage", lambda row: (row["stage"] or "Unknown").capitalize()),
            Column("readiness", "Readiness decision", lambda row: "Recorded" if row["ready_digest"] else "Not recorded"),
            Column("updated", "Updated", lambda row: readable_time(row["updated_at"])),
            Column("piece", "Key", lambda row: row["piece"], hidden=True)],
        empty="No pieces match these filters.",
        empty_next="Clear the filters, or import the writing collection to see its pieces here.")
    filters = tag("form",
        select("Stage", "stage", [("", "Every stage")] +
               [(value, value.capitalize()) for value in sorted({row["stage"] for row in rows if row["stage"]})], stage),
        select("Readiness decision", "readiness", [("", "Any decision"),
            ("recorded", "Recorded"), ("missing", "Not recorded")], readiness),
        select("Collection", "parked", [("active", "Active pieces"), ("parked", "Parked pieces"),
                                        ("all", "All pieces")], parked),
        tag("input", type="hidden", name="q", value=query),
        tag("button", "Apply filters", type="submit", class_="primary"),
        tag("a", "Clear filters", href="/classic/writing"), method="get", action="/classic/writing", class_="writing-filters")
    return page("Writing", "writing",
        tag("p", "From an idea to a finished piece, with the next step in view.", class_="lead"),
        command_reference("sd writing list --json"),
        filters,
        tag("p", "A recorded readiness decision is the saved review. Open a piece to check it against the current draft.", class_="hint"),
        listing.render())


#: Draft paragraphs the editor shows; the rest stay in the file.
PARAGRAPHS = 6


def _draft(document: str | None) -> tuple[list[str], int]:
    """The draft's first paragraphs and its word count, from the text after `## Draft`."""
    prose = document.split("## Draft", 1)[1] if document and "## Draft" in document else ""
    paragraphs = [" ".join(block.split()) for block in prose.split("\n\n") if block.strip() and not block.lstrip().startswith("#")]
    return paragraphs[:PARAGRAPHS], len(prose.split())


def document(connection, *, now):
    """What the v2 Writing page reads: every piece, parked ones too, with its gates, moves and files."""
    pieces = []
    for row in writing.list_pieces(connection, include_parked=True):
        full = writing.piece_state(connection, row["id"])
        state = full["writing"]
        try:
            files = writing.piece_files(row)
        except (OSError, WorkflowError):
            files = {}
        draft, words = _draft(state["document"])
        gates = state["gates"]
        claims = state.get("publications") or []
        live = next((claim for claim in reversed(claims) if claim["url"]), None)
        pieces.append({
            "id": row["id"], "title": row["title"], "stage": row["stage"], "parked": state["parked"], "piece": row["piece"],
            "repo": Path(row["repo"]).name, "folder": "content-parked" if (row["path"] or "").startswith("content-parked/") else "content",
            "updated": row["updated_at"], "revision": full["revision"],
            "gates_ok": gates["ok"], "problems": gates["problems"], "owner": state["owner"],
            "ready_recorded": bool(row["ready_digest"]) and row["ready_digest"] == gates["digest"],
            "publication": {"claim": live["id"], "url": live["url"], "phase": live["phase"]} if live else None,
            "next": state["available_stages"], "corrections": state["correction_stages"],
            "files": files, "draft": draft, "words": words,
        })
    return {"read": now, "stages": list(writing.STAGES), "pieces": pieces}

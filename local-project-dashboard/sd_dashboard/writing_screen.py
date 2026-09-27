"""Find pieces by their writing stage, with saved readiness clearly identified."""

from sd_db import writing

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
    listing = Listing(name="pieces", path="/writing", rows=selected_rows,
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
        tag("a", "Clear filters", href="/writing"), method="get", action="/writing", class_="writing-filters")
    return page("Writing", "writing",
        tag("p", "From an idea to a finished piece, with the next step in view.", class_="lead"),
        command_reference("sd writing list --json"),
        filters,
        tag("p", "A recorded readiness decision is the saved review. Open a piece to check it against the current draft.", class_="hint"),
        listing.render())

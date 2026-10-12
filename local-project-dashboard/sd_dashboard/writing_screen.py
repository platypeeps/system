"""Writing pieces: `document` feeds /api/writing (sd:2125); the old screen was retired in sd:3402.

`document` is each piece's stage, gates and next moves as `writing.piece_state` gives them, its files' sizes, and the
opening of its draft. The repository goes out by its folder name only, so no local path leaves.
"""

from pathlib import Path

from sd_db import writing
from sd_db.workflow import WorkflowError


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

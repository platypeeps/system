"""Writing pieces: `document` feeds /api/writing (sd:2125); the old screen was retired in sd:3402.

`document` is each piece's stage, gates and next moves as `writing.piece_state` gives them, its files' sizes, and the
opening of its draft. The repository goes out by its folder name only, so no local path leaves. Its `attention` is the
page's rail badge (sd:3446, `_attention`).
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
    return {"read": now, "stages": list(writing.STAGES), "pieces": pieces, "attention": _attention(pieces)}


def _gate_state(piece) -> str:
    """A piece's loudest gate lamp, as writing.js `gates` lights them: a verdict not pass is warning; an unstamped
    research file, an unresolved finding or another gate problem with its file present is caution."""
    states = []
    if any("research.md" in problem for problem in piece["problems"]) and piece["files"].get("research.md"):
        states.append("caution")
    for name in ("fact-check", "adversarial"):
        found = [problem for problem in piece["problems"] if name in problem]
        if not found or not piece["files"].get(f"{name}.md"):
            continue
        if any("verdict is not pass" in problem for problem in found):
            states.append("warning")
        elif any("unresolved" in problem for problem in found):
            states.append("caution")
        elif not any("no explicit current gate record" in problem for problem in found):
            states.append("caution")
    return "warning" if "warning" in states else "caution" if states else ""


def _attention(pieces) -> dict:
    """The rail badge (sd:3446): active pieces by their loudest gate lamp."""
    from .v2.pages import attention

    return attention((_gate_state(piece) for piece in pieces if not piece["parked"]),
                     {"warning": "pieces fail a gate", "caution": "pieces need a gate"})

"""The eight-kilobyte brief a new session starts from.

Requirement 7 (`prd.md:1048-1066`): a session records followups and
questions as rows while it works, and the next session in the same checkout
starts from the open ones -- injected by item A's `SessionStart` hook,
which calls this and nothing else for them. The hook is the pack's; what it
injects is the library's, because a brief rendered by the pack from its own
query is a second reader of the same rows, and a second reader is a second
order and a second filter, discovered by the operator and not by a test.

What is in it, and what is not: the open `followup` and `question` notes of
the item whose branch is checked out, or of every not-`done` item in the
repository when no branch matches; newest first; cut at eight kilobytes with
the count of what was cut and the command that lists the rest. Decisions,
proposals, comments, executions and status changes never inject -- they are
read on the item screen or by `sd note list <item>`. A resolved note stays
in the item's history and never returns here.

The bound is on bytes of UTF-8, because that is what a context window is
charged in, and it is on the whole text -- header, notes and the trailer
that names the cut -- so the trailer cannot be what pushes a brief over.
Notes are whole or absent: a note cut in the middle of its body is a note
the next session will act on half of.

    from sd_db.brief import note_brief

    brief = note_brief(connection, str(root))
    if brief.text:
        inject(brief.text)

`reads.brief_items` and `reads.brief_notes` are the queries; this module is
the one place that turns their rows into text, and the only formatted
string the library hands a face.
"""

from __future__ import annotations

import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import paths, reads

__all__ = ["BRIEF_BYTES", "Brief", "checked_out_branch", "note_brief"]

#: Requirement 7's bound: eight kilobytes, of UTF-8.
BRIEF_BYTES = 8 * 1024

#: The command that lists the rest. Named once, because the trailer prints
#: it and a test asserts it.
LIST_COMMAND = "sd note list {item}"


@dataclass(frozen=True)
class Brief:
    """A rendered brief and the numbers behind it.

    `text` is what the hook injects, already within the bound; it is empty
    when there is nothing open, so a caller injects nothing rather than a
    header over no notes. `scope` says which of requirement 7's two cases
    applied: `"branch"` when the checked-out branch matched a live item,
    `"repository"` when every live item in the repository was read.
    `items` are the item ids the brief covers, `shown` and `cut` count
    notes, and `commands` are the `sd note list <item>` lines the trailer
    carries, one per item that lost a note to the bound.
    """

    text: str
    scope: str
    items: tuple[int, ...]
    shown: int
    cut: int
    commands: tuple[str, ...]


def checked_out_branch(path: str | Path) -> str | None:
    """The branch checked out at `path`, or None on a detached HEAD, a bare repository or no repository.

    None is the repository-wide case and not an error: a hook that starts in
    a worktree at a bare commit still gets the open notes of every live item.
    A bare repository's `HEAD` is still a symbolic ref, to its default branch,
    but nothing is checked out there, so it is asked first (sd:1219).
    """
    def git(*argv: str) -> str | None:
        try:
            done = subprocess.run(
                ["git", "-C", str(paths.disk(path)), *argv],
                capture_output=True, text=True, timeout=20,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return done.stdout.strip() if done.returncode == 0 else None

    if git("rev-parse", "--is-bare-repository") != "false":
        return None
    return git("symbolic-ref", "--short", "-q", "HEAD") or None


def _line(note: sqlite3.Row, *, with_item: bool) -> str:
    """One note as text. A multi-line body is indented under its first line."""
    head = f"- [{note['kind']} #{note['id']} {note['timestamp'][:10]}]"
    if with_item:
        head += f" ({note['item_title']}, sd:{note['item']})"
    first, _, rest = str(note["body"]).strip().partition("\n")
    text = f"{head} {first}"
    if rest:
        text += "\n" + "\n".join(f"  {line}" for line in rest.splitlines())
    return text


def _header(items: list[sqlite3.Row], scope: str, branch: str | None, repo: str) -> str:
    if scope == "branch" and len(items) == 1:
        item = items[0]
        return f"Open followups and questions on {item['title']} (sd:{item['id']}, branch {branch}):"
    if scope == "branch":
        return f"Open followups and questions on the {len(items)} items on branch {branch}:"
    return (
        f"Open followups and questions across {len(items)} live items in {repo} "
        f"(no item is on the checked-out branch):"
    )


def _trailer(cut: int, commands: tuple[str, ...]) -> str:
    noun = "note" if cut == 1 else "notes"
    listing = "; ".join(f"`{command}`" for command in commands)
    return f"{cut} more open {noun} not shown; the rest: {listing}"


def _render(header: str, lines: list[str], trailer: str | None) -> str:
    parts = [header, *lines]
    if trailer:
        parts.append(trailer)
    return "\n".join(parts) + "\n"


def note_brief(
    connection: sqlite3.Connection,
    repo: str,
    *,
    branch: str | None = None,
    limit: int = BRIEF_BYTES,
) -> Brief:
    """Requirement 7's brief for the checkout at `repo`, within `limit` bytes.

    `repo` is the path the item rows carry -- the registered checkout root,
    as `item.repo` names it. `branch` is the checked-out branch; left None,
    it is read from the checkout at `repo` with `git symbolic-ref`, and a
    detached HEAD or an unreadable checkout falls through to the
    repository-wide case. A caller that already knows the branch passes it
    and nothing is spawned.

    The cut is greedy from the top: notes are added newest first until the
    next one, with the trailer that would then be needed, no longer fits.
    Whatever was cut is counted, and the trailer names `sd note list <item>`
    for every item a cut note belongs to, so the rest is one command away.
    """
    if branch is None:
        branch = checked_out_branch(repo)
    items = reads.brief_items(connection, repo, branch=branch)
    scope = (
        "branch"
        if items and all(reads.on_branch(row["branch"], branch) for row in items)
        else "repository"
    )
    ids = tuple(int(row["id"]) for row in items)
    notes = reads.brief_notes(connection, list(ids))
    if not notes:
        return Brief(text="", scope=scope, items=ids, shown=0, cut=0, commands=())

    header = _header(items, scope, branch, repo)
    with_item = len(items) > 1
    lines = [_line(note, with_item=with_item) for note in notes]

    # The size of each candidate is counted, not rendered: every part of
    # `_render`'s text is followed by one newline, so a candidate is the
    # header, its lines and its trailer, each plus one byte. Walking from all
    # notes down keeps the cut items in first-appearance order with one move
    # per step, and the text is rendered once, for the candidate that fits
    # (sd:1219: rendering and encoding every prefix was quadratic).
    sizes = [len(line.encode("utf-8")) + 1 for line in lines]
    size = len(header.encode("utf-8")) + 1 + sum(sizes)
    cut_items: list[int] = []
    for shown in range(len(notes), -1, -1):
        if shown < len(notes):
            size -= sizes[shown]
            item = notes[shown]["item"]
            if item in cut_items:
                cut_items.remove(item)
            cut_items.insert(0, item)
        cut = len(notes) - shown
        commands = tuple(LIST_COMMAND.format(item=item) for item in cut_items)
        trailer = _trailer(cut, commands) if cut else None
        if size + (len(trailer.encode("utf-8")) + 1 if trailer else 0) <= limit:
            text = _render(header, lines[:shown], trailer)
            return Brief(text=text, scope=scope, items=ids, shown=shown, cut=cut, commands=commands)
    # Not even the header and a trailer fit: the bound is smaller than a
    # line. Hand back the trailer alone, so the count and the command
    # survive even where the notes do not.
    return Brief(
        text=_trailer(len(notes), commands) + "\n", scope=scope, items=ids,
        shown=0, cut=len(notes), commands=commands,
    )

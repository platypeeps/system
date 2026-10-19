"""Research: the board, source reader and claim map behind the default UI's Research page (sd:2122).

The page is `v2/research.html`; it holds no rows. It reads two JSON documents:
`/api/research`, built here by `document`, and `/api/research/<checkout>`, one
checkout's source registry and claim map, built by `sources`.

**The readers.** The board is `collectors.collect_research`, the reader the
Resources > Research view renders: every checkout under `REPO_ROOT` (one group
deep) that carries a `research.conf.py`, its declared documents and whether
each built page is fresh, its config refusal, and `git_facts`. This module adds
three filesystem reads the design asks for: the stage (which numbered
directory holds Markdown), the source registry (the Markdown tables in
`SOURCES.md`, `10-sources/registry.md` and `10-sources/references.md`) and the
claim map. None runs repository code, and none writes.

**The claim map** (sd:5064 ruling). A claim is a sentence in the map, brief
or published documents (`CLAIM_DIRS`) that cites a ledger row: it links the
row's URL or a deeper path under it, or names the row's ID. The row's grade
decides it: supported when an A, B or M row backs it, unverified when only C,
U or ungraded rows do. A Status section is a change log, so the
design's START HERE reading was dropped, and with it the inferred and corrected
states. A document the reader does not read is named with its reason.

**No round reader.** The design counts review rounds per brief. Nothing reads
them yet, so the document says so (`ROUNDS_REASON`) and the page shows rounds
as unknown with that reason.

**One module, two roles**, as `fleet.py`: imported, it is the page side, and
`collect` runs this same file as a child, `python3 -I research_screen.py
<area> [<checkout>] <seconds>`, under the collectors' `Budget`. Run as that
child, it loads `collectors.py` by path and prints the document. The reason is
`fleet.py`'s: `collect_research` runs three git commands per checkout, and a
checkout that hangs is cut by the child's deadline, not waited on by the server.
"""

from __future__ import annotations

import importlib.util
import json
import posixpath
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

__all__ = ["CAP", "CLAIM_DIRS", "CLAIM_TYPES", "DIRS", "REGISTRIES", "ROUNDS_REASON", "claims", "collect", "document",
           "grade_legend", "parse_registry", "project", "source_key", "sources"]

#: The page's budget for one reading, the Resources > Research view's `VIEW_SECONDS`, and how much sooner the child
#: stops so its reason arrives before the page's kill does (`fleet.FLEET_MARGIN`).
RESEARCH_SECONDS = 5.0
MARGIN = 1.0
#: This file, run as the child; a page never supplies the path.
CHILD = Path(__file__).resolve()
AREAS = ("board", "sources")
#: The research-repo standard's numbered layout, in stage order.
DIRS = ("00-overview", "10-sources", "20-map", "30-brief", "40-docs")
#: Where a checkout keeps its source ledger, in the order the reader shows them.
REGISTRIES = ("SOURCES.md", "10-sources/registry.md", "10-sources/references.md")
#: The independent review passes a brief may take (the command pack's planning review rule).
CAP = 2
ROUNDS_REASON = "no round reader: the dashboard does not read review rounds yet"
#: Where claims are read: the map, brief and published documents (sd:5064 ruling).
CLAIM_DIRS = ("20-map", "30-brief", "40-docs")
#: Ledger grades: A primary, B secondary, C summary only, U unverified, M measured here. A, B and M support a claim.
GRADES = frozenset("ABCUM")
SUPPORTING = frozenset("ABM")


def _letters(grades) -> list[str]:
    return sorted(grades, key="ABCUM".index)


def _either(words: list[str]) -> str:
    return ", ".join(words[:-1]) + f" or {words[-1]}"


#: The claim map's types and how each is graded (sd:5297): the one copy the page's legend, filter and hover text read.
#: `grades` names the ledger grades each type rests on, so the page can add what the ledger says each one means.
CLAIM_TYPES = {
    "supported": {"rule": f"cites an {_either(_letters(SUPPORTING))} source", "grades": "".join(_letters(SUPPORTING))},
    "unverified": {"rule": f"cites only {_either([*_letters(GRADES - SUPPORTING), 'ungraded'])} sources",
                   "grades": "".join(_letters(GRADES - SUPPORTING))},
}
#: A ledger's own grade legend, as each research checkout writes it above its tables: `**A** = primary source, …`, one
#: grade per line or several on one line. A definition runs to the next one or the end of its line, so a note on the next
#: line is not read into it; a bold letter inside a table row is a cell, not a definition.
LEGEND = re.compile(r"(?<!\w)\*\*([ABCUM])\*\*[ \t]*=[ \t]*([^\n]+?)(?=\s*\*\*[ABCUM]\*\*[ \t]*=|[ \t]*$)", re.M)
#: The claim reader's bounds: documents read, bytes per document (the collectors' bound for a research source), the
#: longest sentence kept, the claims and skipped documents the document carries, and the seconds it keeps for printing
#: when the deadline nears.
CLAIM_FILES = 200
CLAIM_BYTES = 2 * 1024 * 1024
CLAIM_CHARS = 240
CLAIMS = 150
UNREAD = 10
CLAIM_MARGIN = 0.5
#: A registry file larger than this is named, not read; the collectors' bound for a research source.
REGISTRY_BYTES = 2 * 1024 * 1024
#: How many ledger rows the sources document carries, and the bytes it may print: under the collectors' 64 KB ceiling
#: with room to spare, so a long ledger is cut here, with its total, and never refused at the budget.
ROWS = 60
PRINT_BYTES = 56 * 1024
#: Longest cell the reader keeps; the page shows a cut cell with an ellipsis.
CELL = 200
CONF_CHARS = 1000
#: A checkout key: its path under REPO_ROOT, one group deep. The first character rules out `.` and `..`.
KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}(?:/[A-Za-z0-9][A-Za-z0-9._-]{0,99})?")


def _collectors():
    # `collectors.py` loaded by path, as `fleet.py` and `reports_screen.py` load it.
    path = Path(__file__).resolve().parents[1] / "collectors.py"
    spec = importlib.util.spec_from_file_location("sd_dashboard_research_collectors", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("research collectors are unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# -- the page side ------------------------------------------------------------


def collect(area, key=None, *, within=None):
    """The child's document for `area`, read under the page's budget; a `ValueError` names why it was not."""
    if area not in AREAS or (area == "sources") != (key is not None) or (key is not None and not KEY.fullmatch(key)):
        raise ValueError("unknown research reading")
    module = _collectors()
    budget = module.Budget(RESEARCH_SECONDS, within=within)
    seconds = max(budget.seconds - MARGIN, 0.0)
    # The page's start on the shared clock, as `fleet.collect` passes it (sd:2501): the child's deadline counts from here.
    since = time.clock_gettime(time.CLOCK_MONOTONIC)
    argv = [sys.executable, "-I", str(CHILD), area, *([key] if key else []), f"{seconds:g}", f"{since:.6f}"]
    try:
        process = budget.run(argv, label=f"research {area}")
    except module.OverBudget as error:
        raise ValueError(f"research reading was stopped at its budget: {error}") from None
    if process.returncode:
        raise ValueError(process.stderr.strip() or f"research reader exited {process.returncode} without a reason")
    try:
        found = json.loads(process.stdout)
    except ValueError:
        raise ValueError("research reader returned no document") from None
    if not isinstance(found, dict):
        raise ValueError("research reader returned incomplete output")  # noqa: TRY004 - external document validation
    return found


def document(*, now: str, backend=None) -> dict:
    """The board: one row per research checkout, or the reason none was read."""
    try:
        found = (backend or collect)("board")
        projects, error = found.get("projects"), ""
        if not isinstance(projects, list):
            raise ValueError("research reader returned no projects")  # noqa: TRY004 - external document validation
    except (OSError, ValueError) as failure:
        found, projects, error = {}, [], str(failure)
    return {
        "read": now,
        "error": error,
        "root": found.get("root", ""),
        "projects": projects,
        "cut": found.get("cut", 0) if isinstance(found.get("cut"), int) else 0,
        "cap": CAP,
        "dirs": list(DIRS),
        "rounds": {"available": False, "reason": ROUNDS_REASON},
        "attention": _attention(projects, error),
    }


def _attention(projects, error: str) -> dict:
    """The rail badge (sd:3446): render-stale projects. An unread config is unknown, not caution, so it does not count;
    a board that was not read at all is unknown."""
    from .v2.pages import attention

    if error:
        return {"state": "unknown", "n": 0, "what": "research not read"}
    return attention(("caution" if (project.get("render") or {}).get("s") == "caution" else "" for project in projects),
                     {"caution": "render-stale projects"})


def sources(key: str, *, now: str, backend=None, connection=None) -> dict | None:
    """One checkout's source registry, None when no research checkout has that key.

    With `connection`, each row's adversarial reviews too (sd:5101): `reviews` maps a row key to its briefs, newest
    first, as `adversarial.source_reviews` finds them; None with `reviews_error` when they were not read.
    """
    if not KEY.fullmatch(key):
        return None
    try:
        found = (backend or collect)("sources", key)
    except (OSError, ValueError) as failure:
        return {"read": now, "key": key, "error": str(failure), "files": [], "rows": [], "total": 0, "claims": None,
                "checkout": None, "reviews": None, "reviews_error": "the ledger was not read"}
    if not found.get("found"):
        return None
    reviews, reviews_error = None, "not read here"
    if connection is not None:
        from . import adversarial

        try:
            reviews, reviews_error = adversarial.source_reviews(connection, found.get("checkout")), ""
        except (OSError, ValueError, LookupError) as failure:
            reviews_error = str(failure)
    return {"read": now, "key": key, "error": "", "files": found.get("files", []), "rows": found.get("rows", []),
            "total": found.get("total", 0), "checkout": found.get("checkout"), "reviews": reviews,
            "reviews_error": reviews_error,
            # The claim map; None when the child sent none, so the page says unknown rather than "no claims".
            "claims": {"rows": found["claims"], "total": found.get("claims_total", 0), "tally": found.get("tally", {}),
                       "graded": found.get("graded", 0), "dirs": list(CLAIM_DIRS), "read": found.get("docs_read", 0), "unread": found.get("unread", []),
                       # sd:5297: what each type means and how it is graded, and what the ledger says each grade means.
                       "types": CLAIM_TYPES, "grades": found.get("grades") if isinstance(found.get("grades"), dict) else {}}
            if isinstance(found.get("claims"), list) else None}


# -- the child ----------------------------------------------------------------


def _shown(path: Path) -> str:
    """A path as the page shows it: under the home folder as `~/…`."""
    home = Path.home()
    return "~/" + path.relative_to(home).as_posix() if path.is_relative_to(home) else str(path)


def _key(repo: Path, root: Path) -> str:
    try:
        return repo.relative_to(root).as_posix()
    except ValueError:
        return repo.name


def stage(repo: Path) -> list[str] | None:
    """The numbered directories that hold Markdown, in order; None when the checkout has none of them."""
    present = [name for name in DIRS if (repo / name).is_dir() and not (repo / name).is_symlink()]
    if not present:
        return None
    return [name for name in present if next((repo / name).rglob("*.md"), None) is not None]


def render(item: dict) -> dict:
    """Render freshness from the collector's documents: a state, a phrase and a detail line."""
    if item.get("error"):
        return {"s": "unknown", "txt": "config unread", "sub": "per-document freshness needs a readable research.conf.py"}
    docs = item.get("docs") or []
    if not docs:
        return {"s": "unknown", "txt": "no documents declared", "sub": "research.conf.py lists none"}
    stale = sum(doc.get("state") == "stale" for doc in docs)
    unbuilt = sum(doc.get("state") == "not built" for doc in docs)
    newest = max((doc.get("updated") or "" for doc in docs), default="")
    if stale or unbuilt:
        parts = [f"{stale} stale" if stale else "", f"{unbuilt} not built" if unbuilt else ""]
        return {"s": "caution", "txt": f"{stale + unbuilt} of {len(docs)} documents not fresh",
                "sub": " · ".join(p for p in parts if p)}
    return {"s": "ok", "txt": f"{len(docs)} of {len(docs)} fresh", "sub": f"newest source {newest}" if newest else ""}


def project(item: dict, root: Path, *, dirs=None) -> dict:
    """One board row from one `collect_research` item."""
    repo = Path(item["path"])
    label = item.get("project")
    if isinstance(label, dict):
        label = label.get("name") or label.get("title")
    git = item.get("git")
    docs = item.get("docs") or []
    # Every free-text cell is cut at CELL: a config may declare 100 documents with titles of any length.
    return {
        "key": _key(repo, root),
        "label": _cut(label if isinstance(label, str) and label.strip() else item["name"]),
        "path": _shown(repo),
        "dirs": stage(repo) if dirs is None else dirs,
        "render": render(item),
        "conf": _cut(str(item["error"]), CONF_CHARS) if item.get("error") else None,
        "docs": [{"title": _cut(str(d.get("title", ""))), "src": _cut(str(d.get("src", ""))), "state": d.get("state", ""),
                  "updated": d.get("updated", "")} for d in docs],
        "docs_total": len(docs),
        "git": None if not git else {key: _cut(value) if isinstance(value := git.get(key), str) else value
                                     for key in ("branch", "dirty", "behind", "last_iso", "subject")},
    }


def board(collectors) -> dict:
    root = Path(collectors.REPO_ROOT)
    # Only the checkouts `checkout` accepts, so the board lists no row whose ledger is a 404 and nothing linked out of the root.
    items = [item for item in collectors.collect_research() if checkout(root, _key(Path(item["path"]), root)) == Path(item["path"])]
    out = {"root": _shown(root), "projects": [project(item, root) for item in items], "cut": 0}
    # The page reads the child through a 64 KiB budget: past PRINT_BYTES, drop listed documents from the longest list first
    # (docs_total and render keep the whole count), then whole projects from the end, counted in "cut".
    projects, size = out["projects"], _printed(out)
    while size > PRINT_BYTES and projects:
        longest = max(projects, key=lambda p: len(p["docs"]))
        listed = longest["docs"] if longest["docs"] else projects
        size -= _printed(listed[-1]) + (len(listed) > 1)  # the element and its comma
        if listed is projects:
            out["cut"] += 1
            size += len(str(out["cut"])) - len(str(out["cut"] - 1))
        listed.pop()
    return out


def _printed(found) -> int:
    return len(json.dumps(found, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _cut(text: str, limit: int = CELL) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def checkout(root: Path, key: str) -> Path | None:
    """The research checkout a key names: `collect_research`'s walk, one group deep, so no other path is read."""
    repo = root / key
    if not KEY.fullmatch(key):
        return None
    # The walk's guard: a linked group, checkout or config is never read. REPO_ROOT itself may be a link, as the walk allows.
    conf = repo / "research.conf.py"
    if ("/" in key and repo.parent.is_symlink()) or repo.is_symlink() or conf.is_symlink() or not conf.is_file():
        return None
    return repo if repo.resolve().is_relative_to(root.resolve()) else None


LINK = re.compile(r"\[([^\]]*)\]\((https?://[^)\s]+)\)")
BARE = re.compile(r"<(https?://[^>\s]+)>")


def _cell(text: str, limit: int = CELL) -> str:
    """Markdown cell text as plain text: links keep their label, emphasis and code marks go."""
    text = LINK.sub(r"\1", text)
    text = BARE.sub(r"\1", text)
    text = re.sub(r"~~(.*?)~~", r"\1", text)
    text = re.sub(r"(\*\*|__|`|\*)", "", text).replace("\\|", "|").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _url(cells: list[str]) -> str:
    for cell in cells:
        match = LINK.search(cell) or BARE.search(cell) or re.search(r"https?://[^\s|)>]+", cell)
        if match:
            url = match.group(2) if match.re is LINK else match.group(1) if match.re is BARE else match.group(0)
            return url if len(url) <= 400 else ""
    return ""


#: A Markdown link to a file rather than a page, `[name](files/x.pdf)` or `[name](</abs/path x.pdf>)` (sd:5112).
FILE_LINK = re.compile(r"\]\((?:<([^>\n]+)>|((?![a-z][a-z0-9+.-]*:|#)[^)\s]+))\)")


def _file(cells: list[str], ledger: str) -> str:
    """The first file a cell links, as a path under the checkout, or absolute when the link is; "" when none."""
    for cell in cells:
        match = FILE_LINK.search(cell)
        if match:
            target = match.group(1) or match.group(2)
            path = target if target.startswith("/") else posixpath.normpath(posixpath.join(posixpath.dirname(ledger), target))
            return path if len(path) <= 400 and not path.startswith("..") else ""
    return ""


def _row(line: str) -> list[str]:
    body = line.strip()
    body = body[1:] if body.startswith("|") else body
    body = body[:-1] if body.endswith("|") else body
    return [cell.strip() for cell in re.split(r"(?<!\\)\|", body)]


def grade_legend(text: str) -> dict:
    """What a ledger says each grade means, by letter, as plain text; the first definition of a letter wins."""
    found = {}
    for match in LEGEND.finditer(text):
        if not text[text.rfind("\n", 0, match.start()) + 1:match.start()].lstrip().startswith("|"):
            found.setdefault(match.group(1), _cell(" ".join(match.group(2).split())))
    return found


def parse_registry(text: str, name: str) -> tuple[dict, list[dict]]:
    """A ledger file's provenance paragraph and its table rows, each under the heading above its table."""
    lines = text.splitlines()
    prov, section, heads, rows, i = [], "", None, [], 0
    started = False
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if re.match(r"#{1,6}\s", stripped):
            section = stripped.lstrip("#").strip()
            started = started or stripped.startswith("##")
            heads = None
        elif "|" in stripped and i + 1 < len(lines) and re.fullmatch(r"\|?[\s:|-]+\|?", lines[i + 1].strip()) \
                and "-" in lines[i + 1] and (stripped.startswith("|") or "|" in lines[i + 1]):
            # A table may leave out its outer pipes (sd:2414); then its delimiter row carries one, so prose with a pipe
            # above a `---` rule stays provenance.
            heads = [_cell(h) for h in _row(stripped)]
            started = True
            i += 2
            continue
        elif "|" in stripped and heads:
            cells = _row(stripped)
            if len(cells) >= 2 and cells[0]:
                # The grade is the first cell (traces-research), or a column headed Grade beside an ID column.
                at = next((k for k, h in enumerate(heads) if h.lower() == "grade"), 0)
                grade = _cell(cells[at]) if at < len(cells) else ""
                rows.append({"f": name, "sec": section, "id": _cell(cells[0])[:40], "title": _cell(cells[1]),
                             "grade": grade if grade in GRADES else "",
                             "url": _url(cells[1:]), "c3": _cell(cells[2]) if len(cells) > 2 else "",
                             "c4": _cell(cells[-1]) if len(cells) > 3 else "",
                             "h": [heads[0] if heads else "", heads[1] if len(heads) > 1 else "",
                                   heads[2] if len(heads) > 2 else "", heads[-1] if len(heads) > 3 else ""]})
                if linked := _file(cells[1:], name):
                    rows[-1]["file"] = linked  # only on a row that links a file: every byte counts against PRINT_BYTES
        elif not started and stripped and stripped != "---":
            prov.append(stripped)
        elif not stripped:
            heads = None  # a blank line ends a table
        i += 1
    text_prov = " ".join(prov)
    return {"prov": text_prov if len(text_prov) <= 600 else text_prov[:599].rstrip() + "…"}, rows


def source_key(url: str) -> str:
    """The key a link and a ledger row share: the URL without its fragment or a trailing slash."""
    return url.strip().split("#", 1)[0].rstrip("/")


#: A ledger ID as the ledgers write it in their first column: one word with a digit, such as S12, P2, LOG-21201 or
#: S3-old. A grade letter has no digit, and a title has spaces, so neither is an ID.
ID = re.compile(r"(?=\S*\d)[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)*")


def is_id(text: str) -> bool:
    return len(text) <= 20 and bool(ID.fullmatch(text))


def row_key(row: dict) -> str:
    """A ledger row's key: its URL's, else its ID's (`id:S12`), else the file it links (`file:10-sources/files/x.pdf`),
    else none."""
    if row.get("url"):
        return source_key(row["url"])
    if is_id(row["id"]):
        return f"id:{row['id']}"
    return f"file:{row['file']}" if row.get("file") else ""


def ledger_key(key: str, index) -> str | None:
    """The ledger row a link cites: its own URL, or else the longest ledger URL on the same host whose path holds the
    link's at a path boundary (`/org/repo` holds `/org/repo/blob/x`, not `/org/repository`)."""
    while key not in index:
        head, sep, _ = key.rpartition("/")
        if not sep or head.endswith(":/") or "://" not in head:
            return None
        key = head
    return key


FENCE = re.compile(r"(```|~~~)")
#: A line that starts a unit of its own: a heading, a list item or a table row.
STARTS = re.compile(r"(#{1,6}\s|[-*+]\s|\d{1,3}[.)]\s|\|)")
#: A sentence ends at . ! or ? and the space after it.
ENDS = re.compile(r"(?<=[.!?])\s+")


def units(text: str):
    """(line, text) for each paragraph, heading, list item and table row outside fenced code; a paragraph keeps its
    line breaks, so a sentence can name the line it starts on."""
    lines, start, fence = [], 0, None

    def flush():
        if lines:
            yield start, "\n".join(lines)
            lines.clear()

    for number, line in enumerate(text.splitlines(), 1):
        stripped = re.sub(r"^(>\s?)+", "", line.strip()).strip()  # a quote reads as its text
        mark = FENCE.match(stripped)
        if fence:
            fence = None if mark and mark.group(1) == fence else fence
            continue
        if mark:
            yield from flush()
            fence = mark.group(1)
            continue
        if not stripped or STARTS.match(stripped):
            yield from flush()
        if stripped.startswith("|"):
            # A table row is one unit: its cells joined, its outer pipes gone.
            yield number, " · ".join(cell for cell in _row(stripped) if cell)
        elif stripped:
            start = start if lines else number
            lines.append(re.sub(r"^(#{1,6}|[-*+]|\d{1,3}[.)])\s+", "", stripped))  # a heading or item reads as its text
    yield from flush()


def sentences(text: str):
    """(line offset, sentence) for each sentence of a unit. A cut inside a link's label or address is joined back."""
    pieces, at, held, offset = [], 0, "", 0
    for match in [*ENDS.finditer(text), None]:
        end = match.start() if match else len(text)
        piece = text[at:end]
        if not held:
            offset = text.count("\n", 0, at)
        held = f"{held} {piece}" if held else piece
        if match is None or (held.count("[") <= held.count("]") and held.count("(") <= held.count(")")):
            pieces.append((offset, held))
            held = ""
        at = match.end() if match else at
    return pieces


def claims(repo: Path, rows: list[dict], time_left=None) -> dict:
    """The claim map (sd:5064 ruling): each sentence in the map, brief and published documents that cites a ledger row,
    by its URL, a deeper path under it, or its ID. A claim is supported when it cites an A, B or M row, and unverified
    when it cites only C, U or ungraded rows; a ledger without grades leaves every claim unverified. A document that is
    not read is named with its reason; the walk stops at its file bound and at the child's deadline."""
    index, ids = {}, {}
    for row in rows:
        key = row_key(row)
        if key:
            index.setdefault(key, set()).add(row["grade"])
            if is_id(row["id"]):
                ids.setdefault(row["id"], set()).add(key)
    # The IDs this ledger holds, longest first, each a whole word: S12 is not S120, S12-old or the section S12.3.
    alternatives = "|".join(map(re.escape, sorted(ids, key=len, reverse=True)))
    named = re.compile(rf"(?<![\w-])({alternatives})(?![\w-]|\.\d)") if ids else None
    found, unread, read = [], [], 0
    paths = [path for name in CLAIM_DIRS if (repo / name).is_dir() and not (repo / name).is_symlink()
             for path in sorted((repo / name).rglob("*.md"))]
    for number, path in enumerate(paths):
        name = path.relative_to(repo).as_posix()
        left = time_left() if time_left else None
        if number >= CLAIM_FILES or (left is not None and left < CLAIM_MARGIN):
            why = f"past the {CLAIM_FILES}-document bound" if number >= CLAIM_FILES else "the reading ran out of time"
            unread.append({"doc": f"{len(paths) - number} more documents", "error": why})
            break
        try:
            if path.is_symlink() or not path.resolve().is_relative_to(repo.resolve()):
                raise ValueError("a link, which the reader does not follow")
            if path.stat().st_size > CLAIM_BYTES:
                raise ValueError("larger than the 2 MB the reader reads")
            text = path.read_text(encoding="utf-8", errors="replace")
        except (OSError, ValueError) as error:
            unread.append({"doc": name, "error": _cut(str(error))})
            continue
        read += 1
        for line, unit in units(text):
            for offset, sentence in sentences(unit):
                linked = (ledger_key(source_key(m.group(2) if m.re is LINK else m.group(1)), index)
                          for m in [*LINK.finditer(sentence), *BARE.finditer(sentence)])
                cited = (key for m in (named.finditer(sentence) if named else ()) for key in sorted(ids[m.group(1)]))
                keys = [key for key in dict.fromkeys([*linked, *cited]) if key]
                if keys:
                    grades = set().union(*(index[key] for key in keys)) - {""}
                    found.append({"doc": name, "line": line + offset, "text": _cell(" ".join(sentence.split()), CLAIM_CHARS),
                                  "s": "supported" if grades & SUPPORTING else "unverified", "grades": "".join(sorted(grades)),
                                  "keys": keys})
    if len(unread) > UNREAD:
        # The list of skipped documents is bounded too: the rest are counted in one entry.
        unread[UNREAD - 1:] = [{"doc": f"{len(unread) - UNREAD + 1} more documents", "error": "skipped as well; the list stops here"}]
    tally = {state: sum(claim["s"] == state for claim in found) for state in ("supported", "unverified")}
    tally["ungraded"] = sum(not claim["grades"] for claim in found)  # unverified because no cited row has a grade
    return {"claims": found, "tally": tally, "read": read, "unread": unread, "graded": sum(bool(row["grade"]) for row in rows)}


def registry(repo: Path, time_left=None) -> dict:
    files, rows, grades = [], [], {}
    for name in REGISTRIES:
        path = repo / name
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(repo.resolve()):
            continue
        stat = path.stat()
        mtime = datetime.fromtimestamp(stat.st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
        if stat.st_size > REGISTRY_BYTES:
            files.append({"file": name, "mtime": mtime, "prov": "", "error": "larger than the 2 MB the reader reads"})
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        head, found = parse_registry(text, name)
        grades = {**grade_legend(text), **grades}  # an earlier ledger file's definition wins
        files.append({"file": name, "mtime": mtime, "prov": head["prov"], "error": ""})
        rows.extend(found)
    mapped = claims(repo, rows, time_left)
    cited = {}
    for claim in mapped["claims"]:
        for key in claim["keys"]:
            cited[key] = cited.get(key, 0) + 1
    for row in rows:
        if row["grade"] == row["id"] or not row["grade"]:
            del row["grade"]  # a first-cell grade prints once, as the row's ID; only a Grade column adds the key
        row["key"] = row_key(row)
        row["cited"] = cited.get(row["key"], 0) if row["key"] else 0
    total = len(rows)
    # Cited rows are kept first, so a claim can light its sources; the rest fill to ROWS. Both stay in ledger order.
    keep = sorted(sorted(range(total), key=lambda i: not rows[i]["cited"])[:ROWS])
    rows = [rows[i] for i in keep]
    shown = mapped["claims"][:CLAIMS]
    out = {"found": True, "files": files, "rows": rows, "total": total, "claims": shown, "grades": grades,
           "claims_total": len(mapped["claims"]), "tally": mapped["tally"], "graded": mapped["graded"], "docs_read": mapped["read"],
           "unread": mapped["unread"]}
    # Past PRINT_BYTES, drop from the end of whichever list prints more: claims or rows.
    size = _printed(out)
    while size > PRINT_BYTES and (rows or shown):
        listed = shown if shown and (not rows or _printed(shown) >= _printed(rows)) else rows
        size -= _printed(listed[-1]) + (len(listed) > 1)
        listed.pop()
    return out


def main(argv):
    started = time.monotonic()
    number = r"\d+(\.\d+)?"
    base = 3 if argv and argv[0] == "sources" else 2
    if not argv or argv[0] not in AREAS or len(argv) not in (base, base + 1) \
            or not all(re.fullmatch(number, value) for value in argv[base - 1:]):
        print("usage: research_screen.py board <seconds> [<since>] | sources <checkout> <seconds> [<since>]", file=sys.stderr)
        return 2
    area, seconds = argv[0], float(argv[base - 1])
    if len(argv) == base + 1:
        # The page's start on the shared clock: what this interpreter spent starting is taken from the deadline, never
        # added to it (`fleet.main`). A reading from the future counts as now, and one older than the budget as spent.
        spent = time.clock_gettime(time.CLOCK_MONOTONIC) - float(argv[base])
        started -= min(max(spent, 0.0), seconds)
    try:
        collectors = _collectors()
        with collectors.set_deadline(seconds, started=started):
            if area == "board":
                found = board(collectors)
            else:
                repo = checkout(Path(collectors.REPO_ROOT), argv[1])
                # The checkout's path, so the page side can find its adversarial reviews (sd:5101).
                found = {**registry(repo, collectors.time_left), "checkout": str(repo)} if repo else {"found": False}
    except Exception as error:  # noqa: BLE001 - the page shows this reason
        print(f"research {area}: {error!r}"[:4096], file=sys.stderr)
        return 1
    sys.stdout.write(json.dumps(found, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

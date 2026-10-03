"""Research: the board, source reader and claim map behind the default UI's Research page (sd:2122).

The page is `v2/research.html`; it holds no rows. It reads two JSON documents:
`/api/research`, built here by `document`, and `/api/research/<checkout>`, one
checkout's source registry, built by `sources`.

**The readers.** The board is `collectors.collect_research`, the reader the
Resources > Research view renders: every checkout under `REPO_ROOT` (one group
deep) that carries a `research.conf.py`, its declared documents and whether
each built page is fresh, its config refusal, and `git_facts`. This module adds
two filesystem reads the design asks for and nothing reads yet: the stage (which
numbered directory holds Markdown) and the source registry (the Markdown tables
in `SOURCES.md`, `10-sources/registry.md` and `10-sources/references.md`).
Neither runs repository code, and neither writes.

**No round or claim reader.** The design counts review rounds per brief and
maps a START HERE document's Status claims to their sources. Nothing reads
either yet, so the document says so (`ROUNDS_REASON`, `CLAIMS_REASON`) and
the page shows both as unknown with that reason.

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
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

__all__ = ["CAP", "CLAIMS_REASON", "DIRS", "REGISTRIES", "ROUNDS_REASON", "collect", "document", "parse_registry",
           "project", "sources"]

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
CLAIMS_REASON = "no claim reader: the dashboard does not extract claims from a Status section yet"
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
        "claims": {"available": False, "reason": CLAIMS_REASON},
    }


def sources(key: str, *, now: str, backend=None) -> dict | None:
    """One checkout's source registry, None when no research checkout has that key."""
    if not KEY.fullmatch(key):
        return None
    try:
        found = (backend or collect)("sources", key)
    except (OSError, ValueError) as failure:
        return {"read": now, "key": key, "error": str(failure), "files": [], "rows": [], "total": 0}
    if not found.get("found"):
        return None
    return {"read": now, "key": key, "error": "", "files": found.get("files", []), "rows": found.get("rows", []),
            "total": found.get("total", 0)}


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


def _cell(text: str) -> str:
    """Markdown cell text as plain text: links keep their label, emphasis and code marks go."""
    text = LINK.sub(r"\1", text)
    text = BARE.sub(r"\1", text)
    text = re.sub(r"~~(.*?)~~", r"\1", text)
    text = re.sub(r"(\*\*|__|`|\*)", "", text).replace("\\|", "|").strip()
    return text if len(text) <= CELL else text[: CELL - 1].rstrip() + "…"


def _url(cells: list[str]) -> str:
    for cell in cells:
        match = LINK.search(cell) or BARE.search(cell) or re.search(r"https?://[^\s|)>]+", cell)
        if match:
            url = match.group(2) if match.re is LINK else match.group(1) if match.re is BARE else match.group(0)
            return url if len(url) <= 400 else ""
    return ""


def _row(line: str) -> list[str]:
    body = line.strip()
    body = body[1:] if body.startswith("|") else body
    body = body[:-1] if body.endswith("|") else body
    return [cell.strip() for cell in re.split(r"(?<!\\)\|", body)]


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
                rows.append({"f": name, "sec": section, "id": _cell(cells[0])[:40], "title": _cell(cells[1]),
                             "url": _url(cells[1:]), "c3": _cell(cells[2]) if len(cells) > 2 else "",
                             "c4": _cell(cells[-1]) if len(cells) > 3 else "",
                             "h": [heads[0] if heads else "", heads[1] if len(heads) > 1 else "",
                                   heads[2] if len(heads) > 2 else "", heads[-1] if len(heads) > 3 else ""]})
        elif not started and stripped and stripped != "---":
            prov.append(stripped)
        elif not stripped:
            heads = None  # a blank line ends a table
        i += 1
    text_prov = " ".join(prov)
    return {"prov": text_prov if len(text_prov) <= 600 else text_prov[:599].rstrip() + "…"}, rows


def registry(repo: Path) -> dict:
    files, rows = [], []
    for name in REGISTRIES:
        path = repo / name
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(repo.resolve()):
            continue
        stat = path.stat()
        mtime = datetime.fromtimestamp(stat.st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
        if stat.st_size > REGISTRY_BYTES:
            files.append({"file": name, "mtime": mtime, "prov": "", "error": "larger than the 2 MB the reader reads"})
            continue
        head, found = parse_registry(path.read_text(encoding="utf-8", errors="replace"), name)
        files.append({"file": name, "mtime": mtime, "prov": head["prov"], "error": ""})
        rows.extend(found)
    total = len(rows)
    rows = rows[:ROWS]
    out = {"found": True, "files": files, "rows": rows, "total": total}
    while rows and len(json.dumps(out, ensure_ascii=False).encode("utf-8")) > PRINT_BYTES:
        rows.pop()
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
                found = registry(repo) if repo else {"found": False}
    except Exception as error:  # noqa: BLE001 - the page shows this reason
        print(f"research {area}: {error!r}"[:4096], file=sys.stderr)
        return 1
    sys.stdout.write(json.dumps(found, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

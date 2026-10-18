"""Add sources to a research checkout's ledger from the Research page's Sources tab (sd:5112).

The operator pastes URLs or drops files; this module writes one ledger row per source, deterministically, with no
model in the loop, and lands it through the merge lane. It never writes the operator's checkout:

1. A dropped file is first staged, `POST /api/research/intake/file` with the raw bytes, under
   `<state>/system/source-intake/staged/<sha256>`; the JSON request names it by that hash. The server's JSON bound is
   64 KiB, so a file never rides in it.
2. `POST /api/research/intake` fetches `origin`, adds a worktree outside `~/repos` on a fresh branch
   `source-intake/<stamp>` at `origin/main`, and reads the ledger there: the first registry file (the Research page's
   `REGISTRIES` order) with a table headed by a Grade column.
3. Each source becomes one row in that table's own columns: the next free S-ID when the first column is an ID, the
   grade `U` (added, not yet read), the title and link, and "added <day>, not yet read" in the notes cell. A URL's
   title is the page's `<title>`, through the source review's bounded fetch, else its host and path. A file keeps its
   name, and lives in `10-sources/files/`, or, over `FILE_BYTES`, under the bulk storage root
   (`sd config get sd.bulk_storage_root`) as `<root>/<repository>/sources/`, linked from the row.
4. A URL the ledger already holds, or a file whose SHA-256 a stored file has, is refused by name, and so is the whole
   request: nothing is written.
5. It commits (`Authored-with: script`), files an item with `--ref source-intake:<stamp>`, and enqueues the branch
   with `sd-ship lane enqueue --deliver`. A failure before the item undoes the worktree, its branch and any bulk copy;
   a failed enqueue keeps the commit and the item, notes the item with the reason, and answers the line that retries.

The source review (`adversarial`'s `source` kind, sd:5101) then reads each new row and recommends its real grade.
"""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit

__all__ = ["FILE_BYTES", "READING", "STAGE_BYTES", "add", "request", "stage"]

#: The general ledger for a source no topic checkout fits (sd:5112 decision): the default target.
READING = "research/reading"
#: A file larger than this is not committed; it goes under the bulk storage root, and the row links it there.
FILE_BYTES = 5 * 1024 * 1024
#: The largest file the stage takes, and how long a staged file no request used is kept.
STAGE_BYTES = 200 * 1024 * 1024
STAGE_DAYS = 1
#: Sources one request may add, and the title fetch's bound (the source review's fetch, cut shorter).
SOURCES = 20
TITLE_SECONDS = 8
TITLE_CHARS = 120
#: The files a drop may hold: what a ledger row can cite and the source review can read.
SUFFIXES = (".pdf", ".md", ".markdown", ".html", ".htm", ".txt")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}")
SHA = re.compile(r"[0-9a-f]{64}")
GIT_SECONDS = 60
ENQUEUE_SECONDS = 180


def _state() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "system" / "source-intake"


def _name(raw: str) -> str:
    """A dropped file's name as stored: letters, digits, dot, dash and underscore, the rest dashes; its suffix kept."""
    base = os.path.basename(raw or "").strip()
    stem, suffix = os.path.splitext(base)
    if suffix.lower() not in SUFFIXES:
        raise ValueError(f"{base or 'A file'} is not one of {', '.join(SUFFIXES)}, so it is not added.")
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-._")[:100] or "source"
    return stem + suffix.lower()


def stage(name: str, length: str, stream) -> tuple[int, dict]:
    """Keep one dropped file's bytes under its SHA-256 until a request names it; `(status, document)`."""
    if not length.isdigit() or not 0 < int(length) <= STAGE_BYTES:
        return 400, {"error": f"A dropped file is 1 byte to {STAGE_BYTES // (1024 * 1024)} MB."}
    try:
        stored = _name(unquote(name))  # the page sends it URI-encoded: a header carries ASCII only
    except ValueError as problem:
        return 400, {"error": str(problem)}
    folder = _state() / "staged"
    folder.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - STAGE_DAYS * 86400
    for old in folder.iterdir():
        if old.stat().st_mtime < cutoff:
            old.unlink(missing_ok=True)
    digest, left = hashlib.sha256(), int(length)
    with tempfile.NamedTemporaryFile(dir=folder, prefix=".part-", delete=False) as sink:
        try:
            while left:
                chunk = stream.read(min(left, 1 << 20))
                if not chunk:
                    raise OSError("the upload ended before its length")
                digest.update(chunk)
                sink.write(chunk)
                left -= len(chunk)
        except OSError:
            Path(sink.name).unlink(missing_ok=True)
            raise
    sha = digest.hexdigest()
    os.replace(sink.name, folder / sha)
    return 200, {"sha256": sha, "size": int(length), "name": stored}


def request(values: dict, who: str):
    """`POST /api/research/intake`: `{target?, urls?, files?}`, checked here, written by `add` on the connection."""
    if not set(values) <= {"target", "urls", "files"}:
        raise ValueError("Adding sources takes a target, urls and files, and nothing else.")
    target = values.get("target") or READING
    urls, files = values.get("urls") or [], values.get("files") or []
    if not isinstance(target, str) or not isinstance(urls, list) or not isinstance(files, list):
        raise ValueError("Adding sources takes a checkout key, a list of URLs and a list of staged files.")
    if not urls and not files:
        raise ValueError("Paste a URL or drop a file to add a source.")
    if len(urls) + len(files) > SOURCES:
        raise ValueError(f"Add at most {SOURCES} sources at once.")
    for url in urls:
        if not isinstance(url, str) or len(url) > 400 or not re.fullmatch(r"https?://[^\s/|<>]+[^\s|<>]*", url):
            raise ValueError(f"{str(url)[:80]!r} is not an http or https address.")
    for one in files:
        if not isinstance(one, dict) or not {"name", "sha256"} <= set(one) <= {"name", "sha256", "size"} or not SHA.fullmatch(str(one["sha256"])):
            raise ValueError("Each file is the name and sha256 the drop's upload answered.")
    return lambda connection: add(connection, target=target, urls=urls, files=files, who=who)


def _git(directory: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(directory), *args], capture_output=True, text=True, timeout=GIT_SECONDS,
                          check=False)
    if done.returncode:
        raise OSError(f"git {args[0]} in {directory} failed: {(done.stderr or done.stdout).strip()[-300:]}")
    return done.stdout.strip()


def _title(url: str) -> str:
    """The page's `<title>` through the source review's bounded fetch, else the URL's host and path."""
    from . import adversarial

    found = re.search(r"^Its title: (.+)\.$", adversarial._fetch(url, seconds=TITLE_SECONDS), re.MULTILINE)
    parts = urlsplit(url)
    title = found.group(1) if found else (parts.netloc + parts.path.rstrip("/"))
    return re.sub(r"\s+", " ", title).replace("|", "-").replace("[", "(").replace("]", ")")[:TITLE_CHARS].strip()


def _bulk_root() -> Path | None:
    from .queue_screen import command

    done = subprocess.run([command("sd"), "config", "get", "sd.bulk_storage_root"], capture_output=True, text=True,
                          timeout=30, check=False)
    value = done.stdout.strip()
    return Path(value).expanduser() if done.returncode == 0 and value else None


def _table(work: Path):
    """The ledger file and table rows are added to: `(path, heads, line after its last row, all lines)`."""
    from . import research_screen

    for name in research_screen.REGISTRIES:
        path = work / name
        if not path.is_file():
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        for i in range(len(lines) - 1):
            head, rule = lines[i].strip(), lines[i + 1].strip()
            if head.startswith("|") and re.fullmatch(r"\|[\s:|-]+\|?", rule):
                heads = [research_screen._cell(h).lower() for h in research_screen._row(head)]
                if "grade" in heads:
                    end = i + 2
                    while end < len(lines) and lines[end].strip().startswith("|"):
                        end += 1
                    return path, heads, end, lines
    raise ValueError(f"No ledger in {work.name} has a table with a Grade column ({', '.join(research_screen.REGISTRIES)}).")


def _cells(heads: list[str], *, sid: str, title: str, link: str, day: str) -> str:
    """One row in the table's own columns (sd:5112): ID, Grade, Source, Link and notes are found by their headings."""
    out = ["—"] * len(heads)
    taken = set()
    if heads[0] in ("id", "#"):
        out[0] = sid
        taken.add(0)
    grade = heads.index("grade")
    out[grade] = "U"
    taken.add(grade)
    linked = next((k for k, h in enumerate(heads) if h in ("link", "url") and k not in taken), None)
    source = next((k for k, h in enumerate(heads) if k not in taken and k != linked and
                   re.match(r"(source|doc|document|title|name|reference)\b", h)), None)
    if source is None:
        source = next(k for k in range(len(heads)) if k not in taken and k != linked)
    out[source] = f"[{title}]({link})" if linked is None else title
    taken.add(source)
    if linked is not None:
        out[linked] = f"[link]({link})"
        taken.add(linked)
    free = [k for k in range(len(heads)) if k not in taken]
    if free:
        note = next((k for k in free if re.search(r"status|note", heads[k])), free[-1])
        out[note] = f"added {day}, not yet read"
    return "| " + " | ".join(out) + " |"


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def add(connection, *, target: str, urls: list, files: list, who: str) -> dict:
    """Write the rows in a fresh worktree, commit, file the item and enqueue it; see the module's steps."""
    from sd_db import paths, repos, workflow, writes

    from . import research_screen, server
    from .queue_screen import _run, command

    root = Path(os.environ.get("REPO_ROOT", "~/repos")).expanduser()
    checkout = research_screen.checkout(root, target) if research_screen.KEY.fullmatch(target) else None
    if checkout is None:
        raise workflow.WorkflowError(f"{target} is not a research checkout under {research_screen._shown(root)}")
    home = repos.row_for(connection, checkout)
    if home is None:
        raise workflow.WorkflowError(f"{target} is not a registered repository (see sd-db repo list), so its ledger "
                                     f"cannot land through the lane")
    staged = _state() / "staged"
    for one in files:
        if not (staged / one["sha256"]).is_file():
            raise workflow.WorkflowError(f"{one['name']} was not staged, or its stage expired; drop it again")
    named = [_name(one["name"]) for one in files]
    big = [one for one in files if (staged / one["sha256"]).stat().st_size > FILE_BYTES]
    bulk = _bulk_root() if big else None
    if big and bulk is None:
        raise workflow.WorkflowError(f"{big[0]['name']} is over {FILE_BYTES // (1024 * 1024)} MB and no bulk storage "
                                     f"root is set (sd config set sd.bulk_storage_root <folder>), so it is not added")
    shelf = bulk / checkout.name / "sources" if bulk else None

    _git(checkout, "fetch", "-q", "origin")
    base = _git(checkout, "rev-parse", "--verify", "origin/main^{commit}")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    branch, work = f"source-intake/{stamp}", Path.home() / "worktrees" / f"{checkout.name}-source-intake-{stamp}"
    work.parent.mkdir(parents=True, exist_ok=True)
    _git(checkout, "worktree", "add", "-q", "-b", branch, str(work), base)
    copied: list[Path] = []
    try:
        ledger, heads, end, lines = _table(work)
        _, rows = research_screen.parse_registry(ledger.read_text(encoding="utf-8"), ledger.relative_to(work).as_posix())
        held = {research_screen.source_key(row["url"]) for row in rows if row["url"]}
        files_dir = work / "10-sources" / "files"
        # ponytail: hashes every stored file on each add, and S-IDs count from origin/main, so two adds queued at once can
        # take one S-ID; keep a hash index beside the files, or allocate from the lane's queue, if either starts to bite.
        hashes = {_sha(path): path.name for folder in (files_dir, shelf) if folder and folder.is_dir()
                  for path in folder.iterdir() if path.is_file()}
        refused = [url for url in urls if research_screen.source_key(url) in held]
        seen: set = set()
        for url in urls:
            key = research_screen.source_key(url)
            if key in seen:
                refused.append(url)
            seen.add(key)
        for one, name in zip(files, named):
            if one["sha256"] in hashes:
                refused.append(f"{name} (the same file as {hashes[one['sha256']]})")
            elif one["sha256"] in seen:
                refused.append(name)
            elif (files_dir / name).exists() or (shelf and (shelf / name).exists()):
                refused.append(f"{name} (another file has that name)")
            seen.add(one["sha256"])
        if refused:
            raise workflow.WorkflowError(f"Already in the ledger, so nothing was added: {', '.join(refused)}")
        numbers = [int(m.group(1)) for line in lines if (m := re.match(r"\|\s*S(\d+)\b", line.strip()))]
        next_id = max(numbers, default=0) + 1
        day = stamp[:4] + "-" + stamp[4:6] + "-" + stamp[6:8]
        added, new = [], []
        for url in urls:
            title = _title(url)
            new.append(_cells(heads, sid=f"S{next_id}", title=title, link=url, day=day))
            added.append({"kind": "url", "title": title, "key": research_screen.source_key(url), "id": f"S{next_id}"})
            next_id += 1
        for one, name in zip(files, named):
            source = staged / one["sha256"]
            if source.stat().st_size > FILE_BYTES:
                shelf.mkdir(parents=True, exist_ok=True)
                place = shelf / name
                shutil.copyfile(source, place)
                copied.append(place)
                link, key = (f"<{place}>" if " " in str(place) else str(place)), f"file:{place}"
            else:
                files_dir.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, files_dir / name)
                relative = os.path.relpath(files_dir / name, ledger.parent)
                link, key = relative, f"file:{(files_dir / name).relative_to(work).as_posix()}"
            new.append(_cells(heads, sid=f"S{next_id}", title=name, link=link, day=day))
            added.append({"kind": "file", "title": name, "key": key, "id": f"S{next_id}", "bulk": key.startswith("file:/")})
            next_id += 1
        if heads[0] in ("id", "#"):
            for one in added:
                one["key"] = one["key"] if one["kind"] == "url" else f"id:{one['id']}"
        else:
            for one in added:
                one.pop("id")
        ledger.write_text("\n".join(lines[:end] + new + lines[end:]) + "\n", encoding="utf-8")
        _git(work, "add", "-A")
        summary = f"Add {len(added)} source{'s' if len(added) > 1 else ''} to {target}'s ledger"
        listed = "\n".join(f"- {one['title']}" for one in added)
        _git(work, "commit", "-q", "-m", summary, "-m",
             f"Added from the Research page's Sources tab as grade U, not yet read (source-intake:{stamp}):\n\n{listed}",
             "-m", "Authored-with: script")
        head = _git(work, "rev-parse", "HEAD")
        filed = server.capture_ref(connection, ref=f"source-intake:{stamp}", who=who, title=summary,
                                   body=f"{listed}\n\nWorktree: {work}", repo=home["path"])
    except BaseException:
        for place in copied:
            place.unlink(missing_ok=True)
        subprocess.run(["git", "-C", str(checkout), "worktree", "remove", "--force", str(work)], capture_output=True,
                       timeout=GIT_SECONDS, check=False)
        subprocess.run(["git", "-C", str(checkout), "branch", "-D", branch], capture_output=True, timeout=GIT_SECONDS,
                       check=False)
        raise
    item = filed["item"]["id"]
    for one in files:
        (staged / one["sha256"]).unlink(missing_ok=True)
    body = _state() / f"{stamp}.md"
    body.parent.mkdir(parents=True, exist_ok=True)
    body.write_text(f"## Summary\n\nsd:{item}: {summary}, as grade U rows, not yet read. The source review on the "
                    f"Research page grades each one.\n\n{listed}\n\n## Test plan\n\nLedger rows and source files only; "
                    f"the gate runs the repository's check.\n", encoding="utf-8")
    argv = [command("sd-ship"), "-C", str(work), "lane", "enqueue", "--item", str(item), "--title", summary,
            "--body-file", str(body), "--deliver"]
    answer = _run(argv, ENQUEUE_SECONDS)
    enqueued = answer.get("exit") == 0 and answer.get("ok") is not False
    if not enqueued:
        reason = answer.get("error") or answer.get("message") or f"exit {answer.get('exit')}"
        writes.add_note(connection, item, "comment",
                        f"Not enqueued: {reason}. The commit {head[:12]} is in {work}; retry with: {shlex.join(argv)}")
        connection.commit()
    return {"item": item, "branch": branch, "worktree": str(work), "head": head, "ledger": ledger.relative_to(work).as_posix(),
            "repo": paths.key(str(home["path"])), "added": added, "enqueued": enqueued,
            "error": "" if enqueued else f"Not enqueued: {reason}. sd:{item} names the line that retries."}

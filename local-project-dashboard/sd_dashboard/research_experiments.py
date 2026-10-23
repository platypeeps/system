"""Research experiments (sd:5296): tie a research checkout to a row of the team's experiments database in the work Notion.

    python3 -m sd_dashboard.research_experiments run <run folder>    one run the dashboard started; it holds the lock

The design is `docs/work/2026-10-19-research-notion-experiments/design.md`. The Research page's Details hold three
actions: attach an experiment from the cached list, create one from a description, and write a dated status entry. The dashboard cannot call MCP tools, so each outward read or write is a headless `claude -p` run
with an exact allowlist, started through `sweep.spawn` under one lock, `<state>/lock`: one Notion writer at
a time. Every outward write is two runs: a draft that reads only and leaves `draft.json`, then an apply the operator
confirms after the page showed that draft, bound to it by its SHA-256 (`fingerprint`).

Storage:

- the link: the checkout's repo declaration of kind `experiment` (`KIND`), `{"page", "data_source", "url", "title"}`.
  A pack that does not know the kind holds none, so every checkout reads "not linked"; attach then says so.
- the experiments database: the `sd.notion_experiments` setting (`SOURCE_KEY`), read only.
- the state folder: `experiments.json`, the list cache, replaced whole; `runs/<stamp>/` per run, with `request.json`,
  `record.json`, the model's `rows.json`, `draft.json` or `out.json`, and `run.log`.

A run that dies leaves its record `running` with a dead pid, read as failed. An apply writes `out.json` right after
its outward create, so the next apply of the same draft starts from it and links without a run. Without one, the next
apply searches first, and a miss is `unknown`, never "not created": only `recreate`, confirmed, creates again. A status apply compares the page with the draft's `before` and `after`
and writes nothing when either moved since the draft.
"""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

from . import realm_sweep
from .sweep import DASHBOARD, spawn

__all__ = ["KIND", "document", "request", "run", "state_dir", "tools"]

KIND = "experiment"
SOURCE_KEY = "sd.notion_experiments"
ACTIONS = ("create", "status")
STATUSES = ("Todo", "In Progress", "Blocked", "Done")
#: The runs the page lists, newest first; the rows a list run may cache; the bytes a draft or a model file may hold.
RUNS_SHOWN = 30
ROWS = 200
FILE_BYTES = 256 * 1024
DESCRIPTION = 2000
TIMEOUT = 1800
RUNNER = (sys.executable, "-m", "sd_dashboard.research_experiments")
STAMP = re.compile(r"\d{8}-\d{6}-\d{6}")
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
PAGE = re.compile(r"[0-9a-f]{32}")
SHA = re.compile(r"[0-9a-f]{64}")
NOTION = "mcp__notion__notion-{}".format
#: Notion's hosted MCP server, as `claude mcp add` records it; the Notion runs load it alone.
NOTION_MCP = {"type": "http", "url": "https://mcp.notion.com/mcp"}
#: Each run's MCP tools, by `verb` and `action` (the design's table). Nothing else outward is allowed.
MCP = {
    ("list", None): (NOTION("search"), NOTION("fetch")),
    ("draft", "create"): (NOTION("fetch"),),
    ("draft", "status"): (NOTION("fetch"),),
    ("apply", "create"): (NOTION("search"), NOTION("fetch"), NOTION("create-pages")),
    ("apply", "status"): (NOTION("fetch"), NOTION("update-page")),
}
#: Denied beside `realm_sweep.DENIED`: Bash.
DENIED_TOO = ("Bash",)
TOLD = ("This unattended run has no person to answer. Write reaches only the run folder. Use only the MCP tools the "
        "prompt names; never delete or move a page or block. "
        "An MCP server may still be connecting "
        "when the run starts: if ToolSearch reports it still connecting, wait and search again, up to five times.")
REPLY = ("Reply with a short report. Its last line is `RESULT: ok`, or `RESULT: failed, <step>: <reason>` when a step "
         "could not finish.")


def state_dir() -> Path:
    """`st_state_dir research-experiments`, as each sweep has its own."""
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "system" / "research-experiments"


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _read_json(path: Path):
    """The file's JSON, None when it is not there; a file that does not read raises ValueError naming it."""
    try:
        if path.stat().st_size > FILE_BYTES:
            raise ValueError(f"{path.name} is over {FILE_BYTES} bytes")
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        raise ValueError(f"{path.name} did not read: {error}") from None


def page_id(value) -> str:
    """A Notion page id as 32 lower-case hex digits, dashes dropped; "" when `value` is not one."""
    text = str(value or "").replace("-", "").lower()
    return text if PAGE.fullmatch(text) else ""


def source_id(value) -> str:
    """The data source's UUID from the setting (`collection://<uuid>`) or a bare one; "" when it is neither."""
    text = str(value or "").strip().lower().removeprefix("collection://")
    return text if UUID.fullmatch(text) else ""


# -- what a run reads ---------------------------------------------------------


def _repo_root() -> Path:
    return Path(os.environ.get("REPO_ROOT", "~/repos")).expanduser()


def checkout(key: str) -> Path | None:
    """The research checkout `key` names, by the board's own walk; None when there is none."""
    from .research_screen import checkout as find

    return find(_repo_root(), str(key or ""))


def settings(connection) -> dict:
    """`{"source", "problems": {"source"}}`. A failed read is unknown, never unset."""
    out = {"source": "", "problems": {"source": ""}}
    try:
        from sd_db import writes

        rows = writes.read_settings(connection, [SOURCE_KEY])
    except Exception as error:  # noqa: BLE001 - any read failure is named, as realm_sweep.held does
        why = f"the settings did not read from the workflow database: {type(error).__name__}: {error}"
        out["problems"] = {"source": why}
        return out
    raw = rows.get(SOURCE_KEY)
    out["source"] = source_id(raw)
    if not out["source"]:
        out["problems"]["source"] = (f"{SOURCE_KEY} is not set; set it on Settings to the experiments data source"
                                     if not raw else f"{SOURCE_KEY} is not a data source id (collection://<uuid>)")
    return out


def linked(connection, place: Path) -> tuple[dict | None, str]:
    """The checkout's `experiment` declaration, or None; and why it was not read, which is never "not linked"."""
    from sd_db import repos
    from sd_db.errors import SdDbError

    reader = getattr(repos, "declarations", None)
    if reader is None:
        return None, "not read: this dashboard's sd_db has no declarations reader; update the command pack"
    try:
        held = reader(connection, place)
    except (OSError, ValueError, sqlite3.Error, SdDbError) as error:
        return None, f"not read: {error}"
    if held is None:
        return None, f"{place} is not a registered repository (sd-db repo add)"
    value = held.get(KIND)
    if value is None:
        return None, ""
    if not isinstance(value, dict) or not page_id(value.get("page")) or not source_id(value.get("data_source")):
        return None, f"the {KIND} declaration does not read as a page and a data source; clear it with sd-db repo declare"
    return value, ""


# -- runs ---------------------------------------------------------------------


def _held(state: Path) -> str:
    from .sweep import _held as held

    return held(state) or ""


def _run(folder: Path, running: str) -> dict:
    """One run as the page reads it: its request, its state, and what it left. A dead pid, or no record once the lock
    is free, reads failed; a file that does not read is named, never taken as absent."""
    out = {"run": folder.name, "state": "failed", "reason": "", "log": str(folder / "run.log")}
    try:
        asked = _read_json(folder / "request.json") or {}
        record = _read_json(folder / "record.json")
    except ValueError as error:
        out["reason"] = str(error)
        return out
    out.update({k: asked.get(k) for k in ("verb", "action", "project", "page", "draft", "recreate")})
    if record is None:
        if running:
            out.update(state="running", reason="starting")
        else:
            out["reason"] = f"the run ended without a record of its own; see {folder.parent.parent / 'start.log'}"
        return out
    out.update({k: record.get(k) for k in ("state", "reason", "started", "finished", "fingerprint", "linked")})
    if out["state"] == "running" and not realm_sweep.alive(record.get("pid")):
        if running:
            out["reason"] = "its runner stopped, and its model run still holds the lock; nothing starts until that ends"
        else:
            out.update(state="failed", reason=f"the run (pid {record.get('pid')}) stopped without recording an "
                                              f"outcome; see {out['log']}")
    if asked.get("verb") == "draft" and out["state"] == "done":
        try:
            out["draft"] = _read_json(folder / "draft.json")
        except ValueError as error:
            out.update(state="failed", reason=str(error))
    return out


def runs(state: Path, limit: int | None = RUNS_SHOWN) -> list[dict]:
    folder = state / "runs"
    names = sorted((p.name for p in folder.iterdir() if STAMP.fullmatch(p.name)), reverse=True) if folder.is_dir() else []
    running = _held(state)
    return [_run(folder / name, running if i == 0 else "") for i, name in enumerate(names[:limit])]


def _applies(state: Path, stamp: str) -> list[dict]:
    """Every apply of the draft `stamp`, newest first."""
    return [r for r in runs(state, None) if r.get("verb") == "apply" and r.get("draft") == stamp]


def document(connection, project: str = "") -> dict:
    """`GET /api/research/experiments[?project=<key>]`: the settings, the cache, the runs, and the project's link."""
    state = state_dir()
    found = settings(connection)
    out = {"read": _now(), "running": _held(state), "source": found["source"],
           "problems": found["problems"], "cache": None, "cache_error": "",
           "runs": [], "runs_error": "", "project": project, "link": None, "link_error": ""}
    try:
        cache = _read_json(state / "experiments.json")
        if cache is not None:
            out["cache"] = {**cache, "current": bool(found["source"]) and cache.get("source") == found["source"]}
    except ValueError as error:
        out["cache_error"] = str(error)
    try:
        out["runs"] = runs(state)
    except OSError as error:
        out["runs_error"] = f"the runs did not read: {error}"
    if project:
        place = checkout(project)
        if place is None:
            out["link_error"] = f"no research checkout has the key {project}"
        else:
            out["link"], out["link_error"] = linked(connection, place)
    return out


# -- the POST -----------------------------------------------------------------


def request(values: dict):
    """`POST /api/research/experiments/act`'s checks, before a connection: the start as `action_route` returns one."""
    verb = values.get("verb")
    shapes = {"list": {"verb"}, "attach": {"verb", "project", "page", "replaces"},
              "draft": {"verb", "project", "action", "description"}, "apply": {"verb", "run", "fingerprint", "recreate"}}
    if verb not in shapes or not set(values) <= shapes[verb]:
        raise ValueError("Experiments takes verb list, attach (project, page, replaces), draft (project, action, "
                         "description) or apply (run, fingerprint, recreate).")
    if verb == "list":
        return lambda connection: _start(connection, {"verb": "list", "action": None})
    if verb == "attach":
        page = page_id(values.get("page"))
        if not page:
            raise ValueError("page is a Notion page id, 32 hex digits.")
        return lambda connection: attach(connection, str(values.get("project") or ""), page, values.get("replaces"))
    if verb == "draft":
        action, text = values.get("action"), values.get("description")
        if action not in ACTIONS:
            raise ValueError(f"action is one of {', '.join(ACTIONS)}.")
        if (action == "create") != isinstance(text, str) or (action == "create" and not text.strip()):
            raise ValueError("A create draft takes a description; status takes none.")
        if action == "create" and len(text) > DESCRIPTION:
            raise ValueError(f"The description is over {DESCRIPTION} characters.")
        asked = {"verb": "draft", "action": action, "project": str(values.get("project") or ""),
                 **({"description": text.strip()} if action == "create" else {})}
        return lambda connection: _start(connection, asked)
    stamp, fingerprint = str(values.get("run") or ""), str(values.get("fingerprint") or "")
    if not STAMP.fullmatch(stamp) or not SHA.fullmatch(fingerprint) or not isinstance(values.get("recreate", False), bool):
        raise ValueError("apply takes run (a draft's stamp), fingerprint (its SHA-256) and recreate (true or false).")
    return lambda connection: apply(connection, stamp, fingerprint, values.get("recreate", False))


def attach(connection, project: str, page: str, replaces):
    """Link `project` to `page`, a row of the cached list read from the current data source. Writes the workflow
    database only; `replaces` is the page the confirm named as the link now, so a link moved since is a 409."""
    place = checkout(project)
    if place is None:
        return 400, {"error": f"no research checkout has the key {project}"}
    found = settings(connection)
    if found["problems"]["source"]:
        return 409, {"error": found["problems"]["source"]}
    try:
        cache = _read_json(state_dir() / "experiments.json")
    except ValueError as error:
        return 409, {"error": f"the experiments list {error}; refresh it"}
    if not cache or cache.get("source") != found["source"]:
        return 400, {"error": "the experiments list was read from another data source, or never; refresh it first"}
    row = next((r for r in cache.get("rows") or [] if isinstance(r, dict) and page_id(r.get("page")) == page), None)
    if row is None:
        return 400, {"error": f"{page} is not in the experiments list; refresh it, then pick a listed one"}
    held, why = linked(connection, place)
    if why:
        return 409, {"error": f"the link {why}"}
    if page_id((held or {}).get("page")) != page_id(replaces):
        return 409, {"error": "the link changed since the page read it; read it again", "stale": True}
    value = {"page": page, "data_source": found["source"], **_named(row)}
    return _link(connection, place, value)


def _named(row: dict) -> dict:
    url, title = row.get("url"), row.get("title")
    return {**({"url": url} if isinstance(url, str) and url.startswith("https://") else {}),
            **({"title": title[:200]} if isinstance(title, str) and title.strip() else {})}


def _link(connection, place: Path, value: dict):
    from sd_db import repos

    try:
        _, before = repos.set_declaration(connection, place, KIND, value)
    except repos.RepoRefusal as error:
        if "not a declaration kind" in str(error):
            return 409, {"error": f"this dashboard's command pack does not know the {KIND} declaration yet; update the "
                                  "command pack, then attach again"}
        return 409, {"error": str(error)}
    return 200, {"linked": value, "before": before}


def _start(connection, asked: dict, draft: bytes | None = None):
    """Check what the run needs, then start it under the lock; the run folder is written only once the lock is held."""
    found = settings(connection)
    if found["problems"]["source"]:
        return 409, {"error": found["problems"]["source"]}
    asked = {**asked, "source": found["source"]}
    if asked["verb"] != "list":
        place = checkout(asked["project"])
        if place is None:
            return 400, {"error": f"no research checkout has the key {asked['project']}"}
        held, why = linked(connection, place)
        if why:
            return 409, {"error": f"the link {why}"}
        if held and source_id(held.get("data_source")) != found["source"]:
            return 409, {"error": f"source: the link names data source {held.get('data_source')}, not the configured "
                                  f"{found['source']}; attach again from a fresh list", "stale": True}
        asked.update(checkout=str(place), label=asked["project"], page=page_id((held or {}).get("page")),
                     url=(held or {}).get("url") or "")
        if asked["action"] == "create" and asked["page"]:
            return 409, {"error": f"{asked['project']} is linked already; attach replaces the link"}
        if asked["action"] != "create" and not asked["page"]:
            return 409, {"error": f"{asked['project']} is not linked to an experiment; attach or create one first"}
    state = state_dir()
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    folder = state / "runs" / stamp

    def prepare():
        folder.mkdir(parents=True)
        if draft is not None:
            (folder / "draft.json").write_bytes(draft)
        realm_sweep.write_record(folder / "request.json", asked)

    what = f"{asked['verb']} {asked['action'] or 'experiments'}"
    status, body = spawn(state, DASHBOARD, [*RUNNER, "run", str(folder)],
                         f"python3 -m sd_dashboard.research_experiments run {folder}", "An experiments run", prepare)
    return status, ({**body, "run": stamp, "what": what} if status == 202 else body)


def apply(connection, stamp: str, fingerprint: str, recreate: bool):
    """Start the apply of draft `stamp`, only while the draft the page showed still holds (the design's evidence)."""
    state = state_dir()
    folder = state / "runs" / stamp
    try:
        drafted = _read_json(folder / "request.json")
        record = _read_json(folder / "record.json") or {}
        body = (folder / "draft.json").read_bytes()
    except (OSError, ValueError) as error:
        return 409, {"error": f"the draft {stamp} did not read: {error}"}
    if not drafted or drafted.get("verb") != "draft" or record.get("state") != "done":
        return 409, {"error": f"{stamp} is not a finished draft"}
    if drafted.get("action") not in ACTIONS:
        return 409, {"error": f"{stamp} drafts a {drafted.get('action')}, which this dashboard no longer applies; draft again"}
    if hashlib.sha256(body).hexdigest() != fingerprint:
        return 409, {"error": "fingerprint: the draft changed since the page showed it; read it again", "stale": True}
    found = settings(connection)
    place = checkout(drafted.get("project"))
    held, why = linked(connection, place) if place else (None, "has no checkout")
    if why:
        return 409, {"error": f"the link {why}"}
    now = {"source": found["source"], "page": page_id((held or {}).get("page"))}
    if held and source_id(held.get("data_source")) != found["source"]:
        now["source"] = f"{found['source']} (the link names {held.get('data_source')})"
    for field, value in now.items():
        if value != drafted.get(field):
            return 409, {"error": f"{field}: it changed since the draft ({drafted.get(field) or 'none'} then, "
                                  f"{value or 'none'} now); draft again", "stale": True}
    earlier = _applies(state, stamp)
    if any(r["state"] in ("running", "done") for r in earlier):
        return 409, {"error": f"applied: an apply of {stamp} is running or done"}
    if any(r["state"] == "unknown" for r in earlier) and not recreate:
        return 409, {"error": "unknown: an earlier apply found no trace of its write; refresh and attach, or apply "
                              "again with recreate, which may make a duplicate"}
    outs = [o for o in (_out(state / "runs" / r["run"]) for r in earlier) if o]
    if drafted["action"] == "create" and outs and page_id(outs[0].get("page")):
        # An earlier apply created the row and stopped before the link: link it without a run.
        value = {"page": page_id(outs[0]["page"]), "data_source": found["source"], **_named(
            {"url": outs[0].get("url"), "title": json.loads(body).get("title")})}
        return _link(connection, place, value)
    asked = {"verb": "apply", "action": drafted["action"], "project": drafted["project"], "draft": stamp,
             "fingerprint": fingerprint, "recreate": recreate, "earlier": bool(earlier)}
    return _start(connection, asked, draft=body)


def _connect():
    """The runner's own write connection, for the link a create leaves; the server's POSTs get theirs from the server."""
    from sd_db import connect

    return connect()


def _out(folder: Path) -> dict | None:
    try:
        found = _read_json(folder / "out.json")
    except ValueError:
        return None
    return found if isinstance(found, dict) else None


# -- the run ------------------------------------------------------------------


def tools(asked: dict, folder: Path) -> tuple[list[str], list[str]]:
    """`(allowed, denied)` for the run: its MCP row, Read and Write in its folder, Read of the checkout for a draft.
    Denied is `realm_sweep.DENIED` less this run's own tools, so no deny shadows an allow, plus Bash. Write's grant is an `Edit(path)` rule:
    Claude Code checks every file write against Edit rules and ignores a `Write(path)` one (sd:5751)."""
    key = (asked["verb"], asked.get("action"))
    allowed = [f"Read(/{folder}/**)", f"Edit(/{folder}/**)", *MCP[key]]
    if asked["verb"] == "draft":
        allowed.append(f"Read(/{asked['checkout']}/**)")
    denied = [n for n in (*realm_sweep.DENIED, *DENIED_TOO) if n not in allowed]
    return allowed, denied


def claude_argv(asked: dict, folder: Path, prompt: str) -> list[str]:
    """The run: exact allowlist, dontAsk, never skipped permissions. Every run loads notion alone, which finds the
    credentials its login stored under that name. `--setting-sources project`
    skips the user's own server list, and without `--strict-mcp-config` the claude.ai connectors load in its place:
    the first draft called the claude.ai Notion fetch, which dontAsk refused (sd:5751)."""
    allowed, denied = tools(asked, folder)
    servers = ["--strict-mcp-config", "--mcp-config", json.dumps({"mcpServers": {"notion": NOTION_MCP}})]
    return [realm_sweep.claude_binary(), "-p", prompt, "--permission-mode", "dontAsk", "--permission-prompts", "none",
            "--setting-sources", "project", *servers, "--tools", "Read,Write,ToolSearch",
            "--allowedTools", ",".join(allowed), "--disallowedTools", ",".join(denied), "--append-system-prompt", TOLD]


def pointer(asked: dict) -> str:
    """The line a created row's body opens with, which a later apply searches for (the design's look before write)."""
    return f"Linked from research project {asked['project']}, draft {asked['draft']}."


def prompt(asked: dict, folder: Path) -> str:
    verb, action = asked["verb"], asked.get("action")
    facts = [f"- Run folder: {folder}. Write files only there.",
             f"- Experiments data source: collection://{asked['source']}"]
    if verb != "list":
        facts.append(f"- Research project: {asked['project']}, checked out at {asked['checkout']}.")
    if asked.get("page"):
        facts.append(f"- Experiment page: {asked['page']} {asked.get('url') or ''}".rstrip())
    head = "\n".join(facts)
    if verb == "list":
        return f"""You list the team's experiments for the operator's dashboard, unattended. Nobody answers questions.

{head}

1. Fetch the data source with notion-fetch to learn its title property, Stage and Status.
2. Read its rows with notion-search within the data source, up to {ROWS}, most recently edited first.
3. Write {folder}/rows.json: a JSON array, one object per row:
   {{"page": "<page id>", "url": "<page url>", "title": "...", "stage": "...", "status": "...", "edited": "<last edited time>"}}

{REPLY}
"""
    if verb == "draft":
        read = ("Read the checkout's overview, brief and status documents for context. Read nothing else on disk. "
                "You write nothing to Notion: the operator confirms this draft, and another run writes it.")
        if action == "create":
            return f"""You draft a new row for the team's experiments database, unattended. Nobody answers questions.

{head}
- The operator's description: {asked['description']}

{read}

1. Fetch the data source once to learn its properties.
2. Write {folder}/draft.json:
   {{"title": "<a short experiment title>", "problem": "<the Problem property: two to four sentences>",
    "body": "<the page body, Markdown: hypothesis, approach, how success is measured, and the checkout's path>"}}
   Stage will be Idea. Use only what the description and the checkout say.

{REPLY}
"""
        today = datetime.datetime.now().astimezone().date().isoformat()
        return f"""You draft a dated status update for one experiment, unattended. Nobody answers questions.

{head}

{read}

1. Fetch the experiment page. Its status section is the heading named Status or Status updates and all under it, to
   the next heading of the same level; none is an empty section. Note its Status property.
2. Write {folder}/draft.json:
   {{"before": {{"section": "<the section exactly as fetched>", "status": "<the Status property as fetched>"}},
    "after": {{"section": "<the new section>", "status": "<one of {', '.join(STATUSES)}>"}},
    "entry": "<the new entry alone>"}}
   The new section keeps the heading. The new entry is first, headed {today}: what changed, where it stands, next
   steps. Each earlier entry follows, condensed to one line that starts with its date, newest first; drop none.

{REPLY}
"""
    spec = json.loads((folder / "draft.json").read_text(encoding="utf-8"))
    draft = f"- The approved draft: {folder}/draft.json. Write exactly what it says; add nothing."
    out = f"{folder}/out.json"
    if action == "create":
        if asked["recreate"]:
            first = "An earlier apply may have created this row; the operator chose to create it again. Go to step 2."
        elif asked["earlier"]:
            first = (f"An earlier apply may have created this row before it recorded it. Search the data source for "
                     f"\"{spec.get('title')}\" with notion-search and fetch each result. If one body holds the pointer "
                     f"line, write {out} as {{\"outcome\": \"adopted\", \"page\": \"<its id>\", \"url\": \"<its url>\"}} "
                     f"and stop. If none does, write {out} as {{\"outcome\": \"unknown\"}} and stop: a search can miss a "
                     "row, so create nothing.")
        else:
            first = "Go to step 2."
        return f"""You write one approved experiment to the team's experiments database, unattended. Nobody answers.

{head}
{draft}
- Pointer line: {pointer(asked)}

1. {first}
2. Create one page in the data source with notion-create-pages: its title property set to the draft's title, Problem
   to its problem, Stage to Idea. The body is the pointer line, then the draft's body.
3. Right after the create, before any other call, write {out} as
   {{"outcome": "created", "page": "<the new page id>", "url": "<its url>"}}.
Never update, move or delete any page.

{REPLY}
"""
    return f"""You write one approved status update to one experiment page, unattended. Nobody answers questions.

{head}
{draft}

1. Fetch the experiment page. Compare its status section with the draft's before.section, and its Status property
   with before.status.
2. Judge each part. Equal to before: write it. Equal to after: skip it, an earlier apply wrote it. Anything else:
   stale, a person changed it after the draft.
3. If either part is stale, write nothing to the page. Otherwise, with notion-update-page, replace only the status
   section's content with after.section, and set Status to after.status; touch nothing else on the page.
4. Write {out} as {{"page": "<the page id>", "section": "written|skipped|stale", "status": "written|skipped|stale"}}.

{REPLY}
"""


def _checked(asked: dict, folder: Path) -> tuple[str, dict]:
    """After a model run that said ok: what it left, judged. `(state, fields for the record)`; a reason on failure."""
    verb, action = asked["verb"], asked.get("action")
    if verb == "list":
        rows = _read_json(folder / "rows.json")
        if not isinstance(rows, list) or len(rows) > ROWS:
            raise ValueError(f"rows.json is not a list of at most {ROWS} rows")
        kept = [{"page": page_id(r.get("page")), "url": r.get("url") if str(r.get("url", "")).startswith("https://") else "",
                 **{k: str(r.get(k) or "")[:200] for k in ("title", "stage", "status", "edited")}}
                for r in rows if isinstance(r, dict) and page_id(r.get("page"))]
        if len(kept) != len(rows):
            raise ValueError("rows.json has a row without a page id")
        realm_sweep.write_record(state_dir() / "experiments.json",
                                 {"read": _now(), "source": asked["source"], "rows": kept, "run": folder.name})
        return "done", {"reason": f"{len(kept)} experiments listed"}
    if verb == "draft":
        draft = _read_json(folder / "draft.json")
        _shape(action, draft)
        return "done", {"fingerprint": hashlib.sha256((folder / "draft.json").read_bytes()).hexdigest()}
    out = _out(folder)
    if out is None:
        raise ValueError("the run wrote no out.json")
    if out.get("outcome") == "unknown":
        return "unknown", {"reason": "the earlier write was not found; nothing was written. Refresh and attach, or "
                                     "apply again with recreate"}
    if action == "create":
        page = page_id(out.get("page"))
        if not page:
            raise ValueError("out.json names no page id")
        title = json.loads((folder / "draft.json").read_text(encoding="utf-8")).get("title")
        value = {"page": page, "data_source": asked["source"], **_named({"url": out.get("url"), "title": title})}
        try:
            with contextlib.closing(_connect()) as connection:
                status, body = _link(connection, Path(asked["checkout"]), value)
        except Exception as error:  # noqa: BLE001 - an open or write failure is named; out.json stays for the next apply
            status, body = 0, {"error": f"{type(error).__name__}: {error}"}
        if status != 200:
            raise ValueError(f"created {page}, but the link was not written: {body['error']}; apply again to link it")
        return "done", {"linked": value}
    if action != "status":
        raise ValueError(f"{verb} {action} is not an action this dashboard applies")
    if page_id(out.get("page")) != asked["page"]:
        raise ValueError(f"out.json names page {out.get('page')}, not the linked {asked['page']}")
    parts = {part: out.get(part) for part in ("section", "status")}
    if "stale" in parts.values():
        raise ValueError("stale: the page changed after the draft, so nothing was written; draft again")
    if not set(parts.values()) <= {"written", "skipped"}:
        raise ValueError(f"out.json says {parts}, not written or skipped")
    return "done", {"reason": f"section {parts['section']}, Status {parts['status']}"}


def _shape(action: str, draft) -> None:
    """A draft the confirm can show and the apply can write; anything else raises ValueError naming it."""
    def text(value, name):
        if not isinstance(value, str):
            raise ValueError(f"draft.json: {name} is not text")

    if not isinstance(draft, dict):
        raise ValueError("draft.json is not an object")
    if action == "create":
        for name in ("title", "problem", "body"):
            text(draft.get(name), name)
        if not draft["title"].strip():
            raise ValueError("draft.json: title is empty")
    elif action == "status":
        for side in ("before", "after"):
            part = draft.get(side)
            if not isinstance(part, dict):
                raise ValueError(f"draft.json: {side} is not an object")
            text(part.get("section"), f"{side}.section")
            text(part.get("status"), f"{side}.status")
        if draft["after"]["status"] not in STATUSES:
            raise ValueError(f"draft.json: after.status is not one of {', '.join(STATUSES)}")
    else:
        raise ValueError(f"draft.json: {action} is not a draft this dashboard shows")


def _lock(state: Path) -> int | None:
    """The descriptor of `<state>/lock` that `spawn` passed this runner, None when it holds none. The model run inherits
    it, so a runner killed mid-run leaves its writer holding the lock, and no next apply starts beside that writer.
    The file open but not locked is no lock: a runner started by hand holds none."""
    if not _held(state):
        return None
    try:
        want = os.stat(state / "lock")
        names = os.listdir("/dev/fd")
    except OSError:
        return None
    for name in names:
        with contextlib.suppress(OSError, ValueError):
            seen = os.fstat(int(name))
            if (seen.st_dev, seen.st_ino) == (want.st_dev, want.st_ino):
                return int(name)
    return None


def run(folder: Path, out, err) -> int:
    """One run, under the lock `spawn` holds for it: the record, the model, then what it left, judged."""
    folder = Path(folder).resolve()
    if folder.parent != (state_dir() / "runs").resolve() or not STAMP.fullmatch(folder.name):
        print(f"experiments: {folder} is not a run folder", file=err)
        return realm_sweep.EXIT_ERROR
    asked = json.loads((folder / "request.json").read_text(encoding="utf-8"))
    record = {"state": "running", "started": _now(), "finished": None, "reason": None, "pid": os.getpid(),
              "log": str(folder / "run.log")}
    realm_sweep.write_record(folder / "record.json", record)

    def end(state: str, reason: str, **more) -> int:
        record.update(state=state, reason=reason, finished=_now(), **more)
        realm_sweep.write_record(folder / "record.json", record)
        print(f"experiments: {state}: {reason}", file=out if state == "done" else err)
        return realm_sweep.EXIT_OK if state == "done" else realm_sweep.EXIT_ERROR

    lock = _lock(state_dir())
    if lock is None:
        return end("failed", "this runner holds no experiments lock; start it from the dashboard")
    timeout = int(os.environ.get("RESEARCH_EXPERIMENTS_TIMEOUT", TIMEOUT))
    try:
        argv = claude_argv(asked, folder, prompt(asked, folder))
    except (KeyError, OSError, ValueError) as error:
        return end("failed", f"the run was not built: {error}")
    with (folder / "run.log").open("a", encoding="utf-8") as logged:
        code, reason, _ = realm_sweep.ask(argv, folder, timeout, logged, keep=(lock,))
    if code != realm_sweep.EXIT_OK:
        return end("failed", reason)
    try:
        state, more = _checked(asked, folder)
    except (OSError, ValueError) as error:
        return end("failed", str(error))
    return end(state, more.pop("reason", reason), **more)


def main(argv: list[str], out=None, err=None) -> int:
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    if argv[:1] in (["-h"], ["--help"], ["help"]):
        print(__doc__.strip().split("\n\n")[1], file=out)
        return 0
    if len(argv) == 2 and argv[0] == "run":
        return run(Path(argv[1]), out, err)
    print("usage: python3 -m sd_dashboard.research_experiments run <run folder>", file=err)
    return realm_sweep.EXIT_ERROR


if __name__ == "__main__":
    # The pack's lib/ first, as realm_sweep does: the dashboard starts this as its own process.
    from . import pack_library

    sys.path.insert(0, str(pack_library()))
    sys.exit(main(sys.argv[1:]))

"""File issue (sd:5103): one click on a task drafts a public-safe GitHub or Jira issue about it; a second files it.

The operator asked on 2026-10-18 (sd:5103): per repository, GitHub filing on or off (`repo.github_issues`) and a Jira
project (`repo.jira_project`); on a task, File GitHub issue and File Jira issue; and no internal information or
reference in what is filed. Nothing is filed without the operator's Confirm on the exact text: there is no auto-file.

Steps, each one state of the run file `<state>/runs/<id>.json`:

1. `start`, in the POST `/api/items/<id>/file-issue` `{revision, target}`: the item's revision must be the one the page
   read, its repository must enable the target, and the lock `<state>/lock` must be free (a 409 names the run holding
   it). The run file is written `queued`, with the item's text beside it as `<id>.input.md` and what the sanitizer
   must strip (the target, the other registered repositories, the checkout's branch and worktree names, this
   machine's names). This file then runs as the runner, in a session of its own, holding the lock's descriptor.
2. `run`, the draft: `claude -p` with no tools and no MCP servers (email_draft's `CLAUDE` line, read from the run
   file) reads `PROMPT` and the item on stdin, bounded by `MODEL_TIMEOUT`; its stderr goes to `<id>.log`. The reply is
   a `Title:` line, a blank line and a body. The drafting run holds no filing tool and no credential use.
3. `sanitize`, deterministic, after the draft: NFKC, format characters dropped, then every match of the run's own names
   and of `RULES`, in that order, replaced by `REMOVED` until nothing changes; the body gains `TOKEN_LINE`, a random
   token of this run; then `findings` must be empty and `leak-guard.sh scan` must pass over the exact title and
   body. A leftover hit, a privacy-pattern line, a missing
   pattern file or an over-long text marks the run `blocked`, naming the category or the line, never the value.
   Otherwise `ready`, with `digest`.
4. Preview: `GET /api/issue-runs/<id>` answers the state file: the exact title, body and target. A read never files.
5. `confirm`, in the POST `/api/issue-runs/<id>/confirm` `{digest}`: the digest must be the previewed text's, the
   repository must still enable that same target, the text must pass `findings` and the scan again, and the lock must
   be free. The run is written `filing` before the call, so a server that dies mid-call leaves a run that reads
   `unknown`, and Confirm refuses it. GitHub: `gh issue create`, once (a server process has no MCP session, so gh is
   its one way; sessions use the GitHub MCP tools). Jira: `POST /rest/api/3/issue` through `sd_db.shadow_jira`'s
   transport and settings (`JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`, from the server's environment). Only an
   answer that proves a refusal (a 4xx but 408, gh's checks before it creates, no sign-in) returns the run to
   `ready` for another Confirm. Any other outcome is not known: the run looks for its issue (`find_github` lists the
   repository's newest issues, `find_jira` runs JQL by project and age; both match only the run's token, never a
   title), links it if found, and reads `unknown` if not. Check (`/api/issue-runs/<id>/check`) looks again for
   an `unknown` run's issue and never files.
6. Link-back, in the same POST: the item gains `--ref github:<owner>/<repo>#N` or `jira:<KEY-N>` when it carries no
   source of its own (`sd task add --ref`'s `task-ref`), and one comment note naming the URL; the run is `linked`. A
   link-back that fails leaves `filed` with the reason, and Confirm again retries the link-back only.

`discard` ends a run that is not filing or filed; an `unknown` one may have filed, as its reason says. `status` reads
a run whose runner is gone without an outcome as `failed`, and a `filing` run whose server is gone as `unknown`.
"""

from __future__ import annotations

import datetime
import fcntl
import hashlib
import json
import os
import re
import secrets
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import unicodedata
from pathlib import Path

__all__ = ["BODY_MAX", "PROMPT", "REMOVED", "RULES", "TARGETS", "TITLE_MAX", "check", "confirm", "discard",
           "findings", "request", "run", "sanitize", "scan", "start", "status"]

#: This repository's checkout: the leak guard's `scan` lives here.
CHECKOUT = Path(__file__).resolve().parents[2]
LEAK_GUARD = CHECKOUT / "local-leak-guard" / "leak-guard.sh"
TARGETS = ("github", "jira")
TRACKER = {"github": "GitHub", "jira": "Jira"}
MODEL_TIMEOUT = 180
FILE_TIMEOUT = 60
SCAN_TIMEOUT = 20
GIT_SECONDS = 10
#: How long a queued run may wait for its runner, and a filing run for its server, before it reads as failed or unknown.
STARTUP = 60
FILING_GRACE = FILE_TIMEOUT + 30
#: GitHub takes 256 title and 65,536 body characters, Jira 255 and 32,767; these sit under both.
TITLE_MAX = 200
BODY_MAX = 20000
#: The item text the model reads, newest notes kept when it is longer.
TEXT_MAX = 12000
#: What stands where the sanitizer took something out, so the preview shows the place.
REMOVED = "[removed]"
#: Passes of `RULES` before a text that still changes is refused.
PASSES = 12
RUN_ID = re.compile(r"(github|jira)-[1-9][0-9]{0,18}-[0-9]{8}T[0-9]{6}Z")
DIGEST = re.compile(r"[0-9a-f]{64}")
WHO = "dashboard"

PROMPT = ("Write a short public bug report or feature request about the work item below, for an issue tracker read by "
          "people outside this workspace. State the problem or request, the expected and the actual behaviour, and "
          "what would settle it. Leave out every internal detail: item or note numbers, file paths, branch, worktree, "
          "host, machine or session names, email addresses, internal URLs, other repositories, people's names and "
          "anything about how the work is tracked or run here. Invent nothing the item does not state. Reply with "
          "exactly this and nothing else: one line `Title: <title>`, one blank line, then the body in plain Markdown.")
NOTE = "Filed {tracker} issue {ref}: {url}"
#: The last line of every filed body: a random token, unique to the run, that recovery matches and nothing else does.
#: Opaque, so it names nothing internal; a comment on GitHub, which Markdown hides, and a plain line on Jira.
TOKEN_LINE = {"github": "<!-- filing-token: {token} -->", "jira": "Filing token: {token}"}

# -- the sanitizer ---------------------------------------------------------------------------------------------------
#
# Every rule is a category and a pattern; a match is replaced by REMOVED. Patterns are linear: no nested quantifier,
# so a megabyte of hostile text costs a pass per rule, not a backtracking blow-up.

_PRIVATE_HOST = r"(?:local|lan|internal|intranet|corp|home\.arpa|localdomain|localhost|ts\.net|tailscale\.net)"
#: A run of characters a rule can start in begins only where the character before cannot extend it, so a long run is
#: read once from its start and never from every position inside it (the property test feeds such runs). Each
#: look-behind is its run's own class, never `\w`: a letter such as `é` glued before a name must not hide it.
RULES = (
    ("credential", re.compile(r"(?<![A-Za-z0-9_])(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
                              r"|sk-[A-Za-z0-9_-]{10,}|xox[abprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}"
                              r"|eyJ[A-Za-z0-9_-]{8,}+\.[A-Za-z0-9_-]{8,}+\.[A-Za-z0-9_-]{8,})")),
    ("credential", re.compile(r"(?i)\bbearer\s++\S+|-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("session name", re.compile(r"(?i)claude\.ai/code\S*|session_[A-Za-z0-9]{6,}")),
    ("internal URL", re.compile(r"(?i)(?<![a-z0-9+.-])(?:file|vscode|obsidian|x-[a-z-]++)://\S*")),
    ("internal URL", re.compile(r"(?i)(?<![a-z0-9+.-])[a-z][a-z0-9+.-]*+://(?:[^\s/@]*+@)?(?:"
                                r"(?:[a-z0-9-]++\.)*" + _PRIVATE_HOST + r"(?![a-z0-9-])"
                                r"|(?:\d{1,3}\.){3}\d{1,3}|\[[0-9a-f:.]*+\]|[a-z0-9-]++(?![.a-z0-9-]))\S*")),
    # An empty local part too: `o@@example.test` leaves `@example.test`, which still names the mail domain.
    ("email address", re.compile(r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*+@[A-Za-z0-9-]++(?:\.[A-Za-z0-9-]++)+")),
    ("local path", re.compile(r"(?:~|\$HOME|\$\{HOME\})/\S*|(?<![\w.])~(?=[\s,;:)\]]|$)")),
    # No look-behind: `/System/Volumes/Data/Users/x` and a glued `/Us/Users/ers` are paths too; a public URL with such
    # a segment loses its tail, which blocks nothing and leaks nothing.
    ("local path", re.compile(r"/(?:Users|Volumes|home|private|var/folders|tmp|opt/homebrew)(?![\w.-])\S*")),
    ("local path", re.compile(r"(?i)(?<![A-Za-z])[A-Z]:\\(?:Users|Documents and Settings)\\\S*")),
    # `sd` with no word boundary, and `sd:` before a removal, so a glued `wordsd:12` or a nested `sd:sd:12` goes whole.
    ("item reference", re.compile(r"(?i)sd\s*+[:#-]\s*+(?:\d+|\[removed\])|(?<![&/])#\d+\b|\bnote\s++\d+\b")),
    ("host name", re.compile(r"(?i)(?<![a-z0-9.-])(?:[a-z0-9-]++\.)+" + _PRIVATE_HOST + r"(?![\w-])"
                             r"|(?<![\w.-])localhost(?![\w-])")),
    ("IP address", re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")),
    ("branch name", re.compile(r"(?<![A-Za-z0-9._-])[A-Za-z0-9._-]++/\d{4}-\d{2}-\d{2}[A-Za-z0-9._-]*")),
)
#: Characters a reader does not see but a pattern stops at: zero-width, joiners, bidi controls (Unicode category Cf).
_INVISIBLE = {"Cf", "Co", "Cs"}


def normalized(text: str) -> str:
    """NFKC, with format and private-use characters gone and every control but tab and newline a space.

    A fullwidth `／Ｕｓｅｒｓ／` or a zero-width space inside `sd:12` reads as the plain text once this runs, so the
    rules see what a reader of the issue sees.
    """
    text = unicodedata.normalize("NFKC", text.replace("\r\n", "\n").replace("\r", "\n"))
    out = []
    for char in text:
        category = unicodedata.category(char)
        if category in _INVISIBLE:
            continue
        out.append(" " if category == "Cc" and char not in "\n\t" else char)
    return "".join(out)


def _literal_rules(context: dict) -> list[tuple[str, re.Pattern]]:
    """The run's own names, as rules: the repositories that are not the target, branches, worktrees, this machine."""
    rules = []
    for category, key in (("private repository", "repositories"), ("branch name", "branches"),
                          ("host name", "hosts")):
        names = sorted({name for name in context.get(key) or () if isinstance(name, str) and len(name) >= 3},
                       key=len, reverse=True)
        for slashed in (True, False):
            group = [name for name in names if ("/" in name) == slashed]
            if group:
                alternation = "|".join(re.escape(name) for name in group)
                # A name with a slash is distinctive, so it goes even glued to a word; a plain one (a host such as
                # `hub`) keeps an ASCII boundary, or `github` would lose its tail. `é` before it is no boundary.
                edge = ("", "") if slashed else (r"(?<![A-Za-z0-9._-])", r"(?![A-Za-z0-9_-])")
                rules.append((category, re.compile(r"(?i)" + edge[0] + "(?:" + alternation + ")" + edge[1])))
    home = context.get("home")
    if isinstance(home, str) and len(home) > 1:
        rules.append(("local path", re.compile(re.escape(home.rstrip("/")) + r"\S*")))
    return rules


def _rules(context: dict) -> list[tuple[str, re.Pattern]]:
    # The run's own names first: a generic rule that took a name's first half (`o@@example.testEXAMPLE/...` as an email)
    # would leave its second half for no rule to find.
    return [*_literal_rules(context), *RULES]


def findings(title: str, body: str, context: dict) -> list[str]:
    """The categories a rule still finds in the text as it would be filed; empty means none. Never the value."""
    found = []
    for category, pattern in _rules(context):
        if category not in found and (pattern.search(title) or pattern.search(body)):
            found.append(category)
    return found


def _strip(text: str, rules, removed: dict) -> str | None:
    """`text` with every rule's match replaced, to a fixed point; None when it still changes after `PASSES`."""
    for _ in range(PASSES):
        before = text
        for category, pattern in rules:
            text, hits = pattern.subn(REMOVED, text)
            if hits:
                removed[category] = removed.get(category, 0) + hits
        if text == before:
            return text
    return None


def sanitize(title: str, body: str, context: dict) -> dict:
    """`{"title", "body", "removed", "blocked"}`: the text as it would be filed, what came out, and what blocks it.

    `removed` counts each category taken out. `blocked` lists what stops a filing: a category a rule still finds, an
    empty or over-long text. It names no value. The privacy patterns are `scan`'s, run after this on the result.
    """
    rules = _rules(context)
    removed: dict[str, int] = {}
    blocked: list[str] = []
    title = " ".join(normalized(title).split())
    body = normalized(body).strip()
    if len(title) > 4 * TITLE_MAX or len(body) > 4 * BODY_MAX:
        return {"title": "", "body": "", "removed": removed,
                "blocked": [f"the draft is longer than {4 * BODY_MAX} characters, so it was not read"]}
    stripped_title, stripped_body = _strip(title, rules, removed), _strip(body, rules, removed)
    if stripped_title is None or stripped_body is None:
        return {"title": "", "body": "", "removed": removed,
                "blocked": [f"the text still changed after {PASSES} sanitizer passes"]}
    title, body = " ".join(stripped_title.split()), stripped_body.strip()
    blocked += [f"a {category} is left after sanitizing" for category in findings(title, body, context)]
    if not title.replace(REMOVED, "").strip(" .:-"):
        blocked.append("the title is empty after sanitizing")
    if not body.replace(REMOVED, "").strip():
        blocked.append("the body is empty after sanitizing")
    if len(title) > TITLE_MAX:
        blocked.append(f"the title is {len(title)} characters; the limit is {TITLE_MAX}")
    if len(body) > BODY_MAX:
        blocked.append(f"the body is {len(body)} characters; the limit is {BODY_MAX}")
    return {"title": title, "body": body, "removed": removed, "blocked": blocked}


def scan(title: str, body: str, guard: Path | str = LEAK_GUARD, environ: dict | None = None) -> list[str]:
    """What `leak-guard.sh scan` blocks in the text, by line, never the value; empty means it passed.

    The title is line 1 and the body follows it. No pattern file, a file grep cannot apply, a scan that fails or does
    not answer is a block too: unknown is not clean.
    """
    text = title + "\n" + body + "\n"
    try:
        done = subprocess.run(["sh", str(guard), "scan"], input=text, capture_output=True, text=True,
                              timeout=SCAN_TIMEOUT, env=environ, check=False)
    except subprocess.TimeoutExpired:
        return [f"the privacy-pattern scan did not answer within {SCAN_TIMEOUT} s"]
    except OSError as problem:
        return [f"the privacy-pattern scan did not run: {type(problem).__name__}"]
    if done.returncode == 0:
        return []
    if done.returncode == 3:
        return ["no privacy-pattern file, or no pattern in it, so the text could not be checked; create "
                "<config>/privacy-patterns (local-leak-guard/README.md)"]
    if done.returncode != 1:
        return [f"the privacy-pattern scan failed (exit {done.returncode})"]
    lines = sorted({int(word) for word in done.stdout.split() if word.isdigit()})
    if not lines:
        return ["the privacy-pattern scan found a match but named no line"]
    return ["a privacy pattern matches the title" if line == 1 else f"a privacy pattern matches body line {line - 1}"
            for line in lines]


def digest(target: dict, title: str, body: str) -> str:
    """What Confirm must send back: the previewed text and its target, hashed together."""
    return hashlib.sha256(json.dumps([target, title, body], sort_keys=True).encode("utf-8")).hexdigest()


# -- where things live -----------------------------------------------------------------------------------------------


def state_dir() -> Path:
    """`st_state_dir issue-filing`: what runs write and nobody edits."""
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "system" / "issue-filing"


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(now: datetime.datetime) -> str:
    return now.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write(path: Path, document: dict) -> None:
    """The state file, replaced whole, so a reader never sees half of one."""
    partial = path.with_name(path.name + ".partial")
    partial.write_text(json.dumps(document, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(partial, path)


def _git(directory: Path, *args: str) -> str | None:
    try:
        done = subprocess.run(["git", "-C", str(directory), *args], capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=GIT_SECONDS, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout if done.returncode == 0 else None


# -- step 1: the target and the start --------------------------------------------------------------------------------


def target_of(connection, repo_path: str | None, kind: str) -> dict:
    """`{"ok", "target", "why"}`: where a `kind` issue for a repository goes, or why it cannot go.

    GitHub needs `github_issues` on and a github.com remote; Jira needs `jira_project` set. A store that cannot answer
    is not on: the reason says so.
    """
    from sd_db import repos
    from sd_db.errors import SdDbError

    if not repo_path:
        return {"ok": False, "target": None, "why": "the item names no repository"}
    row = repos.row_for(connection, repo_path)
    if row is None:
        return {"ok": False, "target": None, "why": "the item's repository is not registered"}
    try:
        if kind == "github":
            if not repos.github_issues(connection, row["path"]):
                return {"ok": False, "target": None, "why": "GitHub issues are off for this repository (Management)"}
            remote = repos.parse_remote(row["remote"])
            if remote.kind != "github":
                return {"ok": False, "target": None, "why": "the repository's remote is not on github.com"}
            return {"ok": True, "target": {"kind": "github", "repo": f"{remote.owner}/{remote.name}"}, "why": None}
        key = repos.jira_project(connection, row["path"])
    except (SdDbError, AttributeError) as problem:
        return {"ok": False, "target": None, "why": f"the setting could not be read: {problem or type(problem).__name__}"}
    if not key:
        return {"ok": False, "target": None, "why": "no Jira project is set for this repository (Management)"}
    return {"ok": True, "target": {"kind": "jira", "project": key}, "why": None}


def targets(connection, repo_path: str | None) -> dict:
    """Both targets for one repository, as Tasks' rows carry them."""
    return {kind: target_of(connection, repo_path, kind) for kind in TARGETS}


def _text(body) -> str:
    try:
        value = json.loads(body or "")
    except ValueError:
        return body or ""
    return value.get("text") or "" if isinstance(value, dict) else body or ""


def prompt_input(state: dict) -> str:
    """The item as the model reads it: title, status, description and every note but status changes, newest kept."""
    row = state["item"]
    notes = [f"- {note['kind']}: {note['body']}" for note in state["notes"]
             if note["kind"] != "status_change" and note["body"]]
    head = f"Title: {row['title']}\nStatus: {row['status']}\nDescription: {_text(row.get('body')) or '(none)'}\n"
    while notes and len(head) + sum(len(line) + 1 for line in notes) > TEXT_MAX:
        notes.pop(0)
    return f"{PROMPT}\n\n---\n{head}Notes, oldest first:\n" + ("\n".join(notes) or "(none)") + "\n"


def _context(connection, checkout: Path | None, target: dict) -> dict:
    """What the sanitizer strips beyond its fixed rules: names this run can enumerate from disk and the store."""
    from sd_db import repos

    own = target.get("repo", "").lower()
    repositories = []
    for row in repos.registered(connection):
        remote = repos.parse_remote(row["remote"])
        if remote.kind == "github" and f"{remote.owner}/{remote.name}".lower() != own:
            repositories.append(f"{remote.owner}/{remote.name}")
            # The bare name too when it is a name, not a word (`secret-plans`, not `tool`), and not the target's own.
            if "-" in remote.name and remote.name.lower() != own.rpartition("/")[2]:
                repositories.append(remote.name)
    branches, worktrees = [], []
    if checkout is not None and checkout.is_dir():
        heads = _git(checkout, "for-each-ref", "--format=%(refname:short)", "refs/heads") or ""
        # A branch named `main` or `fix` is a word; a branch name with a slash or a dash is a name.
        branches = [name for name in heads.split() if ("/" in name or "-" in name)]
        listed = _git(checkout, "worktree", "list", "--porcelain") or ""
        worktrees = [Path(line[len("worktree "):]).name for line in listed.splitlines() if line.startswith("worktree ")]
    host = socket.gethostname()
    hosts = {host, host.split(".")[0]}
    return {"repositories": repositories, "branches": branches + [name for name in worktrees if "-" in name],
            "hosts": sorted(name for name in hosts if name), "home": str(Path.home())}


def _held_by(handle) -> str:
    handle.seek(0)
    return handle.read().strip() or "unknown"


def _lock():
    """The lock's handle, taken without waiting, or `(None, holder)`."""
    state = state_dir()
    (state / "runs").mkdir(parents=True, exist_ok=True)
    handle = open(state / "lock", "a+", encoding="utf-8")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        holder = _held_by(handle)
        handle.close()
        return None, holder
    return handle, None


def start(connection, item: int, kind: str, *, expected_revision: str, now: datetime.datetime | None = None):
    """Step 1: `(202, run)`; `(409, {"error"})` when a run holds the lock or the target is off."""
    from sd_db import paths, workflow

    from .email_draft import CLAUDE

    now = now or _now()
    state = workflow.item_state(connection, item)
    if state["revision"] != expected_revision:
        raise workflow.StaleItem(f"item {item} changed; reload it before applying this change")
    row = state["item"]
    found = target_of(connection, row.get("repo"), kind)
    if not found["ok"]:
        return 409, {"error": f"File {TRACKER[kind]} issue is off for #{item}: {found['why']}."}
    handle, holder = _lock()
    if handle is None:
        return 409, {"error": f"An issue draft or filing is already running (run {holder}); one runs at a time. "
                              f"Wait for it, then start this one."}
    try:
        run_id = f"{kind}-{item}-{now.astimezone(datetime.timezone.utc):%Y%m%dT%H%M%SZ}"
        runs = state_dir() / "runs"
        record = runs / f"{run_id}.json"
        if record.exists():
            return 409, {"error": f"Run {run_id} started this second; start it again in a moment."}
        checkout = paths.expand(row["repo"]) if row.get("repo") else None
        written = runs / f"{run_id}.input.md"
        written.write_text(prompt_input(state), encoding="utf-8")
        document = {"id": run_id, "item": item, "kind": kind, "target": found["target"], "state": "queued",
                    "reason": None, "title": None, "body": None, "removed": {}, "blocked": [], "digest": None,
                    "input": str(written), "context": _context(connection, checkout, found["target"]),
                    "claude": CLAUDE, "guard": str(LEAK_GUARD), "started": _iso(now), "finished": None,
                    "pid": None, "server": None, "filing_at": None, "url": None, "ref": None, "linked": None}
        _write(record, document)
        handle.seek(0)
        handle.truncate()
        handle.write(run_id)
        handle.flush()
        try:
            runner = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "run", str(record),
                                       str(handle.fileno())],
                                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                      pass_fds=(handle.fileno(),), start_new_session=True, cwd=str(state_dir()))
        except OSError as problem:
            _write(record, {**document, "state": "failed", "reason": f"the runner did not start: {problem}",
                            "finished": _iso(_now())})
            raise
        threading.Thread(target=runner.wait, name=f"issue draft {run_id}", daemon=True).start()
        return 202, public(document)
    finally:
        handle.close()


def request(number: int, values: dict, revision: str):
    """The POST's checks, before the server opens a writer; the start as `action_route` returns one."""
    if set(values) != {"target"} or values["target"] not in TARGETS:
        raise ValueError("File issue takes the item revision and a target: github or jira.")
    return lambda connection: start(connection, number, values["target"], expected_revision=revision)


# -- step 2 and 3: the runner ----------------------------------------------------------------------------------------


def reply(text: str) -> tuple[str, str]:
    """`(title, body)` from the model's reply, or `ValueError` saying what was wrong with it."""
    lines = text.strip().splitlines()
    found = re.fullmatch(r"\**Title:\**\s*(.+)", lines[0].strip()) if lines else None
    if not found:
        raise ValueError("the reply did not start with a Title line")
    body = "\n".join(lines[1:]).strip()
    if not body:
        raise ValueError("the reply had no body")
    return found[1].strip(), body


def _model(document: dict, log: Path) -> str:
    """The reply of one bounded tool-less `claude -p`; `TimeoutError` or `RuntimeError` saying why there is none."""
    text = Path(document["input"]).read_text(encoding="utf-8")
    with open(log, "w", encoding="utf-8") as sink:
        child = subprocess.Popen(["sh", "-c", document["claude"]], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=sink, text=True, start_new_session=True)
        try:
            out, _ = child.communicate(text, timeout=MODEL_TIMEOUT)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            child.communicate()
            raise TimeoutError(f"claude -p gave no answer in {MODEL_TIMEOUT} s") from None
    if child.returncode != 0:
        said = [line for line in log.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()]
        raise RuntimeError(f"claude -p exited {child.returncode}: {said[-1] if said else 'no output'}")
    return out


def draft(document: dict, log: Path) -> dict:
    """Steps 2 and 3 for one run file's document: the fields the run file gains."""
    try:
        title, body = reply(_model(document, log))
    except (TimeoutError, RuntimeError, ValueError, OSError) as error:
        return {"state": "failed", "reason": f"{error}; nothing was drafted or filed"}
    clean = sanitize(title, body, document["context"])
    # The token goes in before the scan and the digest, so the preview, the scan and the filing read one text.
    token = secrets.token_hex(8)
    body = clean["body"] + "\n\n" + TOKEN_LINE[document["kind"]].format(token=token)
    blocked = clean["blocked"] or scan(clean["title"], body, document["guard"])
    if blocked:
        # A blocked text is neither kept nor shown: the reason names the category or the line, never the value.
        return {"removed": clean["removed"], "blocked": blocked, "state": "blocked", "reason": "; ".join(blocked)}
    return {"title": clean["title"], "body": body, "token": token, "removed": clean["removed"], "blocked": blocked,
            "state": "ready", "digest": digest(document["target"], clean["title"], body)}


def run(record: Path, lock: int | None = None) -> int:
    """The runner: the draft and the sanitizer; 0 for a ready or blocked run, 1 for a failed one."""
    document = json.loads(record.read_text(encoding="utf-8"))
    document.update(state="drafting", pid=os.getpid())
    _write(record, document)
    document.update(draft(document, record.with_suffix(".log")), finished=_iso(_now()))
    # The lock's last descriptor goes before the write a page reads as settled, so a Confirm at once finds it free.
    if lock is not None:
        os.close(lock)
    _write(record, document)
    return 1 if document["state"] == "failed" else 0


# -- step 4: the preview ---------------------------------------------------------------------------------------------


def public(document: dict) -> dict:
    """What a page reads: the run less the runner's own fields."""
    return {key: value for key, value in document.items()
            if key not in ("pid", "server", "context", "claude", "guard", "input")}


def _alive(pid) -> bool:
    if type(pid) is not int or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read(run_id: str):
    """`(record, document)`, or `(status, {"error"})` as a tuple of one int and a dict."""
    if not RUN_ID.fullmatch(run_id or ""):
        return None, (404, {"error": "No such issue run."})
    record = state_dir() / "runs" / f"{run_id}.json"
    try:
        return record, json.loads(record.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, (404, {"error": "No such issue run."})
    except (OSError, ValueError) as problem:
        return None, (503, {"error": f"The state of run {run_id} could not be read: {problem}"})


def _settled(document: dict, now: datetime.datetime) -> dict:
    """The run as a reader should take it: a gone runner is failed, a gone filing server is unknown."""
    document = dict(document)
    if document["state"] == "drafting" and not _alive(document.get("pid")):
        document.update(state="failed", reason=f"the runner (pid {document.get('pid')}) stopped without a draft; "
                                               f"nothing was filed")
    elif document["state"] == "queued":
        started = datetime.datetime.fromisoformat(document["started"].replace("Z", "+00:00"))
        if (now - started).total_seconds() > STARTUP:
            document.update(state="failed", reason=f"the runner never started within {STARTUP} s; nothing was filed")
    elif document["state"] == "filing":
        since = datetime.datetime.fromisoformat(document["filing_at"].replace("Z", "+00:00"))
        if not _alive(document.get("server")) or (now - since).total_seconds() > FILING_GRACE:
            document.update(state="unknown", reason=f"the filing did not record an answer, so whether "
                                                    f"{TRACKER[document['kind']]} holds the issue is not known; look "
                                                    f"for \"{document['title']}\" there before you draft it again")
    return document


def status(run_id: str, *, now: datetime.datetime | None = None):
    """`(status, document)` for one run. A read: it files nothing and writes nothing."""
    record, document = _read(run_id)
    if record is None:
        return document
    return 200, public(_settled(document, now or _now()))


# -- step 5 and 6: confirm, file, link back --------------------------------------------------------------------------

GH = ('f="$HOME/.config/shell/env.sh"; [ ! -f "$f" ] || . "$f" >/dev/null 2>&1; '
      'exec "${GH_BIN:-$(command -v gh || echo /opt/homebrew/bin/gh)}" "$@"')
#: What gh prints only when GitHub or gh itself refused the call, so no issue exists: a 4xx answer but 408, the
#: repository lookup gh makes before it creates, issues switched off, and no sign-in. Anything else may have filed.
GH_REFUSED = re.compile(r"\bHTTP 4(?!08)[0-9]{2}\b|GraphQL: Could not resolve to a |has disabled issues"
                        r"|gh auth login")


class NotFiled(Exception):
    """The tracker refused, so nothing was filed; Confirm may run again."""


class Unknown(Exception):
    """The call left and no clear answer came back, so whether an issue exists is not known."""


def file_github(target: dict, title: str, body: str) -> tuple[str, str]:
    """`(url, ref)` for one `gh issue create`; `NotFiled` or `Unknown` otherwise.

    gh, not the GitHub MCP tools: this runs in the server process, which has no MCP session. The URL must name the
    target repository, so an issue reported elsewhere is not linked as this one.
    """
    repo = target["repo"]
    try:
        done = subprocess.run(["sh", "-c", GH, "sh", "issue", "create", "--repo", repo, "--title", title,
                               "--body-file", "-"], input=body, capture_output=True, text=True,
                              timeout=FILE_TIMEOUT, start_new_session=True, check=False)
    except subprocess.TimeoutExpired:
        raise Unknown(f"gh gave no answer in {FILE_TIMEOUT} s") from None
    except OSError as problem:
        raise NotFiled(f"gh did not run: {type(problem).__name__}") from None
    if done.returncode != 0:
        said = [line for line in done.stderr.splitlines() if line.strip()]
        why = f"gh exited {done.returncode}: {said[-1] if said else 'no message'}"
        # A nonzero exit is no proof: GitHub can create the issue and the answer still fail on the way back.
        if GH_REFUSED.search(done.stderr):
            raise NotFiled(why)
        raise Unknown(why)
    found = re.search(r"https://github\.com/([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)/issues/([1-9][0-9]*)", done.stdout)
    if not found:
        raise Unknown("gh exited 0 but printed no issue URL")
    if f"{found[1]}/{found[2]}".lower() != repo.lower():
        raise Unknown(f"gh named an issue in another repository than {repo}")
    return found[0], f"github:{repo}#{found[3]}"


def _adf(body: str) -> dict:
    """The body as an Atlassian document: one paragraph per blank-line block, its lines as hard breaks."""
    content = []
    for block in re.split(r"\n\s*\n", body):
        lines = [line for line in block.splitlines()]
        parts = []
        for index, line in enumerate(lines):
            if index:
                parts.append({"type": "hardBreak"})
            if line:
                parts.append({"type": "text", "text": line})
        if any(part["type"] == "text" for part in parts):
            content.append({"type": "paragraph", "content": parts})
    return {"type": "doc", "version": 1, "content": content}


def file_jira(target: dict, title: str, body: str, transport=None, environ=None) -> tuple[str, str]:
    """`(url, ref)` for one `POST /rest/api/3/issue`, as a Task; `NotFiled` or `Unknown` otherwise."""
    from sd_db import shadow_jira

    config = shadow_jira.settings(environ)
    missing = shadow_jira.missing(config)
    if missing:
        raise NotFiled(f"Jira is not configured: {', '.join(missing)} not set"
                       f"{' (' + config['refused'] + ')' if config['refused'] else ''}; set them in "
                       f"<config>/project-dashboard/.env and restart the dashboard")
    fields = {"project": {"key": target["project"]}, "summary": title, "issuetype": {"name": "Task"},
              "description": _adf(body)}
    payload, error = shadow_jira._request(config["base"] + "/rest/api/3/issue", config, {"fields": fields}, transport)
    if error:
        # Only a refusal proves nothing was filed: the credentials, a redirect that was not followed, a 4xx but 408. A
        # 5xx, a 408 or a connection that broke after the request left may have filed.
        status = re.match(r"Jira returned ([0-9]{3})\b", error)
        if error.startswith(("Jira rejected", "Jira redirected")) or (
                status and 400 <= int(status[1]) < 500 and status[1] != "408"):
            raise NotFiled(error)
        raise Unknown(error)
    key = (payload or {}).get("key") or ""
    if not re.fullmatch(re.escape(target["project"]) + r"-[1-9][0-9]*", key):
        raise Unknown("Jira answered without the new issue's key")
    return f"{config['base']}/browse/{key}", f"jira:{key}"


FILERS = {"github": file_github, "jira": file_jira}


def find_github(target: dict, token: str, since: datetime.datetime) -> tuple[str, str] | None:
    """`(url, ref)` of the issue whose body carries this run's `token`, or None; `Unknown` when the check fails.

    The repository's newest issues, not search: GitHub's search index lags, and a check that missed a fresh issue would
    read as not filed. Only the token identifies the filing: a title, or a title and a time, can be someone else's.
    `since` is unused here; Jira's query needs it.
    """
    repo = target["repo"]
    try:
        done = subprocess.run(["sh", "-c", GH, "sh", "api",
                               f"repos/{repo}/issues?state=all&sort=created&direction=desc&per_page=50"],
                              capture_output=True, text=True, timeout=FILE_TIMEOUT, start_new_session=True,
                              check=False)
    except subprocess.TimeoutExpired:
        raise Unknown(f"the check gave no answer in {FILE_TIMEOUT} s") from None
    except OSError as problem:
        raise Unknown(f"the check did not run: {type(problem).__name__}") from None
    try:
        issues = json.loads(done.stdout) if done.returncode == 0 else None
    except ValueError:
        issues = None
    if not isinstance(issues, list):
        said = [line for line in done.stderr.splitlines() if line.strip()]
        raise Unknown(f"the check failed: gh exited {done.returncode}: {said[-1] if said else 'no list'}")
    line = TOKEN_LINE["github"].format(token=token)
    for issue in issues:
        if not isinstance(issue, dict) or "pull_request" in issue or line not in str(issue.get("body") or ""):
            continue
        found = re.fullmatch(r"https://github\.com/([A-Za-z0-9._-]+/[A-Za-z0-9._-]+)/issues/([1-9][0-9]*)",
                             str(issue.get("html_url", "")))
        if found and found[1].lower() == repo.lower():
            return found[0], f"github:{repo}#{found[2]}"
    return None


def find_jira(target: dict, token: str, since: datetime.datetime, transport=None,
              environ=None) -> tuple[str, str] | None:
    """`(url, ref)` of the issue in the project, created since `since`, whose description carries this run's `token`,
    or None; `Unknown` when the check fails. JQL selects by project and age; the token is compared here, since `text ~`
    is a word search, and only the token identifies the filing."""
    from sd_db import shadow_jira

    config = shadow_jira.settings(environ)
    if shadow_jira.missing(config):
        raise Unknown(f"the check could not run: {', '.join(shadow_jira.missing(config))} not set")
    minutes = max(1, int((_now() - since).total_seconds() // 60) + 1)
    # The project key passed the column's CHECK (an upper-case letter, then letters, digits or `_`), so it needs no
    # escaping inside the quotes.
    jql = f'project = "{target["project"]}" AND created >= -{minutes}m ORDER BY created DESC'
    payload, error = shadow_jira._request(config["base"] + "/rest/api/3/search/jql", config,
                                          {"jql": jql, "maxResults": 50, "fields": ["description"]}, transport)
    if error or not isinstance((payload or {}).get("issues"), list):
        raise Unknown(f"the check failed: {error or 'Jira answered without an issue list'}")
    line = TOKEN_LINE["jira"].format(token=token)
    for issue in payload["issues"]:
        key = str((issue or {}).get("key", ""))
        # The description is an Atlassian document; the token line is one text node in it.
        description = json.dumps(((issue or {}).get("fields") or {}).get("description"))
        if line in description and re.fullmatch(re.escape(target["project"]) + r"-[1-9][0-9]*", key):
            return f"{config['base']}/browse/{key}", f"jira:{key}"
    return None


FINDERS = {"github": find_github, "jira": find_jira}
#: How far before `filing_at` Jira's query reaches, for a clock that differs from the tracker's.
LOOK_SKEW = 120


def _look(record: Path, document: dict, why: str, finders=None):
    """After an outcome that is not known: link the run's issue if the tracker lists it, else record `unknown`."""
    finders = finders or FINDERS
    tracker = TRACKER[document["kind"]]
    since = datetime.datetime.fromisoformat(document["filing_at"].replace("Z", "+00:00")) - datetime.timedelta(
        seconds=LOOK_SKEW)
    try:
        if not document.get("token"):
            # A run file from before tokens: a title match alone could be another issue, so it stays not known.
            raise Unknown("this run carries no filing token, so no issue can be told apart as its own")
        found = finders[document["kind"]](document["target"], document["token"], since)
    except Unknown as failed:
        found, why = None, f"{why}; {failed}"
    if found is None:
        document.update(state="unknown", reason=f"{why}; whether {tracker} holds the issue is not known. Look for "
                                                f"\"{document['title']}\" there, or check again, before you draft it "
                                                f"again", checked=_iso(_now()))
        _write(record, document)
        return None
    url, ref = found
    document.update(state="filed", url=url, ref=ref, finished=_iso(_now()), reason=None, found=True)
    _write(record, document)
    return document


def _link(connection, document: dict) -> str:
    """Step 6: the ref, when the item has no source of its own, and the note; idempotent, so a retry adds neither twice."""
    from sd_db import workflow, writes

    note = NOTE.format(tracker=TRACKER[document["kind"]], ref=document["ref"], url=document["url"])
    with workflow.transaction(connection):
        state = workflow.item_state(connection, document["item"])
        row = state["item"]
        if row.get("source") is None and row.get("external_id") is None:
            taken = writes.item_by_external(connection, "task-ref", document["ref"])
            if taken is None:
                writes.set_item_fields(connection, document["item"], source="task-ref", external_id=document["ref"])
        if not any(existing["body"] == note for existing in state["notes"]):
            workflow.add_item_note(connection, document["item"], kind="comment", body=note, who=WHO)
    return note


def confirm(connection, run_id: str, posted: str, *, filers=None):
    """Steps 5 and 6: `(200, run)` once linked; a refusal or an outcome that is not linked otherwise."""
    filers = filers or FILERS
    record, document = _read(run_id)
    if record is None:
        return document
    document = _settled(document, _now())
    if document["state"] == "filed":
        return _linked(connection, record, document)
    if document["state"] != "ready":
        return 409, {"error": f"Run {run_id} is {document['state']}, so there is nothing to file"
                              f"{': ' + document['reason'] if document.get('reason') else ''}."}
    if posted != document["digest"]:
        return 409, {"error": "The text on the page is not the draft this run holds; read the preview again."}
    from sd_db import workflow

    found = target_of(connection, workflow.item_state(connection, document["item"])["item"].get("repo"),
                      document["kind"])
    if not found["ok"] or found["target"] != document["target"]:
        return 409, {"error": f"The item's repository no longer files this run's {TRACKER[document['kind']]} target"
                              f"{': ' + found['why'] if found['why'] else ''}; draft it again."}
    left = [f"a {category} is in the text" for category in findings(document["title"], document["body"],
                                                                      document["context"])]
    left = left or scan(document["title"], document["body"], document["guard"])
    if left:
        return 409, {"error": f"Nothing was filed: {'; '.join(left)}."}
    handle, holder = _lock()
    if handle is None:
        return 409, {"error": f"Run {holder} holds the issue lock; wait for it, then confirm again."}
    try:
        handle.seek(0)
        handle.truncate()
        handle.write(run_id)
        handle.flush()
        document.update(state="filing", server=os.getpid(), filing_at=_iso(_now()), reason=None)
        _write(record, document)
        try:
            url, ref = filers[document["kind"]](document["target"], document["title"], document["body"])
        except NotFiled as refused:
            document.update(state="ready", reason=f"not filed: {refused}")
            _write(record, document)
            return 502, {"error": f"{TRACKER[document['kind']]} filed nothing: {refused}. Confirm may run again."}
        except Unknown as unknown:
            # Not known is not refused: look for the issue before anything offers to file it again.
            if _look(record, document, str(unknown)) is None:
                return 504, {"error": document["reason"] + "."}
            return _linked(connection, record, document)
        document.update(state="filed", url=url, ref=ref, finished=_iso(_now()))
        _write(record, document)
    finally:
        handle.close()
    return _linked(connection, record, document)


def _linked(connection, record: Path, document: dict):
    from sd_db.errors import SdDbError

    try:
        note = _link(connection, document)
    except (SdDbError, sqlite3.Error) as problem:
        document.update(reason=f"filed at {document['url']}, but the item was not linked: {problem}")
        _write(record, document)
        return 503, {"error": f"{TRACKER[document['kind']]} issue {document['url']} was filed, but #{document['item']} "
                              f"was not linked: {problem}. Confirm again links it without filing again."}
    document.update(state="linked", linked=note, reason=None)
    _write(record, document)
    return 200, public(document)


def confirm_request(run_id: str, values: dict):
    if set(values) != {"digest"} or not isinstance(values["digest"], str) or not DIGEST.fullmatch(values["digest"]):
        raise ValueError("Confirm takes the digest of the previewed text.")
    if not RUN_ID.fullmatch(run_id):
        raise ValueError("No such issue run.")
    return lambda connection: confirm(connection, run_id, values["digest"])


def check(connection, run_id: str, *, finders=None):
    """Look again for an `unknown` run's issue: `(200, run)` once found and linked, 409 while it is still not known.
    It never files: a run whose outcome is not known can only be found, linked or discarded."""
    record, document = _read(run_id)
    if record is None:
        return document
    document = _settled(document, _now())
    if document["state"] != "unknown":
        return 409, {"error": f"Run {run_id} is {document['state']}; only a run whose outcome is not known is "
                              f"checked."}
    if _look(record, document, f"{TRACKER[document['kind']]} lists no issue with this title yet", finders) is None:
        return 409, {"error": document["reason"] + "."}
    return _linked(connection, record, document)


def check_request(run_id: str, values: dict):
    if values or not RUN_ID.fullmatch(run_id):
        raise ValueError("Check takes no values.")
    return lambda connection: check(connection, run_id)


def discard(run_id: str):
    """End a ready, blocked, failed or unknown run; an unknown one may have filed, as its reason says. Filing and filed
    runs stay."""
    record, document = _read(run_id)
    if record is None:
        return document
    document = _settled(document, _now())
    if document["state"] in ("queued", "drafting", "filing", "filed", "linked"):
        return 409, {"error": f"Run {run_id} is {document['state']}; it cannot be discarded now."}
    document.update(state="discarded")
    _write(record, document)
    return 200, public(document)


def discard_request(run_id: str, values: dict):
    if values or not RUN_ID.fullmatch(run_id):
        raise ValueError("Discard takes no values.")
    return lambda connection: discard(run_id)


def link_of(row) -> str | None:
    """The issue an item's ref names, as a URL, for the row's link icon; None for any other ref."""
    if row["source"] != "task-ref" or not row["external_id"]:
        return None
    found = re.fullmatch(r"github:([A-Za-z0-9._-]+/[A-Za-z0-9._-]+)#([1-9][0-9]*)", row["external_id"])
    if found:
        return f"https://github.com/{found[1]}/issues/{found[2]}"
    found = re.fullmatch(r"jira:([A-Z][A-Z0-9_]+-[1-9][0-9]*)", row["external_id"])
    if found:
        from sd_db import shadow_jira

        base = shadow_jira.settings()["base"]
        return f"{base}/browse/{found[1]}" if base else None
    return None


if __name__ == "__main__":
    # `run <state file> <lock descriptor>`: the descriptor stays open until this process exits.
    if len(sys.argv) != 4 or sys.argv[1] != "run":
        sys.exit("usage: issue_filing.py run <state file> <lock descriptor>")
    sys.exit(run(Path(sys.argv[2]), int(sys.argv[3])))

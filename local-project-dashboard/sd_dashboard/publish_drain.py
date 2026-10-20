"""Publish now (sd:5102): one headless drain of a document's mirror requests, by the pack's publication contract.

A Documents row posts `/api/documents/publish-now` with `{"root", "file"}`, a Briefs row `/api/briefs/publish-now` with
`{"item"}`. `start` runs in the POST, in the adversarial review's run shape (`adversarial.py`), and answers 202 at once:

1. The lock, `<state>/lock`, taken without waiting: a second drain while one holds it is a 409 naming the run.
2. The document's requests: `<key>.<out>.notion.json` and `<key>.<out>.drive.json` in the pack's `QUEUE` whose `repo`
   is this checkout. None is a 400: tick a destination first.
3. The run's state file, `<state>/runs/<id>.json`, as `queued`, with each request's name and fingerprint, and the
   prompt beside it as `<id>.prompt.md`.
4. This file as a script, in a session of its own, holding the lock's descriptor.

The runner (`run`) marks the run `running` and pipes the prompt to `claude -p`, bounded at `DRAIN_SECONDS` (its process
group killed past it). The prompt names the contract's six drain steps and nothing else to do. The tools are the
allowlist below: Notion and Drive may search, read, create in a container and update; nothing deletes, moves, shares or
changes a permission. Edit covers the queue and, for a document, its checkout's research.conf.py; Bash covers the two
commands the steps name. The allowlist cannot pin a container: the prompt and the contract do.

A trash request (an untick, sd:5489) adds two tools for that run only: Notion's `notion-delete-pages` and Drive's
`update_drive_file`, both of which move one copy to the trash, recoverably. The prompt then names the contract's trash
rule in place of "never trash". A drain with no trash request gets neither.

The outcome is read from the queue, not from the model: a request whose receipt says its fingerprint was delivered is
`delivered`, with the receipt's URL; any other is `left`, and the run is `failed`. A request left behind is the safe
state the contract's failure table recovers from at the next drain.

`status` answers `GET /api/documents/publish-now/<id>` from the state file, as `adversarial.status` does.
"""

from __future__ import annotations

import datetime
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import threading
from pathlib import Path

__all__ = ["DRAIN_SECONDS", "STARTUP", "allowed", "brief_request", "request", "run", "start", "status"]

#: The bound on one drain, in seconds.
DRAIN_SECONDS = 900
#: How long a queued run may wait for its runner to say it started before it reads as failed.
STARTUP = 60
RUN_ID = re.compile(r"drain-[A-Za-z0-9._-]{1,60}-[0-9]{8}T[0-9]{6}Z")
#: The MCP tools the drain may call: look, read, create in a container, update. Server names as Claude Code knows them.
NOTION = ("notion-search", "notion-fetch", "notion-create-pages", "notion-update-page")
DRIVE = ("search_drive_files", "list_drive_items", "get_doc_content", "create_drive_folder", "import_to_google_doc",
         "create_doc", "batch_update_doc", "modify_doc_text", "insert_doc_elements")
#: What a trash request adds, for that drain only (sd:5489): each moves one copy to the trash, where it can be restored.
TRASH_TOOLS = ("mcp__notion__notion-delete-pages", "mcp__workspace-mcp__update_drive_file")
CLAUDE = ('f="$HOME/.config/shell/env.sh"; [ ! -f "$f" ] || . "$f" >/dev/null 2>&1; '
          'ulimit -S -n 65536 2>/dev/null; '
          'exec "${CLAUDE_BIN:-$(command -v claude || echo "$HOME/.local/bin/claude")}" -p --model sonnet '
          '--restricted --no-session-persistence --tools "Read,Edit,Bash" --add-dir "$1" "$2" "$3" --allowedTools "$4"')
STEP5_DOC = "write `page=` or `file=` into {conf}, on the entry the request names; change nothing else in it."
#: A brief's designation is its item's `fields.publish`; step 6 records the URL on that item (`fields.published`), and the
#: delivered URL carries into the same container on the next publish; it no longer pins the copy, so the drain writes no file.
STEP5_BRIEF = ("skip it: this is a routine brief, and step 6 records the URL on its item; "
               "the delivered URL carries into the same container next time.")
PROMPT = """Drain these mirror requests by the publication contract, {contract},
section "How an outward mirror is updated", steps 1 to 6. Read that section first, then follow it exactly.

Requests, one JSON file each:
{requests}

- Step 2: read the channels setting with `{sd_db} repo channels {checkout}`.
- Step 4: write the id into the request file above.
- Step 5: {step5}
- Step 6: run `{kit} delivered <request file name> <fingerprint> --url <url>`.

Write only in the container the request resolves to. {never} A request you cannot finish stays in the queue as it is.
End with one line per request: `<request file name>: delivered <url>`, `<request file name>: trashed` or
`<request file name>: left: <reason>`.
"""
NEVER = "Never delete, move, trash or share a page or file, and change no permission."
#: For a drain that holds a trash request: the contract's section, and nothing beyond the one copy.
NEVER_BUT_TRASH = ("A request with `action: \"trash\"` follows the contract's section \"An unticked destination moves its copy "
                   "to the trash\" in place of steps 3 to 5, and step 6 without `--url`. Trash only the one copy each such "
                   "request names; never delete, move, trash or share anything else, and change no permission.")


def state_dir() -> Path:
    """`st_state_dir publish-drain`: what runs write and nobody edits."""
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "system" / "publish-drain"


def _iso(now: datetime.datetime) -> str:
    return now.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write(path: Path, document: dict) -> None:
    partial = path.with_name(path.name + ".partial")
    partial.write_text(json.dumps(document, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(partial, path)


def allowed(queue: Path, checkout: Path | None, sd_db: str, kit: str, trash: bool = False) -> str:
    """The `--allowedTools` value: the MCP allowlist (with `TRASH_TOOLS` when `trash`), Read, Edit on the queue and the conf
    (None for a brief), and the two commands."""
    tools = [f"mcp__notion__{name}" for name in NOTION] + [f"mcp__workspace-mcp__{name}" for name in DRIVE]
    tools += list(TRASH_TOOLS) if trash else []
    tools += ["Read", f"Edit(/{queue}/**)", *([f"Edit(/{checkout / 'research.conf.py'})"] if checkout else []),
              f"Bash({sd_db} repo channels *)", f"Bash({kit} delivered *)"]
    # Comma-separated: a Bash rule holds spaces, and claude reads the list by commas as well as spaces.
    return ",".join(tools)


def _requests(queue: Path, checkout: Path, out: str) -> list[dict]:
    """This document's pending Notion and Drive requests: name, path and fingerprint."""
    from . import publishing

    found = []
    for channel in publishing.OUTWARD:
        path = queue / f"{publishing.repo_key(checkout)}.{out}.{channel}.json"
        record = publishing._record(path)
        if record and publishing._same(record.get("repo"), checkout):
            found.append({"name": path.name, "path": str(path), "channel": channel,
                          "fingerprint": record.get("fingerprint"), "trash": record.get("action") == publishing.TRASH})
    return found


def start(connection, checkout: Path, out: str, *, brief: bool = False, now: datetime.datetime | None = None,
          spawn=subprocess.Popen):
    """Steps 1 to 4 above: `(202, run)`, or `(409, {"error"})` while a drain holds the lock. `brief` drains a routine
    brief's requests, whose designation is no file (`STEP5_BRIEF`)."""
    from . import pack_bin, publishing

    now = now or datetime.datetime.now(datetime.timezone.utc)
    kit = publishing.kit()
    queue = kit.QUEUE
    state = state_dir()
    runs = state / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    handle = open(state / "lock", "a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.seek(0)
            return 409, {"error": f"A drain is already running (run {handle.read().strip() or 'unknown'}); one runs "
                                  f"at a time. Wait for it to finish, then press Publish now again."}
        requests = _requests(queue, checkout, out)
        if not requests:
            raise ValueError(f"Nothing is queued for {out}: tick Drive or Notion under Publish to first.")
        label = re.sub(r"[^A-Za-z0-9._-]", "-", Path(out).name)[:60].strip("-.") or "x"
        run_id = f"drain-{label}-{now.astimezone(datetime.timezone.utc):%Y%m%dT%H%M%SZ}"
        record = runs / f"{run_id}.json"
        if record.exists():
            return 409, {"error": f"Run {run_id} started this second; press Publish now again in a moment."}
        bin_dir = pack_bin()
        sd_db, script = str(bin_dir / "sd-db"), f"{sys.executable} {bin_dir / 'sd-research-kit'}"
        contract = bin_dir.parent / "skills" / "_shared" / "references" / "publication-contract.md"
        prompt = runs / f"{run_id}.prompt.md"
        trash = any(r["trash"] for r in requests)
        prompt.write_text(PROMPT.format(
            contract=contract,
            requests="\n".join(f"- {r['path']}" for r in requests), sd_db=sd_db, checkout=checkout, kit=script,
            step5=STEP5_BRIEF if brief else STEP5_DOC.format(conf=checkout / "research.conf.py"),
            never=NEVER_BUT_TRASH if trash else NEVER), encoding="utf-8")
        document = {"id": run_id, "state": "queued", "reason": None, "checkout": str(checkout), "out": out,
                    "queue": str(queue), "receipts": str(kit.receipts()), "references": str(contract.parent),
                    "requests": requests, "results": None, "prompt": str(prompt),
                    "allowed": allowed(queue, None if brief else checkout, sd_db, script, trash), "timeout": DRAIN_SECONDS,
                    "started": _iso(now), "finished": None, "pid": None}
        _write(record, document)
        handle.seek(0)
        handle.truncate()
        handle.write(run_id)
        handle.flush()
        try:
            runner = spawn([sys.executable, str(Path(__file__).resolve()), "run", str(record), str(handle.fileno())],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           pass_fds=(handle.fileno(),), start_new_session=True, cwd=str(state))
        except OSError as problem:
            _write(record, {**document, "state": "failed", "reason": f"the runner did not start: {problem}",
                            "finished": _iso(datetime.datetime.now(datetime.timezone.utc))})
            raise
        # Reaped here when it ends, so a long-lived server keeps no zombie.
        threading.Thread(target=runner.wait, name=f"publish drain {run_id}", daemon=True).start()
        return 202, public(document)
    finally:
        handle.close()


def request(values: dict, *, config_path=None, repo_root=None):
    """The POST's checks: `{root, file}` naming a research document the page lists; the start as a write."""
    from . import documents, documents_screen, publishing

    if set(values) != {"root", "file"} or not all(isinstance(values[k], str) for k in values):
        raise ValueError("Send the document's root and file.")
    if documents.resolve(values["root"], values["file"], config_path, repo_root) is None:
        raise ValueError(f"No document {values['root']}/{values['file']} is served: read the documents again.")
    root = next((r for r in documents.roots(config_path, repo_root) if r.key == values["root"]), None)
    checkout = documents_screen._checkout(root) if root else None
    if checkout is None or not (checkout / "research.conf.py").is_file():
        raise ValueError("Only a research-kit document is mirrored: its checkout holds no research.conf.py.")
    outs = [out for out in publishing.choices(checkout)["docs"] if f"{Path(out).name}.html" == values["file"]]
    if len(outs) != 1:
        raise ValueError(f"research.conf.py has no single literal entry rendering {values['file']}.")
    return lambda connection: start(connection, checkout, outs[0])


def brief_request(values: dict):
    """POST /api/briefs/publish-now: `{item}` naming a routine brief; the start as a write."""
    from . import publishing

    if set(values) != {"item"} or type(values["item"]) is not int:
        raise ValueError("Send the brief's item.")

    def write(connection):
        try:
            row, checkout, _ = publishing._brief(connection, values["item"])
        except publishing.Refused as refused:
            raise ValueError(str(refused)) from None
        return start(connection, checkout, row["title"], brief=True)

    return write


def public(document: dict) -> dict:
    """What a page reads: the state file less the runner's own fields."""
    return {key: value for key, value in document.items()
            if key not in ("pid", "allowed", "timeout", "prompt", "receipts", "references")}


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


def status(run_id: str, *, now: datetime.datetime | None = None):
    """`(status, document)` for one run; a run whose runner is gone without an outcome reads failed."""
    if not RUN_ID.fullmatch(run_id):
        return 404, {"error": "No such drain run."}
    try:
        document = json.loads((state_dir() / "runs" / f"{run_id}.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return 404, {"error": "No such drain run."}
    except (OSError, ValueError) as problem:
        return 503, {"error": f"The state of run {run_id} could not be read: {problem}"}
    now = now or datetime.datetime.now(datetime.timezone.utc)
    if document["state"] == "running" and not _alive(document.get("pid")):
        document.update(state="failed", reason=f"the runner (pid {document.get('pid')}) stopped without recording an "
                                               f"outcome; the queue still holds what was not delivered")
    elif document["state"] == "queued":
        started = datetime.datetime.fromisoformat(document["started"].replace("Z", "+00:00"))
        if (now - started).total_seconds() > STARTUP:
            document.update(state="failed", reason=f"the runner never started within {STARTUP} s")
    return 200, public(document)


def _record(path: Path) -> dict:
    """`publishing._record`, here because the runner runs this file alone: the JSON object at `path`, or `{}`."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def outcome(document: dict) -> tuple[list[dict], str | None]:
    """Each request's result from the queue and its receipt, and the run's failure reason (None when all delivered)."""
    results, left = [], []
    for asked in document["requests"]:
        receipt = _record(Path(document["receipts"]) / asked["name"])
        pending = _record(Path(asked["path"]))
        if asked.get("trash") and asked["fingerprint"] and receipt.get("trashed") == asked["fingerprint"]:
            newer = bool(pending) and pending.get("fingerprint") != asked["fingerprint"]
            results.append({"name": asked["name"], "state": "trashed", "url": None,
                            "newer": "a tick queued the copy again; the next drain writes it" if newer else None})
        elif asked["fingerprint"] and receipt.get("delivered") == asked["fingerprint"]:
            newer = bool(pending) and pending.get("fingerprint") != asked["fingerprint"]
            results.append({"name": asked["name"], "state": "delivered", "url": receipt.get("url"),
                            "newer": "a render queued newer content; the next drain writes it" if newer else None})
        else:
            results.append({"name": asked["name"], "state": "left", "url": None, "newer": None})
            left.append(asked["name"])
    return results, (f"left in the queue: {', '.join(left)}; the next drain picks them up" if left else None)


def run(record: Path, lock: int | None = None) -> int:
    """The runner: claude on the prompt, then the outcome from the queue; 0 when every request was delivered."""
    document = json.loads(record.read_text(encoding="utf-8"))
    document.update(state="running", pid=os.getpid())
    _write(record, document)
    log = record.with_suffix(".log")
    argv = ["sh", "-c", CLAUDE, "sh", document["queue"], document["checkout"], document["references"], document["allowed"]]
    with open(log, "w", encoding="utf-8") as sink, open(document["prompt"], encoding="utf-8") as prompt:
        child = subprocess.Popen(argv, stdin=prompt, stdout=sink, stderr=subprocess.STDOUT, cwd=document["checkout"],
                                 start_new_session=True, pass_fds=(lock,) if lock is not None else ())
        try:
            code = child.wait(timeout=document["timeout"])
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait()
            code = None
    results, reason = outcome(document)
    if code is None:
        reason = f"the drain did not end within {document['timeout']} s; its process group was killed" + (
            f"; {reason}" if reason else "")
    elif code != 0 and reason:
        said = [line for line in log.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()]
        reason = f"claude exited {code}" + (f" ({said[-1][:300]})" if said else "") + f"; {reason}"
    document.update(state="failed" if reason else "done", reason=reason, results=results,
                    finished=_iso(datetime.datetime.now(datetime.timezone.utc)))
    # Claude has exited, so this is the lock's last descriptor: free it before the write a page reads as settled.
    if lock is not None:
        os.close(lock)
    _write(record, document)
    return 1 if reason else 0


if __name__ == "__main__":
    # `run <state file> <lock descriptor>`: the descriptor stays open, unread, until this process and claude exit.
    if len(sys.argv) != 4 or sys.argv[1] != "run":
        sys.exit("usage: publish_drain.py run <state file> <lock descriptor>")
    sys.exit(run(Path(sys.argv[2]), int(sys.argv[3])))

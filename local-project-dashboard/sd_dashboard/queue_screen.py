"""Queue (sd:2585): each repository's `sd-ship lane` queue, one row per item in one of five states, and its reorder.

Reads, per request: `sd-ship -C <repo> lane list` for each registered repository with a lane folder, the builder
gate logs (`gate-*.log`) in that folder, `sd gate status --json` for the gate slots, and `os.getloadavg()` (macOS
`vm.loadavg`). The pack owns the lane root; one `lane list` names it, as its `queue` path
`<root>/<repo>/lane/queue/queue.json`, so this file repeats no rule for where lanes live.

The five states, from each entry's `status`:

  merging   `running`: the runner holds it; phase is `merge` once its prepare log exists, else `prepare`.
  next      `pending`, in queue order; a `held` one keeps its place and the runner skips it.
  building  a builder gate log in the lane folder, changed in the last `BUILDING_HOURS`: empty while its gate runs.
  blocked   the newest entry of an item that `failed`, was `skipped` or stopped `prepared`, in the last `BLOCKED_DAYS`,
            with the first line of its reason.
  landed    `merged` today, in this machine's zone, with the merge commit and the pull request its subject names.

Writes: `move` runs `sd-ship -C <repo> lane move|hold|release|cancel`, the queue's only writers. The verbs take no
revision, so this compares the page's revision (a digest of the pending order and holds) with a fresh `lane list`
first and refuses a stale one. That check is best effort: each verb holds the queue lock only inside itself, so a
write that lands between the check and the verb is not caught. The answer names the order the verb left instead.
The runner reads the queue again at each item boundary, so an edit takes effect there.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

LIST_SECONDS = 20
WRITE_SECONDS = 30
GIT_SECONDS = 5
BUILDING_HOURS = 6
BLOCKED_DAYS = 3
#: What the page sends as `action`: `move`'s relative places, the two hold verbs, and cancel.
ACTIONS = ("up", "down", "top", "hold", "release", "cancel")
#: Who acts on a blocked entry, by its status and failed step.
WHO = {("prepared", None): "operator", ("skipped", None): "builder", ("failed", "prepare"): "builder",
       ("failed", "merge"): "lane", ("failed", None): "lane"}
EDITS = "Edits take effect at the next item boundary: the runner reads the queue again before each item, never mid-merge."


def command(name: str) -> str:
    """A pack command on PATH, else in the installed pack's checkout: the service's PATH has no pack directory."""
    found = shutil.which(name)
    if found:
        return found
    from sd_db import pack

    try:
        checkout = json.loads(pack.receipt_path(Path.home()).read_text(encoding="utf-8")).get("checkout")
    except (OSError, ValueError, AttributeError):
        checkout = None
    return str(Path(checkout) / "bin" / name) if checkout else name


def _run(argv: list[str], seconds: int) -> dict:
    """A pack command's JSON answer; a failure to run or parse is `{"ok": False, "error": ...}`."""
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=seconds, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        return {"ok": False, "error": f"{Path(argv[0]).name} did not run: {error}"}
    try:
        answer = json.loads(done.stdout)
    except ValueError:
        answer = None
    if not isinstance(answer, dict):
        return {"ok": False, "error": (done.stderr or done.stdout).strip()[-400:] or f"exit {done.returncode}"}
    return answer


def lane_list(path: str) -> dict:
    return _run([command("sd-ship"), "-C", path, "lane", "list"], LIST_SECONDS)


def revision(entries: list[dict]) -> str:
    """The pending order and holds, which is everything a reorder reads."""
    pending = [[row.get("item"), bool(row.get("held"))] for row in entries if row.get("status") == "pending"]
    return hashlib.sha256(json.dumps(pending).encode()).hexdigest()[:16]


def _when(stamp: str | None) -> float | None:
    try:
        return datetime.strptime(stamp or "", "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def _pr(path: str, commit: str) -> str | None:
    """The pull request a squash commit's subject names, `(#161)`, or None."""
    try:
        done = subprocess.run(["git", "-C", path, "log", "-1", "--format=%s", commit], capture_output=True, text=True,
                              timeout=GIT_SECONDS, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    found = re.search(r"\(#([0-9]+)\)\s*$", done.stdout.strip())
    return found[1] if found else None


def _phase(entry: dict, logs: Path) -> str:
    started = _when(entry.get("started_at")) or 0
    for log in logs.glob(f"prepare-{entry.get('item')}-*.log"):
        try:
            if log.stat().st_mtime >= started:
                return "merge"
        except OSError:
            continue
    return "prepare"


def _building(lane: Path, now: float) -> list[dict]:
    """Each builder gate log changed in the last `BUILDING_HOURS`, newest first."""
    rows = []
    for log in lane.glob("gate-*.log"):
        try:
            changed, text = log.stat().st_mtime, log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if now - changed > BUILDING_HOURS * 3600:
            continue
        try:
            result = json.loads(text) if text.strip() else None
        except ValueError:
            result = None
        row = {"state": "building", "builder": log.stem.removeprefix("gate-"), "log": str(log), "changed": changed}
        if result is None:
            row.update(gate="running" if not text.strip() else "unreadable", summary=None, head=None)
        else:
            row.update(gate="pass" if result.get("status") == "success" else "fail", summary=result.get("summary"),
                       head=str(result.get("head") or "")[:12] or None)
        rows.append(row)
    return sorted(rows, key=lambda row: -row["changed"])


def rows(entries: list[dict], *, path: str, lane: Path, now: float) -> list[dict]:
    """The lane's rows in page order: merging, next, building, blocked, landed."""
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    merging, following, blocked, landed = [], [], [], []
    newest = {row.get("item"): row for row in entries}  # later entries of an item replace earlier ones
    for entry in entries:
        status, item = entry.get("status"), entry.get("item")
        base = {"item": item, "title": entry.get("title") or ""}
        if status == "running":
            started = _when(entry.get("started_at"))
            merging.append({**base, "state": "merging", "phase": _phase(entry, lane / "logs"),
                            "elapsed": int(now - started) if started else None,
                            "head": str(entry.get("expected_head") or "")[:12]})
        elif status == "pending":
            gate = entry.get("speculation") or {}
            following.append({**base, "state": "next", "position": len(following) + 1, "held": bool(entry.get("held")),
                              "head": str(entry.get("expected_head") or "")[:12],
                              "gate": gate.get("status") or "not gated", "gate_summary": gate.get("summary") or gate.get("reason")})
        elif status in ("failed", "skipped", "prepared") and newest.get(item) is entry:
            finished = _when(entry.get("finished_at"))
            if finished and now - finished <= BLOCKED_DAYS * 86400:
                who = WHO.get((status, entry.get("step"))) or WHO.get((status, None), "lane")
                reason = (str(entry.get("reason") or "").strip().splitlines() or [status])[0]
                blocked.append({**base, "state": "blocked", "status": status, "step": entry.get("step"),
                                "reason": reason, "who": who, "finished": entry.get("finished_at")})
        elif status == "merged":
            finished = _when(entry.get("finished_at"))
            if finished and time.strftime("%Y-%m-%d", time.localtime(finished)) == today:
                commit = str(entry.get("merge_commit") or "")
                landed.append({**base, "state": "landed", "commit": commit[:12] or None,
                               "pr": _pr(path, commit) if commit else None, "finished": entry.get("finished_at")})
    return merging + following + _building(lane, now) + blocked + landed[::-1]


def _load() -> dict:
    one, five, fifteen = os.getloadavg()
    trend = "rising" if one > five * 1.1 else "falling" if one < five * 0.9 else "steady"
    return {"load1": round(one, 1), "load5": round(five, 1), "load15": round(fifteen, 1), "trend": trend}


def _gates() -> dict:
    status = _run([command("sd"), "gate", "status", "--json"], LIST_SECONDS)
    if "slots" not in status:
        return {"error": status.get("error") or "sd gate status gave no slot count"}
    return {"running": len(status.get("holders") or []), "cap": status["slots"], "waiting": len(status.get("waiters") or [])}


def document(connection, *, now: str = "") -> dict:
    """The page's document: the header readings and one section per repository lane."""
    from sd_db import repos

    clock = _when(now) or time.time()
    paths = [os.path.expanduser(row["path"]) for row in repos.registered(connection)]
    lanes, problems, root = [], [], None
    for path in paths:
        if not os.path.isdir(path):
            continue
        if root is not None and not (root / Path(path).resolve().name / "lane").is_dir():
            continue
        answer = lane_list(path)
        if not answer.get("ok") or not isinstance(answer.get("queue"), str):
            problems.append({"repo": path, "error": answer.get("error") or "lane list named no queue"})
            continue
        lane = Path(answer["queue"]).parent.parent
        root = root or lane.parent.parent
        if not lane.is_dir():
            continue
        entries = answer.get("entries") or []
        lanes.append({"repo": Path(path).name, "path": path, "lane": str(lane), "revision": revision(entries),
                      "rows": rows(entries, path=path, lane=lane, now=clock)})
    return {"read": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(clock)), "load": _load(), "gates": _gates(),
            "lanes": lanes, "problems": problems, "root": str(root) if root else None, "edits": EDITS,
            "registered": len(paths)}


def move(payload: dict):
    """Check the page's request, then return the write: a best-effort revision check, then one lane verb."""
    values = dict(payload)
    if (set(values) != {"repo", "item", "action", "revision"} or not isinstance(values["repo"], str)
            or type(values["item"]) is not int or not 1 <= values["item"] <= 9223372036854775807
            or values["action"] not in ACTIONS or not isinstance(values["revision"], str)):
        raise ValueError("Name the repository, the item, one of up, down, top, hold, release or cancel, and the lane's revision.")

    def write(connection):
        from sd_db import repos, workflow

        path = next((os.path.expanduser(row["path"]) for row in repos.registered(connection)
                     if os.path.expanduser(row["path"]) == values["repo"]), None)
        if path is None:
            raise ValueError("That repository is not registered.")
        listed = lane_list(path)
        if not listed.get("ok"):
            raise ValueError(f"The lane was not read: {listed.get('error')}")
        if revision(listed.get("entries") or []) != values["revision"]:
            raise workflow.StaleItem("The queue changed since the page read it. Read it again, then retry.")
        item, action = str(values["item"]), values["action"]
        verb = ["move", item, action] if action in ("up", "down", "top") else [action, item]
        answer = _run([command("sd-ship"), "-C", path, "lane", *verb], WRITE_SECONDS)
        if not answer.get("ok"):
            raise ValueError(answer.get("error") or "sd-ship lane refused the change.")
        return {"ok": True, "action": action, "item": values["item"], "pending": answer.get("pending"), "edits": EDITS}
    return write

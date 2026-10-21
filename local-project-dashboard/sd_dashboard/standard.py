"""Standard setup (sd:5610): Management previews and applies the managed-repo standard through `sd fleet standard`.

Management reads `GET /api/repos/standard/preview?path=<key>` (`preview`): the pack verb's plan, one step per setting,
with its `digest` and `used_before`. Nothing moves on a read. The operator confirms, and the page posts
`/api/repos/standard` with that digest (`request`); the verb rebuilds the plan, refuses a changed one, and runs only
the steps the digest covers, the sd-db `managed` and `runner_merge` writes last. The verb holds the rules and the
GitHub writes; this module only runs it in the registered checkout, against the database the dashboard serves.

Answers: a plan is 200, also when a step failed (its `result` says so and the verb held the lane steps). Exit 3, or
argparse's 2, is a refusal that wrote nothing: 409. No answer, or one past the bound, may follow a write, so it is 502
with "Check again".
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

__all__ = ["preview", "request"]

#: The plan reads about twenty GitHub settings, each bounded by the pack's 20 s `gh` limit.
PREVIEW_SECONDS = 180
APPLY_SECONDS = 300
#: 3 is the verb's refusal; 2 is argparse's, as from a pack that has no `standard` verb yet. Neither wrote.
REFUSED = (2, 3)


def _checkout(connection, key: str) -> Path:
    from sd_db import paths, repos

    row = next((row for row in repos.registered(connection) if row["path"] == key), None)
    if row is None:
        raise ValueError(f"{key} is not a registered repository. Nothing ran.")
    path = paths.disk(row["path"])
    if not path.is_dir():
        raise ValueError(f"{path} is not a directory on this machine. Nothing ran.")
    return path


def _database(connection) -> str:
    return str(connection.execute("PRAGMA database_list").fetchone()[2])


def _run(connection, path: Path, extra: list[str], seconds: int) -> tuple[int, dict]:
    from .queue_screen import command

    argv = [command("sd"), "fleet", "standard", "--json", "--database", _database(connection), *extra]
    try:
        done = subprocess.run(argv, cwd=str(path), capture_output=True, text=True, timeout=seconds, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        return 502, {"error": f"sd fleet standard gave no answer: {error}. It may have written: Check again.",
                     "outcome": "unknown"}
    said = done.stderr.strip()[-400:]
    try:
        plan = json.loads(done.stdout)
    except ValueError:
        plan = None
    if done.returncode in REFUSED:
        return 409, {"error": said or "sd fleet standard refused", **({"plan": plan} if isinstance(plan, dict) else {})}
    if done.returncode in (0, 1) and isinstance(plan, dict):
        return 200, plan
    return 502, {"error": f"sd fleet standard exited {done.returncode}: {said or 'no plan'}. It may have written: "
                          "Check again.", "outcome": "unknown"}


def preview(connection, parameters: dict) -> tuple[int, dict]:
    """`GET /api/repos/standard/preview?path=<key>`: the plan, and the digest its apply must match."""
    key = parameters.get("path")
    key = key[0] if isinstance(key, list) else key
    if not key:
        return 400, {"error": "Name the checkout: path=<key>."}
    try:
        path = _checkout(connection, key)
    except ValueError as error:
        return 409, {"error": str(error)}
    return _run(connection, path, [], PREVIEW_SECONDS)


def request(payload: dict):
    """`POST /api/repos/standard`: the page's checks before the server opens a writer; the write as `action_route`
    takes it."""
    values = dict(payload)
    if (set(values) != {"path", "digest"} or not isinstance(values["path"], str) or not values["path"]
            or not isinstance(values["digest"], str) or not re.fullmatch(r"[0-9a-f]{64}", values["digest"])):
        raise ValueError("Name the checkout and the digest of the plan the preview showed.")

    def apply(connection):
        return _run(connection, _checkout(connection, values["path"]), ["--apply", "--digest", values["digest"]],
                    APPLY_SECONDS)

    return apply

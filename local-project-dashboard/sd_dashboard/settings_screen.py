"""Settings' Fleet section (sd:3989): the `sd.*` keys whose one value serves every machine.

The pack keeps them as setting rows in the workflow database (`sd_lib.FLEET_KEYS`). This page reads them with
`sd config list sd --json` and writes one with `sd config set sd.<key> <value>`: the pack holds each key's pattern,
description and the row-wins rule, so none is copied here. Each row shows its value, its source (`database`, `file`
or `default`) and its description; a key whose pattern is a list of words gets a select.

A failed list is named and lists nothing. A refused set answers sd's reason (400). A set that runs past `SD_SECONDS`
may have committed before it was stopped, so its outcome is unknown: it answers 504 with `unknown`, and the page reads
the value again. There is no stale-write check: two concurrent manual writes are outside the threat model, and the
answer carries the value the set replaced, so the page can say when it differs from the one it showed.
"""

from __future__ import annotations

import json
import re
import subprocess

__all__ = ["FLEET", "document", "request"]

#: An `sd config` run's ceiling: one pack start and one small read or write.
SD_SECONDS = 5.0

#: The pack's `sd_lib.FLEET_KEYS`, in the order the page lists them; `test_v2_settings` pins the two equal.
FLEET = ("fleet_owners", "copilot_review", "hub_name")

#: A pattern that is a list of words, which the page offers as a select.
CHOICES = re.compile(r"[a-z]+(\|[a-z]+)+")

#: What `sd config set` prints for a set: `sd.<key>: <was> -> <value>`, and ` (workflow database)` when it wrote the row.
SET_LINE = re.compile(r"sd\.(\w+): (.*) -> (.*?)( \(workflow database\))?")


def _sd(*args: str) -> subprocess.CompletedProcess:
    """One `sd` run within `SD_SECONDS`; `ValueError` when sd is not on PATH, `TimeoutExpired` past the bound."""
    try:
        return subprocess.run(["sd", *args], capture_output=True, text=True, timeout=SD_SECONDS, check=False)
    except FileNotFoundError:
        raise ValueError("sd is not on the dashboard's PATH") from None


def _said(done: subprocess.CompletedProcess) -> str:
    return (done.stderr.strip().splitlines() or [""])[0]


def _fleet() -> list[dict]:
    try:
        done = _sd("config", "list", "sd", "--json")
    except subprocess.TimeoutExpired:
        raise ValueError(f"sd config list sd ran past its {SD_SECONDS:g} seconds") from None
    if done.returncode:
        raise ValueError(f"sd config list sd exited {done.returncode}" + (f": {_said(done)}" if _said(done) else ""))
    try:
        listed = json.loads(done.stdout)
    except ValueError:
        listed = None
    if not isinstance(listed, dict):
        raise ValueError("sd config list sd --json printed no JSON object")
    rows = []
    for key in FLEET:
        spec = listed.get(key)
        if not isinstance(spec, dict) or spec.get("source") not in ("database", "file", "default"):
            raise ValueError(f"sd config list sd --json gives no sd.{key} with its source; the pack predates fleet settings")
        pattern = spec.get("pattern") if isinstance(spec.get("pattern"), str) else ""
        rows.append({"key": key, "value": spec.get("value") if isinstance(spec.get("value"), str) else None,
                     "source": spec["source"], "description": str(spec.get("description") or ""),
                     "choices": pattern.split("|") if CHOICES.fullmatch(pattern) else None})
    return rows


def document(*, now: str) -> dict:
    """The Fleet section: each fleet key's row, or None with the reason the list was not read."""
    try:
        return {"read": now, "fleet": _fleet(), "reason": ""}
    except ValueError as failed:
        return {"read": now, "fleet": None, "reason": str(failed)}


def request(payload) -> object:
    """`POST /api/settings/fleet {key, value}`: the write that runs `sd config set`, or `ValueError` (400) before sd runs."""
    key, value = payload.get("key"), payload.get("value")
    if set(payload) != {"key", "value"} or key not in FLEET or not isinstance(value, str):
        raise ValueError(f"Expected {{key, value}}, the key one of {', '.join(FLEET)}.")
    value = value.strip()
    if not value or value.startswith("-"):
        raise ValueError(f"sd.{key} takes a value; unset it with sd config unset sd.{key}.")
    return lambda connection: _set(key, value)


def _set(key: str, value: str):
    try:
        done = _sd("config", "set", f"sd.{key}", value)
    except subprocess.TimeoutExpired:
        return 504, {"error": f"sd config set ran past its {SD_SECONDS:g} seconds, so whether sd.{key} changed is unknown; "
                              "read it again", "unknown": True}
    if done.returncode:
        raise ValueError(f"sd config set sd.{key} refused" + (f": {_said(done)}" if _said(done) else f" (exit {done.returncode})"))
    match = SET_LINE.fullmatch(done.stdout.strip())
    if not match or match[1] != key:
        return 502, {"error": f"sd config set printed {done.stdout.strip()[:200]!r}, so what it set is unknown; read it again",
                     "unknown": True}
    return {"key": key, "value": match[3], "was": None if match[2] == "(unset)" else match[2], "database": bool(match[4])}

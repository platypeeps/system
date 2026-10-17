"""HOA: the water system behind the default UI's HOA page (sd:2116).

The page is `v2/hoa.html`; it holds no data. It reads one JSON document,
`/api/hoa`, built here by `document`.

**Where the data is.** The design reads one HOA checkout: the asset map
(`water/map/assets.geojson`, `water/map/base.geojson`), the Mission telemetry
export (`water/metrics/mission/*.csv`) and the checkout's open `followup`
items. The checkout is the one repo in the workflow database's repo table
whose folder is named `hoa` (sd:3985), so no config file holds its path. A
second repo with that folder name is an error that names both, never a guess.

**Each source is guarded on its own.** A file that is missing or does not
parse is that part's `error`; the other parts still answer, and the page shows
the failed part as unknown with the reason, never as an empty list. Nothing
here writes, runs a command or reaches the network: the map draws the vectors
in the files, and the page loads no tiles.
"""

from __future__ import annotations

import csv
import json
import math
import os
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from sd_db import reads, repos, workflow, writes

from .documents import _config_dir, pipe_lines

__all__ = ["ALARM_ROOT", "SOURCE_AGES", "SOURCE_SECONDS", "SOON_DAYS", "STALE_DAYS", "THRESHOLDS", "TREND_DAYS", "acknowledge", "alarm_key", "document",
           "thresholds"]

#: The folder name that picks the checkout out of the repo table.
FOLDER = "hoa"

#: Rows the trends carry: the page's widest range (30d).
TREND_DAYS = 30

#: A followup due within this many days is caution; one past its due date is a warning.
SOON_DAYS = 7

#: The export is daily and the pump file lags the analog file by one day, so a newest row two days old is on time.
STALE_DAYS = 2

DEFAULTS = {"mission": "water/metrics/mission", "assets": "water/map/assets.geojson", "base": "water/map/base.geojson"}

#: Each Mission file, the column that dates a row, and the columns the page reads.
FILES = {
    "analog": ("analog-daily.csv", "date", ("samples", "aquifer_ft_min", "aquifer_ft_mean", "aquifer_ft_max", "tank_ft_min",
                                            "tank_ft_mean", "tank_ft_max", "a2_flow_gpm_mean", "booster_psi_mean")),
    "pump": ("pump-daily.csv", "date", ("a2_starts", "a2_runtime_hours", "a2_gallons", "a2_flow_gpm_running", "a2_gallons_metered")),
    "alarms": ("alarm-events.csv", "datetime", ("kind", "event", "notified", "minutes")),
}

#: The trend series' problem levels (sd:4969), in this machine's config directory: the hoa checkout states its levels in
#: prose only (the tank's fire floor), so no file there names one. The checkout ships `hoa-thresholds.conf.example`.
THRESHOLDS = _config_dir() / "hoa-thresholds.conf"

#: The trend series a level may name: `hoa.js` SERIES keys.
SERIES = ("tank", "runtime", "aquifer")

#: The hoa checkout's one read of each source's age (hoa sd:4965), run from the checkout; `--json` is what the lamps read.
SOURCE_AGES = "tools/source-ages.py"

#: Its ceiling: one Python start and a few small CSV reads.
SOURCE_SECONDS = 10.0

#: The sources the Overview lamps show, and the fields of each that the page reads.
LAMPS = ("mission", "vantaca")
SOURCE_KEYS = ("source", "label", "latest", "age_days", "allowed_days", "state", "ends", "refresh", "todo", "error")

ASSET_KEYS = ("id", "type", "name", "status", "accuracy", "source", "notes", "updated")
BASE_KEYS = ("kind", "ain", "label", "luc", "acres")


def checkout(connection) -> tuple[str | None, Path | None, list[str]]:
    """The registered repo whose folder is named `hoa`: its recorded path, its place on disk, and what stops the read."""
    found = [row["path"] for row in repos.registered(connection) if Path(row["path"]).name == FOLDER]
    if len(found) > 1:
        return None, None, [f"two repos are named {FOLDER}: {', '.join(found)}"]
    if not found:
        return None, None, []
    return found[0], Path(os.path.expanduser(found[0])), []


def _paths(repo: Path | None) -> dict:
    return {key: repo / place if repo else None for key, place in DEFAULTS.items()}


def _day(text: str) -> date:
    return date.fromisoformat(text[:10])


def _csv(path: Path, stamp: str, columns: tuple[str, ...]) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = [name for name in (stamp, *columns) if name not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"{path.name} has no {', '.join(missing)} column")
        rows = [{name: (row.get(name) or "").strip() for name in (stamp, *columns)} for row in reader]
    for number, row in enumerate(rows, 2):
        try:
            _day(row[stamp])
        except ValueError:
            raise ValueError(f"{path.name} line {number}: {row[stamp]!r} is not a date") from None
    return sorted(rows, key=lambda row: row[stamp])


def _mission(folder: Path | None, today: date) -> dict:
    out = {"dir": str(folder or ""), "error": "", "files": [], "latest": "", "age_days": None, "stale": False,
           "analog": [], "pump": [], "alarms": [], "alarms_today": {"count": None, "reason": "", "acked": 0}}
    if folder is None:
        out["error"] = "no export folder: no repo named hoa is registered"
        return out
    for key, (name, stamp, columns) in FILES.items():
        try:
            rows = _csv(folder / name, stamp, columns)
        except FileNotFoundError:
            out["files"].append({"name": name, "rows": 0, "latest": "", "error": f"{name} is not in the export folder"})
            continue
        except (OSError, UnicodeDecodeError, ValueError, csv.Error) as error:
            out["files"].append({"name": name, "rows": 0, "latest": "", "error": str(error)})
            continue
        latest = rows[-1][stamp] if rows else ""
        out["files"].append({"name": name, "rows": len(rows), "latest": latest, "error": ""})
        if key == "alarms":
            since = (_day(latest) - timedelta(days=TREND_DAYS)).isoformat() if rows else ""
            out[key] = [row for row in rows if row[stamp] >= since]
        else:
            out[key] = rows[-TREND_DAYS:]
    broken = [f["error"] for f in out["files"] if f["error"]]
    if broken:
        out["error"] = "; ".join(broken)
    days = [f["latest"][:10] for f in out["files"] if f["latest"]]
    if days:
        out["latest"] = max(days)
        out["age_days"] = (today - _day(out["latest"])).days
        out["stale"] = out["age_days"] > STALE_DAYS
    alarms = next((f for f in out["files"] if f["name"] == FILES["alarms"][0]), None)
    if alarms and not alarms["error"]:
        if alarms["latest"][:10] == today.isoformat():
            real = [row for row in out["alarms"] if row["datetime"][:10] == alarms["latest"][:10] and _real(row)]
            out["alarms_today"] = {"count": len(real), "reason": ""}
        else:
            out["alarms_today"]["reason"] = f"the export ends {alarms['latest'][:10] or 'with no rows'}"
    elif alarms:
        out["alarms_today"]["reason"] = alarms["error"]
    return out


def thresholds(path: Path | None = None) -> dict:
    """Each series' problem level, `{"dir": "below"|"above", "value"}`, from `<series>|below|<number>` lines; a line not
    read is named in `problems`. No file is no level, which the page says; a file that does not read is `error`."""
    path = THRESHOLDS if path is None else path
    out = {"source": str(path), "levels": {}, "problems": [], "error": ""}
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return out
    except (OSError, UnicodeDecodeError) as error:
        out["error"] = f"{path.name} did not read: {error}"
        return out
    for number, _, fields in pipe_lines(text):
        if len(fields) != 3 or fields[0] not in SERIES or fields[1] not in ("below", "above"):
            out["problems"].append(f"line {number}: expected <series>|below|<number> or <series>|above|<number>, "
                                   f"series one of {', '.join(SERIES)}")
            continue
        try:
            value = float(fields[2])
        except ValueError:
            value = math.nan
        if not math.isfinite(value):
            out["problems"].append(f"line {number}: {fields[2]!r} is not a number")
        elif fields[0] in out["levels"]:
            out["problems"].append(f"line {number}: {fields[0]} has a level already")
        else:
            out["levels"][fields[0]] = {"dir": fields[1], "value": value}
    return out


def sources(place: Path | None) -> dict:
    """The Mission export's and Vantaca's age, from `SOURCE_AGES --json` (sd:4965), within `SOURCE_SECONDS`.

    The script exits 1 when a source did not read and still prints every row, so its rows are kept and the failed one is
    `unknown` with its `error`; any other failure, a timeout or a script the checkout lacks, is this part's `error`.
    """
    out = {"error": "", "rows": [], "command": f"python3 {SOURCE_AGES} --json", "repo": str(place or "")}
    if place is None:
        out["error"] = "no checkout: no repo named hoa is registered"
        return out
    script = place / SOURCE_AGES
    if not script.is_file():
        out["error"] = f"{SOURCE_AGES} is not in the checkout"
        return out
    try:
        done = subprocess.run([sys.executable, str(script), "--json"], cwd=place, capture_output=True, text=True,
                              timeout=SOURCE_SECONDS, check=False)
    except subprocess.TimeoutExpired:
        out["error"] = f"{SOURCE_AGES} ran past its {SOURCE_SECONDS:g} seconds"
        return out
    except OSError as error:
        out["error"] = f"{SOURCE_AGES} did not start: {error}"
        return out
    said = (done.stderr.strip().splitlines() or [""])[0]
    try:
        rows = json.loads(done.stdout)["sources"] if done.returncode in (0, 1) else None
        if rows is not None and not all(isinstance(row, dict) for row in rows):
            raise TypeError("a source is not an object")
    except (ValueError, KeyError, TypeError) as error:
        out["error"] = f"{SOURCE_AGES} printed no source list: {error}"
        return out
    if rows is None:
        out["error"] = f"{SOURCE_AGES} exited {done.returncode}" + (f": {said}" if said else "")
        return out
    out["rows"] = [{key: row.get(key) for key in SOURCE_KEYS} for row in rows if row.get("source") in LAMPS]
    return out


# ---------------------------------------------------------------------------
# Acknowledge (sd:4967): the workflow database's `view` state, the store Designs' Hide and Documents' Pin write
# (sd:3428, sd:3447). An acknowledged alarm event is a hidden view keyed `hoa-alarm/<datetime> <event>`: the row's own
# fields, so the key holds across reads and across a newer export. Nothing reaches the Mission portal.

#: The key prefix of an alarm event's view state.
ALARM_ROOT = "hoa-alarm"


def alarm_key(row: dict) -> str:
    return f"{ALARM_ROOT}/{row['datetime']} {row['event']}"


def _acks(connection, mission: dict) -> None:
    """Mark each alarm row `key` and `acked`, count today's acknowledged alarms apart, and say whether the store was read.
    A store that does not read acknowledges nothing: every row shows, and `acks.reason` says why Acknowledge is off."""
    from .documents_screen import views

    kept, unread = views(connection)
    for row in mission["alarms"]:
        row["key"] = alarm_key(row)
        row["acked"] = kept.get(row["key"], {}).get("hidden", False)
    mission["acks"] = {"available": not unread, "reason": unread}
    today = mission["alarms_today"]
    if today["count"] is not None:
        day = max(row["datetime"][:10] for row in mission["alarms"])
        real = [row for row in mission["alarms"] if row["datetime"][:10] == day and _real(row)]
        today["acked"] = sum(row["acked"] for row in real)
        today["count"] = len(real) - today["acked"]


def _real(row: dict) -> bool:
    """An alarm, not a generator test or a return to normal."""
    return row["kind"] != "generator" and "Normal" not in row["event"]


def acknowledge(payload: dict):
    """`POST /api/hoa/alarm`: `{"key", "acked"}`; the write as `action_route` returns one. The write reads the export
    again, so a key the export no longer holds is refused as stale and nothing is written."""
    key, acked = payload.get("key"), payload.get("acked")
    if set(payload) != {"key", "acked"} or not isinstance(key, str) or not isinstance(acked, bool) \
            or not key.startswith(f"{ALARM_ROOT}/"):
        raise ValueError("Send the alarm event's key and acked, true or false.")
    if not hasattr(writes, "set_view"):
        raise ValueError("The command pack has no view store yet (sd:3430): pull it and restart the dashboard.")

    def write(connection):
        _, place, problems = checkout(connection)
        if problems or place is None:
            raise ValueError(f"Nothing was acknowledged: {problems[0] if problems else 'no repo named hoa is registered'}.")
        name, stamp, columns = FILES["alarms"]
        try:
            rows = _csv(_paths(place)["mission"] / name, stamp, columns)
        except (OSError, UnicodeDecodeError, ValueError, csv.Error) as error:
            raise ValueError(f"Nothing was acknowledged: {name} did not read: {error}") from None
        if key not in {alarm_key(row) for row in rows}:
            raise workflow.StaleItem(f"{name} holds no alarm event {key[len(ALARM_ROOT) + 1:]}. Nothing was acknowledged. "
                                     "Read HOA again.")
        return {"key": key, **writes.set_view(connection, key, hidden=acked)}

    return write


def _geojson(path: Path | None, keys: tuple[str, ...], kind: str) -> dict:
    if path is None:
        return {"error": f"no {kind} file: no repo named hoa is registered", "features": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        features = []
        for feature in data["features"]:
            props, geometry = feature["properties"], feature["geometry"]
            row = {key: props.get(key, "") for key in keys}
            if geometry["type"] == "Point":
                row["lng"], row["lat"] = (round(float(v), 7) for v in geometry["coordinates"][:2])
            elif geometry["type"] == "Polygon":
                row["rings"] = [[[round(float(x), 7), round(float(y), 7)] for x, y, *_ in ring] for ring in geometry["coordinates"]]
            else:
                continue
            features.append(row)
    except FileNotFoundError:
        return {"error": f"{path.name} is not there", "features": []}
    except (OSError, UnicodeDecodeError, ValueError, KeyError, TypeError) as error:
        return {"error": f"{path.name} did not parse: {error.__class__.__name__}: {error}", "features": []}
    return {"error": "", "features": features}


def _followups(connection, repo: str | None, now: str, today: date) -> dict:
    """The checkout's open `followup` items, ranked: overdue, then due within a week, then the rest; priority breaks ties."""
    if repo is None:
        return {"error": "no checkout: no repo named hoa is registered", "rows": []}
    rows = []
    for item in reads.backlog_items(connection, kind="followup", repo=repo, now=now):
        if item["status"] == "done":
            continue
        try:
            due = (_day(item["due"]) - today).days if item["due"] else None
        except ValueError:
            due = None
        state = "queued" if due is None or due > SOON_DAYS else "caution" if due >= 0 else "warning"
        rows.append({"id": f"followup:{item['id']}", "item": item["id"], "title": item["title"], "status": item["status"],
                     "priority": item["priority"], "due": item["due"] or "", "due_days": due, "state": state,
                     "since": item["status_since"] or ""})
    order = {"warning": 0, "caution": 1, "queued": 2}
    rows.sort(key=lambda row: (order[row["state"]], row["priority"] is None, row["priority"] or 0,
                               row["due_days"] is None, row["due_days"] or 0, row["item"]))
    return {"error": "", "rows": rows}


def document(connection, *, now: str) -> dict:
    """The HOA page's document: the map, the Mission export and the checkout's open followups."""
    recorded, place, problems = checkout(connection)
    state = "error" if problems else "read" if recorded else "missing"
    today = datetime.fromisoformat(now.replace("Z", "+00:00")).date()
    paths = _paths(place)
    mission, followups = _mission(paths["mission"], today), _followups(connection, recorded, now, today)
    if problems:
        mission["error"], followups["error"] = problems[0], problems[0]
    _acks(connection, mission)
    return {
        "read": now,
        "config": {"state": state, "source": "the repo table", "problems": problems},
        "mission": mission,
        "assets": _geojson(paths["assets"], ASSET_KEYS, "assets") if not problems else {"error": problems[0], "features": []},
        "base": _geojson(paths["base"], BASE_KEYS, "base") if not problems else {"error": problems[0], "features": []},
        "followups": followups,
        "thresholds": thresholds(),
        "sources": sources(place) if not problems else {"error": problems[0], "rows": [], "command": "", "repo": ""},
        "attention": _attention(mission, followups),
    }


def _attention(mission: dict, followups: dict) -> dict:
    """The rail badge (sd:3446): the annunciator cells that can light, as hoa.js draws them: alarms today, overdue or
    soon followups, and a stale export. Water, tank, pumps and aquifer are never lit."""
    from .v2.pages import attention

    states = [r["state"] for r in followups["rows"]] if not followups["error"] else []
    cells = ["warning" if mission["alarms_today"]["count"] else "",
             "warning" if "warning" in states else "caution" if "caution" in states else "",
             "caution" if mission["latest"] and mission["stale"] else ""]
    return attention(cells, {"caution": "HOA annunciator cells", "warning": "HOA annunciator cells", "ok": "HOA water system"})

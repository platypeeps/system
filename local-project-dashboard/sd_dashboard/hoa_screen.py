"""HOA: the water system behind the default UI's HOA page (sd:2116).

The page is `v2/hoa.html`; it holds no data. It reads one JSON document,
`/api/hoa`, built here by `document`.

**Where the data is.** The design reads one HOA checkout: the asset map
(`water/map/assets.geojson`, `water/map/base.geojson`), the Mission telemetry
export (`water/metrics/mission/*.csv`) and the checkout's open items of every
kind but report. The checkout is the one repo in the workflow database's repo table
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
import os
from datetime import date, datetime, timedelta
from pathlib import Path

from sd_db import reads, repos

__all__ = ["SOON_DAYS", "STALE_DAYS", "TREND_DAYS", "document"]

#: The folder name that picks the checkout out of the repo table.
FOLDER = "hoa"

#: Rows the trends carry: the page's widest range (30d).
TREND_DAYS = 30

#: An item due within this many days is caution; one past its due date is a warning.
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
           "analog": [], "pump": [], "alarms": [], "alarms_today": {"count": None, "reason": ""}}
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
            real = [row for row in out["alarms"] if row["datetime"][:10] == alarms["latest"][:10]
                    and row["kind"] != "generator" and "Normal" not in row["event"]]
            out["alarms_today"] = {"count": len(real), "reason": ""}
        else:
            out["alarms_today"]["reason"] = f"the export ends {alarms['latest'][:10] or 'with no rows'}"
    elif alarms:
        out["alarms_today"]["reason"] = alarms["error"]
    return out


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


def _items(connection, repo: str | None, now: str, today: date) -> dict:
    """The checkout's open items of every kind, ranked: overdue, then due within a week, then the rest; priority breaks
    ties. A report is its family's (`places.area_of`), so it is not here (sd:4959)."""
    from .places import area_of

    if repo is None:
        return {"error": "no checkout: no repo named hoa is registered", "rows": []}
    rows = []
    for item in reads.backlog_items(connection, repo=repo, now=now):
        if item["status"] == "done" or area_of(item["repo"], item["kind"], repo) != "hoa":
            continue
        try:
            due = (_day(item["due"]) - today).days if item["due"] else None
        except ValueError:
            due = None
        state = "queued" if due is None or due > SOON_DAYS else "caution" if due >= 0 else "warning"
        rows.append({"id": f"item:{item['id']}", "item": item["id"], "title": item["title"], "kind": item["kind"],
                     "status": item["status"], "priority": item["priority"], "due": item["due"] or "", "due_days": due,
                     "state": state, "since": item["status_since"] or ""})
    order = {"warning": 0, "caution": 1, "queued": 2}
    rows.sort(key=lambda row: (order[row["state"]], row["priority"] is None, row["priority"] or 0,
                               row["due_days"] is None, row["due_days"] or 0, row["item"]))
    return {"error": "", "rows": rows}


def document(connection, *, now: str) -> dict:
    """The HOA page's document: the map, the Mission export and the checkout's open items."""
    recorded, place, problems = checkout(connection)
    state = "error" if problems else "read" if recorded else "missing"
    today = datetime.fromisoformat(now.replace("Z", "+00:00")).date()
    paths = _paths(place)
    mission, items = _mission(paths["mission"], today), _items(connection, recorded, now, today)
    if problems:
        mission["error"], items["error"] = problems[0], problems[0]
    return {
        "read": now,
        "config": {"state": state, "source": "the repo table", "problems": problems},
        "mission": mission,
        "assets": _geojson(paths["assets"], ASSET_KEYS, "assets") if not problems else {"error": problems[0], "features": []},
        "base": _geojson(paths["base"], BASE_KEYS, "base") if not problems else {"error": problems[0], "features": []},
        "items": items,
        "attention": _attention(mission, items),
    }


def _attention(mission: dict, items: dict) -> dict:
    """The rail badge (sd:3446): the annunciator cells that can light, as hoa.js draws them: alarms today, overdue or
    soon items, and a stale export. Water, tank, pumps and aquifer are never lit."""
    from .v2.pages import attention

    states = [r["state"] for r in items["rows"]] if not items["error"] else []
    cells = ["warning" if mission["alarms_today"]["count"] else "",
             "warning" if "warning" in states else "caution" if "caution" in states else "",
             "caution" if mission["latest"] and mission["stale"] else ""]
    return attention(cells, {"caution": "HOA annunciator cells", "warning": "HOA annunciator cells", "ok": "HOA water system"})

"""HOA: the water system behind the default UI's HOA page (sd:2116).

The page is `v2/hoa.html`; it holds no data. It reads one JSON document,
`/api/hoa`, built here by `document`.

**Where the data is.** The design reads one HOA checkout: the asset map
(`water/map/assets.geojson`, `water/map/base.geojson`), the Mission telemetry
export (`water/metrics/mission/*.csv`) and the checkout's open `followup`
items. The checkout's path and its contents describe a place, and this
repository is public, so the path lives in `<config>/project-dashboard/hoa.conf`
(`hoa.conf.example` is the template), one line per setting:

    repo|<path>        the HOA checkout; its open `followup` items are the obligations
    mission|<path>     optional: the export folder, default <repo>/water/metrics/mission
    assets|<path>      optional: the asset layer, default <repo>/water/map/assets.geojson
    base|<path>        optional: lots, tracts and plats, default <repo>/water/map/base.geojson

A line this module cannot read is named in `config.problems` and skipped.

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

from sd_db import reads

from .documents import _config_dir

__all__ = ["CONFIG", "SOON_DAYS", "STALE_DAYS", "TREND_DAYS", "document", "parse"]

#: In this machine's config directory, never supplied by a request. The checkout ships `hoa.conf.example` only.
CONFIG = _config_dir() / "hoa.conf"

#: Rows the trends carry: the page's widest range (30d).
TREND_DAYS = 30

#: A followup due within this many days is caution; one past its due date is a warning.
SOON_DAYS = 7

#: The export is daily and the pump file lags the analog file by one day, so a newest row two days old is on time.
STALE_DAYS = 2

KEYS = ("repo", "mission", "assets", "base")
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


def parse(text: str) -> tuple[dict, list[str]]:
    """The settings, and one problem per line that was not read."""
    found, problems = {}, []
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, bar, value = (part.strip() for part in line.partition("|"))
        if not bar or key not in KEYS or not value:
            problems.append(f"line {number}: expected <key>|<path>, key one of {', '.join(KEYS)}")
        elif key in found:
            problems.append(f"line {number}: {key} is set twice")
        elif not os.path.isabs(os.path.expanduser(value)):
            problems.append(f"line {number}: {key} needs an absolute path")
        else:
            found[key] = Path(os.path.expanduser(value))
    if "repo" not in found and not problems:
        problems.append("no repo line: the HOA checkout is not named")
    return found, problems


def _paths(found: dict) -> dict:
    repo = found.get("repo")
    return {key: found.get(key) or (repo / DEFAULTS[key] if repo else None) for key in DEFAULTS}


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
        out["error"] = "no export folder: hoa.conf names no repo or mission path"
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
        return {"error": f"no {kind} file: hoa.conf names no repo or {kind} path", "features": []}
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


def _followups(connection, repo: Path | None, now: str, today: date) -> dict:
    """The checkout's open `followup` items, ranked: overdue, then due within a week, then the rest; priority breaks ties."""
    if repo is None:
        return {"error": "no checkout: hoa.conf names no repo", "rows": []}
    rows = []
    for item in reads.backlog_items(connection, kind="followup", repo=str(repo), now=now):
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


def document(connection, *, now: str, config: Path | None = None) -> dict:
    """The HOA page's document: the map, the Mission export and the checkout's open followups."""
    config = CONFIG if config is None else config
    source = f"<config>/project-dashboard/{config.name}"
    try:
        text = config.read_text(encoding="utf-8")
    except FileNotFoundError:
        state, found, problems = "missing", {}, []
    except (OSError, UnicodeDecodeError) as error:
        state, found, problems = "error", {}, [f"not read: {error.__class__.__name__}"]
    else:
        found, problems = parse(text)
        state = "partial" if problems else "read"
    today = datetime.fromisoformat(now.replace("Z", "+00:00")).date()
    paths = _paths(found)
    return {
        "read": now,
        "config": {"state": state, "source": source, "problems": problems},
        "mission": _mission(paths["mission"], today),
        "assets": _geojson(paths["assets"], ASSET_KEYS, "assets"),
        "base": _geojson(paths["base"], BASE_KEYS, "base"),
        "followups": _followups(connection, found.get("repo"), now, today),
    }

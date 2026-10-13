"""Home: the tiles behind the default UI's Home page (sd:2117).

The page is `v2/home.html`; it holds no tiles. It reads one JSON document,
`/api/home`, built here by `document`.

**Which tiles.** The design names the critical Home Assistant entities one
home has: alarm panels, locks, leak and smoke sensors. Those names describe
the operator's house, and this repository is public, so they live in
`<config>/project-dashboard/home-tiles.conf` (`home-tiles.conf.example` is the
template), one line per tile:

    headline|<entity_id>|<name>[|<type>]          the lamp above the groups (the problems sensor)
    tile|<group>|<entity_id>|<name>[|<type>]      a tile, in its group, in file order

`<type>` is optional: `alarm`, `lock`, `toggle` or `sensor`, the command set
the tile gets. Without it the page types the tile by its domain; a virtual
binary sensor that works as a switch needs `toggle` said.

A line this module cannot read is named in `config.problems` with its line
number and skipped; the other lines still count.

**Live state (sd:3427).** Each document makes one `GET /api/states` call to
Home Assistant, bounded by `TIMEOUT`, with the URL and token local-ha-mcp
uses: `HA_MCP_URL` and `HA_TOKEN` from the environment, else from
`<config>/ha-mcp/.env`, the environment winning as in `ha-mcp.sh`. The REST
base is that URL's scheme and host. Each tile gets `state` (its value, last
change, device class and unit) or `error`, a short name for why it has none.
A failed read leaves `reader.available` false with `reader.reason`, and every
tile carries the short error; an entity the answer does not list is
`NOT_LISTED` on its tile alone. The token goes only into the request header:
no document, error or log line holds it, nor the host. No proxy is used and
no redirect is followed, so the token goes to that host alone.

**No writes.** `writes.available` is false with `WRITES_REASON`: every command
stays off. Nothing here writes to Home Assistant.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import socket
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from .documents import _config_dir, pipe_lines

__all__ = ["CONFIG", "NOT_LISTED", "NO_TILES", "TIMEOUT", "TYPES", "WRITES_REASON", "document", "parse"]

#: In this machine's config directory, never supplied by a request. The checkout ships `home-tiles.conf.example` only.
CONFIG = _config_dir() / "home-tiles.conf"

#: Seconds for each socket step of the one Home Assistant read; the page waits on it.
TIMEOUT = 3.0

#: The most bytes of `/api/states` read; a larger answer is a named error, not a partial list.
LIMIT = 16 * 1024 * 1024

#: Why no command runs, on every command: this slice reads Home Assistant and never writes to it.
WRITES_REASON = "the dashboard sends no Home Assistant commands yet"

#: A tile whose entity the read answer does not list.
NOT_LISTED = "not in Home Assistant"

#: The reader's reason when there is no tile list: nothing to read, so nothing is called.
NO_TILES = "no tile list, so Home Assistant was not read"

#: An HA entity id: `<domain>.<object_id>`, lower case, as HA writes them.
ENTITY = re.compile(r"[a-z_]+\.[a-z0-9_]+")

#: The command sets a tile can have (the design's object types on Home).
TYPES = ("alarm", "lock", "toggle", "sensor")


def parse(text: str) -> tuple[dict | None, list[dict], list[str]]:
    """The headline, the groups in file order, and one problem per line that was not read."""
    headline, groups, problems, seen = None, {}, [], set()
    for number, _, fields in pipe_lines(text):
        kind, kind_type = fields[0], None
        if kind == "headline" and len(fields) in (3, 4):
            group, entity, name = None, fields[1], fields[2]
            kind_type = fields[3] if len(fields) == 4 else None
        elif kind == "tile" and len(fields) in (4, 5):
            group, entity, name = fields[1], fields[2], fields[3]
            kind_type = fields[4] if len(fields) == 5 else None
        else:
            problems.append(f"line {number}: expected headline|<entity_id>|<name>[|<type>] or tile|<group>|<entity_id>|<name>[|<type>]")
            continue
        if kind_type is not None and kind_type not in TYPES:
            problems.append(f"line {number}: type {kind_type!r} is not one of {', '.join(TYPES)}")
            continue
        if not ENTITY.fullmatch(entity):
            problems.append(f"line {number}: {entity!r} is not an entity id (domain.object_id)")
            continue
        if not name or (kind == "tile" and not group):
            problems.append(f"line {number}: a tile needs a group and a name")
            continue
        if entity in seen:
            problems.append(f"line {number}: {entity} is listed twice")
            continue
        if kind == "headline" and headline is not None:
            problems.append(f"line {number}: a second headline")
            continue
        seen.add(entity)
        tile = {"id": entity, "name": name, "domain": entity.split(".", 1)[0], "type": kind_type}
        if kind == "headline":
            headline = tile
        else:
            groups.setdefault(group, []).append(tile)
    return headline, [{"name": name, "tiles": tiles} for name, tiles in groups.items()], problems


class _Failed(Exception):
    """A read that gave no states: `short` goes on each tile, `reason` on the page."""

    def __init__(self, short: str, reason: str):
        super().__init__(reason)
        self.short, self.reason = short, reason


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is an error: following it would send the token to wherever it points."""

    def redirect_request(self, *args, **kwargs):
        return None


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def _settings(environ) -> tuple[str, str]:
    """local-ha-mcp's REST base and token: the environment wins over `<config>/ha-mcp/.env`, a `change-me` value is unset."""
    import system_tools_config  # documents._config_dir put lib/ on sys.path

    try:
        saved = system_tools_config.read_env("ha-mcp", environ)
    except (OSError, UnicodeDecodeError) as error:
        raise _Failed("HA not configured", f"<config>/ha-mcp/.env was not read: {error.__class__.__name__}") from None
    values = {}
    for name in ("HA_TOKEN", "HA_MCP_URL"):
        value = environ.get(name) or saved.get(name) or ""
        values[name] = "" if "change-me" in value else value
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise _Failed("HA not configured", f"{' and '.join(missing)} not set: export {'it' if len(missing) == 1 else 'them'}, or copy "
                      "local-ha-mcp/.env.example to <config>/ha-mcp/.env and fill it in")
    try:
        url = urlsplit(values["HA_MCP_URL"])
        url.port  # a port that is not a number raises here
    except ValueError:
        url = None
    if url is None or url.scheme not in ("http", "https") or not url.hostname:
        raise _Failed("HA not configured", "HA_MCP_URL is not an http or https URL")
    return f"{url.scheme}://{url.netloc}", values["HA_TOKEN"]


def _read_states(environ=None, timeout: float = TIMEOUT) -> dict[str, dict]:
    """Every entity's state from one `GET /api/states`, by entity id. Raises `_Failed` naming why there is none."""
    base, token = _settings(os.environ if environ is None else environ)
    request = urllib.request.Request(f"{base}/api/states", headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            body = response.read(LIMIT + 1)
    except urllib.error.HTTPError as error:
        error.close()
        if error.code in (401, 403):
            raise _Failed("HA refused the token", f"Home Assistant refused the token (HTTP {error.code})") from None
        if 300 <= error.code < 400:
            raise _Failed("HA answer not read", f"Home Assistant answered with a redirect (HTTP {error.code}); it is not followed") from None
        raise _Failed(f"HA error {error.code}", f"Home Assistant answered HTTP {error.code}") from None
    except (TimeoutError, socket.timeout):
        raise _Failed("HA timed out", f"Home Assistant gave no answer within {timeout:g} s") from None
    except urllib.error.URLError as error:
        if isinstance(error.reason, (TimeoutError, socket.timeout)):
            raise _Failed("HA timed out", f"Home Assistant gave no answer within {timeout:g} s") from None
        raise _Failed("HA did not answer", f"Home Assistant did not answer: {error.reason.__class__.__name__}") from None
    except (OSError, http.client.HTTPException) as error:
        raise _Failed("HA did not answer", f"Home Assistant did not answer: {error.__class__.__name__}") from None
    if len(body) > LIMIT:
        raise _Failed("HA answer not read", f"the /api/states answer is over {LIMIT // (1024 * 1024)} MiB")
    try:
        rows = json.loads(body)
    except ValueError:
        rows = None
    if not isinstance(rows, list) or not all(map(_row, rows)):
        raise _Failed("HA answer not read", "the /api/states answer is not a list of states")
    return {row["entity_id"]: row for row in rows}


def _row(row) -> bool:
    """A state row as Home Assistant writes them; one of any other shape fails the whole read."""
    return (isinstance(row, dict) and isinstance(row.get("entity_id"), str) and isinstance(row.get("state"), str)
            and isinstance(row.get("last_changed"), str) and isinstance(row.get("attributes", {}), dict))


def _state(row: dict) -> dict:
    """What a tile shows of one state row: no other attribute leaves this module (an `entity_picture` carries a token)."""
    attributes = row.get("attributes") or {}
    device_class, unit = (attributes.get(name) for name in ("device_class", "unit_of_measurement"))
    return {"value": row["state"], "changed": row["last_changed"],
            "device_class": device_class if isinstance(device_class, str) else None, "unit": unit if isinstance(unit, str) else None}


def document(*, now: str, config: Path | None = None, environ=None, timeout: float = TIMEOUT) -> dict:
    """The Home page's document: the configured tiles, each with its live state or a named error."""
    config = CONFIG if config is None else config
    source = f"<config>/project-dashboard/{config.name}"
    try:
        text = config.read_text(encoding="utf-8")
    except FileNotFoundError:
        state, headline, groups, problems = "missing", None, [], []
    except (OSError, UnicodeDecodeError) as error:
        state, headline, groups, problems = "error", None, [], [f"not read: {error.__class__.__name__}"]
    else:
        headline, groups, problems = parse(text)
        state = "partial" if problems else "read"
    tiles = ([headline] if headline else []) + [tile for group in groups for tile in group["tiles"]]
    reader, states, short = {"available": False, "reason": NO_TILES}, {}, NO_TILES
    if tiles:
        try:
            states, reader = _read_states(environ, timeout), {"available": True, "reason": ""}
        except _Failed as failed:
            reader, short = {"available": False, "reason": failed.reason}, failed.short
    for tile in tiles:
        row = states.get(tile["id"])
        tile["state"] = _state(row) if row else None
        tile["error"] = None if row else NOT_LISTED if reader["available"] else short
    return {
        "read": now,
        "config": {"state": state, "source": source, "problems": problems},
        "reader": reader,
        "writes": {"available": False, "reason": WRITES_REASON},
        "headline": headline,
        "groups": groups,
    }

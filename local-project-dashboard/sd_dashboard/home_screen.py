"""Home: the tiles behind the default UI's Home page (sd:2117).

The page is `v2/home.html`; it holds no tiles. It reads one JSON document,
`/api/home`, built here by `document`.

**Which tiles.** The design names the critical Home Assistant entities one
home has: alarm panels, locks, leak and smoke sensors. Those names describe
the operator's house, and this repository is public, so they live in
`<config>/project-dashboard/home-tiles.conf` (`home-tiles.conf.example` is the
template), one line per tile:

    headline|<entity_id>|<name>          the lamp above the groups (the problems sensor)
    tile|<group>|<entity_id>|<name>      a tile, in its group, in file order

A line this module cannot read is named in `config.problems` with its line
number and skipped; the other lines still count.

**No state is read.** The dashboard has no Home Assistant reader yet (the
design source's backend list: "HA reader"). `reader.available` is false and
`reader.reason` says so, so every tile is unknown with that reason and every
command is off with it. Nothing here calls Home Assistant or writes anything.
"""

from __future__ import annotations

import re
from pathlib import Path

from .documents import _config_dir

__all__ = ["CONFIG", "READER_REASON", "document", "parse"]

#: In this machine's config directory, never supplied by a request. The checkout ships `home-tiles.conf.example` only.
CONFIG = _config_dir() / "home-tiles.conf"

#: Why no tile has a state: the one reason the page shows on every tile and every command.
READER_REASON = "no Home Assistant reader: the dashboard does not read HA state yet"

#: An HA entity id: `<domain>.<object_id>`, lower case, as HA writes them.
ENTITY = re.compile(r"[a-z_]+\.[a-z0-9_]+")


def parse(text: str) -> tuple[dict | None, list[dict], list[str]]:
    """The headline, the groups in file order, and one problem per line that was not read."""
    headline, groups, problems, seen = None, {}, [], set()
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        fields = [f.strip() for f in line.split("|")]
        kind = fields[0]
        if kind == "headline" and len(fields) == 3:
            group, entity, name = None, fields[1], fields[2]
        elif kind == "tile" and len(fields) == 4:
            group, entity, name = fields[1], fields[2], fields[3]
        else:
            problems.append(f"line {number}: expected headline|<entity_id>|<name> or tile|<group>|<entity_id>|<name>")
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
        tile = {"id": entity, "name": name, "domain": entity.split(".", 1)[0]}
        if kind == "headline":
            headline = tile
        else:
            groups.setdefault(group, []).append(tile)
    return headline, [{"name": name, "tiles": tiles} for name, tiles in groups.items()], problems


def document(*, now: str, config: Path | None = None) -> dict:
    """The Home page's document: the configured tiles, and why none has a state."""
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
    return {
        "read": now,
        "config": {"state": state, "source": source, "problems": problems},
        "reader": {"available": False, "reason": READER_REASON},
        "headline": headline,
        "groups": groups,
    }

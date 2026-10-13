# Home

Home is at `/home` (sd:2117): the design's critical Home Assistant tiles and
its wall display (`?kiosk=1`). `GET /api/home` (`home_screen.document`) lists
the tiles `<config>/project-dashboard/home-tiles.conf` names, one
`headline|<entity_id>|<name>` or `tile|<group>|<entity_id>|<name>` per line.
An optional last field, `alarm`, `lock`, `toggle` or `sensor`, sets the tile's
commands when its domain would not. Entity ids describe a house, so the
checkout ships only `home-tiles.conf.example`. A line the page cannot read is
named and skipped; without the file the page says "No tile list" and draws no
grid. Each read makes one `GET /api/states` call to Home Assistant (sd:3427),
bounded by `home_screen.TIMEOUT`, with the URL and token local-ha-mcp uses:
`HA_MCP_URL` and `HA_TOKEN` from the environment, else `<config>/ha-mcp/.env`.
The LaunchAgent does not carry `HA_TOKEN`, so the server reads that file. Each
tile shows its state in the design's grammar and its age from `last_changed`.
A failed read (not configured, down, token refused, timed out, redirected, an
answer it cannot parse) names its error on every tile, and an entity Home
Assistant does not list names its own; the page still draws. The token never
reaches the page, and no redirect is followed. Under launchd the server's
interpreter may need macOS Local Network permission to reach a LAN host, as
local-ha-mcp's Node does. The design's arm, disarm, lock, unlock and toggle
commands are registered but off with `home_screen.WRITES_REASON`, so the page
sends nothing. Read state is copy only: its `curl` line names `$HA_TOKEN` and
`$HA_URL` and never holds a value. The page reads `/api/home` through the
shell's reader (`read.js`) when it opens, on Read again, and each minute while
it is visible (sd:3522); the minute read draws no loading state, so the tiles
keep their place, and a failed read draws no tile and says why.

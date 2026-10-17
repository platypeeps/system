# Home

Home is at `/home` (sd:2117): the design's critical Home Assistant tiles and
its wall display (`?kiosk=1`). `GET /api/home` (`home_screen.document`) lists
the tiles one text names, one `headline|<entity_id>|<name>` or
`tile|<group>|<entity_id>|<name>` per line. An optional last field, `alarm`,
`lock`, `toggle` or `sensor`, sets the tile's commands when its domain would
not. Entity ids describe a house, so the checkout ships only
`home-tiles.conf.example`. A line the page cannot read is named and skipped;
with no list the page says "No tile list" and draws no grid.

The text is the workflow database's setting row `dashboard.home_tiles`
(sd:3987). The row wins: once it exists, `<config>/project-dashboard/home-tiles.conf`
is not read; with no row the file answers. A failed row read says "tile list
not read" with its reason, reads no file in its place and calls Home
Assistant for nothing. Edit tiles opens a text editor on the row, else the
file, else the example. Save posts `POST /api/home/tiles {text, before}`:
it parses first and refuses a text with problems (400, each with its line
number), and refuses a `before` that is not the digest of the text the page
showed (409, nothing written). A save toasts with Undo, which posts the
previous text. The first Save is the import; the lead deletes the file after
one nightly cycle. Each read makes one `GET /api/states` call to Home Assistant (sd:3427),
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
sends nothing to Home Assistant. Read state is copy only: its `curl` line names `$HA_TOKEN` and
`$HA_URL` and never holds a value. The page reads `/api/home` through the
shell's reader (`read.js`) when it opens, on Read again, and each minute while
it is visible (sd:3522); the minute read draws no loading state, so the tiles
keep their place, and a failed read draws no tile and says why.

Views (`places.js`, `places.py`): Overview is the page above; Tasks, Briefs and
Reports each read their own document, `GET /api/home/tasks`, `/briefs` and
`/reports`, only when shown, and the address keeps the view as `#view=<id>`.
Tasks are the open items with no repo: personal and home items have none; Tasks' Home view holds the same rows. A report is in no Tasks view. A brief is home's when its series, lower-cased with spaces as hyphens,
names a job of the `home` family in `report-families.conf`; a report
is home's when its job is in that family. A missing checkout, family or file
makes the view unknown with the reason and the line to add, never an empty list.
Until the shell carries a view switch, `places.js` draws its own segmented
control (`viewSwitch`, the one adapter).

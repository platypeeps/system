# Home

Home is at `/home` (sd:2117): the design's critical Home Assistant tiles and
its wall display (`?kiosk=1`). `GET /api/home` (`home_screen.document`) lists
the tiles `<config>/project-dashboard/home-tiles.conf` names, one
`headline|<entity_id>|<name>` or `tile|<group>|<entity_id>|<name>` per line.
An optional last field, `alarm`, `lock`, `toggle` or `sensor`, sets the tile's
commands when its domain would not. Entity ids describe a house, so the
checkout ships only `home-tiles.conf.example`. A line the page cannot read is
named and skipped; without the file the page says "No tile list" and draws no
grid. The dashboard reads no Home Assistant state yet, so every tile shows as
unknown with that reason and none shows ok. The design's arm, disarm, lock,
unlock and toggle commands are registered but off with it, so the page sends
nothing. Read state is copy only: its `curl` line names `$HA_TOKEN` and
`$HA_URL` and never holds a value. The page reads `/api/home` once, through the
shell's reader (`read.js`); a failed read draws no tile and says why.

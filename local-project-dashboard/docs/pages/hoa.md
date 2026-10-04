# HOA

HOA is at `/hoa` (sd:2116): the water system's map, Mission export trends,
open followups and alarm events. `GET /api/hoa` (`hoa_screen.document`) reads
the checkout `<config>/project-dashboard/hoa.conf` names, one `<key>|<path>`
per line: `repo` is the checkout, and `mission`, `assets` and `base` override
the default places of the export folder and the two map files. The path names
a place, so the checkout ships only `hoa.conf.example`. Without the file the
page says "No HOA checkout" and every cell is unknown.

Each source is guarded on its own: a file that is missing or does not parse is
named, its cells and charts are unknown with that reason, and the other parts
still answer. The trends carry the export's last 30 days. Mission data is
caution when the newest row is more than two days old (`STALE_DAYS`). Alarms
today is unknown until the export holds a row for today. The obligations are
the checkout's open `followup` items: overdue is a warning, due within a week
is caution (`SOON_DAYS`), the rest are queued. Water has no reader: the export
holds daily rows, not the pump's state now.

Gaps from the design: the map is one SVG of the files' vectors with Fit, zoom
and drag, not Leaflet, so there are no asset clusters and no aerial button.
Draft followup asks the chat, which has no backend yet; nothing is sent.

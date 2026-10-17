# HOA

HOA is at `/hoa` (sd:2116): the water system's map, Mission export trends,
alarm events and the checkout's open items. `GET /api/hoa` (`hoa_screen.document`) reads
the checkout the repo table holds under the folder name `hoa` (sd:3985): the
export is in `water/metrics/mission`, the map in `water/map/assets.geojson` and
`water/map/base.geojson`. Two repos with that folder name are an error that
names both. With none, the page says "No HOA checkout" and every cell is
unknown.

Each source is guarded on its own: a file that is missing or does not parse is
named, its cells and charts are unknown with that reason, and the other parts
still answer. The trends carry the export's last 30 days. Mission data is
caution when the newest row is more than two days old (`STALE_DAYS`). Alarms
today is unknown until the export holds a row for today. The Tasks cell and the
Tasks view hold the checkout's open items of every kind but report (sd:4959):
overdue is a warning, due within a week is caution (`SOON_DAYS`), the rest are
queued. Water has no reader: the export holds daily rows, not the pump's state now.

Lots (sd:4964): each lot carries its situs address and owner of record from
the checkout's gitignored `downloads/parcels/lots.json`, which the hoa repo's
`tools/fetch-lot-parcels.py` writes from the county parcel layer. The join is by
the lot's index among `base.geojson`'s lots, and only against the base file the
lot data names by sha256. The map titles each lot with its address and, zoomed
in, draws it; a tap shows the address, owner and parcel in Details. Missing or
stale lot data makes the read partial and each lot unknown, naming the script.
The data names homeowners, so it is read at request time and never copied; this
repo's tests use synthetic names.

Gaps from the design: the map is one SVG of the files' vectors with Fit, zoom
and drag, not Leaflet, so there are no asset clusters and no aerial button.
Draft followup asks the chat, which has no backend yet; nothing is sent.

Views (`places.js`, `places.py`): Overview is the page above. Tasks is one
table of `/api/hoa`'s items, ranked by urgency: the id is its own first column,
every column sorts (a third click returns to the rank), and Kind narrows the rows.
Briefs and Reports each read their own document, `GET /api/hoa/briefs` and
`/reports`, only when shown, and the address keeps the view as `#view=<id>`.
A brief is hoa's when its series, lower-cased with spaces as hyphens,
names a job of the `hoa` family in `report-families.conf`, or it is filed under the hoa checkout; a report
is hoa's when its job is in that family. A missing checkout, family or file
makes the view unknown with the reason and the line to add, never an empty list.
Until the shell carries a view switch, `places.js` draws its own segmented
control (`viewSwitch`, the one adapter).

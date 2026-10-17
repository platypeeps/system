# HOA

HOA is at `/hoa` (sd:2116): the water system's map, Mission export trends,
open followups and alarm events. `GET /api/hoa` (`hoa_screen.document`) reads
the checkout the repo table holds under the folder name `hoa` (sd:3985): the
export is in `water/metrics/mission`, the map in `water/map/assets.geojson` and
`water/map/base.geojson`. Two repos with that folder name are an error that
names both. With none, the page says "No HOA checkout" and every cell is
unknown.

Each source is guarded on its own: a file that is missing or does not parse is
named, its cells and charts are unknown with that reason, and the other parts
still answer. The trends carry the export's last 30 days. Mission data is
caution when the newest row is more than two days old (`STALE_DAYS`). Alarms
today is unknown until the export holds a row for today. The obligations are
the checkout's open `followup` items: overdue is a warning, due within a week
is caution (`SOON_DAYS`), the rest are queued. Water has no reader: the export
holds daily rows, not the pump's state now.

Each trend shows its window's min (lowest daily low), median (of the daily
values) and max (highest daily high), with faint marks at the min and max
(sd:4969). A problem level is a dashed line, and a day beyond it is a warning.
The levels are per machine, in `<config>/project-dashboard/hoa-thresholds.conf`
(template `hoa-thresholds.conf.example`): the hoa checkout states its levels in
prose only. Without the file a trend says "no problem level set".

Acknowledge on an alarm event (sd:4967) posts `/api/hoa/alarm`, the workflow
database's `view` state that Designs' Hide writes, keyed
`hoa-alarm/<datetime> <event>`. Undo writes it back. Acknowledged events hide
behind the Show acknowledged chip and leave the Alarms today lamp. The write
reads the export again, so an event it no longer holds is refused (409) by
name. Nothing reaches the Mission portal.

Data sources (sd:4965) shows one age lamp each for the Mission export and
Vantaca, from the checkout's `python3 tools/source-ages.py --json`, run with a
10-second bound (`SOURCE_SECONDS`). A script that is missing, times out, exits
past 1 or prints no source list makes the panel unknown with that reason.
Vantaca's row hides after its `ends` date. Refresh now gives the source's
refresh line to copy: the dashboard does not start either command yet, pending
the run-route decision on sd:4965.

Gaps from the design: the map is one SVG of the files' vectors with Fit, zoom
and drag, not Leaflet, so there are no asset clusters and no aerial button.
Draft followup asks the chat, which has no backend yet; nothing is sent.

Views (`places.js`, `places.py`): Overview is the page above; Issues, Briefs and
Reports each read their own document, `GET /api/hoa/issues`, `/briefs` and
`/reports`, only when shown, and the address keeps the view as `#view=<id>`.
Issues are the open items whose repo is the hoa checkout; a report is in no Issues view. A brief is hoa's when its series, lower-cased with spaces as hyphens,
names a job of the `hoa` family in `report-families.conf`, or it is filed under the hoa checkout; a report
is hoa's when its job is in that family. A missing checkout, family or file
makes the view unknown with the reason and the line to add, never an empty list.
Until the shell carries a view switch, `places.js` draws its own segmented
control (`viewSwitch`, the one adapter).

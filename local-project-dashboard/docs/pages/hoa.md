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

Data sources (sd:4965) shows one age lamp per source the checkout's
`python3 tools/source-ages.py --json` lists (Mission export, CORE, Vantaca,
TMT), run with a 10-second bound (`SOURCE_SECONDS`). A script that is missing,
times out, exits past 1 or prints no source list makes the panel unknown with
that reason. A row hides after its `ends` date. Refresh now starts the source's
manual cron job (`hoa_screen.REFRESH_JOBS`: `hoa-mission-export`,
`hoa-vantaca-pull`, each `JOB_SCHEDULE="manual"`) through `/api/jobs/<name>/retry`
with the job's revision, the route Management's Retry posts. The jobs
inventory gives the job's state, its off reason (not installed, running, or a
start launchd refuses) and the last line of its log; while it runs, the page
reads again every 15 seconds (`SRC_POLL`).

The last Data sources row is the HOA sweep (sd:5095). Run sweep asks first,
then posts `/api/hoa/sweep`, which starts the checkout's `tools/sweep.py run`
detached, holding `<state>/system/hoa-sweep/lock` (`sd_dashboard/sweep.py`): a second
post while it runs is a 409. That script runs one `claude -p` with an exact
allowlist: it reads both Drives and the alias mail, updates the status and the
VP brief, saves followups as Gmail drafts (never sent) and hands its commit to
the lane. `/api/hoa` carries its `sweep` part, also at
`/api/hoa/sweep/status`: `tools/sweep.py status --json`, bounded at 10
seconds, and whether the lock is held. A run that ended without a record of
its own reads failed, naming `start.log`.

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
The views are `window.PAGE_VIEWS`, set from the markup before `shell.js`; the shell's view switch draws
them in `#viewswitch`, and until that shell lands `places.js` draws a segmented control there (`viewSwitch`).

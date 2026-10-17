# Reports

Reports is at `/reports` (sd:2121): job-family lamps, a seven-day run cadence
per job, the report ledger with filters and saved views, and Details with a
line diff against the job's previous report. The shell's view switch, under
the lamps, shows the Reports view (the ledger, the default) or the Cadence view
(`#view=cadence`); a family lamp or a cadence row shows the Reports view.
`GET /api/reports`
(`reports_screen.document`) gives the newest 200 reports v1 lists, the launchd
jobs, and each job's runs per local day, read from its `cron-jobs.sh` log. A
day the job's calendar leaves out is not scheduled. The families come from
`<config>/project-dashboard/report-families.conf`, one
`family|<key>|<label>|<icon>|<job>,<job>,...` per line. Job names are the
operator's, so the checkout ships only `report-families.conf.example`; without
the file no family lamp is drawn and every job is listed under "Other jobs".
A family lamp follows each job's own last scheduled run before today, however
rare; a job whose last scheduled run the log does not hold makes it unknown.
Acknowledge posts v1's route with the report's revision. It is off while an
open followup holds the report. Its Undo posts `/api/reports/<n>/reopen` with
the revision the acknowledge answered; `reporting.reopen` moves the report back
to the status it was acknowledged from (sd:2395). The page reads
`/api/reports` through the shared reader, `read.js` (sd:2487). The Job filter
lists every job the reading names, with or without a report (sd:2430).
The bulk bar acknowledges picked reports one by one. A clean run files no
report since the quiet-run rule, so the page has no clean preview or batch;
`sd reports acknowledge --all-clean` stays a CLI verb.
Retry posts the job route, as Management does. The old Reports area was
retired in sd:3402; the Resources views stay in the palette as Resources (classic).

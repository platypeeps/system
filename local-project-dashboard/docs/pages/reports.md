# Reports

Reports is at `/reports` (sd:2121): job-family lamps, a seven-day run cadence
per job, the report ledger with filters and saved views, and Details with a
line diff against the job's previous report. `GET /api/reports`
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
open followup holds the report. It asks first and has no Undo: sd-db has no
verb that reopens a report. Select clean reads `GET /api/reports/clean?before=<date>`,
v1's preview, and the bulk bar acknowledges the picked reports one by one.
Retry posts the job route, as Management does. No status mail is read: the
dashboard holds no message store. The old screen stays in the palette as
Reports (classic) for the Resources views and the attributed batch.

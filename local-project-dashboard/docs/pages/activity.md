# Activity

Activity is at `/activity` (sd:2111): one timeline of the last 24 hours, as an
annunciator, a lane per kind and a ledger banded by hour. `GET /api/activity`
(`activity_screen.document`) reads only what the library already records:
merges are the delivery notes `sd-ship` writes, runs are runner assignments
and launchd jobs placed at their log time, and commands are the execution
journal v1 Operations > Commands listed, older records too for the "All read"
range; palette and runner runs alike (sd:2183). Reviews are the review each
merge carried, read from the same delivery note's `review_selection` and
`review_clearance` (sd:2211): clean or advisory is ok, blocking findings whose
dispositions were accepted are a caution, and a merge with no review record is
counted, not drawn. A review that never reached a merge is not recorded, and a
review's time is its merge's. The design's deploy and mail kinds are left out:
no repository records deployments, and the operator ruled out a mail reader
(sd:2212). A kind whose every source failed to read draws hatched, unknown
rather than zero, with the reason. The timeline's filters are one bar: state
and range as segmented controls, a Repo list that filters as you type, and the
picked kinds, repos and query as removable tokens; the lamps and lanes filter
by kind. Job retry posts the job's revision
and asks first after exit 127, command not found, naming the exit (sd:3392). Its toast says "Retry
started" only when launchctl accepted the request; a refusal names launchctl's
message, and a timeout or a lost answer says the outcome is not known, as
Management words it (sd:3391). Show output reads the
execution record, page by page past 64 KiB (sd:2416). Reconcile, on an
unfinished palette record, posts `/api/palette/<note>/reconcile`, which observes
the target and never replays the command (sd:3391). With Show output it takes
over v1 Operations > Commands, which sd:3402 retired. The page reads through
the shell's reader, `read.js` (sd:2489): a landed write rereads, and a failed
reread keeps the rows and says the write landed.

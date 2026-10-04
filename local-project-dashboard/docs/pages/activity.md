# Activity

Activity is at `/activity` (sd:2111): one timeline of the last 24 hours, as an
annunciator, a lane per kind and a ledger banded by hour. `GET /api/activity`
(`activity_screen.document`) reads only what the library already records:
merges are the delivery notes `sd-ship` writes, runs are runner assignments
and launchd jobs placed at their log time, and commands are the execution
journal v1 Operations > Commands lists, older records too for the "All read"
range; palette and runner runs alike (sd:2183). Reviews are the review each
merge carried, read from the same delivery note's `review_selection` and
`review_clearance` (sd:2211): clean or advisory is ok, blocking findings whose
dispositions were accepted are a caution, and a merge with no review record is
counted, not drawn. A review that never reached a merge is not recorded, and a
review's time is its merge's. Deploys and mail have no collector; the document
names each with its reason and the page draws it unknown, not zero. Requeue carries Undo; job retry posts the job's revision
and is off for exit 127, command not found (sd:2415); Show output reads the
execution record, page by page past 64 KiB (sd:2416). The page reads through
the shell's reader, `read.js` (sd:2489): a landed write rereads, and a failed
reread keeps the rows and says the write landed.

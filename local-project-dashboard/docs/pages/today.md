# Today

Today is the first page, at `/` and `/today`: the same
`/api/now` rows the classic Now drew, as an annunciator and a ranked ledger. Its
capture is the shell's, as on every page: a task or followup through `POST /api/items`,
a note on the selected item through `POST /api/items/<id>/notes`.
It reads `/api/now` through the shell's reader (`read.js`): of two refreshes
only the newer one draws, and a failed read clears every row and its commands.
Status mail, backups, HOA water and Wants you wait for collectors.
Snooze hides a row for 1 hour, until the next 08:00 or for 1 week; it posts
`/api/snooze`, and the row waits under Snoozed with an Unsnooze (sd:1896).
A row whose problem reads otherwise shows again before its time.
Your work lists the items `sd today` lists, from the same `/api/now` document; each row has the same Snooze,
keyed `today:item:<id>`, and the item shows again early when its status or due date changes (sd:3271).
`sd today` still lists a snoozed item.
Retry on a failed or interrupted job posts `/api/jobs/<job>/retry` with the job's revision from the same read, as
Management does (sd:3386); a job that changed since is refused and Now is read again.
After exit 127, command not found, Retry asks first and names the exit (sd:3392).

# Today

Today is the first page, at `/` and `/today`: the same
`/api/now` rows as the classic Now, as an annunciator and a ranked ledger. Its
capture files a task through `POST /api/items`.
It reads `/api/now` through the shell's reader (`read.js`): of two refreshes
only the newer one draws, and a failed read clears every row and its commands.
Status mail, backups, HOA water and Wants you wait for collectors.
Snooze hides a row for 1 hour, until the next 08:00 or for 1 week; it posts
`/api/snooze`, and the row waits under Snoozed with an Unsnooze (sd:1896).
A row whose problem reads otherwise shows again before its time.
Your work lists the items `sd today` lists, from the same `/api/now` document; each row has the same Snooze,
keyed `today:item:<id>`, and the item shows again early when its status or due date changes (sd:3271).
The classic Today and `sd today` still list a snoozed item.

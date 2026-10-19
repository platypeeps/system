# Briefs

Briefs is at `/briefs` (sd:2112): the routine briefs the workflow database
holds (sd:3459), as cadence lanes per series and a ledger, over 24 hours, 7 or
30 days. `GET /api/briefs` (`briefs_screen.document`) reads them through the
pack's `routine_briefs.rows`, newest first and at most 200, without their
text. A row's source is the brief's series, and its time is when it was first
filed, given only when that falls on the brief's own day. A failed read is an
error with its cause, never an empty page.

Read is every row's first action: it opens the brief's item page,
`/item/<id>`, which renders its Markdown. Open in Obsidian shows only when the
`brief-copies` heartbeat of `sd reports brief render` is ok, names a folder
for the brief's repository, is newer than the row's last change and does not
list the brief as refused; otherwise Details says why there is no copy. The
Vault copies lamp shows that heartbeat: ok, failed with its reason, off, or
unknown. Health shows the same heartbeat as a Vault mirror row.

The design reads brief mail; no mail or watchdog reader exists, so the page
shows no unread state, follow-up flag or failure, and the Missed lamp is
unknown with that reason. Make task opens the capture form, which files
through `POST /api/items`.

Each row shows one icon per destination holding a copy (sd:5102): Drive,
Notion, Obsidian, from the item's `fields.published` and the vault copy. Details'
Publish to writes `fields.publish.<channel>` on the item and queues the brief;
Publish now drains its requests, as on Documents.

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
unknown. Health shows the same heartbeat as its Brief copies row; a failed or
unknown lamp links there, where the refused stems and the reason are.

The design reads brief mail; no mail or watchdog reader exists, so the page
shows no unread state, follow-up flag or failure, and the Missed lamp is
unknown with that reason. Make task opens the capture form, which files
through `POST /api/items`.

Each row shows one icon per destination holding a copy (sd:5102): Drive,
Notion, Obsidian, from the item's `fields.published` and the vault copy. Details'
Publish to writes `fields.publish.<channel>` on the item and queues the brief;
Unticking asks first, queues the copy for the trash and drops the field; Undo ticks it again.
Publish now drains its requests, as on Documents.

Topics (system sd:5778) is the page's third view: one row per `sdw.topic` row, active first, from the
same document's `topics` (`briefs_screen.topics`, through `records.list_rows`). Each row shows the slug, the stage,
the newest line of its `## Memory` section with the line count, and how many lines its `## Sources` section holds;
the hub's brief routines rewrite both, and a topic without one says so. A topic opens on Tasks, where it is edited.
Last signal and the days in range come from each brief's `signal_topics`, which pack sd:5798 copies into its
record. Until then no brief records them, and the view says signal is not recorded, never quiet. A failed topic
read says so with its cause and leaves the briefs.

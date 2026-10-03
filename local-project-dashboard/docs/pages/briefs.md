# Briefs

Briefs is at `/briefs` (sd:2112): the brief notes the vault's
`System/AI Generated/Briefs` folder holds, as cadence lanes per source and a
ledger, over 24 hours, 7 or 30 days. `GET /api/briefs`
(`briefs_screen.document`) reads them through the child Research > Resources >
Briefs runs, `sd_tile.py briefs-rows`, under the same five seconds; the child
stops at 200 rows or 48 KB and the page says how many it shows of how many.
A row's source is the kind in its file name, and its time is the note's
modification time, given only when it falls on the note's own day. The design
reads brief mail; no mail or watchdog reader exists, so the page shows no
unread state, follow-up flag or failure, and the Missed lamp is unknown with
that reason. Make task opens the capture form, which files through
`POST /api/items`; Open in Obsidian opens the note's link.

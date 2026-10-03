# Contributions

Contributions is at `/contributions` (sd:2113): the open rows of the
contributions projection v1 renders, in lanes by who acts next, with each
row's repository scope (internal when sd's repo table holds it) and settled
rows counted per repository. `GET /api/contributions/page`
(`contribution_screen.document`) carries at most `OPEN_LIMIT` open rows and
says when it cut some; its per-scope counts, which the badge, lamps and tallies
read, cover every open row. It sends no local path: a checkout reads
`local: <folder>`, and a draft is the flag `has_draft`. The dashboard reads nothing from GitHub, so the design's
"GitHub now" state and settled-per-day chart say they are not read.
Acknowledge and Make task ask first, since no verb reverses either.
Acknowledge posts to the v1 route. Make task posts `/api/contributions/task`,
which files the task `sd task contribution add` files, with the row's URL as
its identity: the projection links the two, and a second task for the URL is
refused. A write's toast reports the write; the reread after it is separate.
The page reads through the shell's reader, `read.js` (sd:2488): only the
newest read draws, a reread waits for a read that started before its write
landed, and a row a read no longer lists loses its pick and its commands. A
failed reread keeps the rows and says the write landed; a failed load clears
the page. A redraw keeps keyboard focus on the replaced lamp or command, or on
the row's next action when that command went off (sd:2426). Settled rows count per stored repository, so two
checkouts with one folder name stay two bars. Draft nudge, Open on GitHub and
Re-run collector are copy only. The old screen moved to `/classic/contributions`; it still shows
evidence, dependencies and notification delivery.

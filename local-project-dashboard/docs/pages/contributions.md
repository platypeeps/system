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
refused. A write's toast reports the write; the reread after it is separate,
and only the newest reread draws. A row a reread no longer lists loses its
pick and its commands. Settled rows count per stored repository, so two
checkouts with one folder name stay two bars. Draft nudge, Open on GitHub and
Re-run collector are copy only. The old screen moved to `/classic/contributions`; it still shows
evidence, dependencies and notification delivery.

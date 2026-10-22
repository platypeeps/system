# Places

A realm place is at `/places?realm=<id>` (sd:5663): the page of a realm in
`dashboard.realms` whose id is also a family key in
`dashboard.report_families`. Both are setting rows on Settings, so the
repository names no realm. The rail lists each realm place under Places,
after HOA and Home, with its family's icon and no `g` key. A realm whose
line or family line did not read gets no entry. `/ui/sections.js` carries
the realms as `SHELL_REALMS`, each with `place`, the family's icon or null.

The page has HOA's and Home's Tasks, Briefs and Reports views (`places.js`,
`places.py`) and no Overview. `place.js` names the page after the realm and
points each view at `GET /api/place/<id>/tasks`, `/briefs` or `/reports`,
read only when shown:

- Tasks: the open items `places.realm_of` puts in the realm, the rows of
  Tasks' realm view.
- Briefs: briefs whose repo a glob of the realm matches, and briefs whose
  series, lower-cased with spaces as hyphens, names a job of the family.
- Reports: reports of the family's jobs, from the newest 200.

A realm the list does not name, or a missing family line, makes the view
unknown with the line to add, such as
`family|acme|Acme|map|<job>,<job>,...`. A realm or family list that
did not read is `error`, never an empty list. An id Tasks reserves (`hoa`,
`home`, `personal`, `all`) answers 404. Without `?realm=` the page lists
the realm places. Nothing here writes; a brief's Details carry the Briefs
page's commands.

Open | All (sd:5752): Tasks and Reports show their open rows; All adds the
week's done tasks (`places.task_rows`, the Tasks page's bound) or the
acknowledged reports, and the address keeps it as `?tasks=all` or
`?reports=all`. Each row's act is its acknowledge: a task's Done (or Done →
next occurrence for a repeating one), a report's Acknowledge, each with
Undo. A brief has none: it is filed done, and the pack refuses to move it.

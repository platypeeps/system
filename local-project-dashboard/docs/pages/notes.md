# Notes

Notes is at `/notes` (sd:2120): the last seven days, a day at a time, each
with its merges, the items that reached done, the count opened and the runner
runs. `GET /api/notes` (`notes_screen.document`) reads sd.db only and writes
nothing. A day is this machine's day; times show in the viewer's own zone.

Merges are the delivery notes `sd-ship` writes, as Activity reads them. The
design walked each checkout's first-parent history instead; a delivery note
is the same merge, recorded once, with no git walk. Done and opened come from
the day's `status_change` notes (`reads.status_change_notes`). Runs are the
runner assignments that started or ended that day.

A source that raises is named, its section is unknown with that reason, and
the page reports a partial read. Three sources have no reader and always show
as unknown: mail (a network call and a credential), the daily note text, and
quick-note storage.

Gaps from the design: quick notes stay on the page for the visit, as the
design states, because sd store has no loose-note kind. Declaring a
`notes.quick` kind is a pack change and waits on a decision (sd:2120). The
read covers seven days; an older day says it was not read.

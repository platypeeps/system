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

Each day also carries its daily note's text (sd:3426), read from the
Obsidian vault that `local-obsidian-review` and `local-obsidian-tasks` read:
`OBSIDIAN_VAULT`, else either tool's `<config>/<tool>/.env`, else
`~/Documents/Obsidian Vault`. The note's name comes from the vault's
`.obsidian/daily-notes.json` (`folder`, and the `format`, default
`YYYY-MM-DD`); a day folder holding the note of the same name counts too. The
page shows the text as plain text. A day with no note says where it looked;
an unset vault reads "Vault not configured".

A source that raises is named, its section is unknown with that reason, and
the page reports a partial read. Mail has no reader and always shows as
unknown: it needs a network call and a credential.

Gaps from the design: quick notes stay on the page for the visit, as the
design states, because sd store has no loose-note kind. Declaring a
`notes.quick` kind is a pack change and waits on a decision (sd:2120). The
read covers seven days; an older day says it was not read.

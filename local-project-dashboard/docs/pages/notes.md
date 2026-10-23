# Notes

Notes is at `/notes` (sd:2120): the last seven days, a day at a time, each
with its merges, the items that reached done and the count opened. `GET /api/notes` (`notes_screen.document`) reads sd.db only and writes
nothing. A day is this machine's day; times show in the viewer's own zone.

Merges are the delivery notes `sd-ship` writes, as Activity reads them. The
design walked each checkout's first-parent history instead; a delivery note
is the same merge, recorded once, with no git walk. Done and opened come from
the day's `status_change` notes (`reads.status_change_notes`). The page reads
no runner runs and no mail (sd:5289): Activity shows runs, and Today shows mail.

Each day also carries its daily note's text (sd:3426), read from the
Obsidian vault named by `OBSIDIAN_VAULT`, else `<config>/shared.env`, else
`~/Documents/Obsidian Vault`. The note's name comes from the vault's
`.obsidian/daily-notes.json` (`folder`, and the `format`, default
`YYYY-MM-DD`); a day folder holding the note of the same name counts too. The
page shows the text as plain text. A day with no note says where it looked;
an unset vault reads "Vault not configured".

A source that raises is named, its section is unknown with that reason, and
the page reports a partial read.

Quick notes (sd:2549) are `sdw.quick-note` items in the vault, a kind the
writing pack declares. `GET /api/notes/quick` lists them through `sd store
list`, and `POST /api/notes/quick/add` keeps one through `sd store add`, each
the `sd` on the dashboard's PATH. The server checks the kind's rules: one line,
no control character, no double quote, at most `QUICK_MAX` characters. The
title is the local time; a taken title gets ` 2`, ` 3` and so on. A machine
without the kind says so, and Keep is off. Quick notes open in a dialog over
the day, from the date bar's Quick notes button or `/` (sd:5750); a kept note
closes it, and a refused one stays in it with its text.

Gap from the design: the read covers seven days; an older day says it was not
read.

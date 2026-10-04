# Skills

Skills is at `/skills` (sd:2123): the design's lamps, facets and sortable
catalog over every skill in the command pack. `GET /api/skills`
(`skills_screen.document`) gives each skill its status, paths, trial and
install state, and its use in each of the last three Monday-start weeks from
`skill_use`. It also counts the rows that name a path instead of a skill, the
rows per surface, and the skills outside the pack that recorded use. It sends
no local path: a skill's source is its path inside the pack. Try, Review,
Promote and Demote ask first and post the old screen's routes with the
skill's revision; no verb withdraws a trial or a queued request, so none has
Undo. Run, Schedule, Adopt and Scan are copy only: no route creates the item
a run carries, adds a job, adopts a skill or scans sessions. The page reads
through the shell's reader, `read.js`. The old screen moved to
`/classic/skills`; its "Run with agent" link opens Tasks with `?skill=` (sd:2590).

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
through the shell's reader, `read.js`. The old screen was retired in sd:3402.

Re-import and Evaluate (sd:3992) run `sd skill intake --json` through
`POST /api/skills/intake` (`skill_intake.intake`). Re-import is on for a skill
whose Provenance names the intake's source (`imported` in the document);
Evaluate, in the Adopt panel, takes a `https://github.com/` URL and a scope.
The source goes to `sd` as one argument after `--`, never through a shell.
Details shows the report: verdict, source, risk, match, value, revision and
diff. Approve revision appears only under a report that can install, asks
first, and posts the URL, scope and revision that report printed; nothing
installs without it. A refused approve wrote nothing; one that failed any
other way is reported as unknown, and the next Evaluate finishes or undoes it.

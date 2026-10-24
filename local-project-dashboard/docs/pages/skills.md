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
whose Provenance names the intake's source (`imported` in the document).
The Adopt panel lists the skills in `~/.claude/skills` whose Provenance names
it (`user` in the document, sd:3997), each with a Re-import that runs the
intake with `--scope user`. Evaluate, in the same panel, takes a
`https://github.com/` URL and a scope.
The source goes to `sd` as one argument after `--`, never through a shell.
Details shows the report: verdict, source, risk, match, value, revision and
diff. Approve revision appears only under a report that can install, asks
first, and posts the URL, scope and revision that report printed; nothing
installs without it. A refused approve wrote nothing; one that failed any
other way is reported as unknown, and the next Evaluate finishes or undoes it.

Rollback (system sd:5779) sits beside Re-import, for a catalog skill and a
user skill alike. It posts the same route with `rollback: true`, which runs
`sd skill intake <name> --rollback --json`. Details shows the swap back to
the copy the last install replaced: the diff, both commits and the
revision. A skill the last install put there new is removed. Approve
rollback asks first and posts the name, scope and revision that report
printed. The replaced copy is kept, so a second Rollback undoes the first.
The verb refuses a locally edited copy and a pack skill git holds clean,
and names the `git revert` to run through the lane. The Rollback and
Evaluate reports list the pack skills whose Lineage or Provenance names the
same repository, with their commits; a `git log` that failed shows unknown.
A skill installed before the pack kept copies has none, and its Rollback
says so.

The Intake view (system sd:5478) lists the repositories skills are adopted
from. `GET /api/skills/intake/repos` (`skill_intake.repos`) runs
`sd skill intake --tracked --json`: the `skills.intake_repos` setting row,
or, until the list is first changed, the repositories the installed skills
came from: their Provenance, `~/.agents/.skill-lock.json` and the GitHub
origin of a git checkout a user skill links into. Each row shows the skills adopted from it and the last
refresh's time and result; the repositories skills came from that are not
tracked sit under the table, each with Track. Track a repository takes a
`https://github.com/<owner>/<repo>` URL and a scope.
`POST /api/skills/intake/tracking` (`skill_intake.tracking`) runs `--track`,
`--untrack` or `--refresh` with that one URL. Refresh fetches the default
branch and lists its new, changed, blocked, removed and unread skills; Show
report puts one skill's intake report in Details, where Approve is the same
as Evaluate's. A refresh installs nothing. Remove stops tracking and keeps
the installed skills; its Undo tracks the repository again with its scope,
without its last refresh. A list write with no answer is reported as not
known, and the list is read again.

The Usage view (sd:5881), the third after Catalog and Intake, was Metrics'
Skills view. `GET /api/skills/usage` (`skills_screen.usage`) counts every
`skill_use` row over four Monday-start weeks (`reads.skill_use_days`): all
skills per week, this week first, and this week's ten most used, each a link
to its catalog row. It is read the first time the view shows; a failed read
is a row naming it. `/metrics#view=skills` opens this view with its query.

# Tasks

Tasks is the second page, at `/tasks` (sd:2124): the rows v1 `/backlog` reads,
as a board, an Eisenhower matrix and a list, filtered by kind, repo, priority
and due. `GET /api/tasks` (`tasks_screen.document`) gives each row its revision
and the statuses the library allows it. `GET /api/tasks/<id>`
(`tasks_screen.details`) splits the `sd task show --json` reading into status
history and notes, and adds the item's assignments and its external context. A
command that runs posts to the route v1 already answers, and its toast comes
after the write lands. Status, priority, due and recurrence edits and a requeue
carry Undo; resolving a followup note and cancelling an assignment ask first.
Completing a repeating task asks first and names the outcome (sd:2250): the next
occurrence's date, or that sd sets it, or, for a rule sd cannot walk, that the
series ends, on the danger button. `sd work relink` and `sd work cancel` run
through the work routes after a confirm that takes the moved path or the reason;
OK stays off until it is typed (sd:2200), and `progress.work_controls` turns
each off where sd would refuse it. The page reads through the shell's reader,
`read.js` (sd:2484): a landed write rereads, a row the read no longer lists runs
no command, and a failed reread keeps the rows and says the write landed.
Picked rows run as one selection (sd:2590): one `POST /api/run` queues them in
pick order, sequential or parallel, with a time limit and an optional dollar
budget per assignment, or refuses them all. Each row carries
`runner_controls.readiness`, so Run is offered only where every picked row can
queue. `?skill=<name>`, from the classic Skills page, sends that skill with its
catalog revision from `/api/skills`; an unknown or unreadable skill keeps Run
off and says why.

A v1 `/backlog` address answers 301 to `/tasks` with the query v1 read
(`tasks_screen.from_backlog`, sd:2356); a repository path becomes the label
Tasks filters on. v1's screen and its run selection were deleted in sd:2622;
`/classic/backlog` is a 404.

# Tasks

Tasks is the second page, at `/tasks` (sd:2124): the rows v1 `/backlog` reads,
as a board, an Eisenhower matrix and a list, filtered by kind, repo, priority
and due. `GET /api/tasks` (`tasks_screen.document`) gives each row its revision
and the statuses the library allows it. `GET /api/tasks/<id>`
(`tasks_screen.details`) splits the `sd task show --json` reading into status
history and notes, and adds the item's assignments and its external context.
A command that runs posts to the route v1 already answers, and its toast comes
after the write lands. Status, priority, due and recurrence edits and a requeue
carry Undo; resolving a followup note and cancelling an assignment ask first.
`sd work relink` and `sd work cancel` are shown for Copy only.

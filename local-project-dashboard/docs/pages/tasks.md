# Tasks

Tasks is the second page, at `/tasks` (sd:2124): the rows v1 `/backlog` reads,
as a board, an Eisenhower matrix and a list, filtered by kind and repo (two
comboboxes, sd:3441), priority and due. The board's Done column holds only items done in the 7 days before the read
(`status_since`, sd:5305); the list keeps every done item. `GET /api/tasks` (`tasks_screen.document`) gives each row its revision
and the statuses the library allows it. `GET /api/tasks/<id>`
(`tasks_screen.details`) splits the `sd task show --json` reading into status
history and notes, and adds the item's assignments and its external context. A
command that runs posts to the route v1 already answers, and its toast comes
after the write lands. The Details edit the title, description, status, priority, due date, repo and
quadrant in place (the description saves on leaving it, sd:3467), each change one write with its toast (sd:3439); a field the
row cannot take is off with the reason. A card's priority and due date are
dropdowns that make the same writes; Pick a date opens Edit (sd:3440). Status, priority, due, repo and recurrence edits
carry Undo; resolving a followup note and cancelling an assignment ask first.
Completing a repeating task asks first and names the outcome (sd:2250): the next
occurrence's date, or that sd sets it, or, for a rule sd cannot walk, that the
series ends, on the danger button. `sd work relink` and `sd work cancel` run
through the work routes after a confirm that takes the moved path or the reason;
OK stays off until it is typed (sd:2200), and `progress.work_controls` turns
each off where sd would refuse it. The page reads through the shell's reader,
`read.js` (sd:2484): a landed write rereads, a row the read no longer lists runs
no command, and a failed reread keeps the rows and says the write landed.
Tasks queues no runs since the runner stopped (sd:3041). An assignment's Cancel
posts `/api/assignments/<id>/cancel` with the revision the Details read.
Every page opens an item at `/tasks?row=<id>` (`shell.itemHref`, sd:3526); an
item the read does not list, a quiet report or one done before the week,
goes on to its item page, `/item/<id>`.

A vault base row (sd:3977) also carries its `base` (`fields.kind`), `stage` and `score`; other rows have none.
The Base filter and the list's Stage and Score columns show once a row has a base; Score sorts highest first.
A base row's Details show its stage select, declared fields, body and a link to its section of the vault mirror page.
`POST /api/items/<id>/stage` and `POST /api/items/<id>` send a base row to `records.edit_row` under its kind's rules;
every other row keeps its handler. The toast says when the render after the write failed.

An item's Experiments field (sd:4639) shows its designation for the team experiments database (pack sd:3982).
`POST /api/items/<id>/experiment` sends the values `sd task edit <id> --experiment` takes to `projection.designate`;
a pack without the projection turns the field off. Published links the row once a drain records its URL.

A v1 `/backlog` address answers 301 to `/tasks` with the query v1 read
(`tasks_screen.from_backlog`, sd:2356); a repository path becomes the label
Tasks filters on. v1's screen and its run selection were deleted in sd:2622;
`/classic/backlog` is a 404.

Realm, the first pick in the filter bar (sd:5753), narrows every layout to the rows of one realm or place:
Personal (the default), each realm `dashboard.realms` names, HOA or Home; All shows every row, and each choice
carries its count. Each row carries `realm` from `places.realm_of` and `area` from `places.area_of`, the rule the
HOA and Home pages' Tasks views use: the hoa checkout's items, or items with no repo. A report is in neither.
The address keeps a choice other than Personal as `?realm=<id>`; an older `#view=<id>` link lands on that choice.
Clear all keeps the Realm. Tasks declares no `PAGE_VIEWS`, so the rail lists no views under it.

Personal and Work carry Run sweep (sd:5096, sd:5097) in their subhead, with the realm's last run.
It posts `/api/tasks/sweep {"realm"}`, which starts `realm_sweep run <realm>` in the background, one run per realm
(`sd_dashboard/sweep.py`); a second start of a running realm is a 409. The run reads that realm's Google account, mail and Drive,
and for Work also Notion, Jira and GitHub; it files a `<day> - Work sweep` or `<day> - Personal sweep` brief and sd
followups, and saves Gmail drafts. It sends nothing and commits nothing. Its filters are the
`sweep.<realm>.*` settings in the workflow database, which Settings edits under Sweeps (sd:5109); `/api/tasks`' `sweeps` and `GET /api/tasks/sweep/status` read each realm's last run.
Each source keeps its own checkpoint: a done run that could not read one says "not read: jira", and that source
is read again from its older time next run.

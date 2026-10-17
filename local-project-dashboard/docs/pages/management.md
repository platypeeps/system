# Management

Management is at `/management` (sd:2118): the managed repositories with their
review, protection and runtime columns, the checkouts' git state, the lane,
recent assignments, sessions, services and launchd jobs. `GET /api/management`
(`management_screen.document`) builds one document from reads v1 already
makes. Each source is read on its own, so one that fails is a named reason, not
an empty list. Nothing fetches, pulls or calls GitHub: protection is the
nightly collector's reading. Each job carries its last run from the
cron-jobs wrapper's stamp file and the end of its log; a log that cannot be
read is no log, with the reason.
Writes go through the routes the server already answers: job retry and cancel,
service start, stop and restart, and the `sd-db repo` verbs, which refuse a
stale revision and, on a satellite, refuse with `HubOnly`.
A repo's page shows its Dependabot consent (sd:3984), the row's `dependabot`
column: yes or no, written with `sd-db repo dependabot`. A `platypeeps` repo
and one the sweep denies by name show that rule and no toggle.
It also shows `github_issues` (sd:4840): on or off, written with
`sd-db repo github-issues`; a database before schema 35 shows it unread, with no toggle. Open item goes to
the item's row on Tasks. The classic Jobs and Repos areas were retired in
sd:3402; Services, Sessions and Protection stay as palette screens.

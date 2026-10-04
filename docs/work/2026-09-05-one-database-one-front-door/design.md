# Design — one database, one front door, on one page

The `prd.md` beside this file is the decision trail, near two thousand lines
with fifty-two findings answered. This page is what the dashboard is checked
against: each requirement in a few sentences, written on 2026-09-05 at the
operator's request. When the two disagree, the `prd.md` is right and this
page is fixed.

## 1. The database and the library

One SQLite file, `~/.local/share/sd/sd.db`, in WAL mode, holds every piece
of state the pack has. Eleven tables on day one: `repo`, `item`,
`note`, `shadow`, `assignment`, `skill_use`, `trial`, `cost`, `provider`,
`bill`, `state`. Migrations have since added five, sixteen in `sd_db.schema.TABLES` and no other (2026-09-23). `report` and `dep` are `item.kind` values, not tables.
One Python library, `sd_db`, owns the schema and every write, and both faces,
`sd today` in the terminal and the dashboard, read through the same queries.
The library lives in `local-sd-db/` in this repository and is installed into
the pack's virtualenv and the dashboard's as a built copy at a tag.
The provider registry, `providers.yaml`, is identity and seed; enabled,
order and caps are rows the dashboard edits. Every status write leaves a
`status_change` note, so history is never lost. A nightly `sd-db-backup`
writes a dated directory under `~/Documents/sd-backups/`, inside the pair
the clone already copies, restores from it and checks the counts. Restore
fails closed: dispatch and the palette stop until `sd restore resume`.

## 2. The migrations

Five sources, and they are named because naming four nearly-right ones is
how this page misled a plan once already. `index.sqlite` to `shadow`; every
registered repository's `docs/work/*/prd.md` to `item`; the
decision register's open entries O27 to O29; the vault's Blog
Ideas and Topics to `item` rows of kind `idea`; and the two open GitHub
issues to `shadow`, staying open on GitHub. Report emails and cost are not
migration sources: `report` rows are written live under requirement 5 and
`cost` rows by `charge`. Each migration runs twice and reports the same
counts. A source is retired only after a backup taken after its import
restores and holds its rows.

## 3. Obsidian leaves the process

The vault becomes a knowledge base and stops being a process surface: no
ladders, no tasks, no process crons, nothing the pack reads. Its four process cron
jobs go, under item C's landing order and not this item's (requirement 3,
amended 2026-09-12); `vault-cleanup` and `vault-map` are maintenance of the
knowledge base and stay.

## 4. The runner is its own item

The runner is item D, `2026-09-05-the-runner-works-the-queue/`. This item
keeps the `assignment` table, the `runner` heartbeat row, `merge_policy` on
the repository row (`runner_merge` since migration 010), and the calls that create assignments and batches.
The runner landed in `local-sd-runner/` (#227, 2026-09-10); item D (sd:235) is `done`.

## 5. The dashboard is the front door

Five sections: Today, Backlog, Item, Skills, System. Every action is a
button with its command beside it. The Backlog has three views of the same
rows, a list, a board by status, and an Eisenhower matrix, and above them an
age histogram; Today carries the runner board, the timeline, the cost burn
and the provider scorecard. The front door is `https://<mac>.<tailnet>.ts.net`
through Tailscale Serve, the operator's login only, writes same-origin
with a login-bound token. Everything rendered that the dashboard did not
write is escaped text; Markdown is sanitized; a Content-Security-Policy
with no inline script backs both, and no page anywhere may frame the
dashboard, so a click on it is your click. The palette runs allow-listed
argument
vectors from `commands.yaml`, never a shell. The iPad is the reference
device; the iPhone gets Today, the Backlog list, the Item screen and the
palette, the rest postponed. A batch shows its cost bound before it runs.
Reports live as rows; an `attention` row still emails and now pushes, and
each delivery is marked on the row, so a send that died is retried by the
next job or the dashboard's tick.

## 6. Cost and the four numbers

One `cost` row per provider call, written by one library function.
Reservations are rows too, settled by call id. Bills are personal
subscriptions, prepaid balances, a plan with a meter, one capped provider bill.
Today shows four numbers: spent this month, reserved, room, and the
projection's crossing day.

## 7. Nothing lives only in context

Followups, decisions and proposals a session makes are `note` rows written
during the session, so a killed session loses nothing the next one needs.

## 8. herdr brings the agents back

After `herdr` restarts, each pane resumes the agent session it held, from
a small state file the wrapper maintains as panes start — not from a row.
Requirement 8 (`prd.md:1078-1084`) specifies the state file and criterion 17
names no row.

## 9. The backbone knows the new pieces

`personal.agent` builds the database, both agents and the serve route;
`doctor` checks them; a retention table prunes rows nightly after the backup
passed; the backups sit inside the verified pair. The runner's own pulse,
sleep and keys are with the runner, item D.

## 10. The fixture harness lands first

`sd_db.testing`: a fixture remote with a GitHub double, recording provider
doubles, a scripted provider, stubs for `launchctl`, `tailscale`, `curl`,
`caffeinate`, `lsof` and `local-notify`, and a fixture home. Every test in
the three items that names one of those uses it, and it is the first slice
to land.

## Landing order

B's harness, library and migrations; A's ship path, protection, closure and
the installer; B's dashboard read-only with the reports and the
`index.sqlite` retire; then B's dashboard writes, the palette, cost and the
backbone, **requirement 7's note surface, requirement 8's `local-herdr/`
wrapper, and the Skills and Providers write surfaces**; D's runner, after a
spike. Those last three were absent from this list, and from the
`prd.md`'s landing order, which named 5, 6 and 9 for that slice and
scheduled 7 and 8 in no slice at all — an open question, since
`prd.md:1167` says a slice claims only the criteria its text names.
Closed: slice 4 now names all five. The two dashboard slices are separate
because the second one writes, and requirement 6's cost and requirement 9's
backbone land with it — an earlier version of this line collapsed them and a
reader taking slice scope from this page lost both.

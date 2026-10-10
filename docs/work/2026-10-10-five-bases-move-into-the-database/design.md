---
title: Five vault bases move into the workflow database
created: 2026-10-10
item: sd:1145
---

# Design: five-bases-move-into-the-database

This page covers sd:1145 (Market Watch, Topics) and sd:1146 (Blog Ideas, Skill Proposals, Tips and Tricks).
The five bases share one importer, one store and one mirror, so one design serves both.
The items' rows hold status, progress and decisions; this page holds the shape.

## Problem

Five Obsidian bases hold rows as Markdown notes under `<vault>/System/Databases/`.
Each note is one row: frontmatter holds the fields, the body holds generated content.
The operator moves a row with a Meta Bind dropdown inside the note.
Scheduled vault routines read and write the notes, some through `sd store`, some by hand edits.
The workflow database and the dashboard see none of these rows: no list, no filter, no edit, no history.

Operator rulings: the rendered HTML goes to `<vault>/docs/dashboard/`, served by a `root|vault|...` line (2026-09-20, done).
The database is the source of truth, the dashboard edits rows, and the vault gets a read-only rendered mirror (2026-09-30).

Counted on 2026-10-10:

| Base | Notes | Ladder words held |
|---|---|---|
| Blog Ideas | 264 | 162 `inbox`, 95 `declined`, 7 `drafting` |
| Market Watch | 67 | 35 `active`, 18 `candidate`, 14 `paused` |
| Topics | 11 | 9 `active`, 2 `parked` |
| Skill Proposals | 10 | 8 `declined`, 2 `filed` |
| Tips and Tricks | 29 | 25 `inbox`, 3 `ready`, 1 `declined` |

Every note has a body of 764 characters or more and one or two Meta Bind `INPUT[...]` lines, 547 in total.
Notes outside the bases hold 1,233 wikilinks into them: 1,195 to Market Watch, 34 to Blog Ideas, 4 to Topics.

## Approach

One existing table holds the rows. The existing importer fills it once. One library module writes it after that.
The pack's `sd store` verbs keep their names and reach the rows through a second store driver.
The dashboard edits rows through its existing item routes. The library renders the vault mirror after each write.

**Rows: `item`, no migration.** `sources/vault.py` already lands plugin-declared vault kinds in `item`, keyed by `(source, external_id)`.
It was rehearsed in September and never run for real: the live database holds no `vault` rows. `SCHEMA_VERSION` stays 26.

| Column | Value |
|---|---|
| `kind` | `idea`, the importer's contract since migration 009; the plugin kind is `fields.kind` |
| `source`, `external_id` | `vault`, `<kind>:<path relative to the vault root>`; a new row gets the same shape from its base folder and title |
| `path`, `title` | the same relative path, where the Markdown mirror writes; frontmatter `title`, else the file stem |
| `stage`, `status` | the ladder word verbatim; the plugin's `workflow` map applied to it |
| `created_at` | `dateCreated` |
| `fields` | the frontmatter as JSON, plus `kind`, minus `status` (it is `stage`) and `mirror-hash` |
| `body` | `{"markdown": ...}` with the Meta Bind lines removed |

`kind = 'idea'` with `piece IS NULL` stays out of the writing pipeline, which selects `piece IS NOT NULL`.
A Blog Idea promoted to a piece keeps the `promoted_rows` contract (sd:1994). Nothing deletes a row; `declined` is a stage.

**Tasks, Today and counts (D6).** Base rows join the backlog and Today through the shared queries, with no new filter.
The status maps decide what lands where: Today reads `in_progress`, `ready_to_send` and due rows.
Standing states (an `active` Market Watch entity, an `active` Topic) map to `ready`, so they wait on Tasks and do not hold Today forever.
Work states (a `drafting` Blog Idea, an `approved` Tip) map to `in_progress` and `ready_to_send`, so they reach Today.
A base row moves by stage only: `workflow.allowed_statuses` answers `[]` for a `vault` row, and `sd task status` refuses it, naming the stage move.
So the Tasks board does not offer a drag that the stage map would contradict. Read on 2026-10-10, before and after the import:

| View | Now | After the import |
|---|---|---|
| Tasks, open rows | 395 | 671 (275 base rows and 1 quick note) |
| Tasks, done this week | 875 | 981 for 7 days (106 `declined` and `filed` rows), then 875 |
| Today | 51 | 58 (7 `drafting` Blog Ideas); 102 if standing states mapped to `in_progress` |

Base rows carry no priority and no due date, so the matrix places them as not urgent and not important.
The nightly prune parks only priority-4 rows, so it never parks a base row.

**Rejected: a new `record` table.** It needs migration 027, its own write routes, notes, revisions and backup handling.
`item` already has all of them, and the importer already targets it. A new `item.kind` was rejected too: migration 009 shows it needs a `sqlite_master` edit to change a name only.

**Kinds.** The writing plugin's manifest owns every kind (sd:1098). It declares `blog-idea`, `topic`, `tip` and `quick-note`, with maps for the first two.
It gains `market` (candidate `planning`, active `ready`, paused `blocked`, declined `done`; `slug` unique; human-only active, paused, declined)
and `skill-proposal` (filed and declined `done`, no transitions: an archive).
It gains maps for `tip` (inbox `planning`, accepted and ready `ready`, approved `ready_to_send`, published and declined `done`) and `quick-note` (new `planning`).
The `topic` map changes `active` from `in_progress` to `ready`, the standing-state rule above; `blog-idea` keeps its map.
The `blog-idea`, `tip` and `topic` templates lose their Meta Bind lines.

**Import.** `sd-db.sh import vault` keeps freeze, land and verify, with four changes.
It imports every kind with a `workflow` map, so all five bases come in with no importer table.
`freeze` and `rows` share one normalization (no Meta Bind lines, no `fields.status`, no `mirror-hash`), so the verify stays exact.
It reports per-kind note counts and the Meta Bind lines it dropped.
It refuses when the plugin's `store.driver` is `db`: the flip of that one value is the cutover, so no state row can disagree with it.

**Writes: `sd_db/records.py`.** `list_rows`, `get_row`, `add_row`, `edit_row` for rows whose `source` is `vault`.
`edit_row` takes the revision, checks `transitions`, sets `stage` and the mapped `status`, writes a `comment` note `Stage <from> -> <to> by <who>`,
and calls `_transition` on a status change, as `writing.stage` does. A field the kind does not declare refuses.
`rules` (fields, transitions, human-only, protected fields, floor, unique fields) comes from a new `rules` entry in `sd plugin list --json`, validated by the pack.
The pack's `db` driver enforces `human-only` and `protected-fields` for agents; the dashboard is the operator and may make those moves.

**Agents: the pack's `db` driver.** `DRIVERS` becomes `{"vault", "db"}`. Under `db`, `sd store list/get/add/set/set-section` call `sd_db.records`.
`status` maps to `item.stage`. A satellite reaches the hub's rows through the library's remote connection.

| Writer | Today | After |
|---|---|---|
| `sd store` callers: tips-weekly, the tips skill, the dashboard's quick notes, topic reads | vault driver | `db` driver; no caller change |
| market-watch routine | Edit tool on notes | `sd store add sdw.market`, `sd store set-section` |
| blog-idea-accept routine | frontmatter edit | `sd store set sdw.blog-idea` |
| tips-accept routine and its script | the script edits the note | the script calls `sd store set` and `set-section` |
| intel-weekly routine | hand-writes Blog Ideas notes | `sd store add sdw.blog-idea` |
| intel-brief routine | reads the Market Watch folder | `sd store list sdw.market --status active --full` |
| the operator | Meta Bind dropdowns | the dashboard |

The routines live in the vault, outside every repository; the sitting edits them and `sd task note` records each edit.

**Dashboard.** No Bases page: base rows are on Tasks, so a second list would repeat it.
`/api/tasks` adds three row fields, `base` (`fields.kind`), `stage` and `score`, null for other rows.
The Tasks list gains a Base filter and a stage column, and sorts by score; that covers the `.base` views.
`/item/<id>` for a `vault` row shows a stage select, the declared fields, the body and a link to its mirror section.
`POST /api/items/<id>/stage` and `POST /api/items/<id>` send a `vault` row to `records.edit_row`; other rows keep their current handlers.
Health shows the mirror heartbeat and a Render mirror control, `POST /api/records/render`.

**Mirror.** `records.render(connection, vault, kinds=None)`, also `sd-db.sh records render`, writes two read-only copies from rows only.
The Markdown mirror sits at each row's `path`, with `status` from `stage` and a `mirror-hash` of the rest of the file.
The 1,233 wikilinks keep resolving and the `.base` files stay as read-only views.
The renderer overwrites a file only when its `mirror-hash` matches its content, or it has none and its import payload equals the row's.
Any other file is a hand edit, from Obsidian or a phone through Obsidian Sync: it stays, and the render reports it.
A base file with no row is reported and kept.
The HTML is one page per base under `<vault>/docs/dashboard/`: a table of every row, then a section per row not `declined` or `retired`.
Bodies go through `publication_render.md_to_html`. A remote image becomes a link first, since `md_to_html` marks it missing; a `[[wikilink]]` renders as text.
The page obeys the Documents CSP: inline `<style>`, system fonts, no script, no remote resource.
The render runs after each committed `records` write, for that kind, and on demand. It writes a file only when its bytes differ, by temporary file and rename.
It runs on the hub only (`database.refuse_hub_only`) and refuses while a `restore` state row is unresolved.
It records a `heartbeat` state row, key `records-mirror`, body `{at, ok, written, refused, reason}`.
A write never fails for its render: it prints `mirror not updated: <reason>` and the heartbeat says `ok: false`. The verb exits 1 on a refusal or an error.

**New mechanisms**, and what each replaces: `sd_db/records.py` replaces the vault driver's note writes;
three `/api/tasks` row fields and a Base filter replace the `.base` views as the editing surface;
the pack's `db` driver and `rules` replace the vault driver for the writing plugin;
`sd-db.sh records render` and `POST /api/records/render` are the catch-up after a failed render, with no prior equivalent;
`mirror-hash` is the hand-edit check, kept in the file it guards; `market` and `skill-proposal` declare notes that had no kind.
Reused: `item`, the `vault` source, the `heartbeat` state kind, the item routes, `md_to_html`, the `root|vault` line.
No migration, config key or scheduled job is added. Retired: the 547 Meta Bind lines and the routines' hand edits of notes.

## Non-goals

- Other vault bases: Learning, Prompts, the followup bases, TaskNotes.
- The writing pieces under `content/`; they are rows already (`source = 'writing-piece'`).
- Deleting the pack's `vault` store driver; a follow-up item does that once no plugin names it.
- Editing a row from Obsidian after the cutover.

## Acceptance criteria

- [ ] `sd-db.sh import vault` prints, per base, `<n> <kind> note(s)` equal to `find <base folder> -name '*.md' | wc -l`, and `verified`.
- [ ] A second import before the flip reports `0 inserted, 0 updated`.
- [ ] `SELECT json_extract(fields, '$.kind'), count(*) FROM item WHERE source = 'vault' GROUP BY 1` equals the per-base file counts of the same sitting.
- [ ] The dropped Meta Bind count equals `grep -c 'INPUT\['` summed over the bases; `SELECT count(*) FROM item WHERE source = 'vault' AND body LIKE '%INPUT[%'` is 0.
- [ ] After the flip, `sd-db.sh import vault` exits non-zero and names the `db` driver; `sd store list sdw.market --status active` lists the active count of the sitting.
- [ ] `sd-db.sh records render` exits 0 with 0 refused; a second run writes 0 files.
- [ ] A dashboard stage move writes a `comment` note, and a `status_change` note when the status changes.
- [ ] After the import, `reads.backlog_items` holds one open row per open base row more than before (276 on 2026-10-10), and `reads.today_items` one row per `in_progress` or `ready_to_send` base row (7).
- [ ] `sd task status <id> done` on a `vault` row exits non-zero and names the stage move; its `/api/tasks` row has `allowed: []`.
- [ ] `sd-db.sh test`, `dashboard.sh test`, the pack's and the writing plugin's `make check` pass with 0 failures.

## Decisions

Made by the operator on 2026-10-10; the notes on sd:1145 and sd:1146 hold them.

- D1: rows live in `item`, with no migration.
- D2: the vault keeps a Markdown mirror at the notes' paths, plus the HTML pages.
- D3: one HTML page per base; declined rows are table lines only.
- D4: Quick Notes moves to the database with the flip.
- D5: Skill Proposals is an archive kind in the writing manifest.
- D6: base rows join the backlog and Today.

## Failure table

| Step | State moved | Failure | Recovery | Test |
|---|---|---|---|---|
| import | rows written | stopped during land | rerun: the land upserts by identity; verify names any row missing | `test_vault_import`: stop after N upserts, rerun, verify clean |
| import | none | a ladder word the map does not name | the sitting refuses whole and writes nothing; declare it, rerun | the existing refusal test, plus one per new kind |
| import | rows written | a note changed between freeze and verify | verify names it; no `verified` row; rerun | the existing seeded-difference test |
| import to flip | rows hold the import; driver `vault` | a writer edits a note | rerun the import just before the flip; the first render refuses a note that differs from its row | `test_records_render`: a changed note with no `mirror-hash` is refused |
| flip | driver `db` on the hub | the satellite has not pulled the flip and writes a note | the sitting stops the satellite's jobs too; the render reports a file with no row; the operator adds it with `sd store add` and deletes the file | `test_records_render`: a file with no row is reported and kept |
| flip to render | driver `db`; no mirror | the operator moves a dropdown in an old note | the first render refuses that file; the operator moves the row in the dashboard and deletes the file | same test as row 4 |
| after flip | driver `db` | someone runs `sd-db.sh import vault` | the import refuses and names the driver; no row changes | `test_vault_import`: `db` driver refuses, fail-first |
| render | some files written | stopped, or a write error such as a Documents access denial | rerun; each file is a rename, so none is half written | `test_records_render`: error after N files, rerun completes |
| any write | row committed | its render fails | the row stands; heartbeat `ok: false`; the next write or Render mirror catches up | `test_records`: a forced render error leaves the row and a failed heartbeat |
| any write | row committed | it comes from a satellite | the satellite does not render; the hub's next render covers it | `test_records_render`: hub-only skip |
| after render | mirror written | a hand edit to a mirror file | the render refuses that file and reports it | `test_records_render`: a `mirror-hash` mismatch is refused |
| after render | rows restored to an older snapshot | a render would write older content over the mirror | the render refuses until `sd restore resume`; re-apply edits from the mirror first | `test_records_render`: an open restore refuses |
| rollback | driver back to `vault` | rows are newer than the notes | render before the revert; mirror files read back as the rows' payloads; import again | `test_records_render`: render then freeze round-trips every kind |
| routine edit | routine text | an old routine still edits a note by hand | the render refuses the edited file; the next-day check names it | the hand-edit test |

## Slices

Each slice writes its tests first; each test fails on the base branch. Each leaves the system working as named.

1. **S1 importer and stage-only moves** (system: `sources/vault.py`, `workflow.py`). Every declared kind, the shared normalization, per-kind counts, the `db` refusal,
   `allowed_statuses` and `sd task status` refusing a `vault` row, the `workflow.py` guard message.
   Leaves working: the vault stays the source; `import vault` can be rehearsed on all five bases; no live row changes.
2. **S2 `records` module** (system). The read and write functions, `rules_for(kind)` through `sd plugin list --json`.
   Leaves working: nothing calls it yet; the remote surface test still passes.
3. **W1 kinds and maps** (writing plugin). `market`, `skill-proposal`, four new maps, `topic` active to `ready`, two `store.bases` folders, templates without Meta Bind. The driver stays `vault`.
   Leaves working: `sd store add sdw.market` writes vault notes; `sd plugin list --json` shows six workflow kinds.
4. **P1 `db` driver** (command pack). The `db` branch for every `sd store` verb, and `rules` in `sd plugin list --json`. Needs S2 installed.
   Leaves working: every plugin still on `vault` behaves as before.
5. **S3 mirror renderer** (system). The Markdown and HTML mirror, the overwrite rule, the hub and restore refusals, the heartbeat, `sd-db.sh records render`, the render after each write.
   Invoke `hallmark` and apply the UI foundation before the HTML design. Leaves working: no row exists yet, so nothing renders on the live vault.
6. **S4 dashboard** (system). The `/api/tasks` fields, the Tasks Base filter and stage column, the item panel, the route dispatch, the Health heartbeat and `POST /api/records/render`.
   Invoke `hallmark` first. Leaves working: Tasks is unchanged until the sitting adds rows; pieces and tasks use their current handlers.
7. **W2 the flip** (writing plugin): `store.driver` becomes `db`. It merges only inside the sitting.

**The sitting**, on the hub. Record each step's decisive line in `sd task note 1145`.

1. Uninstall the six routine jobs on each machine that runs them (`cron-jobs.sh uninstall <job>`), in a window with none due (they start at 03:00, 06:12 and 12:00). Quit Obsidian on the hub.
2. `sd-db.sh backup`; it reports the restore proof.
3. `git -C "$OBSIDIAN_VAULT" status --porcelain -- System/Databases` prints nothing; note `HEAD`.
4. `sd-db.sh import vault` twice; check the import criteria.
5. Merge W2 through the lane; pull the writing plugin on the hub and the satellite.
6. `sd-db.sh import vault` refuses; `sd store list sdw.market --status active` lists rows.
7. `sd-db.sh records render`: exit 0, refused 0.
8. Edit the vault routines to the After column. The `.base` files stay.
9. Install the six jobs again (`cron-jobs.sh install <job>`).
10. Next day: `cron-jobs.sh status <job>` exits 0 for each; the heartbeat is `ok`.

Before step 5 only rows changed: rerun, or restore the step 2 backup.
From step 5 on, roll back: render if the hub can, revert W2 and pull it, then `sd-db.sh import vault` brings rows level with the notes.
After the sitting, file one pack item to remove the `vault` driver once no plugin names it.

## Risks

- The import proof ties its pass to the source by the `verified` row's source hash, and to the vault by the `HEAD` noted in step 3. A count alone proves nothing; the verify compares identity and content.
- The render check ties a pass to each file by its own `mirror-hash`. A hand edit that also rewrites the hash passes; that needs intent, and the threat model accepts it.
- The flip and the routine edits are separate steps. A routine that runs between them writes a vault note; the render reports it, and the jobs stay uninstalled until step 9.
- Obsidian Sync can carry a phone edit into a mirror file at any time. The render refuses it rather than losing it; the operator still has to act on the report.
- Rendering a whole kind per write reads about 2 MB of Markdown for Market Watch. Accepted; if a write gets slow, the render narrows to the row and its page.
- Tasks grows from 395 to 671 open rows, about 70 percent. Accepted by D6; the Base filter narrows it.
- The import's opening notes carry the sitting's time, so every base row's age starts at 0 that day, and Notes shows 382 openings.
  The 106 done rows sit in the done-this-week window for 7 days. Accepted: `create_item` keeps the real time on purpose.
- A standing state mapped to `ready` never reaches Today. If the operator wants active entities on Today, the map changes in the writing manifest, not here.
- `kind = 'idea'` for rows that are not ideas reads oddly in raw SQL. Accepted for no migration; `fields.kind` names the real kind.
- The six vault routines change outside any repository and outside review. Each edit is recorded on the item; the next-day check is the test.

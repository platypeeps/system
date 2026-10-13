---
title: Routine briefs move into the workflow database; the vault keeps a channel copy
created: 2026-10-13
item: sd:3459
---

# Design: briefs-in-database

Write this page only when the shape needs agreement before the code.
The item's row holds status, progress and decisions; this page holds the shape.
It holds no code. It read `origin/main` d00a9cc of this repository, the command pack's `lib/sd_db` at 403abbf,
the live workflow database read-only, the vault's briefs folder and the two routines that write it.

## Terms

- **Routine brief**: a Markdown brief that a scheduled vault routine writes. This page means this one.
- The other three meanings stay as they are: a research-kit brief (a document published to `Briefs/<repo>/`),
  the brief digest mail (`obsidian-review -k brief`), and the session brief (`sd_db/brief.py`).
- **Stem**: a brief's file name without `.md`, `<YYYY-MM-DD> - <Series>`, such as `2026-10-13 - Daily Intel Brief`.
  Wikilinks name a brief by its stem.
- **Vault copy**: the Markdown file the Obsidian channel writes from a brief's row.

## Rulings this design follows

- Obsidian is a publication target and knowledge base only; tracking lives in sd (sd:3462 and this item, 2026-10-12).
- Channels are chosen per repo through the `channels` setting on the repo row (sd:3471, 2026-10-13).
  The channels design names this item: the routines write rows, and the vault gets briefs only as an Obsidian channel
  copy, under the folder the system repo's setting names.
- The brief digest mail and its queues go with sd:3558, not here (note 16048 on this item).

## Problem

Two vault routines write briefs as notes into `<vault>/System/AI Generated/Briefs/`, and nothing else holds them.
The dashboard's Briefs page (`local-project-dashboard/sd_dashboard/briefs_screen.py`) and the Research > Resources >
Briefs view read those notes through a child process (`sd_tile.py briefs-rows`), because macOS can hold the vault
behind a prompt. So the page shows a refusal when the vault is blocked, and the database has no brief to link,
search or back up.

Counted on 2026-10-13:

| Series | Notes | Dates | Writer |
| --- | --- | --- | --- |
| Daily Intel Brief | 65 | 2026-07-26 to 2026-10-13 | `intel-brief` routine on the hub, daily 06:12 |
| Weekly Intel Synthesis | 15 | 2026-07-26 to 2026-10-10 | `intel-weekly` routine on the hub, Saturday 05:30 |
| Work Daily Intel Brief | 10 | 2026-07-26 to 2026-08-11 | `intel-brief` on the work satellite (Work series) |
| Work Weekly Intel Synthesis | 1 | 2026-07-28 | `intel-weekly` on the work satellite |
| Eight June series (Intel Lead, Daily Operation Report, News and Mentions, Sev-1 Watch) | 29 | 2026-06-16 to 2026-06-26 | retired routines |

That is 120 notes, 2.3 MB. Every note has frontmatter; none has a `mirror-hash` line.
Notes outside the folder hold 427 wikilinks to briefs by stem, 206 of the linking notes in Blog Ideas.
The `Followup - Briefs` base lists briefs by their `related` frontmatter.

What reads the notes today, besides the dashboard:

- `intel-brief` reads its previous brief and the newest Intel Lead brief, to avoid repeats.
- `intel-weekly` reads the last 7 days of both daily series and its previous synthesis.
- Both run the vault scripts `brief-tools.py` (`wordcount`, `links`) and `adversarial.py --target <path>` on the
  note they wrote, then mail the brief.

## Approach

### Where briefs live: `item` rows of kind `report`

One brief is one `item` row. No table, column or migration is added; `SCHEMA_VERSION` stays where channels C1 puts it.

| Column | Value |
| --- | --- |
| `kind` | `report`: a brief is a routine's output, like the run report its job already files |
| `source`, `external_id` | `brief`, the stem; the unique `(source, external_id)` index makes a stem one row |
| `title` | the stem |
| `repo` | the system checkout's key, given as `--belongs-to`; its `channels` setting places the vault copy |
| `status` | `done` when filed: a brief is delivered, not work |
| `fields` | `{"attention": false, "brief": {"series", "day", "at", "words", "lead", "sha256"}}` |
| `body` | `{"text": <the whole Markdown file, frontmatter included>}`, the shape every report body has |

`series` and `day` come from the stem. `at` is when the row was first filed (UTC); an import uses the note's
modification time. `words` and `lead` (200 characters, whitespace folded) are counted from the body without its
frontmatter, at write time, so a page read never parses 2 MB. `sha256` is of `body.text`.

Why `report` rows keep briefs off work views with no new filter: `reads._NOT_A_QUIET_REPORT` drops a report whose
`fields.attention` is not `true` from the backlog, the matrix and the done-this-week window; the 815 run reports on
2026-10-13 already ride that rule. Today (`reads.today_items`) takes `in_progress`, `ready_to_send` and due rows, so a
`done` brief with no due date never reaches it. Retention settles only `planning` reports and deletes no item.
So a brief must stay `done`: nothing may move it.

Five places that act on `kind = 'report'` must skip or refuse `source = 'brief'`, each with a fail-first test:
`reporting.reports` (the Reports list) skips it; `reporting.acknowledge` and `reporting.reopen` refuse it, so no verb
moves a brief to `planning`; the dashboard's `reports_screen.report_controls` draws no control for it; and
`fix_session` offers no debug session for it.

The library module is `sd_db/routine_briefs.py`; `sd_db/brief.py` is the session brief and keeps its name.

### Who writes them, by routine

One verb group under the existing `sd reports`, so no new top-level command:

- `sd reports brief add <file> --belongs-to <checkout> [--replace]` files one brief.
  The file name must be `<YYYY-MM-DD> - <Series>.md`, the file UTF-8 with frontmatter, at most
  `reporting.MAX_REPORT` bytes; anything else is refused before a write (exit 2).
  A new stem creates the row. The same stem with the same `sha256` prints `unchanged` and writes nothing.
  The same stem with other bytes is refused (exit 1) unless `--replace`; `--replace` updates `body` and `fields`
  and adds one `comment` note, `Brief replaced by <who>: <old> -> <new> words`, in one transaction.
- `sd reports brief list [--series S] [--since DAY] [--json]` prints day, series, stem and words, newest first.
- `sd reports brief show <stem>` prints the stored Markdown.
- `sd reports brief import <folder> --belongs-to <checkout> [--replace]` files each `*.md` in a folder as `add`
  does, with each note's modification time as `at`, but renders nothing. It strips a `mirror-hash` line before
  comparing, then reads every row back and prints `verified <n>` when each `sha256` matches. `--replace` takes a
  differing note only when its `mirror-hash` matches its content, that is, a copy this render wrote; it refuses a
  hand-edited copy by stem. The sitting runs `render` once after it.
- `sd reports brief render` writes the vault copies (below).

A satellite runs `add`, `list` and `show` over its existing remote connection; the hub runs every statement.

| Writer | Today | After |
| --- | --- | --- |
| `intel-brief`, hub | reads the previous note; `Edit` writes the note in the vault; gate; mail | `list` and `show` read the previous brief; `Write` puts the draft in the staging folder; `add`; gate and scripts on the draft; `add --replace` with the findings section; mail |
| `intel-weekly`, hub | reads 7 days of notes; writes the note | the same, with `list --since` |
| Both, Work series on the work satellite | as above, in that machine's vault | as above; the hub writes the vault copy |
| Retired June series | nothing | nothing; the import brings the 29 notes in as history |

The staging folder is `~/.cache/sd-briefs/` on each machine: the draft must be a file, because `adversarial.py`
and `brief-tools.py` take a path, and an unattended run cannot pipe.
A run first re-files each draft there from the last 7 days with `add --replace`: equal bytes print `unchanged`,
and a draft that a stopped run left newer than its row (stopped before `add`, or after the findings and before
`add --replace`) replaces it. One machine writes one series, so a draft is never older than its own row.
Each routine's `Edit` grant on the vault folder becomes a `Write` grant on the staging folder and `Bash` grants on
`sd reports brief add`, `list` and `show`. The routines live in the vault, outside every repository; the sitting edits them
and records each edit with `sd task note 3459`.

### How the Briefs page reads them

`briefs_screen.collect` stops running the child. It calls `routine_briefs.rows(connection, limit=200)`, which selects
`source = 'brief'` rows newest first by `fields.brief.day` and `at`, without `body`.
The page keeps its document shape. A row's `id` stays the stem. A new `item` field carries the item id, and the
row's primary action is Read, which opens `/item/<id>`; there the brief's Markdown renders through the existing
`markdown` helper, as a piece's draft does. `static/briefs.js` keeps `item` through its row projection and draws
Read for every row. Open in Obsidian stays a second action, shown only when the copy heartbeat is `ok`, names a
folder, is newer than the row's `updated_at`, and does not list the stem as refused; else `open` and `rel` are null.
A link is then a copy the render reported writing, not a guess from the folder.
`reader.source` becomes `workflow database`, and `reader.copies` carries the heartbeat's state: `ok`, `failed` with
its reason, `off`, or `unknown` when no heartbeat exists. A failed read is `state: error` with its cause, never an
empty page. The watchdog stays unknown; Non-goals say why.

Health gains one row from the same heartbeat, beside the credentials heartbeat row in `health_screen.py`: `failed`
or a heartbeat older than 2 days is a problem; `off` is quiet. A failed render is then visible though `add` exits 0.

The Research > Resources > Briefs view goes: the Briefs page shows the same rows.
That deletes `collectors.collect_briefs`, `sd_tile.tab_briefs` and `tab_brief_rows`, the `briefs` and `briefs-rows`
tile entries, `VIEW_SECONDS["briefs"]`, the `VIEWS` entry, and their tests and docs. The slice's report quotes the
search that shows no caller is left.

### What happens to the existing vault notes

The sitting imports all 120 notes. Each note then is the vault copy of its row, at the same path.
The first render adds a `mirror-hash` line to each, its only change. Wikilinks, the `Followup - Briefs` base and
the Blog Ideas `source-brief` links keep resolving, because the stem and the frontmatter stay.
Nothing deletes a note. A note that is not a brief (no stem shape, no frontmatter) is refused by name and left.

### Whether a vault copy is still published

Yes, through the system repo's `channels` setting, as any document of that repo is:

- The render reads `repos.repo_channels(<system key>)` (channels C1) once per run.
- `obsidian` with a `folder` writes copies there. The sitting sets
  `sd-db repo channels <system checkout> obsidian="System/AI Generated/Briefs"`, so no path moves.
- A setting that omits `obsidian` writes no copy; copies already written stay, as the channels design rules.
- No setting (NULL), or `obsidian: {}`, means the channel default, `Briefs/<repo>`, as for a research document.
  The sitting sets the folder before the first render, so the default is never used by accident.
- A setting that cannot be read (hub unreachable, build mismatch, unresolved restore, a value the validator
  refuses) writes no copy and names the cause: unknown is not "no setting".

The Briefs page and the item view read the rows, so the vault copy is optional for reading.
Q1 asks the operator to confirm it stays on; the design works either way.

**The render.** `routine_briefs.render(connection, vault)` writes one file per row, `<folder>/<stem>.md`, holding
`body.text` with `mirror-hash: <sha256>` added as the last frontmatter line. It writes a file only when its bytes
differ, by temporary file and rename. It overwrites a file only when the file's own `mirror-hash` matches its
content, or it has none and its content equals the row's. Any other file is a hand edit: it stays and is reported.
A `.md` file in the folder with no row is reported and kept. This is the five-bases mirror rule (sd:3976): whichever
of sd:3976 and B2 lands first writes the shared function, and the other calls it.

The render runs on the hub only (`database.refuse_hub_only`) and refuses while a `restore` state row is unresolved.
It holds a lock file beside the database, through `runner_journal.lock` with a 60-second wait, and reads the rows
only after it holds the lock, so a render that read older rows can never write after one that read newer rows.
A wait that runs out is a failed render with that reason. It records one `heartbeat` state row, key
`brief-copies`, body `{at, ok, state, folder, previous_folder, written, refused, reason}`, where `state` is `ok`,
`failed` or `off`, and `refused` lists at most 50 stems. A hub `add` renders the whole set after its commit; a satellite's brief reaches the vault at the hub's next `add` or
`render`, at most a day later. A failed render never fails `add`: it prints `vault copy not written: <reason>` and
exits 0, because the row is the deliverable. `sd reports brief render` exits 1 on any refusal or error.

**New mechanisms**, and what each replaces: the `brief` source on `report` rows replaces the vault folder as the
store; `sd reports brief` replaces the routines' `Edit` writes and the child reader; `routine_briefs.render` replaces
the routines as the vault writer; the `brief-copies` heartbeat is how a failed render shows, with no prior equivalent.
Reused: `item`, `note`, the unique external-id index, the quiet-report rule, `heartbeat`, the `channels` setting,
the remote connection, the five-bases mirror rule, `runner_journal.lock`, the dashboard's `markdown` helper and its
Health heartbeat rows.
Retired: `collect_briefs`, both brief tiles, and the Resources Briefs view.

Rejected:

- **A `brief` table.** It needs a migration, its own write path, notes and backup handling; `item` has them all.
- **A new `item.kind`.** Migration 009 shows that widening the kind CHECK means editing `sqlite_master`.
- **`state` rows.** `state` holds operational records under a closed kind list, and has no notes or revisions.
- **The job's own run report row.** It is filed after the run exits, from the run log, while the brief must exist
  before the mail; and clean run reports settle and fold repeats, which a brief must not.
- **Keep the notes and import them nightly.** The vault would stay the store, against the ruling.
- **An `sd store` kind.** It needs a plugin to own the kind and the five-bases `db` driver (sd:3975), unbuilt;
  a brief has no workflow ladder for a manifest to declare.

## Non-goals

- The brief digest mail and the review queues (sd:3558), and the routines' own mail: email is a later channel.
- The page's watchdog: the run reports of `intel-brief` and `intel-weekly` already hold failures and cadence, and
  reading them is its own item.
- Unread or follow-up state on a brief.
- Deleting the `Followup - Briefs` base, the Blog Ideas base or any vault copy.
- The `aaif-weekly-brief` routine: it publishes to Notion, not to this folder.

## Failure table

Filing moves state in steps: draft, row, vault copy, heartbeat. The sitting moves the store in steps too.

| Step | State moved | Failure | Recovery | Test |
| --- | --- | --- | --- | --- |
| add: read the draft | nothing | bad file name, no frontmatter, not UTF-8, over `MAX_REPORT` | refused before any write, exit 2, naming the rule; the draft stays | `test_routine_briefs`: each refusal leaves no row, fail-first |
| add: commit | the row and its opening note | the hub is unreachable from a satellite | exit 1, no row; the next run files the draft left in the staging folder | `test_routine_briefs`: an unreachable remote exits 1 and writes nothing |
| add: commit | the row, maybe | the commit's answer is lost (`TransactionLost`, `UnknownOutcome`) | exit 1; a rerun finds the row and prints `unchanged`, or creates it; never two rows for one stem | `test_routine_briefs`: stop after commit, rerun prints `unchanged`, one row and one opening note |
| add: same stem | nothing | other bytes and no `--replace` | refused, exit 1, the row unchanged | `test_routine_briefs`, fail-first |
| add --replace | `body`, `fields`, one note | an error between the update and the note | one transaction: the row stays as it was | `test_routine_briefs`: a forced error after the update leaves the old `sha256` and no note |
| routine: between `add` and `add --replace` | the row holds the draft without findings; the draft has them | the run stops after appending the findings | the next run re-files the last 7 days of drafts with `--replace`, so the newer draft replaces the row | `test_routine_briefs`: a draft newer than its row is taken by `--replace` and an equal one prints `unchanged`; the routine text is checked by the next-day step |
| add, hub: render after commit | some vault copies | vault blocked by macOS, a write error, the lock wait runs out | the row stands; `vault copy not written: <reason>`; exit 0; heartbeat `failed`; Health shows it; the next `add` or `render` catches up | `test_routine_briefs_render`: a forced write error leaves the row and a failed heartbeat; `test_health`: a failed and a missing heartbeat each show, fail-first |
| render, twice at once | files | a hub `add` and a hand `render` overlap; one read rows before the other's replace | the lock serializes them and each reads rows after taking it, so the last writer read the newest rows | `test_routine_briefs_render`: a render holding the lock while a replace commits; the second render writes the new bytes, fail-first |
| render | some files written | stopped midway | each file is a rename, so none is half written; rerun writes the rest | `test_routine_briefs_render`: error after N files, rerun completes |
| render | nothing for that file | a hand edit to a vault copy | the file stays and is reported; refused count; `render` exits 1 | `test_routine_briefs_render`: a `mirror-hash` mismatch is kept, fail-first |
| render | nothing | it runs on a satellite | skipped with `HubOnly`; the hub's next `add` or `render` writes the copy | `test_routine_briefs_render`: the hub-only skip |
| render | nothing | a restore is unresolved | refused with the reason in the heartbeat | `test_routine_briefs_render`: an open restore refuses |
| restore | rows older than the vault copies: a stem missing, or an older body | a resumed render would write older bodies over newer copies | before `sd restore resume`: `import <folder> --replace` files missing stems and replaces rows from copies whose `mirror-hash` matches (copies the render wrote); hand-edited copies are refused by stem for the operator; then resume and `render` writes 0 files | `test_routine_briefs`: a snapshot holding an older body of one stem and lacking another; the import levels both; the render then writes nothing, fail-first |
| render: read the setting | nothing | hub unreachable, build mismatch, a refused value | no copy written, the cause named; never the default folder | `test_routine_briefs_render`: each cause writes nothing, fail-first |
| render: setting omits `obsidian` | nothing | the operator turned the channel off | no copy; existing copies stay; heartbeat says `off` | `test_routine_briefs_render` |
| render: folder changed | copies in the new folder | old copies look current | the render names copies left in the previous folder, which the heartbeat recorded; it deletes none | `test_routine_briefs_render`: a folder change reports the old copies |
| import | some rows | stopped midway | rerun: filed stems print `unchanged`, the rest are filed; `verified` only when every row matches | `test_routine_briefs`: stop after N files, rerun, `verified` |
| import | nothing for that note | a note changed between two imports | refused by stem; the sitting reruns with `--replace` for it | `test_routine_briefs`: a changed note is refused without `--replace` |
| import over copies | nothing | a later import reads rendered copies (rollback, restore) | the importer strips the `mirror-hash` line, so a level copy is `unchanged` | `test_routine_briefs`: render, then import, files nothing, fail-first |
| sitting: routines edited | the routine text | an old routine still writes a vault note | the render reports a file with no row; the operator runs `add` on it | `test_routine_briefs_render`: a file with no row is reported and kept |
| sitting: older pack on a machine | nothing | a satellite on a pack before B1 runs a routine | `sd reports brief` is unknown and the remote refuses a build mismatch; the run reports it; the sitting pulls the pack on every machine before the routine edits | none: the build handshake test covers it |
| page: read rows | nothing | the database read fails | `state: error` with the cause; no rows shown as zero | `test_v2_briefs`: a raising reader gives `error`, fail-first |
| Reports list and verbs | a brief's status | a brief shows as a run report; `sd reports reopen` moves it to `planning` | the list skips it; `acknowledge` and `reopen` refuse it; the dashboard draws no control and no debug session | `test_reporting`: list, acknowledge and reopen on a brief, each fail-first; the reports screen and fix-session tests |
| rollback: routines or B3 | nothing yet | a render failed or refused, or the channel is off, so copies lag the rows | stop until the copies are current: set the `obsidian` folder if it was off, run `render` until exit 0 and `refused 0`, then check `render` writes 0 files; only then revert. After reverting the routines, `import` the folder to pick up their new notes | `test_routine_briefs_render`: a refused file exits 1; the import-over-copies test |
| rollback: B3 | nothing | the old page reads the folder again | the copies are current by the row above; the old reader reads them, `mirror-hash` line and all | none: the old reader reads any `.md` |
| restore to before the import | rows gone | the vault copies remain | `import` again; copies read as level | the import-over-copies test |

## Slices

Each slice writes its tests first; each behaviour test fails on `origin/main`.
B1 and B2 need channels C1 (sd:3462) merged and migrated; B2 needs it for the setting read.

1. **B1 rows and verbs** (pack). `sd_db/routine_briefs.py`: `add`, `replace`, `rows`, `show`, `import_folder`;
   `sd reports brief add|list|show|import`; `reporting.reports` skips briefs, `acknowledge` and `reopen` refuse them.
   Tests: `test_routine_briefs`, the `test_reporting` rows. Leaves working: no row exists; the routines and the page are unchanged. About 4 hours.
2. **B2 vault copies** (pack). `routine_briefs.render` with the mirror rule (shared with sd:3976), the setting read,
   hub-only and restore refusals, the lock, the `brief-copies` heartbeat, `sd reports brief render`, the render after a hub
   `add`. Tests: `test_routine_briefs_render`. Leaves working: with no rows, a render writes nothing. About 5 hours.
3. **B3 dashboard** (system). The pin bump; `briefs_screen` reads rows; `briefs.js` keeps `item` and draws Read;
   the Obsidian action gated on the heartbeat; `reader.copies`; the Health row; the item view's Markdown for a
   brief; `report_controls` and `fix_session` skip briefs; the Resources Briefs view and its readers deleted;
   `docs/pages/briefs.md` and `docs/pages/research.md` updated. Invoke `hallmark` first.
   Tests: `test_v2_briefs` rewritten for rows, including a row with no vault copy that still offers Read; the
   Health, reports screen and fix-session rows. Merge it after sitting
   step 5, so the page never shows an empty store. Leaves working: the page lists every imported brief. About 5 hours.

**The sitting**, on the hub, about 1.5 hours. Record each step's decisive line with `sd task note 3459`.

1. Uninstall `intel-brief` and `intel-weekly` on each machine that runs them (`cron-jobs.sh uninstall <job>`),
   outside 05:30 to 07:00. Pull the pack with B1 and B2 on every machine.
2. `sd-db backup`; it prints the restore proof. `git -C "$OBSIDIAN_VAULT" status --porcelain -- "System/AI Generated/Briefs"`
   prints nothing; note `HEAD`.
3. `sd-db repo channels <system checkout> obsidian="System/AI Generated/Briefs"`.
4. `sd reports brief import "$OBSIDIAN_VAULT/System/AI Generated/Briefs" --belongs-to <system checkout>` twice:
   the first prints `120 filed` and `verified 120`, the second `0 filed`.
5. `sd reports brief render`: exit 0, `refused 0`; then a second run writes 0 files.
6. Merge B3 through the lane; check the page lists 120 briefs.
7. Edit both routines and their grants to the After column, on each machine.
8. Install both jobs again. Next day: `sd reports brief list --since <today>` names the day's brief, and
   `cron-jobs.sh status intel-brief` exits 0.

Before step 7, rollback is free: the vault notes are still written by the routines and the rows are extra.
After it, roll back only once `render` exits 0 with `refused 0` and a second run writes 0 files; then revert the
routines' text and run one `import`. If the render cannot get there, stop: the rows stay the store.

## Acceptance criteria

- [ ] The pack's `python3 -m unittest tests.sd_db.test_routine_briefs tests.sd_db.test_routine_briefs_render`
      passes with 0 failures.
- [ ] Step 4 prints `verified 120` on the first import and `0 filed` on the second.
- [ ] `SELECT count(*) FROM item WHERE source = 'brief'` equals `ls <folder>/*.md | wc -l` of the same sitting.
- [ ] After step 5, `git -C "$OBSIDIAN_VAULT" diff --stat -- "System/AI Generated/Briefs"` shows one added line
      per note and no other change.
- [ ] `reads.backlog_items` and `reads.today_items` return the same ids before and after the import.
- [ ] `sd reports list` shows no row with `source = 'brief'`.
- [ ] `sd reports brief add` of a draft whose stem has a row with other bytes exits 1 and changes nothing.
- [ ] `dashboard.sh test` passes with 0 failures, and `GET /api/briefs` lists 120 rows with `reader.source`
      `workflow database`.
- [ ] The day after step 8, the day's brief is a row and its vault copy has a matching `mirror-hash`.

## Open questions

- Q1, filed on sd:3459: keep a vault copy of each brief in `System/AI Generated/Briefs` (recommended), move copies
  to the channel default `Briefs/system`, or publish no vault copy. The design defaults to the first.

Decided here and recorded on the item: `report` rows with source `brief`; the system repo owns briefs; the 29
retired-series notes are imported as history; the Resources Briefs view is deleted.

## Risks

- `kind = 'report'` now means run reports, removal records and briefs. Each reader that lists every report must
  skip briefs; three do today, and a new one could forget. Accepted for no migration; `source` names the kind.
- The import ties its pass to the folder by each row's `sha256` against the file it read, and to the vault by the
  `HEAD` noted in step 2. A count alone proves nothing.
- The render ties a pass to each file by its own `mirror-hash`. A hand edit that also rewrites the hash passes;
  that needs intent, and the threat model accepts it.
- A Work brief filed from the satellite reaches the vault only at the hub's next `add` or `render`, up to a day.
  Accepted: the page reads the row at once.
- Moving the folder later leaves two copies with one stem, and a wikilink may open the old one. The render names
  the old copies; deleting them is the operator's call.
- The routines change outside any repository and outside review. Each edit is noted on the item; the next-day
  check is the test.
- A brief that the routine mails but fails to file is in the inbox and the staging folder only, until the next run
  files the draft. Accepted: the routine's report says so, and the next run is at most a day away.

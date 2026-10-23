---
title: Research projects tied to work Notion experiments - attach, create, deck and status from the Research page
created: 2026-10-19
item: sd:5296
---

# Design — research-notion-experiments

Write this page only when the shape needs agreement before the code.
The item's row holds status, progress and decisions; this page holds the shape.

## Status

Built in the same PR as this page. The lead's go (2026-10-19) changed two choices of round 1: the link is a repo
declaration of a new kind, `experiment`, not a setting; and the brand kit's path comes from the dashboard's `.env`, not
a setting. The state folder and the decks in My Drive's root were approved as written.

Adversarial review, one read-only round on 2026-10-19, found five blocking findings, all fixed here:
R1 `DENIED` denied the Notion writers the apply runs need; R2 a deck stopped before `out.json`, or mid-slide, could
not resume; R3 create recovery took a search miss as "never created"; R4 a status apply could overwrite a teammate's
edit made after the draft; R5 a deck apply re-read the brand kit, outside the confirmed draft.

The Draft deck action was retired in system sd:5536: Build deck and Import to Slides cover decks. The deck rows,
sections and failure-table rows below describe what was removed.

## Terms

- **Experiment**: one row of the team's experiments database in the work Notion workspace.
- **Link**: which experiment a research checkout belongs to: the checkout's repo declaration of kind `experiment`.
- **Run**: one bounded `claude -p` with an exact tool allowlist, started by the dashboard, as the Work sweep runs.
- **Draft**: a run that reads only and leaves a proposal in its run folder.
- **Apply**: a run that writes one approved draft to Notion or Drive, and nothing else.

## Problem

Each research checkout on the Research page is also an experiment the team tracks in Notion.
Today the operator keeps the two apart by hand: finding the row, writing a status entry, and building a slide deck.
The dashboard server cannot call MCP tools, so it cannot read or write Notion or Drive itself.

## Approach

### The four actions

On the Research page, a project's Details gains an Experiment section with four actions.

1. **Attach**: pick an experiment from the cached list and store the link. Writes only the workflow database.
2. **Create**: type a high-level description. A draft run expands it into the row's fields.
   Apply creates the row and stores the link.
3. **Deck**: a draft run writes a slide spec of 6 to 8 slides: overview, approach, current status and next steps.
   Apply builds a new Google Slides deck in the work account's Drive from that spec, styled by the work brand kit.
4. **Status**: a draft run writes a dated entry and the new status section.
   Apply puts the entry on top, keeps earlier entries condensed to one line each, and sets the `Status` property.

Every outward write is two runs: a draft that reads only, then an apply the operator confirms.
The confirm (`askFirst`) shows the draft itself: the row's fields, the slide spec, or the old and new status section.
So the operator sees what will be written before anything leaves the machine.
No run deletes a page, a block, a property value it did not set, or a file.

### Rejected: one run per action

A single run that drafts and writes cannot show its text before writing.
Showing the text after the write breaks the ask-first rule for outward writes.

### The runner: the sweep's path, reused

`sweep.py` already starts a headless run under a lock, in its own session, and reads its record back.
`realm_sweep.py` already holds the `claude -p` call: exact allowlist, `--permission-mode dontAsk`, the token wrapper,
the process-group timeout and the `RESULT:` line.
A new module, `research_experiments.py`, reuses both:

- `sweep.start` splits into `spawn(state, argv, place, command)` and the realm part. Both callers use `spawn`.
- `realm_sweep.ask`, `claude_binary`, `write_record`, `alive` and `account` are imported, not copied.
- One lock, `<state>/lock`, for all experiment runs: one Notion or Drive writer at a time. A second start is a 409.

Rejected: a manual cron job through `/api/jobs/<name>/retry`.
A job is a fixed config file and takes no per-request arguments (project, action, draft).

### What each run may touch

| Run | MCP tools allowed | Other tools |
| --- | --- | --- |
| list | `notion-search`, `notion-fetch` | Write to the run folder |
| draft create, draft status | `notion-fetch` | Read the checkout; Write to the run folder |
| draft deck | `notion-fetch` | Read the checkout and the brand kit's `design.md`; Write to the run folder |
| apply create | `notion-search`, `notion-fetch`, `notion-create-pages` | Read and Write the run folder |
| apply status | `notion-fetch`, `notion-update-page` | Read and Write the run folder |
| apply deck | `search_drive_files`, `create_presentation`, `batch_update_presentation`, `get_presentation`, `get_page` | Read and Write the run folder |

Every run denies by name what `realm_sweep.DENIED` denies, less the writers its own row allows:
`DENIED` names `notion-create-pages` and `notion-update-page`, and a deny wins over an allow.
Each run also denies `notion-move-pages`, `notion-duplicate-page` and the Drive tools that share, move or delete.
Read denies the brand kit's `known-issues.md`, `internal/` and `tests/`. No run has Bash.
A test builds each run's argv and checks that every allowed tool is absent from its deny list.

### Storage

- **The link**: the checkout's repo declaration of kind `experiment`, as #458 holds `review`:
  `{"page": "<32 hex>", "data_source": "<uuid>", "url": "https://...", "title": "..."}`; url and title are optional.
  The dashboard reads it with `repos.declarations` and writes it with `repos.set_declaration`. Attach sends the page
  it showed as the link (`replaces`), so a link moved since the read is a 409.
  A pack without the kind holds none: every checkout reads "not linked", and attach answers 409, "update the
  command pack". A value without a page id and a data source reads as unknown, and attach never overwrites it.
- **The experiments database**: the pack's `sd.notion_experiments`, read only. Unset refuses every run, naming it.
- **The account**: `google.accounts.work.email`, read through `realm_sweep.account("work", ...)`.
- **The brand kit**: `RESEARCH_BRAND_KIT`, the path of the kit's `design.md`, exported or in
  `<config>/project-dashboard/.env`; `.env.example` holds a `change-me` line. Unset, the deck draft is off, and the
  page names the variable and both remedies. The public repository cannot name the kit or its path.
- **The cache and the runs**: `st_state_dir research-experiments`, as each sweep has its own state folder.
  `experiments.json` is the list cache, replaced whole. `runs/<stamp>/` holds `request.json`, `record.json`,
  the model's `rows.json`, `draft.json` or `out.json`, and `run.log`. The page reads the 30 newest run folders.
  A deck's URL stays in its apply's record, not in the declaration.

Rejected: a setting, `research.experiments`, keyed by checkout. The lead ruled for the declaration: it is the
per-repository home, and #458 already reads and writes it from the dashboard.
Rejected: the pack's experiments projection (`fields.experiments`). It projects sd items, and a research checkout
is not an item.

### The pack change

The pack's side, built by its own builder, is one kind and its reader rule:

- `lib/sd_db/repos.py`: `DECLARATION_KINDS` gains `"experiment"`, and `experiment_problems(value)` lists what is
  wrong with a value, empty when it reads: `page` (a Notion page id, 32 hex digits, dashes allowed) and `data_source`
  (a UUID) are required, `url` (https) and `title` optional, any other key refused. `set_declaration` runs it and
  raises `RepoRefusal` on a problem, so `repo declare` and the dashboard's attach share one check.
- `bin/sd_lib.py`: its copy of `DECLARATION_KINDS` gains it too.
- Its tests: `set_declaration` stores a good value and refuses a bad page id, a bad UUID and an unknown key;
  `declarations` returns it.

It landed as pack sd:5302. The dashboard's tests set `DECLARATION_KINDS` with and without the kind, so they pass on
a pack before or after it, and still check the "update the command pack" answer.

### The attach list

A list run reads the experiments database and writes `experiments.json`:
`{"read", "source", "rows": [{"page", "url", "title", "stage", "status", "edited"}]}`, up to 200 rows.
`source` is the data source id the run was given. The page shows the list with its age and a Refresh action.
Attach accepts only a page in the cache whose `source` equals the current `sd.notion_experiments`.
Rejected: a list run each time the picker opens. It takes a minute or more, and the picker would wait on it.

### Evidence that binds an apply to its draft

`POST /api/research/experiments/act {"verb": "apply", "run", "fingerprint"}` starts an apply only when all of these hold:

- the draft run is `done` and its `draft.json` hashes to `fingerprint`, the SHA-256 the page showed;
- the draft's project, action, experiment page, data source and account equal the current link and settings;
- no other apply of that draft is running or `done`.

Each refusal is a 409 that names the field that moved.

### Look before write

Each apply writes its `out.json` right after the outward create, before any further call:
the page id for create, the presentation id for deck. The runner then records the link.
A second apply of the same draft starts from what the first one left:

- create, with an earlier `out.json`: the runner links its page without a run.
- create, without one: the run searches the database for the draft's title, fetches each candidate,
  and adopts the one whose body holds the pointer line, `Linked from research project <label>, draft <stamp>.`
  A search can miss a row, so no match does not prove no create. The run then creates nothing and records
  `unknown`. The page offers Refresh and Attach, or Apply again with `recreate`, whose confirm says a duplicate row
  may result.
- deck, with an earlier `out.json`: the run resumes that deck. Without one, it searches My Drive for the exact title,
  which carries the draft's time. No match is `unknown` as above, and only `recreate` makes another deck.
- status: the outcome depends on the page as the apply finds it (next section).

After an apply, the runner checks that `out.json` names the linked page (status) or a new one (create, deck).
Any other page is recorded as failed, naming it.

### Status binds to the page it read

The status draft records `before`: the status section as fetched and the `Status` value. Its `after` is the new
section and the proposed `Status`. The fingerprint covers both. Apply fetches the page and acts by what it finds:

- the section equals `before`: write `after`; equals `after`: skip, an earlier apply wrote it; else refuse as stale.
- `Status` equals `before`: set the proposed value; equals it already: skip; else refuse as stale.

A stale refusal writes nothing and asks for a new draft. `out.json` records which case each part met.

### The deck

The draft run reads the brand kit's `design.md` in full, then writes the spec: per slide, one line of message
and up to 12 drawn elements (a text box, rectangle, rounded rectangle or ellipse, with its box in points on the
720 by 405 page, and its optional text, fill, text colour, size, font and bold), from which the visuals are built.
Visuals over bullets; one call to action on the last slide, as the kit asks. The project's own figures only.
Apply reads the spec only, never the kit, so the confirmed spec is the whole of what it writes.

The runner builds every Slides request; the model only copies them to `batch_update_presentation` and reports what
the API answered. Each slide's id is `sdexp_<n>`, each element's `sdexp_<n>_<k>`, so each id is valid (5 to 50
characters) and every later request names an id made before it.
Apply runs in rounds. A round sends `batches.json` as written, records each answer in `sent.json`, and stops at the
first refusal; it then reads the deck back with `get_presentation` and `get_page` into `deck.json`.
From that read the runner plans the next round (`plan`): a missing slide is one batch, its `createSlide` after the
spec slide before it, plus its elements; a slide missing elements gets those elements' requests only, by id.
A slide is complete only when every element id it should hold is there.
The first round of a new deck creates it and reads it before any batch; the runner records the one slide it then
holds as `blank` in `out.json`. Once every spec slide is complete, that id alone is deleted, if it is still there.
Nothing else is ever deleted: a slide or shape the operator added between runs stays, and one they removed returns.
No blank recorded (the run stopped before its first read, or the new deck held more than one slide) deletes nothing.
Speaker notes are not written: `workspace-mcp` exposes no `speakerNotesObjectId`, so a notes request could only be
guessed, which this round removes. The deck stays in My Drive's root, and its URL is in the apply's record.

### The page

The Details pane for a project shows: the linked experiment (title, stage, status, link), the latest draft of each
action awaiting apply, and the last run with its state and log path. The four actions are commands in
`research.js`, with `askFirst` and a `consequence` that renders the draft. Draft and Refresh do not ask.
`GET /api/research/experiments?project=<key>` answers the link, cache, runs and settings problems; `POST
/api/research/experiments/act` takes `list`, `attach`, `draft` and `apply`. The `hallmark` skill runs before the UI is built.

## Non-goals

- No detach and no delete. Attaching another experiment replaces the link and names the old one in the confirm.
- No writes to the tasks database, and no change to the pack's experiments projection.
- No scheduled runs. Every run starts from a click.
- No deck folder setting: the deck goes to My Drive's root, and the operator moves it.

## Failure table

| Step | State moved | Failure | Recovery | Test |
| --- | --- | --- | --- | --- |
| Start any run | `<state>/lock`, the run folder, `request.json` | A run holds the lock | 409 naming its start; nothing written | start twice with a held lock |
| Start any run | as above | A setting is unset or the database does not read | refused before the model, naming the key; an unread database is unknown, not unset | unset `sd.notion_experiments`; database open raises |
| List run | `experiments.json` | The run fails or writes bad JSON | the old cache stays; the record is failed | stub claude exits 1; stub writes `[1` |
| Attach | the `experiment` declaration | Page not in the cache, or the cache came from another data source | 400, nothing written | attach an id not listed; change the setting after the list |
| Attach | as above | The link moved since the page read it | 409, stale; the page reads again | link another page, then attach with the old `replaces` |
| Attach | as above | The declaration holds no page id or data source | refused as unknown; never overwritten | seed `{"page": "x"}` and attach |
| Attach | as above | The pack does not know the kind yet | 409 "update the command pack"; the page reads "not linked" | attach with the pack's kinds unpatched |
| Draft run | `draft.json` | The run stops, or ends without `RESULT: ok` | record failed (dead pid reads failed); no outward write | kill the stub mid-run; stub without `RESULT:` |
| Apply start | `request.json` naming the draft | The draft changed, the link moved, or a setting changed | 409 naming the field | each of the three, one test |
| Apply start | as above | A second apply of a done draft | 409 `applied` | apply a draft twice |
| Apply create | Notion row, then `out.json`, then the link | Stop after `out.json`, before the link | the next apply links from `out.json` without a run | stub writes `out.json` then the runner is killed before the link write |
| Apply create | as above | Stop after the Notion create, before `out.json` | the next apply searches, fetches and adopts a verified match; no match is `unknown` and creates nothing | the next apply's prompt carries search, fetch and the no-create rule |
| Apply create or deck | as above | Recovery ended `unknown` | a plain apply is refused; only `recreate`, confirmed, creates again | apply after `unknown` without and with `recreate` |
| Apply create | link write | The declaration write is refused | failed, naming it; `out.json` kept, and the next apply links from it | create with the kind unknown, then apply again with it known |
| Apply status | page content, then `Status` | Stop between the two writes | the next apply finds the section equal to `after`, skips it and sets `Status` | the prompt carries `before`, `after` and the three cases; the earlier failed apply does not block it |
| Apply status | none | A teammate edited the section or `Status` after the draft | refused as stale; nothing written; a new draft is needed | stub reports stale; the record is failed and names it |
| Apply status | page content | `out.json` names another page | record failed, naming the page | stub writes another id |
| Apply deck | presentation, `out.json`, slides, the record's `deck` | Stop after `out.json`, between rounds or between batches | the next apply resumes the deck in `out.json` and plans from its read | `test_a_resume_after_operator_edits_restores_only_what_is_missing_and_deletes_nothing_else` |
| Draft deck | `draft.json` | An id or field the runner cannot build (short id, off-page box, bad colour, unknown key) | the draft fails naming it; an older draft's apply fails before any call | `test_a_draft_the_runner_cannot_build_is_refused_naming_the_field` |
| Apply deck | slides, shapes | A request names an id Slides refuses or one not made yet | the runner makes every id, `sdexp_<n>_<k>`, and each reference follows its create | `test_every_request_names_a_valid_id_made_before_it_or_already_there` |
| Apply deck | one slide | `create_presentation` adds a blank slide | its id is recorded at creation and deleted alone, once every spec slide is whole; none recorded or gone deletes nothing | `test_the_blank_slide_goes_only_when_recorded_at_creation_and_still_there` |
| Apply deck | one slide's elements | A slide exists with elements missing | complete only when every element id is there; the missing ones are sent, by id | `test_a_slide_missing_shapes_gets_only_their_requests_by_id` |
| Apply deck | slides | The operator adds, removes or moves slides and shapes between runs | added ones stay, removed spec ones return in place; only the recorded blank goes | `test_a_resume_after_operator_edits_restores_only_what_is_missing_and_deletes_nothing_else` |
| Apply deck | one batch | Slides refuses a batch | the round stops there; failed naming the API error; the next apply resumes | `test_a_refused_batch_fails_naming_the_api_error_and_the_next_apply_finishes` |
| Apply deck | none | Speaker notes need an id `workspace-mcp` does not report | notes dropped from the spec; nothing guessed | the draft validator refuses `notes` |
| Apply deck | presentation | Stop after the create, before `out.json` | the next apply searches the exact title; a match resumes, no match is `unknown` | the next prompt carries the title search |
| Draft deck | `draft.json` | The kit changes after the draft | none needed: apply reads the spec only | the apply argv has no Read outside the run folder |
| Any run | the record | The server restarts mid-run | the run continues in its own session; the page reads its record | `status` with a live pid and with a dead one |
| Any run | the lock | The runner is killed while its model run writes | the model run inherits the lock, so nothing starts beside it; the run reads running until it ends; a runner without the lock refuses | the fake model receives the lock; a dead runner with the lock held reads running |
| Draft or apply start | `request.json` | The link names another data source than the setting | 409 `source:`; attach again | link with another source; move the link's source after the draft |
| Apply start | the confirm | A newer draft finishes while the confirm is open | Apply posts the draft the confirm showed, with every field shown; a run without a confirm posts nothing | the page test runs Apply twice after one confirm |
| Apply deck | none | A title with a quote breaks the Drive name query | the title drops quotes and backslashes | `test_a_title_with_quotes_still_makes_a_drive_name_query` |
| Older state | none | No state folder, no cache, no setting row | page shows "never read" and "not linked" | empty state folder |

## Slices

One PR: the runner, the routes and the page section, one commit for sd:5296, beside sd:5297's commit.
The `sweep.spawn` split lands in the same PR, since both callers change with it.

## Acceptance criteria

- [ ] `make test SUITE=dashboard` passes with the new tests, none skipped, and no real `claude`, Notion or Drive call.
- [ ] Each failure-table row has its test, and each test failed on `origin/main` before the code.
- [ ] Each run's argv allows only the tools in the table above and denies none of them; a test compares the lists.
- [ ] `tests/test_product_name.py` passes: no product name in the dashboard code or this page.

## Risks

- An allowlist names tools, not pages. `notion-update-page` could write any page the account reaches.
  The prompt names one page, and the runner checks `out.json` after the run. Accepted for one operator.
- `notion-search` may not return every row of a large database. The list names its count and how it was read.
- Status condensing rewrites earlier entries. The confirm shows the old and new section side by side.
- If an sd item also projects this row, the projection treats our `Status` write as a person's edit and yields it.
- The deck's look depends on the four shapes the runner builds. The confirm lists every element before apply.
- Evidence for an apply is the draft's hash and the project, action, page, data source and account it was made for.

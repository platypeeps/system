---
title: The v2 shell owns refresh safety, and each page registers itself
created: 2026-10-02
item: sd:2418
---
# Implement — The v2 shell owns refresh safety, and each page registers itself

## Order

**Land order.** The five ports this item waited for have landed:
Activity (sd:2111, PR 69), Reports (sd:2121, PR 73), Contributions (sd:2113, PR 72),
Documents (sd:2114, PR 67) and Research (sd:2122, PR 68). The branch merged
main at e0abce9. No new port starts until this item lands (D1 in `prd.md`).
The build branch merges main again just before prepare.

**Pages the registry moves.** Eleven, with twelve page routes:

| Page | Routes | API routes moved | Old screen |
| --- | --- | --- | --- |
| Today | `/`, `/today` | none (`/api/now` stays) | takes `/`; classic at `/classic/today` |
| Briefs | `/briefs` | `/api/briefs` | none |
| Tasks | `/tasks` | `/api/tasks`, `/api/tasks/<n>` | Backlog (classic) in the palette |
| Contributions | `/contributions` | `/api/contributions/page`; write `/api/contributions/task` | takes `/contributions` |
| Management | `/management` | `/api/management` | Jobs, Services, Repos, Sessions, Protection stay shared |
| Home | `/home` | `/api/home` | none |
| Health | `/fleet-health` | `/api/health` | none |
| Research | `/research` | `/api/research`, `/api/research/<checkout>` | Resources (classic) |
| Activity | `/activity` | `/api/activity` | none |
| Reports | `/reports` | `/api/reports`, `/api/reports/clean` | Reports (classic) |
| Documents | `/documents` | `/api/documents` | takes `/documents` |

**One pull request, in this commit order.** Each step leaves `dashboard.sh test` green.

1. **Reader tests first.** Add `tests/test_v2_read.py`. It loads `read.js` into
   JavaScriptCore through `osascript`, as the page tests do, with stand-in
   `fetch`, `commands`, `state` and `reconcile`. Cases:
   - Two loads where the older answers last: the newer document stands, and
     `draw` ran once for it.
   - A failure from an older load after a newer success changes nothing.
   - An object put by read 1 and absent from read 2 is retired: no command is
     on it, and its pick is gone.
   - A selected row that read 2 drops: `select(null)` ran, and Details shows `none`.
   - An empty answer and a 500: no object is live, the selection is clear, and
     the state is `empty` or `error`.
   - A reread after a write that fails: rows are still drawn, no object is live,
     and the state is partial and says the change landed.
   - Two rereads after one read started: one more fetch, which starts after
     the first read ends (the write barrier).
   - Three rereads before a queued read starts: they share it.

   Check: every case fails, because `read.js` does not exist.
2. **`v2/static/read.js`, and a new shell method `commands.retire`.** Mark the shell
   change `build: (sd:2418)`. Add `/ui/read.js` before `/ui/shell.js` in each
   page HTML that adopts the reader.
   Check: `test_v2_read.py` passes.
3. **Briefs adopts the reader.** Remove its `generation`, `unselect` and
   failure branch. Its four review tests stay as they are: older refresh,
   no-rows refresh, failed read and refused read. Their stand-in loads the real
   `read.js`. Add one test: an object from a dropped brief runs no command.
   Check: the new test fails before the change and passes after it. The Briefs
   suite passes with the hand-written guards removed.
4. **Fleet Health adopts the reader.** Remove its `generation` and `retire`.
   Its review tests for #64 stay and pass.
   Check: the Health suite passes.
5. **Registry tests first.** Add `tests/test_v2_registry.py` with the
   invariants in the design: one claim per path, HTML exists, section in
   `GROUPS`, 403 without a session, 400 for a query, CSRF for a write, and
   `sections.js` equal to the registry map.
   Check: it fails, because `sd_dashboard/v2/pages/` does not exist.
6. **`sd_dashboard/v2/pages/`.** Add `Page`, `Api`, `Read` and the enumeration.
   Add one file per page in the table above. Each file's `read` is the body of
   its old `/api/` block, unchanged; Contributions' `write` is the body of its
   `action_route` branch, unchanged.
   Check: `test_v2_registry.py` passes.
7. **Switch `v2/__init__.py` and `server.py` to the registry.**
   - Delete `PAGES`, `SECTIONS` and the moved `/api/` blocks.
   - Add `_api` and the `takes` handling in `route()`.
   - `action_route` asks the registry first; delete the `/api/contributions/task` branch.
   - Each page test gains its own registration assertion.
   - Remove the shared literals from `test_default_routes.py` and `test_v2_today.py`,
     and the `SECTIONS` order assertion from `test_v2_briefs.py`.

   Check: `dashboard.sh test` passes. Every 403 and 400 text a page test
   asserts is unchanged.
8. **`dashboard.sh pages` and the README.**
   - Add the verb, with a help line, and a test that its output lists every
     registered section.
   - Replace the table and the palette row with one sentence that names the verb.
   - Move the per-page paragraphs to `local-project-dashboard/docs/pages/<name>.md`:
     today, tasks, home, contributions, briefs, health, research, documents,
     reports, activity. Management has no paragraph to move.

   Check: `python3 tests/test_citations.py` passes, and `sd-docs-lint` passes.

## Mutations

Apply each mutation alone and revert it. Each must fail a named test.

| Mutation | Expected failure |
| --- | --- |
| Drop the generation check after the fetch | older-answers-last case |
| Drop the generation check on the failure path | older-failure case |
| `retire` keeps picks | retired-pick case |
| `retire` leaves the object's type | Briefs dropped-object test |
| Skip `select(null)` when the selection is retired | dropped-selection case |
| Reread failure leaves objects live | reread-failure case |
| `reread()` joins a read that started before it | write-barrier case |
| `_api` skips the session check | registry 403 invariant |
| `_api` accepts a query on a `query=False` route | registry 400 invariant |
| A page names a section missing from `GROUPS` | registry `GROUPS` invariant |

## Gates

Run in the foreground from the worktree root, through `sd gate run --`:

- `local-project-dashboard/dashboard.sh test`
- `python3 tests/test_citations.py`

A gate that the harness moves to the background is awaited with Monitor on
its result line, never rerun.

## Migration of the other pages

Each item below is its own follow-up, filed when this item lands. Each one
adopts the reader on one page, removes that page's hand-written guards, and
keeps the page's review tests. None changes a shared line: the registry has
already moved its registration.

| Page | Gaps it closes | Notes |
| --- | --- | --- |
| Management | generation, retirement, selection, failed read | largest gap on main; re-reads after each write |
| Tasks | generation, retirement, failed read | keeps `DET.gen` for Details; its coalesced reread becomes `reread()` |
| Today | generation, retirement | keeps its `reconcile` `keep` rule for briefs |
| Home | generation, retirement, selection | one load and a clock tick; small |
| Reports | failed read, write barrier | closes PR 73 finding r4167523338; generation and retirement move to the reader |
| Contributions | failed read, reread failure | its reread failure keeps objects live today; `reread(wrote)` becomes `load()` or `reread()` |
| Activity | reread failure | a failed read after a write clears everything today; reference behaviour otherwise |
| Documents | generation, retirement | single load; small |
| Research | generation, retirement | single load; keeps its per-project sources cache |

A port that starts after this item lands adds its own page file and uses
`shell.read` from the start. Its brief says so. Its diff touches no shared
line in the files listed in PRD requirement 10.

## Done when

- `dashboard.sh test` and `tests/test_citations.py` pass on the final head.
- `grep -n '"/api/' local-project-dashboard/sd_dashboard/server.py` lists no
  route that a registered page owns.
- `v2/__init__.py` has no `PAGES` or `SECTIONS` literal.
- The follow-up items are filed in `sd`, one per page in the migration table.

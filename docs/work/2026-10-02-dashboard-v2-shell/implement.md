---
title: The v2 shell owns refresh safety, and each page registers itself
created: 2026-10-02
item: sd:2418
---
# Implement — The v2 shell owns refresh safety, and each page registers itself

## Order

**Land order.** This item lands after the five open ports:

- Activity (sd:2111)
- Reports (sd:2121)
- Contributions (sd:2113)
- Documents (sd:2114)
- Research (sd:2122)

No new port starts until it lands. The build branch merges main again just
before prepare. It then moves every page on main at that time.

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
   - Two rereads during one read in flight: one fetch.

   Check: every case fails, because `read.js` does not exist.
2. **`v2/static/read.js` and `commands.retire` in `shell.js`.** Mark the shell
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
   Add one file per page on main: today, briefs, tasks, home, management,
   health, and each open port that has landed by then. Each file's `read` is
   the body of its old `/api/` block, unchanged.
   Check: `test_v2_registry.py` passes.
7. **Switch `v2/__init__.py` and `server.py` to the registry.**
   - Delete `PAGES`, `SECTIONS` and the moved `/api/` blocks.
   - Add `_api` and the `takes` handling in `route()`.
   - Each page test gains its own registration assertion.
   - Remove the shared literals from `test_default_routes.py` and `test_v2_today.py`.

   Check: `dashboard.sh test` passes. Every 403 and 400 text a page test
   asserts is unchanged.
8. **`dashboard.sh pages` and the README.**
   - Add the verb, with a help line, and a test that its output lists every
     registered section.
   - Replace the table and the palette row with one sentence that names the verb.
   - Move the per-page paragraphs to `local-project-dashboard/docs/pages/<name>.md`.

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
| Reports | generation, retirement, failed read | its refresh cell races its reread today |
| Contributions | moves its own guards to the reader | closes the open #72 finding on "write landed" text |
| Activity | moves its own guards to the reader | reference behaviour; mechanical |
| Documents | retirement | single load; small |
| Research | retirement | single load; small |

A port that starts after this item lands adds its own page file and uses
`shell.read` from the start. Its brief says so. Its diff touches no shared
line in the files listed in PRD requirement 10.

## Done when

- `dashboard.sh test` and `tests/test_citations.py` pass on the final head.
- `grep -n '"/api/' local-project-dashboard/sd_dashboard/server.py` lists no
  route that a registered page owns.
- `v2/__init__.py` has no `PAGES` or `SECTIONS` literal.
- The follow-up items are filed in `sd`, one per page in the migration table.

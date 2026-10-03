---
title: The v2 shell owns refresh safety, and each page registers itself
created: 2026-10-02
item: sd:2418
---
# PRD — The v2 shell owns refresh safety, and each page registers itself

## Problem

Every dashboard v2 port solves two shared problems again, by hand.

**Refresh safety.** A page reads its `/api/<page>` document, draws rows, and
puts one command object per row into the shell. A refresh repeats this.
Each page decides alone whether three guards hold:

- **Generation.** A late answer from an older read must not draw over a newer one.
- **Retirement.** Rows, command objects and picks from an older read must not
  stay live, so an action never runs on data the page no longer shows.
- **Selection.** When the selected row is gone (refresh, empty answer, failed
  read), the selection clears and Details stops offering its commands.

Eleven pages are on main, and no port pull request is open. Only Fleet Health
and Activity hold all three guards on every read. Today, Tasks, Management and
Home have no generation. Today, Tasks, Management, Home, Briefs, Documents and
Research retire nothing. Tasks, Management, Reports and Contributions keep old
command objects live after a failed read. Management never clears a selection
whose row is gone. `design.md` holds the table for each page.

Copilot found these gaps one page at a time. On PRs 64 to 73 it raised
5 race findings, 4 stale-object findings, 4 stale-row or stale-Details
findings and 2 "write landed" findings, each fixed in its own page.
One more is still open on PR 73: a bulk write's shared reread can start
before the last write lands. Each new port repeats the cycle.

**Shared registration.** A port edits the same shared lines as every other
port:

- the module docstring, `PAGES` and `SECTIONS` in `local-project-dashboard/sd_dashboard/v2/__init__.py`;
- `CLASSIC` and `SCREENS` when the old screen moves or stays as "classic";
- one `/api/<page>` block in the `if path == …` chain in `local-project-dashboard/sd_dashboard/server.py`;
- `route()` and `pages.py` when the new page takes the old screen's path;
- the README paragraph, the "Section or screen" table and its palette row;
- the section-map literal in `tests/test_default_routes.py`, its `required`
  tuple and `OLD_SCREENS`;
- the exact `window.SHELL_PAGES` string in `tests/test_v2_today.py`.

The five ports that landed last (Research, Contributions, Activity, Reports,
Documents) each rewrote these lines. They conflicted pairwise, and each
landing forced every open port to merge main again.

## Users

- The operator, who reads the dashboard and runs its commands.
- A builder who ports a page, and the merge lane that lands ports in series.

## Requirements

### A. Refresh safety in the shell

1. The shell offers one reader that a page calls to read its document. Using it
   is the shortest way to load a page; a page that uses it gets all three
   guards without writing any of them.
2. Generation: the reader numbers each read. An answer, or a failure, from a
   read that is not the newest changes nothing on the page.
3. Retirement: after each read, every command object the page put earlier and
   did not put again is retired. A retired object has no command and no pick,
   and its label says it is no longer listed.
4. Selection: after each read, a selection whose row is gone clears. The shell
   then calls `commands.select(null)` and resets Details to the page's
   nothing-selected text.
5. A failed first read or refresh retires every object, clears the page's rows
   and selection, and sets the error state with the reason.
6. A failed reread after a write keeps the rows on screen, retires their
   objects, and sets a partial state. The state says the write landed and the
   rows were not read again. It never says "not changed" for a write that landed.
   A refused write calls `load()`, not `reread()`, so only a landed write can
   produce the "landed" text.
7. A reread after a write never answers with a document read before that write
   landed. Rereads join one read that has not started yet; a read already in
   flight is joined only when no write landed after it started, as Tasks does
   today. A bulk run of N writes therefore ends in at most two reads.
8. A page keeps full control of how it draws rows, lanes, counts and Details.
   The reader decides only when a document is current.

### B. Per-page registration

9. A page registers itself from one file of its own. That file names its
   section, its routes and HTML file, its API routes and how each one reads, and
   any old screen it keeps as "classic" or moves.
10. A new port adds files and changes no shared line in these places:
    `local-project-dashboard/sd_dashboard/v2/__init__.py`, the `/api/` chain,
    `action_route` and `route()` of `local-project-dashboard/sd_dashboard/server.py`, the README,
    `tests/test_default_routes.py`, `tests/test_v2_today.py`.
    A write only the page uses registers in the page file; a write v1 also
    posts stays in `action_route`.
11. The rail order stays as the design source's `GROUPS` in
    `local-project-dashboard/sd_dashboard/v2/static/shell.js` gives it. A page names its section and cannot reorder it.
    A registration that names a section missing from `GROUPS` fails the tests.
12. Every API route keeps today's rules: 403 without a dashboard session, 400
    for a query string unless the route accepts one, CSRF for a write.
    One shared check enforces them for every registered route.
13. Each page's own test asserts its own registration. A shared test enumerates
    the registry and checks invariants (no route claimed twice, each HTML file
    exists, each section is in `GROUPS`, each API route refuses without a
    session). It holds no list of pages.
14. The address map the README shows comes from the registry
    (`dashboard.sh pages`), not from a hand-kept table.

### Migration

15. This item moves every page on main to the registry: Today, Briefs, Tasks,
    Contributions, Management, Home, Health, Research, Activity, Reports and
    Documents. It adopts the reader in Briefs and Fleet Health.
16. Each other page adopts the reader in a follow-up item of its own.
    No new port starts before this item lands.

## Decisions

The operator answered the plan's five questions on 2026-10-03 (`sd task show 2418`, note 8238).

- **D1 — Land order.** Freeze accepted: no new port starts before sd:2418 lands.
  The five ports it waited for have landed (PRs 67, 68, 69, 72, 73).
- **D2 — Reader file.** The reader goes in a new file,
  `local-project-dashboard/sd_dashboard/v2/static/read.js`. `shell.js` changes by `build:` lines only.
- **D3 — Failed reread.** A failed reread after a landed write keeps the rows,
  retires their objects and shows a partial state (requirement 6).
- **D4 — README.** Per-page paragraphs move to
  `local-project-dashboard/docs/pages/<name>.md`. The README table becomes `dashboard.sh pages`.
- **D5 — Adoption.** Briefs and Health adopt the reader in this pull request.
  Every other page adopts it in its own follow-up.

## Out of scope

- Changing what any page reads, draws or offers.
- Polling or auto-refresh. The shell's reload on return after five minutes stays.
- The v1 screens and their routes, except where a port moves one to `/classic/`.
- Per-item Details reads (Tasks' `DET.gen`); the page keeps its own guard.

## Success

- A new port's diff touches no line in the shared files listed in requirement 10.
- A JavaScript test shows each guard: an older answer that arrives last is
  dropped, a retired object runs no command, a vanished selection clears
  Details, and a failed reread keeps rows but runs nothing on them.
- Briefs and Fleet Health pass their existing tests on the reader, with their
  hand-written guards removed.
- `dashboard.sh test` and `tests/test_citations.py` pass.

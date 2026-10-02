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

On main, only Fleet Health holds all three. Today, Tasks and Management have
no generation. Today, Tasks, Management and Briefs retire nothing. Tasks and
Management keep old rows live after a failed read. Management never clears a
selection whose row is gone. Of the open ports, only Activity holds all three.

Copilot found these gaps one page at a time. On PRs 64 to 73 it raised
4 race findings, 3 stale-object findings and 4 stale-row or stale-Details
findings, each fixed in its own page. Two related findings are still open
on PR 72. Each new port repeats the cycle.

**Shared registration.** A port edits the same shared lines as every other
port:

- the module docstring, `PAGES` and `SECTIONS` in `sd_dashboard/v2/__init__.py`;
- `CLASSIC` and `SCREENS` when the old screen moves or stays as "classic";
- one `/api/<page>` block in the `if path == …` chain in `sd_dashboard/server.py`;
- `route()` and `pages.py` when the new page takes the old screen's path;
- the README paragraph, the "Section or screen" table and its palette row;
- the section-map literal in `tests/test_default_routes.py`, its `required`
  tuple and `OLD_SCREENS`;
- the exact `window.SHELL_PAGES` string in `tests/test_v2_today.py`.

All five open port branches rewrite the same lines, so they conflict
pairwise. Each landing forces every open port to merge main again.

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
7. Overlapping rereads after writes join one read in flight, as Tasks does today.
8. A page keeps full control of how it draws rows, lanes, counts and Details.
   The reader decides only when a document is current.

### B. Per-page registration

9. A page registers itself from one file of its own. That file names its
   section, its routes and HTML file, its API routes and how each one reads, and
   any old screen it keeps as "classic" or moves.
10. A new port adds files and changes no shared line in these places:
    `sd_dashboard/v2/__init__.py`, the `/api/` chain in `sd_dashboard/server.py`,
    the README, `tests/test_default_routes.py`, `tests/test_v2_today.py`.
11. The rail order stays as the design source's `GROUPS` in
    `v2/static/shell.js` gives it. A page names its section and cannot reorder it.
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

15. This item moves every page landed on main by the time it lands to the
    registry. It adopts the reader in Briefs and Fleet Health.
16. Each other page adopts the reader in a follow-up item of its own, after it
    lands. No open port branch is rewritten for this item.

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

---
title: Scope the dashboard and UI pass, gaps ranked and slices ordered
created: 2026-10-11
item: sd:1333
---

# Design: dashboard-ui-pass

This page scopes the pass and holds no code.
The operator's rule frames it: the dashboard is the primary surface, so each operator action and state needs a view or a control there.
The survey read `local-project-dashboard/` at `origin/main` 0e5ca0f and the running server's `/api/now` on loopback (GET only).
It applied the hallmark audit verb and `ui-design/foundation/` (`principles.md`, `patterns.md`), with `products/system/design.md` as the dashboard's locked design source.

## Problem

Eighteen rail sections now have a v2 page; `dashboard.sh pages` lists them.
Some operator actions still have no control on any page: the Health page prints a command to copy, and the operator runs it in a terminal.
Other controls exist only on classic screens: `/operations?area=...`, `/protection` and `/item/<n>`.
Those screens use the v1 stylesheet, which breaks the foundation's "Never" list.
One state, the machine-setup doctor, has no reader at all.

### Gaps, ranked

Rank order: first an action or state with no view or control anywhere, then a control that only a classic screen carries, then style drift, then states with no reader.
Sizes: S is under a day, M is one to two days, L is more.

| Rank | Gap | Evidence | Operator impact | Size |
| --- | --- | --- | --- | --- |
| 1 | Post-merge cleanup has no control: Prune registrations, Delete merged branches and Remove build output only copy a command | `sd_dashboard/v2/static/health.js:226` (`copyOnly`), `:253`, `:258`, `:261`; screen Health, `/fleet-health` | The standing rule removes clean worktrees, branches and build output after each merge; the operator or an agent does it in a terminal | M |
| 2 | The machine-setup doctor has no view; nothing in `sd_dashboard/` or `collectors.py` reads it | `grep -rn doctor sd_dashboard` finds nothing; `local-machine-setup/machine-setup.sh:3716` (`cmd_doctor`) prints `ok`, `WARN`, `FAIL` lines | Doctor findings gate work: `local-sd-db/sd-db.sh` goes only once the doctor is clean (`CLAUDE.md`, Structure); the operator must run it by hand to see them | M |
| 3 | A cancelled lane entry cannot be queued again from the dashboard | `sd_dashboard/v2/static/queue.js:100` ("enqueue it again from its worktree"); `sd_dashboard/queue_screen.py:69` (`BLOCKED` omits `cancelled`); `sd-ship lane retry --help` takes failed, skipped or prepared only | A Cancel on Queue is one-way; undoing it needs a terminal in the right worktree | M |
| 4 | Today's failed-job rows offer Retry as a line to copy, though the route exists and two other pages post it | `sd_dashboard/v2/static/today.js:196` ("The v2 page runs no command yet"); `management.js:540` and `activity.js:358` post `/api/jobs/<job>/retry`; live `/api/now` showed two failed-job rows, each with a `retry:` command line | Today is the front door; the loudest row is the one row that cannot act | S |
| 5 | Bill caps and provider controls live only on the classic Usage area | `sd_dashboard/v2/pages/metrics.py:2`; routes `/api/bills/<bill>/cap` and `/api/providers/configure` in `sd_dashboard/server.py` `action_route` | Spend controls sit on a v1 screen that the rail does not reach | M |
| 6 | Job log, job cancel, service filter and paging live only on classic Jobs and Services | `sd_dashboard/v2/pages/management.py:2` | Management shows the jobs but cannot read a log or cancel a run | M |
| 7 | The Commands section is the last rail section still classic | `sd_dashboard/v2/__init__.py:55`; `sd_dashboard/palette.py:44` (`history`, with Read output and Reconcile outcome) | Reading a palette command's output or reconciling an unfinished one needs a v1 screen | M |
| 8 | The attributed batch acknowledge (`--all-clean --who`) lives only on classic Reports | `sd_dashboard/v2/pages/reports.py:3` | Clearing a run of clean reports needs the classic screen | S |
| 9 | Contribution evidence, dependencies and notification delivery live only on classic Contributions | `sd_dashboard/v2/pages/contributions.py:2` | The operator leaves the page to see why a row is there | M |
| 10 | Classic screens break the foundation: a pure `#ffffff` surface, `system-ui` type, no OKLCH tokens, no Dark, Light, System control | `sd_dashboard/static/dashboard.css:20`, `:76`; zero `oklch` or `light-dark` in that file | Every palette screen in ranks 5 to 9 looks and behaves unlike the rest; hallmark audit grade: critical, design-system drift | L in total; shrinks with each slice above |
| 11 | Three page stylesheets carry no Hallmark stamp | `grep -c Hallmark` is 0 for `v2/static/contributions.css`, `health.css`, `research.css` | Hallmark audit grade: major, missing system reference; the next design run cannot read what the page claims | S |
| 12 | States with no reader: mail and the daily note (Notes), Home Assistant state (Home), the document store behind Pin, Hide and Tag (Documents) | `sd_dashboard/v2/pages/notes.py:2`, `home.py:1`, `v2/static/documents.js:152` | Each shows as unknown with its reason, as principle 4 asks; nothing lies, but nothing acts | L each |

The hallmark audit found no other critical tell on the v2 pages: they share one stamped token set (`v2/static/tokens.css:1`), state glyphs and the ranked ledger.

## Approach

Build the missing controls on the v2 page that already shows the state, then delete the classic screen that held the control.
Restyling the v1 stylesheet was rejected: it spends work on screens the pass removes, and two styles would keep drifting.
Each control posts a route the server already has where one exists (ranks 4 to 8), so most slices add no new write path.

Two slices add a write: Health's cleanup (rank 1) and Queue's re-queue (rank 3).
Each follows the existing write contract: the page's revision, a refusal that wrote nothing as 400 or 409, any other failure as unknown (`queue_screen.py:29`).

Routing line from note #3504: the operator's global instruction files for Claude Code, Codex and opencode already name hallmark for UI work.
The repository has no such line, and none names the dashboard's design source.
The line goes in a new path-scoped rule file, `.claude/rules/dashboard-ui.md`, with `paths: local-project-dashboard/**`, plus one row in the `CLAUDE.md` Area rules table.
Proposed line: "Invoke the `hallmark` skill before you design or change a dashboard page or its CSS, and apply `ui-design/foundation/` and `products/system/design.md`: the design source binds every page."

## Non-goals

- New readers for mail, the daily note, Home Assistant or a document store (rank 12): each is its own item.
- Running the Designs page's screenshot or log commands: they run Node tooling in another checkout.
- Changing the private front door, the installer or `/health`.
- A restyle of `sd_dashboard/static/dashboard.css`.

## Failure table

Two slices move state in steps. Each builder extends its row set before the first review round.

| Step | State moved | Failure | Recovery | Test |
| --- | --- | --- | --- | --- |
| Health: prune registrations | `.git/worktrees/<name>` entries whose folder is gone | `git worktree prune` fails mid-list | Prune is idempotent; the next read lists what is left | Stop after the first entry; the reread shows the rest |
| Health: delete merged branches | Local branches already in origin's default branch | One `git branch -d` refuses (unmerged) or the read of origin fails | `-d` refuses alone; a failed origin read is unknown, so no branch is offered | Feed a branch unmerged since the read; the delete must refuse |
| Health: remove build output | `target/`, `node_modules/` in merged, clean worktrees | Removal stops part way through one folder | The row stays listed with what remains; the next run finishes it | Interrupt between two worktrees; the next read lists only the second |
| Queue: re-queue a cancelled entry | Queue entry status `cancelled` to `pending` | The head moved, or the lane changed since the read | `--expected-head` and the lane revision refuse; nothing written | Move the head after the page read; the write answers 409 |

## Slices

Each slice is one PR in this repository unless it says otherwise. Each leaves every machine working: a satellite keeps refusing hub-only writes with `HubOnly`.

1. **Hallmark routing and stamps.** `.claude/rules/dashboard-ui.md` (new), one `CLAUDE.md` row, and the stamp line for `contributions.css`, `health.css` and `research.css`, copied from each design-source stylesheet. Closes the note #3504 request and rank 11. Size S.
2. **Today runs Retry.** `today.js` posts `/api/jobs/<job>/retry` with the row's revision, as `management.js:549` does; exit 127 keeps Retry off. Rank 4. Size S.
3. **Health acts and reads the doctor.** `health_screen.py`, `health.js`, a server write per cleanup action, and a Doctor reading from `machine-setup.sh doctor` lines in a bounded child, as the fleet read runs. One slice because both change the same two files. Ranks 1 and 2. Size L; split it if the failure table shows too much.
4. **Queue re-queues a cancelled entry.** Needs a pack PR first: `sd-ship lane retry` admits `cancelled`, with `--expected-head`. Then `queue_screen.py` and `queue.js` offer Re-queue on a cancelled row. Rank 3. Size M.
5. **Metrics carries the spend controls.** `metrics_screen.py`, `metrics.js`: the bill-cap and provider forms post the existing routes. Rank 5. Size M.
6. **Management reads logs and cancels jobs.** `management_screen.py`, `management.js`: job log, cancel, service filter and paging. Rank 6. Size M.
7. **Activity takes the Commands section.** `activity_screen.py`, `activity.js`: Read output and Reconcile on command rows; the Commands rail entry goes. Rank 7. Size M.
8. **Reports batch acknowledge.** `reports_screen.py`, `reports.js`: the preview and attributed acknowledge, from the existing route. Rank 8. Size S.
9. **Contributions evidence.** `contribution_screen.py`, `contributions.js`: evidence, dependencies and delivery in Details. Rank 9. Size M.
10. **Retire the classic screens whose controls moved.** Delete each `operations_screen.AREAS` entry, `SCREENS` and `classic` label that slices 5 to 9 made redundant; keep any screen that still holds a control. Rank 10. Size M.

Slices 2 to 9 run in parallel once slice 1 lands (operator ruling, question 7); slice 10 runs last.
Slice 1 touches the headers of `health.css` and `contributions.css`, which slices 3 and 9 may touch later; landing slice 1 first avoids a rebase.

## Acceptance criteria

- [ ] `grep -rn "copyOnly\|runs no command yet" local-project-dashboard/sd_dashboard/v2/static/health.js local-project-dashboard/sd_dashboard/v2/static/today.js` prints only the read-only lines (port inspect, storage, volume, review links).
- [ ] `sh local-project-dashboard/dashboard.sh pages` prints no `classic` line.
- [ ] Health shows a Doctor reading on the hub, with a failed run as unknown, never as clean.
- [ ] `grep -c Hallmark` is at least 1 for every file in `local-project-dashboard/sd_dashboard/v2/static/*.css` except `fonts.css`.
- [ ] Each slice's fail-first test fails on `origin/main` and passes on its branch; `make check` passes at each merge.

## Risks

- Health's cleanup deletes files. The confirm names the count and the folders; `git branch -d` and `worktree prune` refuse on their own; build output is removed only for a worktree that is merged and clean at the time of the write, checked again server-side.
- The doctor runs `brew doctor`, which is slow; the reading needs a deadline, and a timed-out run is unknown. Under launchd the doctor may see a different `PATH` than a login shell, so its first live reading needs an operator look.
- Slice 4 depends on a pack change; until it lands, the slice waits and Cancel keeps its warning.
- Evidence binding: each new write carries the revision of the row the page showed (Health: the worktree's head and status digest; Queue: the entry's head and the lane revision), so a write against a changed subject is refused.
- Retiring classic screens removes palette entries the operator may use by habit; slice 10 lists each removed address in its PR body.

## Open questions

All six are answered (2026-10-11). The operator ruled on 2, 4 and the slice order; the integrator decided 1, 3, 5 and 6 as recommended.

1. The global instruction files already carry the hallmark line. Is the repository rule still wanted?
   Answer (integrator): yes. It loads on dashboard paths for any agent in this checkout and names the design source; slice 1 adds it.
2. Should Health run cleanup, or keep copy-only?
   Answer (operator): Health runs cleanup, behind a confirm that names the count.
3. How should Health read the doctor?
   Answer (integrator): parse the existing `ok`, `WARN`, `FAIL` line markers; add no `--json` flag.
4. Should the dashboard enqueue new items, or only re-queue a cancelled entry?
   Answer (operator): re-queue cancelled entries only. Enqueue stays with the builder or the lead.
5. Should Commands become its own v2 page?
   Answer (integrator): no. Activity takes it, as slice 7 says.
6. Restyle the classic screens to the foundation, or retire them?
   Answer (integrator): retire each once its control moves, as slice 10 says.
7. In what order do the slices run?
   Answer (operator): slices 2 to 9 run in parallel once slice 1 lands; slice 10 runs last.

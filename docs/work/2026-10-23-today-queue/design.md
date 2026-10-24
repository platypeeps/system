---
title: Today becomes one page to read and one list to act on; Queue slims to the lane tool
created: 2026-10-23
item: sd:5921
branch: today-5921
---

# Design — today-queue

Mockup: `docs/dashboard/today-queue-mockup.html` (gitignored; desktop and 390px, dark and light, plus Queue).
Samples come from the 2026-10-23 13:29 MDT reads of `/api/now`, `/api/today/status` and `/api/queue`.

## Problem

Today splits one question across four tabs, and the same facts show up in several of them.

- Now lists machine alarms only: failed jobs, PRs that need you, dirty or pinned checkouts, dark collectors.
- Work lists "Working now" (7 sessions) and "Your work": 142 rows today, 114 of them past due, mostly recurring HOA items.
  A list that long cannot be read.
- Inbox lists problem reports, 29 unseen briefs and mail.
- Status prints what `sd today` prints: 10 sections, a 750 KB read.
- Duplicates (checked against the code):
  - Lane entries appear in Work (`active_sessions` lane rows), Status Landing and Queue.
  - Problem reports appear in Inbox and Status Attention.
  - Due items appear in Work and as Status counts.
- Badges do not match the rows:
  - Now's badge (`now_screen._attention`) adds problem reports that Now never lists.
  - Queue's badge counts blocked rows that Today does not show.
- "Work" names both a Today tab and a Tasks realm filter.

The operator cannot answer two questions in one look: what is going on, and what do I have to do now.

## Approach

One Today page, with no tabs, for two jobs, read top to bottom:

1. **What is going on**: one annunciator and one day track. This is a glance; it never asks you to act.
2. **Needs you**: one ranked ledger. It holds every fact that asks you to act, with one action on each row.

A short **In motion** table under the ledger shows what is moving now.

### 1. What is going on (glance)

- **Annunciator**, 8 lamps plus the Observed clock:
  - Jobs, Lane, Reports, Pulls, Mail and Repos are `button.cell` filters on Needs you.
  - Sessions is an `a.cell` to In motion.
  - Gate (slots, waiters, load5) is an `a.cell` to Queue.
  - Each lamp's number counts the same rows the filter shows.
- **Day track**, the page's one evidence strip (hand-rolled SVG on tokens). It covers the last 24 hours with 4 lanes:
  - Landed: a tick for each merge, from the queue's `landed` rows and the delivered section.
  - Jobs: a mark for each failed run, at its log time.
  - Briefs: a mark for each brief filed.
  - Live: the session bars, ending at now.
  - Each mark links to its row. A table fallback sits in a `details`.
  - Beside the track is a 4-line key with links to Tasks filters: Landed 24h, Landing, Due this week, Blocked.
  - Unscheduled (148) is left out: it is backlog, not today.

### 2. Needs you (act)

One ledger, ranked in the shell's bands: Broken ■, Look ▲, Queued ◌, then by age.
Columns: state glyph, What, Why · next step, Source, Age, then one primary button and the `⋯` action menu.

**Rows of the same kind collapse into one group row** with a disclosure, so the list stays short:

- "9 posts ready to send"
- "114 items past due" (the "Why" cell splits it by repo)
- "4 new briefs today"
- "6 in progress, no session"

One pick or Snooze acts on the whole group. Opening the group shows the first 5 rows, then "N more". On 2026-10-23 this turns 142 + 29 + 3 rows into 10.

The toolbar has the Shapeshift field and 4 intent chips: Fix (Broken), Reply (reports, mail, PRs), Ship (ready to send, blocked lane), Read (briefs).
They are views over the one list, not tabs.

### 3. In motion

`active_sessions` rows grouped by item, longest-running first, with an elapsed meter. The first 5 show, then "N more".
The tally line gives "N landed today in M lanes" and links to Queue at `?show=all`. Landed rows are not repeated here.

### Where each piece of data goes

| Data (reader) | Today now | After |
| --- | --- | --- |
| failed / interrupted / unloaded jobs (`now_screen.job_rows`) | Now | Needs you, Broken or Look; Jobs lamp |
| dark collector (`dark_row`) | Now | Needs you, Broken; the lamp goes unknown ▨ |
| PRs needing you (`pr_rows`) | Now | Needs you, Look; Pulls lamp |
| dirty, ahead or pinned checkout, abandoned worktrees | Now | Needs you, Queued (pinned stays Look); Repos lamp |
| problem reports (`today_reports.problems`) | Inbox and Status Attention | Needs you, Look; Reports lamp |
| mail waiting (`today_mail`) | Inbox | Needs you, Look; Mail lamp |
| briefs unseen (`today_reports.briefs`) | Inbox | one group row a day, Queued |
| work: ready_to_send | Work | group row, Look |
| work: past due | Work | group row per repo, Look |
| work: in_progress with no live session | Work | group row, Queued |
| work: in_progress with a session | Work | In motion only |
| blocked lane entry (`queue_screen` `blocked`) | Queue only | Needs you, Broken; Lane lamp |
| merging, next, landed (`queue_screen`) | Work, Status Landing, Queue | Lane lamp; In motion tally; Queue |
| live sessions (`active_sessions`) | Work | In motion; Sessions lamp; day track |
| gate slots and load (`sd gate status`) | Status (operations) | Gate lamp |
| delivered, planned, blocked counts (`sd_db.today.sections` summary) | Status | day-track key, linked to Tasks |
| decisions, review questions | Decide | stay on Decide; one link in the ledger foot |
| health findings (`operations.findings`) | Status | Health; a host finding with attention is a report row |
| unscheduled, followups, closed by hand | Status | `sd today` CLI only |

### Counts and badges: each in one place

- **Rail badge for Today**: the Broken count, else the Look count, from Needs you's rows. A group counts as one row.
  It is the same number as the tally above the ledger.
- **Lamp numbers** count members (114 past due), and each lamp's filter shows exactly those rows.
- **Queue loses its rail badge.** Blocked entries count on Today, where they are rows. Queue is a tool you open, not an inbox.
- **Decide and Health keep their badges.** Today repeats neither: one link line under the ledger.
- The tab counts go, along with `window.todayCount` and `VIEW_OF`.

### Actions per row

| Row | Primary | In `⋯` (and Details) |
| --- | --- | --- |
| failed or interrupted job | Retry (`launchctl kickstart`) | Show log, Snooze, Capture |
| unloaded job | Copy load line | Open Management, Snooze |
| dark collector | Refresh | Open Health |
| blocked lane entry | Retry (`lane retry --expected-head`) | Approve (manual repos only), Open in Queue, Cancel |
| problem report | Acknowledge | Open report, Snooze |
| PR needing you | Open PR | Snooze |
| mail thread | Open thread | Snooze |
| ready-to-send group / item | Open Writing / Send | Snooze |
| past-due group | Triage (opens Tasks, filtered) | Snooze all 1w, Open each |
| briefs group | Read (opens Briefs) | Mark all seen |
| in progress, no session | Open | Snooze |
| dirty, ahead or pinned checkout | Copy the git or refresh line | Open Management, Snooze |

The bulk bar (`x` to pick) offers Snooze, Acknowledge and Mark seen across kinds. Each one skips a row it does not fit and says why.
Details keeps today.js's one-selection rule (`?row=`).

### Queue: keep it, slimmed

Queue stays its own page: it is the lane's control surface. Its writes (move, hold, release, cancel, retry, re-queue) carry lane revisions and confirm dialogs. They need room that a Today row does not have.

Merging it into Today would put reorder controls into the "act" list for a job the runner owns.

Today embeds Queue's state three ways: the Lane lamp, the Gate lamp, and blocked entries as Needs you rows. Each of those links back to Queue.

Queue itself changes:

- **One ledger, a row per active lane**, with a merging · next · blocked pip strip, the head-of-lane line and one primary action.
- The lane's entries open in Details.
- **The Lanes and Idle tabs go.** Idle lanes fold into one line ("Idle, landed today: … Idle, quiet: 14 lanes").
- **No rail badge** (above).

### Rejected

- **Keep four tabs and dedupe**: the operator still has to visit four places to learn there is nothing to do.
- **Fold Decide into Needs you**: decisions need reading and an option pick (Review's numbered buttons). That is a different act from Retry or Acknowledge, and Decide already has its badge.
- **Absorb Queue**: see above.

## Non-goals

- No change to `sd today`, `sd_db.today` (pack) or the lane verbs.
- No new collector: the day track uses only what `/api/now` and `/api/queue` already return, plus `finished` times.
- No change to how Snooze is stored: group snoozes write one row per member through the existing `/api/snooze`.

## Slices

Each slice is one PR. Each leaves Today working on every machine.

1. **Needs you ledger** (system).
   - `now_screen.document` gains `needs`: the ranked union of today's `rows`, problems, mail, blocked lane entries and the work buckets, with group rows (`group`, `members`, `more`).
   - `_attention` counts `needs`.
   - Tests: `test_now_screen` covers ranking, grouping (142 → ≤ 5 work rows), and a badge that equals the Broken/Look rows.
   - Fail-first test: today's badge counts a report that is not a row.
2. **One-page Today** (system).
   - `today.html` loses the 4 views. It gets the annunciator (8 lamps), Needs you and In motion.
   - `today.js` draws `needs`. It absorbs `today-reports.js`; `mail.js` stays if another page uses it.
   - Old `#view=` addresses redirect to `/today`.
   - Tests: the browser suite checks one `h1`, no tab bar, lamp filter round-trips and `?row=` on a group row.
   - The `verify` checks still hold: "Today without Now first", and an act on the top row.
3. **Day track** (system).
   - SVG strip and table fallback in `today.js`, fed by a `track` key on `/api/now`: landed times, failed-job log times, brief days and session starts.
   - Tests: marks equal the source counts; empty sources draw the empty line, not an axis.
4. **Status view removal** (system).
   - Delete `today-status.js` and its CSS.
   - `today_status.py` and `/api/today/status` stay only if slice 3's key reads them; otherwise delete them and their tests.
   - The Status help text moves into the ledger foot.
5. **Queue slim** (system).
   - One lane ledger with pips and entries in Details. Drop the Idle view.
   - Remove `count="/api/queue"` from `pages/queue.py`.
   - Tests: the queue browser suite covers lane rows, idle fold, every existing write from Details, and no rail badge.

Slices 1 and 2 can land as one bundle (same files). Slices 3, 4 and 5 are independent after 2.

## Acceptance criteria

- [ ] `make test SUITE=local-project-dashboard` passes.
- [ ] Against the 2026-10-23 data, Today renders ≤ 15 Needs you rows, and the rail badge equals the Broken (else Look) row count.
- [ ] No fact appears in two Today sections: a test asserts that `needs` ids and In motion item ids do not overlap, except an item with both a session and a blocked entry, which shows in both by design.
- [ ] Today has no tabs. `#view=now|work|inbox|status` lands on `/today`.
- [ ] Queue has no rail badge, and every lane write still works from Details.
- [ ] At 1440, 1024, 414, 375 and 320, dark and light, the page has no sideways scroll and every row action is ≥ 44px on touch.

## Risks

- **Group rows hide urgency.** One overdue tax item could sit inside "114 past due". Mitigation: a member with priority 1, or one past due by more than 30 days that is not recurring, breaks out as its own row. Accepted: a recurring item stays grouped.
- **Losing Status loses `sd today` parity.** The verify `today` reading (Your work equals the `sd today` rows) must change with slice 2. Accepted: the CLI keeps the full text.
- **Blocked entries on Today and on Queue** are one fact in two places. Accepted: Today shows the row to act on; Queue shows the lane in context. Only Today counts it.
- The day track's Live lane is minutes on a 24-hour axis, so it reads as a nub at "now". If that does not earn its space, it may be dropped in slice 3.

## Open questions (recommended answer first)

1. **Should past-due recurring items show on Today at all?** Recommended: yes, as one group row with "Snooze all 1w" and "Triage". Alternative: drop recurring items from Today, which hides real misses.
2. **Should Queue lose its rail badge?** Recommended: yes; Today counts blocked entries once. Alternative: keep both badges and accept the double count.
3. **Should Decide's waiting count show on Today?** Recommended: no; its rail badge already says it, and Today links to it. Alternative: a ninth lamp, which breaks the 8-lamp cap.
4. **Should briefs be a Needs you row or only a link?** Recommended: one Queued group row a day, with Mark all seen. Alternative: Briefs page only; then unread briefs stop being visible from Today.
5. **Should the day track cover 24 hours or since midnight?** Recommended: the last 24 hours, so a morning view still shows last night's failures.

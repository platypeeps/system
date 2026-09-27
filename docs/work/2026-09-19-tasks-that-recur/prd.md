---
title: tasks that recur
created: 2026-09-19
item: sd:1099
---
# PRD — tasks that recur

## Problem

`sd` has no recurrence. Not a missing flag — no column, no verb, no concept.
A row is a dated one-off, and when it closes nothing brings it back.

Three independent checks agree. No `repeat`, `recur`, `rrule`, `cadence` or
`interval` column exists in any of the ten migrations; the only `due` field is
a `TEXT` column validated as strict `YYYY-MM-DD`. A repository-wide grep of
`sd_db` returns only unrelated prose and `interval_seconds`, which is a
health-check poll period. The CLI's single hit is `RecursionError`, a JSON
parse guard.

The consequence is not theoretical. An site domain has thirteen recurring
compliance obligations, such as annual filings, registrations, reserve
deposits and renewals. **Eleven of the thirteen have no row at all.** The morning
digest selects on dated rows, so an obligation without one cannot reach any
digest, ever.

The workaround in place is prose-holds-the-rule, row-holds-the-next-instance,
with a checker that reports which obligations are uncovered. That is a
tripwire, not a mechanism: it tells a person to do by hand what the store
should do. And it has the failure mode every manual step has — the moment an
annual row is completed, the obligation silently goes uncovered until somebody
runs the checker and notices.

## What to model it on

Two plugins in this household solve the same problem differently, and the
difference is the whole design decision. **The live vault runs TaskNotes
4.13.1**, not Obsidian Tasks — worth stating, because the two have opposite
answers on the question that matters most.

| | Obsidian Tasks | TaskNotes 4.13.1 (installed here) |
|---|---|---|
| Rule format | Natural language — `every 3 weeks on Friday` — parsed by `rrule`'s `parseText` | Raw RFC 5545 RRULE string in frontmatter |
| Anchor choice | A text suffix, ` when done`, appended to the rule | An explicit field, `recurrence_anchor`, values `scheduled` or `completion` |
| On completion | Mutates the original **and** synchronously writes a new task line | **Creates nothing.** One note holds the series; completions accumulate in `complete_instances` |
| Instances | Materialized eagerly, one at a time | Materialized on demand across a past/future horizon |

**Take the anchor semantics from Obsidian Tasks and the spelling from
TaskNotes.** The semantic distinction is the valuable part and both have it:

- **Anchored to the schedule** (Obsidian Tasks' default). A weekly task due
  2026-01-01 next falls due 2026-01-08 whether it was completed on time or
  three weeks late. Drift-free. This is what a compliance deadline needs — the
  state agency does not move an annual filing date because last year's
  filing was late.
- **Anchored to completion** (` when done`). A task completed 2026-03-05 with
  `every 10 days` next falls due 2026-03-15. Drifts by design. This is what
  maintenance needs — "inspect the cistern every three years after the last
  time we actually did it".

Both must exist. Getting this wrong in either direction produces an obligation
that is quietly early or quietly late forever.

TaskNotes' spelling is better for a database on both counts: an RRULE string is
unambiguous and already standard, and an explicit anchor column is queryable
where a text suffix is not.

## Recommended shape

**Store the rule and the anchor as columns on `item`, and create the next
instance at completion.**

- New columns via a `012_*.sql` migration — `SCHEMA_VERSION` is 11 and is
  bumped to 12 in the same commit. 011 is already applied
  (`local-sd-db/sd_db/schema/011_judgment.sql`), so the next free number is
  012; check the schema folder again at build time.
  Plain `ALTER TABLE item ADD COLUMN` is proven by
  migrations 002 and 003, and is unaffected by the CHECK-rebuild problem below.
- The rule is a **stdlib subset of RFC 5545 RRULE** (operator decision,
  2026-09-23): `FREQ` (`DAILY`, `WEEKLY`, `MONTHLY`, `YEARLY`), `INTERVAL`,
  `BYMONTH` and `BYMONTHDAY`. No `python-dateutil`: `sd_db` stays
  dependency-free. Any other part is refused at write time with an error that
  names it, never ignored.
- **Not** inside the `fields` JSON. That column is documented as producer-owned
  keys that freeze a row's kind, and it is invisible to SQL predicates, so
  "which tasks recur" would become a full scan.
- Creating on completion rather than materializing on demand, against
  TaskNotes' model, because everything downstream of `sd` — the digest, the
  deadline index, the dashboard — reads rows. An occurrence that is not a row
  is invisible to all of them, which is the defect this item exists to fix.

The insertion point is one chokepoint: `transition`, in
`local-sd-db/sd_db/writes.py`, carries the comment **"The one function that
writes `item.status`"**. Whether
the spawn belongs there or one level up in `change_status` is a design
decision, and the design document takes it.

## Acceptance criteria

- A task can carry a recurrence rule and an anchor, set at `add` and at `edit`.
- A rule part outside the subset — `COUNT`, `UNTIL`, `BYDAY`, `WKST` and the
  rest — is refused at write time with an error naming the part.
- Completing a recurring task creates the next occurrence as a real row, with
  the rule and anchor carried forward.
- Schedule-anchored and completion-anchored both behave as described above,
  with a test for a task completed late that proves they differ.
- A recurring task with no date is refused rather than silently inert.
- `sd task status <id> done` run twice does not create two occurrences.
- The eleven uncovered site compliance obligations can be expressed.
- `check-compliance-rows.py` in the `site` repo reports zero uncovered
  obligations once they are — that script is the end-to-end test.

## Deliberately out of scope

Taken from what Obsidian Tasks refuses, each for a reason worth inheriting:

- **Count limits** (`for 5 times`). Not implemented there; no demand here.
- **End dates** (`until 2026-12-31`). Rejected upstream because the rrule
  library produced progressively *earlier* dates. Inheriting a known bug is a
  choice, not an oversight.
- **Sub-day granularity.** `due` is `YYYY-MM-DD` and validated by round-trip.
  Recurrence stays at that granularity.
- **Cross-instance dependencies.** Obsidian Tasks strips `id` and `dependsOn`
  from a new occurrence so a chain cannot silently re-point. Do the same.

## Not verified

Three gaps, stated rather than guessed:

- Whether `local-project-dashboard` has its own completion route that bypasses
  `change_status`. If it does, the insertion point must move down to
  `transition`.
- The read-side query shapes. Whether future occurrences should be hidden from
  a backlog, and what `sd today` does with them, is unexamined.
- Whether any existing consumer breaks when a completion returns two row ids
  instead of one.

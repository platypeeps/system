---
title: tasks that recur
created: 2026-09-19
item: sd:1099
---
# Design — tasks that recur

## Storage

Two columns on `item`, added by migration `012_recurrence.sql`, with
`SCHEMA_VERSION` bumped from 11 to 12 in the same commit. **Not 011**: that
slot is taken and applied — `local-sd-db/sd_db/schema/011_judgment.sql`
landed, and `SCHEMA_VERSION` is already 11. Two files claiming one number
leaves the apply order undefined, and `migrations()` refuses it. Read the
number off the schema folder when this is built, not off this page. The
second-machine item (`docs/work/2026-09-22-run-the-framework-from-a-second-machine/`)
also plans "the next free migration", so whichever lands second renumbers:

| Column | Type | Holds |
|---|---|---|
| `recurrence` | `TEXT` | An RRULE string in the subset below, or NULL. TaskNotes' spelling |
| `recurrence_anchor` | `TEXT` | `schedule` or `completion`. NULL when `recurrence` is NULL |

A CHECK on `recurrence_anchor` is tempting and should be **weighed against
migration 009's warning**: SQLite cannot `ALTER` a CHECK, and the usual
rebuild-and-rename is unavailable on this table because four foreign keys
reference `item(id)`, two without `ON DELETE`. Migration 009 had to edit
`sqlite_master` under `PRAGMA writable_schema` to widen the `kind` CHECK.
Adding plain columns is unaffected; adding a CHECK buys validation now and
that same surgery later. **Recommendation: validate in the library, not the
schema.** `due` already sets this precedent — it is a plain `TEXT` column
validated by a round-trip through `date.fromisoformat`.

Both columns must be added to the allow-list of `set_item_fields`, in
`local-sd-db/sd_db/writes.py`, and to the keys `_fields` accepts, in
`local-sd-db/sd_db/workflow.py`, which refuses any key outside its frozen
sets. Without both, the columns exist and no CLI path can reach them.

**Built as `RECURRENCE_FIELDS`, beside `USER_FIELDS` and not inside it.** The
dashboard's details form offers exactly `USER_FIELDS`, and its
`ItemRepository` test holds the two equal. Putting the columns in the set
would fail that test until the form grows a recurrence control, which this
item does not build. `_fields` accepts the union.

## The rule engine

A stdlib subset of RFC 5545 RRULE, in `local-sd-db/sd_db/recurrence.py`
(operator decision, 2026-09-23). No `python-dateutil`: `sd_db` stays
dependency-free.

| Part | Accepted | Role |
|---|---|---|
| `FREQ` | `DAILY`, `WEEKLY`, `MONTHLY`, `YEARLY`; required | The period |
| `INTERVAL` | a positive integer; default 1 | Every Nth period |
| `BYMONTH` | a list of 1..12 | Limits `DAILY`, `WEEKLY`, `MONTHLY`; expands `YEARLY` |
| `BYMONTHDAY` | a list of 1..31 or -31..-1 | Limits `DAILY`; expands `MONTHLY`, `YEARLY`; refused with `WEEKLY`, as RFC 5545 does |

Every other part — `COUNT`, `UNTIL`, `BYDAY`, `BYSETPOS`, `WKST` and the
rest — and every sub-day `FREQ` is refused at write time with
`RecurrenceError` naming the part. A duplicated part, an empty value and a
value out of range are refused the same way. Nothing is ignored: a rule the
engine cannot honour would produce dates the operator did not ask for. An
optional `RRULE:` prefix is accepted, and the stored value is the canonical
spelling: upper case, parts in the order of the table, `INTERVAL=1` dropped.

Without `BYMONTHDAY`, `MONTHLY` and `YEARLY` take the day from the start
date, and `YEARLY` without `BYMONTH` takes the month from it, as RFC 5545
does.

**Month end: skip, not clamp.** `BYMONTHDAY=31` in a 30-day month produces no
occurrence that month, and the rule moves on to the next month that has a
31st. This is RFC 5545's rule for an invalid date, and it keeps the stored
string meaning in this store what it means in TaskNotes. The standard's
spelling of "the last day of the month" is `BYMONTHDAY=-1`, and the engine
accepts it. So `FREQ=MONTHLY;BYMONTHDAY=31` from 2026-01-31 next falls on
2026-03-31, and `FREQ=MONTHLY;BYMONTHDAY=-1` from 2026-01-31 on 2026-02-28.
A monthly task due on the 31st that must happen every month is written with
`-1`.

**A rule that never occurs is refused.** `FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=30`
has no date. The write-time check computes the next occurrence from the row's
`due`, and refuses when the search finds none within a hundred years.

## How the next due is computed and stored

`next_after(rule, start, after)` returns the first occurrence of the series
that starts at `start`, strictly after `after` and after `start`. `start` is
the series' `DTSTART`: `INTERVAL` counts periods from it, and the implied day
and month come from it. `after` moves only the threshold, never the phase;
`change_status` leaves it unset.

`start` is the anchor's date:

- `schedule`: the completed row's `due`. Each spawn starts the next one from
  the date it produced, so a chain of spawns lands on the dates of one RFC
  5545 series.
- `completion`: the completion date. The series restarts there, so the next
  due is the first occurrence strictly after the day the work was done. It
  drifts by design, as the PRD asks: "every three years after the last time
  we actually did it". `FREQ=YEARLY;INTERVAL=3` completed on 2027-02-01 next
  falls due 2030-02-01, whatever the closed row's `due` was.

**An early completion counts from the day it was done, on purpose.** A weekly
task due 2026-09-30 under `completion`, done on 2026-09-23, next falls due
2026-09-30: a week after the last time. The new row can carry the date the
closed row carried. That is the anchor's meaning, not a defect; an obligation
that must not move uses `schedule`.

**A completion-anchored rule that some completion date leaves with no
occurrence is refused when written.** The completion date supplies every
part the rule leaves out: the day, the month, and the phase that `INTERVAL`
counts from. So a check against `due` proves nothing about it. Examples,
each from the reviews of PR #542:

- `FREQ=YEARLY;BYMONTH=2` completed on a 30th asks for February 30th.
- `FREQ=YEARLY;BYMONTH=4` and `FREQ=MONTHLY;BYMONTH=4,6,9,11` completed on a
  31st ask for a 31st those months lack.
- `FREQ=YEARLY;INTERVAL=4;BYMONTH=2;BYMONTHDAY=29` restarted in 2029 lands on
  2029, 2033, 2037 and never on a leap year.
- `FREQ=MONTHLY;INTERVAL=2;BYMONTH=1` restarted in an even month never
  reaches January.

`first_barren_start` in `local-sd-db/sd_db/recurrence.py` tries every start
date in one leap cycle, 2028-01-01 to 2031-12-31. Within that cycle a
completion date affects the rule through its month, its day and its place in
the leap cycle, and those four years hold every combination. It returns the
first start with
no occurrence inside `HORIZON_YEARS`, and stops there. `_recurring` refuses
such a rule at `add` and `edit`, naming that date. The message says to use
the `schedule` anchor or add the missing `BYMONTH` or `BYMONTHDAY`. The check
runs before the occurrence check against `due`, so it answers with its
remedy.

The sweep replaced an earlier rule that refused `INTERVAL` above 1 with any
`BYMONTH` or `BYMONTHDAY`. The sweep catches every rule that one caught, and
it accepts sound rules that one refused, such as
`FREQ=MONTHLY;INTERVAL=2;BYMONTHDAY=1`. `FREQ=YEARLY;BYMONTHDAY=31` is sound
too: without `BYMONTH` it expands over every month, so a 31st always exists.

It costs at most one write-time pass over 1461 starts. Measured on this
machine: 0.5 to 6 ms for the common rules, 19 ms for
`FREQ=DAILY;INTERVAL=7;BYMONTHDAY=13`, and 235 ms for the slowest sound rule
found, `FREQ=DAILY;BYMONTH=2;BYMONTHDAY=29`. A refused rule stops at its
first barren start, under 1 ms for every repro.

**The sweep is a best-effort refusal at write; the completion is the
guarantee.** One leap cycle does not model the Gregorian calendar: 2100 is
not a leap year, and the 100-year horizon crosses it. So
`FREQ=YEARLY;INTERVAL=17;BYMONTH=2;BYMONTHDAY=29` passes the sweep, yet
completed on 2032-03-01 its series runs 2049, 2066, 2083, 2100, 2117 and next
meets a leap day in 2168. `FREQ=YEARLY;INTERVAL=17` completed on 2032-02-29
does the same, taking day 29 of February from the completion date. INTERVAL
13, 15, 19, 21 and 25 fail the same way from some completion dates between
2037 and 2050. The sweep still earns its place: it catches the common cases
at `add` and `edit`, where the message can name a remedy.

**The recurrence never blocks a completion** (final review of PR #542).
`_next_occurrence` does not raise when there is no next occurrence. That
covers a rule with none within `HORIZON_YEARS`, a schedule-anchored row with
no `due`, and a rule or anchor written around `_recurring` by raw SQL. The
status change lands, no row is created, and the rule and anchor are cleared
from the completed row as usual. A `comment` note on the item says
"Recurrence ended:" with the reason: the rule, the anchor, the date searched
from, and "no occurrence ... within 100 years" when that was the cause. It all
happens in `change_status`'s one transaction: if the note fails, the
completion rolls back with it. A completion-anchored row needs no `due`,
because it counts from the completion date.

The completion date is the machine's local calendar date (`date.today()`),
not the UTC date of `now()`, because `due` is a calendar date the operator
wrote and a completion at 20:00 in Colorado belongs to that day.

Nothing new stores "next due". The spawned row's own `due` column **is** the
next due: it is a real row, which is what every reader already selects on.

## The two anchors

```
schedule:    next = rule.after(previous_due)
completion:  next = rule.after(completion_date)
```

`schedule` is the default when `recurrence` is set and no anchor is given,
matching Obsidian Tasks. A compliance deadline is the common case and it must
not drift.

**A late completion is the test that separates them.** A weekly task due
2026-01-01, completed 2026-01-22:

| Anchor | Next due | Why |
|---|---|---|
| `schedule` | 2026-01-08 | The rule advanced from the original date, ignoring when the work happened |
| `completion` | 2026-01-29 | The rule advanced from the day it was done |

Under `schedule` the next occurrence can be **already overdue at creation**.
That is correct and must not be smoothed over: three missed weekly filings
should produce a row that says it is three weeks late, not a fresh one
pretending otherwise. Whether to skip forward to the first future occurrence
instead is the one genuinely open question in this design. Obsidian Tasks does
not skip. Recommend matching it, and revisiting only if a real backlog proves
unusable.

## Where the next instance is created

`change_status`, in `local-sd-db/sd_db/workflow.py`, opens one transaction and
calls `transition`, which `local-sd-db/sd_db/writes.py` documents as "The one
function that writes `item.status`". Two candidate sites:

| Site | Catches | Cost |
|---|---|---|
| `change_status`, after `transition`, inside the transaction | CLI completions | Misses any caller that reaches `transition` directly — source imports, runner-driven transitions, possibly the dashboard |
| Inside `transition` | Every path through the public entry point | Couples the trusted primitive to a policy decision, and still misses the private one |

`transition` is not every status-setting path. Producer code calls the private
`_transition` directly — `local-sd-db/sd_db/writing.py`,
`local-sd-db/sd_db/progress.py`, `local-sd-db/sd_db/recovery.py` and
`local-sd-db/sd_db/publication.py` each do — so a hook in `transition` alone
would miss producer-owned completions.

**Recommend `change_status`.** The dashboard question is settled:
`local-project-dashboard/sd_dashboard/server.py` calls
`workflow.change_status`, and `local-project-dashboard/tests/test_criterion_7.py`
asserts it does. Both the CLI and the dashboard therefore reach recurrence at
`change_status`, and scoping the hook to hand-task workflow transitions keeps
producer-owned rows — imports, recovery, publication — out of it, which is
what they should be.

`create_item`, in `local-sd-db/sd_db/writes.py`, is what the new occurrence
calls. It already writes the opening `status_change` note and accepts an
explicit `created_at`.

## The rule moves to the new row

A completion **moves** the rule: the new row carries `recurrence` and
`recurrence_anchor`, and the completed row has both cleared in the same
transaction. The series is therefore always the one open row that carries
the rule, and "which tasks recur" is a plain `WHERE recurrence IS NOT NULL`.
Each row gets a note naming the other — "Next occurrence: item N, due D" on
the completed row, "Recurs from item P" on the new one — so the history
keeps the chain the columns no longer hold.

The new row copies `kind`, `title`, `body`, `priority` and `repo`, and gets
`due` from the rule and status `planning`. It does not copy `branch`, `path`
or `fields`: `fields` holds producer-owned keys such as `contribution`, and
Obsidian Tasks strips cross-instance links for the same reason.

## Idempotence

`change_status` returns early when the row's status already equals the target,
so completing a done task is a no-op and cannot double-spawn. This is a
property worth an explicit test rather than an assumption, because it is the
only thing standing between a double-click and a duplicate obligation.

Moving the rule closes the second path too. Reopening a completed occurrence
and completing it again spawns nothing, because the completed row no longer
carries a rule.

## Write-time refusals

`capture_task` and `edit_item` refuse, each with a named error:

- a rule on a row with no `due`, and clearing `due` on a row that has a rule;
- an anchor with no rule; the anchor defaults to `schedule` when a rule is
  set without one, and clearing the rule clears the anchor;
- a rule on a kind that `change_status` cannot complete. Only
  `TASK_STATUS_KINDS` rows recur; a rule on any other kind would be inert;
- a `completion`-anchored rule that some completion date leaves with no
  occurrence, found by the leap-cycle sweep above. It names the first such
  date, and runs before the occurrence check, so it answers with its remedy.

## Revision and the return shape

`item_state` computes a `revision` as a SHA-256 over the row plus every note,
inside a SAVEPOINT. Because it hashes `dict(row)`, the two new columns fall
inside the concurrency envelope with no extra work.

But a completion now produces **two** rows, and `item_state` describes only the
parent. The caller learns nothing about the occurrence it just created. Either
the return shape gains the new row's id — changing `sd task status --json`
output for every consumer — or it does not, and the CLI prints the id as prose
only. **Recommend adding it to the JSON**, because the digest and the deadline
index will both want to point at it, and a caller that cannot name the row it
caused cannot verify its own effect.

**Decided:** completing a recurring row returns the parent's state plus two
keys. `next_occurrence` holds the new row's id, or None when the recurrence
ended. `next_occurrence_reason` is None when a row was spawned, or the
recorded reason when it was not, so a caller can surface it. Both keys are
absent when the completed row did not recur, so every existing return is
unchanged.

## Rejected

- **Natural-language rules** (`every 3 weeks on Friday`). Obsidian Tasks parses
  these with `rrule`'s `parseText`. Attractive at the CLI and wrong in the
  store: it makes the stored value ambiguous and locks the schema to one
  library's parser. Accept natural language at the CLI if desired, store RRULE.
- **Recurrence in the `fields` JSON.** Producer-owned, kind-freezing, and
  invisible to SQL predicates.
- **On-demand materialization**, TaskNotes' model. Elegant, and wrong here:
  every consumer downstream reads rows, so an occurrence that is not a row is
  invisible to all of them.
- **A recurrence CHECK constraint.** See migration 009's surgery.

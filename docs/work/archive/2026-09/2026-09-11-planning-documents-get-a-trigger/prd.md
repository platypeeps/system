---
title: planning documents get a trigger
created: 2026-09-11
status: done
item: sd:438
---

# PRD — planning documents get a trigger

## Problem

`docs/work/<item>/{prd,design,implement}.md` is where a change with a shape
worth agreeing on gets agreed on, and nothing starts one. The folder appears
when a person opens a session and types `/sd-plan <slug>`, which means the
documents exist for work someone happened to be sitting in front of. The
database holds 8 open items for this repository alone; three have folders.

Three specific gaps, each of which has cost something already:

1. **A task row and its folder have no connection.** `sd-plan` interviews from
   scratch even when a row already carries the title, the body and the repo.
   The row is the thing that knew what the work was, and the interview asks
   again.
2. **There is no way to say "plan this one" from where the work is listed.**
   The dashboard shows the queue. Acting on a row means leaving the dashboard,
   opening a terminal in the right checkout, and typing a slug by hand.
3. **Nothing does this unattended.** Items age in `planning` until the
   45-day sweep parks them. `sd-status` ranks `idle-planning` at 100 and
   `undated-planning` beside it; the finding is a report nobody is obliged to
   answer.

There is a fourth gap that is not about triggering at all, and it is the one
that breaks silently. **`sd-plan` writes a folder and never registers it.**
Since the retirement, status lives in the `item` row and `import docs-work`
refuses on every repository, so a folder written by the skill has no row and
no readable status anywhere. `sd-status` calls that `status-unreadable`. It
happened once by hand on 2026-09-10 and took a hand-written `INSERT` to undo.
Automating folder creation without closing this multiplies it by the number of
folders the automation writes.

## Requirements

1. A person looking at an item in the dashboard can trigger the creation of
   its planning documents without leaving the dashboard, and without the
   request blocking while an agent works.
2. A scheduled job picks candidate items and triggers the same thing, without
   a person present.
3. The trigger is one executor, not two. A button and a cron job that build
   documents by different routes will diverge, and only one of them will be
   the one anybody debugs.
4. Every folder produced this way has an `item` row when the run ends —
   whichever route produced it.
5. A repository participates only if it has been named as participating. A
   scheduled job that acts on every registered repository acts on
   repositories nobody asked it to touch.
6. Selection is derived, not stored: the job asks the database which items are
   candidates each night rather than reading a list somebody maintains.
7. A run that produces nothing is not a failure. Findings are not failures is
   the standing convention for scheduled jobs here, and an empty queue is a
   finding.

## Assumptions

These are assumptions, not requirements, and each is checkable:

- The runner service is installed and dispatching on any machine that runs the
  scheduled job. Verified on this machine 2026-09-11: `runner.sh status`
  reports `healthy: true`, `dispatch_allowed: true`, pid 1457.
- The runner's default assignment budget of 90 minutes (`budget_minutes=90`,
  in `enqueue` of `local-sd-db/sd_db/runner.py`) is a sufficient bound for one
  planning run. One run has been measured (read 2026-09-17 from exec note 709
  on sd:442): 2026-09-11, 15:58:54Z to 16:14:43Z, 15 min 49 s. Until
  2026-09-17 this line cited a 24-hour ceiling and said nothing had measured
  a run.
- `sd-plan` produces documents of adequate quality unattended. Until
  2026-09-11 it had only ever been run with a person answering its
  interview; one unattended run has since written three documents (exec
  note 709 on sd:442, merged in `2639a7aa`), and nobody has yet judged them
  against an interviewed set (this said "only ever been run with a person
  answering" until 2026-09-17). This is the weakest assumption on the page
  and the acceptance criteria below name it.

## Acceptance criteria

- [x] Triggering from the dashboard for an item that has no folder produces
      `docs/work/<slug>/{prd,design,implement}.md` on a branch, and an `item`
      row keyed `(source='docs/work', external_id='<repo>::<path>')`.
- [x] The dashboard request returns before the run finishes, and the run's
      output is readable afterwards without re-running it.
- [ ] Triggering twice for the same item does not produce two folders.
- [x] The scheduled job, run by hand, selects at most one item per
      participating repository and no item from a repository that is not
      participating.
- [x] The scheduled job exits 0 when it selects nothing.
- [ ] `sd-status` reports no `status-unreadable` item after a run, in the
      repository the run targeted.
- [x] The documents from one unattended run are read by a person and judged
      against what `/sd-plan` produces with a person answering. This criterion
      is a human judgement and is recorded as one, not as a check.

## References

- `sd:424` — the registration gap, and the verb that closed it for this repo.
- `platypeeps/system#243` — `sd-db.sh work register`.
- `platypeeps/sd-ai-command-pack` `skills/sd-plan/SKILL.md` — the procedure
  that writes the documents, `disable-model-invocation: true`.

## Log

- **2026-09-17** — Delivered. One live unattended run from the dashboard
  (sd:981, exec note 2700, assignment 10, 15m54s by the assignment's own
  timestamps) and the nightly by hand are measured on sd:438's note 2707 and
  comment 2711; the owner judged the documents good enough (note 2707, line
  8). Five criteria are ticked on that evidence. Two stay open on purpose:
  a second trigger for the same item was not pressed (the dry run reads the
  working tree, not the row's branch, so it is not the test the page means;
  `implement.md` does count exec note 718 as that second trigger, and this
  entry leaves criterion 3 open regardless; annotated 2026-10-03, sd:1238);
  and `sd-status` in the targeted repository showed the same seven
  `status-unreadable` folders before and after the run, all of them task
  rows that predate it, so the run added none but the strict zero is not
  met. The nightly's pass is vacuous: no repository participated that day.
  The prompt's citation defect found in the judgement is sd:990.

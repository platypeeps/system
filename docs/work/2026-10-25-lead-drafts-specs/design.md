---
title: The headless lead drafts a spec for each captured item; one tap marks it ready
created: 2026-10-25
item: sd:6013
branch: designs-5986
---

# Design — lead-drafts-specs

This page covers system sd:6013. Binding ruling: the lead drafts, and the operator approves (Q33, note #28839).
The lead here is the headless lead: the build dispatcher of system sd:6012 (Q34, note #28838),
designed in `docs/work/2026-10-24-build-dispatcher/design.md`.

## Problem

An item captured on the dashboard is one line, in `planning`.
Someone must still write its spec and open questions before a builder can take it.
Today a lead session does that by hand, so items wait until the operator starts one.

## What exists

- **Capture.** The dashboard's capture box files a `task` in `planning` through `workflow.capture_task`, with `who` `dashboard`.
- **The headless lead.** `sd_db.build` queues assignments and claims them under the machine and repo caps.
  A launchd tick runs one bounded `claude -p` per run, with heartbeat, reap, cost rows and a Builds panel.
- **sd-plan.** Its step 1 already describes an unattended run: use the existing context,
  record routine choices on the item, and ask only what would change the work.
  Its step 6 promotes to `ready` only with acceptance criteria and no open blocking line.
- **Decisions.** A `question` note with `Option:` lines lists on Decide's Decisions view, one button per option.
  A "(Recommended)" option gets a Choose recommended button (sd:5306).
- **Status.** `POST /api/items/<id>/status` with the item's revision already moves an item to `ready`.
  `workflow.change_status` refuses while the item holds an active assignment.

So a draft needs no new store, note kind or route. It needs one new run kind and one card.

## Approach

### Queue at capture

- When a capture names a managed repo with a remote, the same transaction queues one assignment, scope `draft`.
- A capture with no repo, or a personal kind, queues nothing. It stays a plain `planning` item.

### Draft run (pack, `sd_db.build`)

- The tick claims a queued draft as it claims a build.
  At most one draft runs at a time, and it counts against the machine cap only: it holds no branch, so it takes no repo slot.
- The item stays `planning`. Its active assignment already blocks status writes, so nobody marks it ready mid-draft.
- Steps: `claimed`, `drafting`, then the end.
  `drafting` runs one `claude -p` in the repo's checkout, with the read tools only (`Read`, `Grep`, `Glob`),
  on Opus, with a 15 min bound and the run row's `builder_pgid`.
- The prompt is a fixed pack template that applies sd-plan's step 1 and the design template's sections.
  It asks for one reply: `## Spec` (problem, approach, acceptance criteria, risks),
  then `## Questions`, at most three, each with `Option:` lines and the recommended one first, marked "(Recommended)".
- The supervisor parses the final `result` line, never the exit code, as the build judge does.
- One transaction under the run's CAS writes all of this:
  - one `comment` note that starts `Draft spec:`;
  - one `question` note per question;
  - the assignment `done`, and the run released.
  So the notes appear all at once or not at all.
- The model writes nothing itself. It holds no `sd`, `Bash` or edit tool, so its notes pass through the parser.
- A design page (`design.md`) stays a lead session's job.
  The spec says so when the shape needs one, and the operator can leave the item in `planning`.

### The one tap: a Drafts group on Decide

- Decide's Decisions view gains a **Drafts** group above the questions.
  An item lists there while it is `planning` and its newest `draft` assignment is `done`.
- One card per item: title, repo, the `Draft spec:` note, and its open question count.
- **Mark ready** posts the existing status route with the card's revision.
  A stale revision is a 409, as everywhere.
- The item's questions stay where they are: in the same view's questions, below the group.
- Decide is the page where the operator already approves, and the phone layout (pack sd:6017) covers it.
  Rejected: a button on each Tasks row. Tasks lists every item, and the draft text does not fit there.

## Non-goals

- Drafts for items filed by `sd task add` or before this lands. Open question 1 asks.
- A Redraft control. A note on the item and a new capture cover it.
- Writing `design.md` headless.
- A spend cap. Each draft is one bounded run.

## Failure table

| Step | State moved | Failure | Recovery | Test |
| --- | --- | --- | --- | --- |
| Capture | item and draft assignment | the repo is not managed, or has no remote | the item files, and no draft is queued | `test_capture_without_a_managed_repo_queues_no_draft` |
| Capture | same | killed in the transaction | SQLite rolls back both; no item without its draft | `test_capture_and_draft_queue_in_one_transaction` |
| Claim | run row, assignment running | a second tick races | `BEGIN IMMEDIATE` serializes, and the second sees the first's run | `test_one_draft_runs_at_a_time` |
| Drafting | `claude -p` group, log | hang, or the supervisor dies | the 15 min bound kills the group; reap retries once, then ends the run `blocked` with an action note | `test_a_hung_draft_is_killed_and_retried_once` |
| Drafting | none | the model tries a write tool | denied: the run holds the read tools only | `test_a_draft_run_denies_write_tools` |
| Write | notes, assignment done, run released | no final line, or no `## Spec` | unknown is no draft: the run ends `blocked` with the log path; nothing is written | `test_an_unparsed_reply_writes_no_note` |
| Write | same | a late old supervisor writes after reap | its CAS names a released run, so nothing changes | `test_a_late_draft_writes_nothing` |
| Mark ready | item `ready` | the draft is still running | `change_status` refuses while the assignment is active | `test_mark_ready_refuses_during_a_draft` |
| Mark ready | item `ready` | the item changed since the card loaded | a stale revision is a 409; nothing moves | the status route's existing revision test |
| Mark ready | item `ready` | a question is still open | per open question 2 | `test_mark_ready_with_an_open_question` |

## Slices

Both land in the pack: the dashboard and `sd_db` live there (pack sd:5987).

1. **pack: draft runs.** `feat(build): headless lead drafts specs for captured items`.
   Capture queues, the claim, the draft step, the parser and the one write.
   Fail-first: `test_capture_and_draft_queue_in_one_transaction`.
2. **pack: the Drafts group.** `feat(dashboard): Drafts on Decide, one tap marks ready`.
   Fail-first: a DOM test that finds no Mark ready control on a drafted item. Waits on slice 1.

## Acceptance criteria

- [ ] A capture in a managed repo shows a `Draft spec:` note and its questions within one draft run, with no session open.
- [ ] A capture with no repo queues no assignment: `sd assignments list` shows none for it.
- [ ] Mark ready on a drafted card moves the item to `ready`, and `sd task show` names `dashboard` as the actor.
- [ ] A fake model reply with no `## Spec` leaves the item in `planning` with no new note.

## Risks

- **Cost.** Each capture in a managed repo starts one Opus run.
  The dispatcher design measured $0.31-0.50 per run before work. Accepted: one bounded run, one at a time.
- **Read access.** The draft reads the repo's checkout. Private values sit in the config folder, outside it.
- **A weak draft gets approved.** One tap is easy. Mark ready still leaves the build review and the lane review in front of a merge.
- **Evidence.** The parser reads only the run's own final line, tied to its `session_id`.
  A reply from another run cannot land on this item.

## Open questions

Each is filed on system sd:6013 as a question note.

1. Note #31085: do older `planning` items get a Draft button, or only new captures? Recommended: only new captures.
2. Note #31086: may Mark ready move an item whose draft questions are still open? Recommended: no.
   The card shows the count, and the tap waits until each question is answered.

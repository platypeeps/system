---
title: One review queue for labels and nightly suggestions, kept in the workflow database
created: 2026-10-12
item: sd:3635
---

# Design: review-queue

This page covers two items that share one queue: sd:3635 (a labelling and review framework any repository or experiment plugs into) and sd:3638 (a nightly review job whose suggestions the operator approves in the dashboard).
It holds no code.
The inventory read `origin/main` 6c4317b of this repository, the command pack's `lib/sd_db` and `bin/`, the live workflow database read-only, `ui-design/foundation/` and `ui-design/products/system/design.md`.

## Problem

Labelling keeps coming back, and each flow builds its own store and its own sheet.
The Jev experiment draws a CSV sheet and a key file; two hand-label tasks wait on a JSON file with an empty column; the judgment table has a label write path that no tool feeds.
On 2026-10-12 the `judgment` table held 29,355 rows and 0 labels.
The operator has no fast way to answer a stream of small questions, and a labelled set has no common export with provenance.
sd:3638 adds a second stream: up to five suggestions a night that need Approve or Reject.

### Inventory

| Flow | What gets labelled | Where the label lives | What it needed | Reused here as |
| --- | --- | --- | --- | --- |
| Decisions panel (sd:3012), `local-project-dashboard/sd_dashboard/decisions.py` | A choice on a `question` note with two or more `Option:` lines | A `decision` note "Ruling on note #N: option", and the question resolved, in one transaction under the item revision | One button per option; a stale page gets 409 | The store, the question format and the write path |
| Jev experiment entry 2 (sd:2764), `local-jev/jev_experiment.py` `cmd_sheet`, `sealed` | A review tier per head, by hand | CSV sheet and key files outside the checkout; the seal is each answer file's SHA-256 in a note on sd:2764 | Blind: candidates shuffled, model names only in the key. Sealed: no outcome read before the seal. Written once. A `sure` score (1 to 3) and a note | Blind and sealed modes; the seal in a note |
| Jev corpus (sd:2790), `local-jev/jev_corpus.py` | Nothing yet; it keeps each request and response for relabelling | JSONL, folder 0700, 30 days | Raw state as sent, which may be private | A source a plug-in reads; the question it files carries only redacted text |
| Judgment labels (sd:2107, sd:2185), pack `lib/sd_db/judgment.py` `label` | The later right answer for one model call | `judgment.override`, `override_source`, `override_at` | One write path; a different second label is refused without `replace`; a label never comes from the prediction under test | A plug-in's reader writes each exported label through `judgment.label` |
| Phoenix, `local-genai-traces` | Nothing; it stores spans, with content when an experiment records it | Phoenix and `storage/raw/traces.jsonl` | Content may be sensitive | A source a plug-in reads |
| Lane finding dispositions, pack `bin/sd_ship_dispositions.py` | Fix, rebut or park for each blocking finding | The `state` table, bound by digests to the report and head | Evidence tied to the exact head and report; a reason a reader can check | The binding rule: each label names its subject and the hash of the question shown |
| Weekly KISS audit, `<config>/cron-jobs/jobs/<host>/kiss-audit-weekly.job` | Nothing; it files one item of prose | One sd item | Read-only `claude -p` with `sd task add` | The shape of the nightly job |
| Hand-label tasks sd:2213 and sd:2832 (other repositories) | Field names; 3,766 traces | A JSON fixture with an empty human column | The operator fills JSON by hand; sd:2832 is blocked on it | First plug-ins; their volume sets the batch limit |
| Benchmark repository on the work satellite (named in sd:3635) | Scored runs and report review | Not read | Not read | Open gap: read it from the work satellite before slice 1 lands |

The inventory found one store that already does most of the job: a question note, its options and its ruling.
It found no flow that notes cannot carry, so this design adds no table.

## Approach

**One queue, made of existing rows.**

- A **batch** is one sd item filed with `--ref review:<queue>:<batch>`.
  The unique index on `(source, external_id)` makes the batch idempotent: a second filing finds it.
- The batch's first note is a `comment` header: `Queue`, `Schema`, `Source` (repository and commit of the producer), `Mode` and, in sealed mode, `Seal`.
  A note is append-only, so the header cannot change after filing; the item body can, so it holds nothing binding.
- A **question** is a `question` note in the Decisions format, plus one `Subject:` line that names what is labelled (`<repo>@<sha>:<path>`, a trace id, a judgment row).
- A **label** is the Decisions ruling: a `decision` note "Ruling on note #N: <option>", written with the question's resolution in one transaction.
- A label's **comment** is a `comment` note "On note #N: <text>", filed with the shell's `n` capture.
- **Undo** withdraws a ruling: a `decision` note "Withdrawn: ruling #M on note #N" and the question reopened, in one transaction.
  This needs one new `sd_db` function that sets `resolved_at` back to NULL; history keeps both notes.

Rejected alternatives:

- A `label` or `suggestion` table: a second queue and a second store for what a question note and its ruling already hold, with revision checks the Decisions path already has.
- Phoenix annotations or a hosted labelling tool: traces only, a separate service, no link to work items, and outside the foundation design language.
- A generated HTML report for the nightly review: read-only, so no approve control.

### Plug-in contract

A repository or experiment plugs in by writing one batch file and filing it; it declares nothing else.

```json
{"schema": 1, "queue": "jev-entry-2", "batch": "C1", "title": "Label: C1 review tiers",
 "repo": "~/repos/system", "source": "system@6c4317b", "mode": "sealed", "limit": 500,
 "seal": {"sha256": "<64 hex>", "name": "key-C1.csv"},
 "questions": [{"subject": "example.test/repo@1a2b3c4:src/app.py", "text": "Which review tier fits this change?",
                "evidence": ["diff: git show 1a2b3c4"], "options": ["skip", "cheap", "standard", "deep"]}]}
```

`sd-db review file <batch.json>` files it: the item, the header note and every question note in one transaction.
A Python plug-in may call `sd_db.review.file` instead.
The plug-in reads its labels back with `sd-db review export` and acts on them itself, for example through `judgment.label` with source `review-<item>`.

### Label schema

- Options: 2 to 9, unique, each at most 80 characters; the label is the option text, exactly as filed.
- Question text: at most 4 KiB; at most 5 evidence lines; `Subject` at most 200 characters and unique in its batch.
- A label carries no confidence score until an experiment needs one (operator, 2026-10-12).

### Modes

- **open**: the queue shows the question, its subject, evidence and the item's other notes.
- **blind**: the queue shows the question text and options only: no subject, no other notes, no tally by option.
  The producer shuffles the options and keeps hidden answers out of the text; the framework cannot check text, so the producer's tests must.
- **sealed**: blind, plus a key file the producer keeps outside the database.
  Its SHA-256 is in the header, written before any question exists.
  `export --key <file>` refuses a key whose hash differs and refuses while a question is open.
  It closes the batch (status done) before it reads the key, and a done batch refuses every ruling and withdrawal.
  So no label can follow a look at the key, which is the experiment's unseen rule.

### Export with provenance

`sd-db review export <item> [--key <file>] --out <path>` writes JSONL, through a `.part` file and a rename.
The dashboard offers the same document as a download (labels only, never the key).

- Line 1, the manifest: batch item, ref, queue, source, mode, seal hash, the pack commit that exported it, the export time, counts of questions, labels and open questions.
- One line per question: note id, subject, the SHA-256 of the question body, options, the label and its position, the ruling note, who ruled and when, withdrawn rulings, and comments.
  With `--key`, the key's row for that subject is joined in.

### Queue UX

The queue is its own rail page, **Review** (operator, 2026-10-12), in the dashboard's existing shell (`products/system/design.md`): annunciator, ranked ledger and Details.

- Review's annunciator has one lamp per open batch: "▲ Label: C1 review tiers · 287 of 300 open", "▲ Nightly review 2026-10-12 · 5 open".
  A lamp filters the ledger to its batch (`?batch=<item>`, written through `shell.url`).
- Today's Decisions panel skips batch questions and shows one line, "▲ 292 questions wait on Review", that links there; plain decisions keep their rows.
- The ledger lists the open questions, nightly suggestions first, then labelling batches by age.
  Details shows one question large, its options as numbered buttons at least 44px tall.
- Keys: `1` to `9` label the selected question and move to the next open one; `j`/`k` move without a label, which is skip; `u` undoes the last label for 10 seconds (the shell's undo command); `n` captures a comment "On note #N: " on the batch; `Esc` clears the batch filter; `?` lists the keys.
  The page binds no shell key; it declares `PAGE_LIST`, and binds digits under the allowance filed on ui-design as sd:3636 (operator, 2026-10-12).
  The rail entry and its `g`-chord come with that allowance.
- The head subhead line carries progress in tabular mono: "13 labelled · 287 open · 4 skipped · read 09:14 MDT".
- A label lands quietly: the row leaves the ledger, the next question opens, and a toast names it with Undo.
  A 409 shows `▲` "The batch changed; reread", rereads and keeps the place.
  A failed read shows `▨` with its reason, never "nothing to review".
- Touch: the option buttons are the controls; the view works without a keyboard.

### Nightly review job (sd:3638)

One job file in the config folder, shaped like `kiss-audit-weekly.job`, plus a tracked example in `local-cron-jobs/examples/` and its prompt.
It runs at 06:15 local: after the night build, outside the 02:xx slot.

1. **Apply**, deterministic: `sd-db review apply nightly`.
   Each "Approve" ruling becomes one task, `--ref review-approved:<note>`; a ref that exists is skipped, never updated.
   A batch with no open question is closed; a batch older than 7 days has its open questions resolved with "Expired, not ruled" and is closed.
2. **Review**, the model's only step: `claude -p` with read-only tools reads the past day.
   Workflow: lane runs, merges, review rounds and repeat blockers, gate failures, stalls.
   Sessions: builder budgets, failures, retries, cost from transcripts.
   Operations: cron results, health check, dashboard health, stale services, disk and store size.
   It receives the subjects rejected in the last 90 days and writes a batch file, `<bulk root>/system/nightly-review/<date>.json`, through `.part` and a rename.
   It holds no `sd` write tool.
3. **File**, deterministic: `sd-db review file` on that file, with `limit` 5.
   It drops a suggestion whose subject was rejected in the last 90 days, files the first five in the model's rank, and prints what it dropped.

Each suggestion's text names its evidence (log path, PR, note id) and its expected gain; the options are Approve and Reject, with no Defer; an unruled suggestion expires after 7 days (operator, 2026-10-12).
The job runs on Sonnet, as the KISS audit does (operator, 2026-10-12).
A rejected ruling stays on its batch as the record that suppresses a repeat.

### Volume limits

- A batch holds at most 500 questions; a larger set splits into batches.
- The nightly batch holds at most 5 suggestions.
- 500 questions add about 1,000 notes; the database held 16,172 notes on 2026-10-12, so no index or table change is needed.

## Non-goals

- Labels written back into Phoenix as annotations.
- Moving lane finding dispositions into the queue; they keep their digests in the `state` table.
- Folding the weekly KISS audit into the nightly job now; that waits for two weeks of nightly runs and gets its own item (operator, 2026-10-12).
- More than one labeller, agreement scores or labeller identity beyond the note's `session`.
- A new CLI verb for the operator; `sd-db review` is plumbing that plug-ins and jobs call.

## Failure table

| Step | State moved | Failure | Recovery | Test |
| --- | --- | --- | --- | --- |
| File a batch | Item, header note, question notes, one transaction | Process stops after some notes | The transaction rolls back; the rerun files the whole batch | An injected error after the second question leaves no item and no note |
| File again | Nothing | Same ref, same content | No-op; returns the existing item | Filing twice returns one item id |
| File again | Nothing | Same ref, other content | Refused, naming the first difference | A changed option is refused; the batch is unchanged |
| File sealed | Header with seal, in the same transaction | No seal, or a malformed hash | Refused before any write | A sealed batch without `seal` files nothing |
| Label | Ruling note and resolved question, one transaction (exists) | Stale revision | 409; the page rereads | The existing Decisions test, plus one on a batch question |
| Label or withdraw | Nothing | The batch is done | Refused | A ruling on a closed sealed batch is refused |
| Withdraw (undo) | Withdrawal note and reopened question, one transaction | The ruling was already withdrawn | Refused; nothing moves | A second withdrawal is refused |
| Sealed export | Batch set done, then the key read, then the file renamed | Process stops after done, before the file | The rerun on a done batch with the same key exports; no label could land between | The process stops after the status change; a ruling attempt is refused; the rerun writes the file |
| Sealed export | Nothing | A key from another batch | Refused: the hash ties the key to this batch's header | Batch B's key against batch A is refused |
| Sealed export | Nothing | A question still open | Refused, naming the count | One open question refuses the export |
| Nightly review | `<date>.json` through `.part` and a rename | `claude -p` fails, times out or writes invalid JSON | No file; the job exits 1 and its cron report shows it; nothing is filed. A failed run is unknown, not "no suggestions" | Filing invalid JSON refuses and files nothing |
| Nightly file | As "File a batch" | The job reruns the same day | The file exists, so the model does not run again; filing the same file is a no-op | Running the file step twice files one batch |
| Nightly apply | One task per approval, one transaction each, then the batch closed | Process stops after the first task | The rerun skips refs that exist and files the rest, then closes | The process stops after task one; the rerun files task two once and never edits task one |
| Nightly apply | Open questions resolved as expired, then the batch closed | Process stops between | The rerun resolves the rest and closes | A stop after one expiry; the rerun closes the batch |
| Nightly suppression | Nothing | A suggestion repeats a subject rejected in 90 days | Dropped and printed | A rejected subject in the database drops the matching suggestion |
| Queue read | Nothing | The Review page's read fails | `▨` with the reason; never an empty queue | The dashboard test for a failed read |
| Older state | Nothing | Question notes on items that are not batches | Shown as today, one row each | The existing Decisions tests pass unchanged |
| Rollback | Nothing | The dashboard rolls back to the previous commit | Each batch question shows as its own decision; labels still land | Covered by the existing tests of the old code |
| Rollback | Nothing | The pack rolls back, so `sd-db review` is missing | The job exits 1 and reports it; no partial batch | The job example's test checks the exit code |

## Acceptance criteria

- [ ] `python3 -m unittest tests.test_review` in the pack passes with 0 failures and covers every failure-table row that names a pack step.
- [ ] `sh local-project-dashboard/dashboard.sh test` passes with 0 failures, with a test for Today's batch line, the Review page's document and a failed read.
- [ ] A batch of 300 synthetic questions files in one transaction, Review shows it as one lamp, and Today's Decisions panel shows one line for it.
- [ ] On Review, labelling 10 questions takes 10 key presses and writes 10 ruling notes; `u` after the tenth reopens it.
- [ ] A sealed export with another batch's key exits non-zero and changes nothing.
- [ ] One nightly run files one batch of at most 5 questions; a second run the same day files nothing new.
- [ ] `sd-docs-lint` passes from the repository root.

## Risks

- Subjects for nightly suggestions come from a model, so the same idea may get a new subject and slip past suppression; the model also sees the rejected list, which narrows the gap.
- Blindness depends on the producer: the framework hides notes and subjects, but it cannot tell whether question text leaks an answer.
- Question bodies live in the workflow database and its backups; a plug-in must redact as the Jev corpus does before it files.
- Evidence binding: a label ties to its question note id and the SHA-256 of the question body; a batch ties to its ref and its header's source commit; a sealed key ties to the header's hash.
- The benchmark repository on the work satellite was not read; its review flow may need something notes cannot carry.
- A daily `claude -p` run adds model use every night; the 30-minute cap from the KISS job bounds it.

## Decisions

The operator ruled on 2026-10-12, on commit a88c9e2.

- Queue location: its own rail page, Review; Today's Decisions panel links there.
- Label keys: digits `1` to `9` on Review; the design-source allowance is ui-design sd:3636.
- Nightly options: Approve and Reject; an unruled suggestion expires after 7 days; no Defer.
- Plumbing: `sd-db review file|export|apply`, beside `sd-db judgments`, is approved.
- Confidence score: none until an experiment needs one.
- KISS audit: folds into the nightly job after two weeks of nightly runs, as its own item.
- Nightly model: Sonnet.

## Slices

Each slice is one PR. Estimates are working days for one builder, review rounds included.

1. **Pack, sd:3635**, 1.5 days: `sd_db.review` (file, withdraw, export, apply) and `sd-db review file|export|apply`, with the failure-table tests for every pack step.
   Nothing reads it yet, so every machine works as before.
   Before it lands, read the benchmark repository's review flow on the work satellite and confirm the batch file carries it.
2. **ui-design, sd:3636**, 0.5 day: the Review rail entry, its `g`-chord and the digit allowance in `products/system/design.md`, with the `verify.mjs` rule.
   Independent of slice 1.
3. **System, sd:3635**, 2 days: the Review page (annunciator, ledger, Details, keys, export download, undo through withdraw) and Today's one-line link.
   Needs slices 1 and 2. Hallmark runs first, per `.claude/rules/dashboard-ui.md`. Plain decisions work as before.
4. **System, sd:3638**, 1 day: the nightly job example in `local-cron-jobs/examples/`, its prompt and its test; the operator's config gets the job file.
   Needs slice 1; it works before slice 3, since Today's Decisions panel shows the questions one row each until then.
5. **First plug-in, sd:3635**, 0.5 day: a Jev entry 2 sheet in `local-jev`, or sd:2213's names in their own repository, filed as a batch and exported, to prove the contract end to end.
   Needs slices 1 and 3.

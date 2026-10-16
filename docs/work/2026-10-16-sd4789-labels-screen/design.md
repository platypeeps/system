---
title: Kev label files and the Jev hand-label sheet go through the Review queue; Kev's review app retires
created: 2026-10-16
item: sd:4789
branch: sd4789-labels-screen
---

# Design: sd4789-labels-screen

Write this page only when the shape needs agreement before the code.
The item's row holds status, progress and decisions; this page holds the shape.

## Problem

Three labelling paths exist, and only one keeps its labels where a tool can read them.

- Kev's label review (`tools/review` in the Kev checkout under `local-kev/kev/`) is a browser app.
  Its labels live in `localStorage` and leave only as a downloaded `reviews.jsonl`.
  It has no blind mode: a blind file sets `proposed_label` empty, and then `Enter` records an empty accept.
- `local-jev/jev_experiment.py labels sheet` writes a CSV sheet, a Markdown copy and a key CSV, filled by hand.
- The Review queue (pack `lib/sd_db/review.py`, sd:3635; dashboard Review page, sd:3709) already holds batches in sd.db.
  It has open, blind and sealed modes, one question at a time, digits 1 to 9, Undo, comments and a JSONL export.

The item asked for two new tables and a Labels screen.
Both would repeat the Review queue, whose design rejected "a label table" as a second store (system `docs/work/2026-10-12-review-queue/design.md`).

## Approach

Kev's file format and the Jev sheet become two producers of Review batches. No table, screen or key is added.

### Pack: `sd_db.review`

- `from_kev(text, *, batch, mode, source)` turns Kev JSONL into a batch file, the dict `review.file` takes.
  - One question per line: `subject` is the line's `id`; `text` is the instructions, each option's description as `<key>: <description>`, then the document.
  - `options` are the option keys in file order, then `DROP`, the literal `(drop)`. A drop is a label like any other, so Undo and the export treat it the same way.
  - The proposed label and the judges become evidence lines: `Proposed label: <key>` and `Judge <model>: <label>, <rationale>`. Blind and sealed batches never send evidence to the page.
  - Every refusal names the line: not JSON, no string `id`, `document` or `proposed_label`, options not an object, a duplicate `id`, a non-empty proposed label that is not an option (an empty one is no proposal, as in a blind Kev file), more than 8 options (9 with drop), more than 3 judges (5 evidence lines).
    The existing batch limits then apply unchanged: option keys at most 80 characters, text at most 4 KiB, subject at most 200 characters, 500 questions.
    A line over a limit refuses the file; nothing is truncated and nothing is skipped.
  - `file` gains `suppress=True`. The Kev import passes `False`, so the 90-day `Reject` suppression the nightly queue needs never drops a Kev question named like a rejected one; a whole file files or nothing does.
  - Blank lines are skipped, as Kev does.
- `reviews(lines)` turns `export` lines into the `reviews.jsonl` shape, one line per labelled question in batch order:
  `{"id", "verdict", "label", "proposed_label", "note", "reviewed_at"}`.
  - `verdict` is `drop` for `(drop)`, with `label` null.
    It is `accept` when the label equals a non-empty proposed label in an open batch, else `relabel`.
    A blind or sealed batch never yields `accept`: the labeller did not see a proposal.
  - `note` joins the question's comments with a newline; `reviewed_at` is the ruling's time; `proposed_label` is `""` when none.
- CLI, inside the existing `sd-db review` verb, no new command:
  - `sd-db review file --kev FILE --batch NAME [--blind]` files a Kev file under queue `kev`, title `Label: NAME`, source `kev:<file name>`.
  - `sd-db review export ITEM --out PATH --format reviews` writes the `reviews.jsonl` shape through the existing `.part` and rename.
    `--format review`, the default, keeps today's lines.

Why `sd-db review` and not `sd task label`: the queue already lives there, its CLI already files and exports batches, and the system dashboard reads the same module.

### Accept, drop and the old Enter bug

The design source binds the dashboard (`ui-design products/system/design.md`, § Keys): a page must not bind `Enter`, `x`, `n` or `u`, which belong to the shell, and Review owns digits 1 to 9.
So on the Review page:

- Accept is the digit of the proposed option, which Details marks "proposed" in an open batch. There is no accept key, so no key can record an empty accept.
- Drop is the `(drop)` option's digit and button.
- `n` stays the shell's capture, which files the comment the export reads as the note.
- `u` stays the shell's Undo, which withdraws the last label while its toast shows.

This replaces the item's literal `x` and `Enter` keys with the design source's keys.

### System

- Dashboard: Details marks the proposed option in an open batch, read from its `Proposed label:` evidence line. A blind or sealed batch sends no evidence, so nothing is marked.
  No route, write or key is added: filing stays in the CLI, labels use `/api/review/rule` and `/api/review/withdraw`, as today.
- `jev_experiment.py labels sheet --population C1` files the sheet as one blind Review batch, `review:entry-2-hand-labels:<population>`, through `sd_db.review.file` (operator decision, 2026-10-16).
    Its queue, title and source name no arm, since Details shows them in a blind batch.
  - Options `skip`, `cheap`, `standard`, `deep`; the text holds what the CSV held (repository alias, commit, paths, size, routing reason, candidates), and no arm name.
  - The draw is unchanged: `label_kit` still runs with the same seed. Its key is not written: the analysis recomputes it from the sealed inputs whose SHA-256 the summary records.
    So no key file exists while labels are open.
  - `sheet-<population>.json` keeps the counts and input hashes, and adds the batch item and ref.
  - A rerun finds the same batch (same digest) and writes nothing new. While a question is open it prints the open count.
  - Once every question has a label, the rerun closes the batch (status done, its own transaction), so no ruling or withdrawal can land after it.
    The same transaction first checks an existing hand file, so a refusal leaves the batch open.
    Then it writes `labels/hand-<population>.jsonl` once: one line per question, `{row, label, ruling, ruled_at, comments}`, from `review.read`, with no export time, so a rerun on the closed batch writes the same bytes. It prints the file's SHA-256 for sd:2764.
  - It refuses before filing when `labels/sheet-<population>.csv` or `labels/key-<population>.csv` exists: the old sheet's files would be a second, unsynced label source. None exists today (C1's `labels/` holds `C1.jsonl` only).
  - `sure` goes: the Review design records the operator's ruling that a label carries no confidence score until an experiment needs one, and the pre-registration's analysis reads none. A labeller who wants to note doubt uses `n`.
  - The experiment now writes sd.db, through `sd_db.review.file` alone; its docstring says so. Every other read stays `connect(write=False)`.
- Jev's contract holds: nothing in the pack or the dashboard calls Jev, and only the experiment calls the Review queue.
- `local-kev/README.md` points label review at the Review page and the two CLI lines.

### Kev's `tools/review`

It is tracked in the upstream Kev repository, not here; nothing in this repository names it.
Its removal is the operator's call: see the item's question note. This change makes nothing here depend on it.

### Rejected

- Two tables and a Labels screen (the item's plan): a second label store and a second queue screen for what notes and rulings already hold.
- A sealed batch for the Jev sheet: it needs a key file outside the store, which the operator's decision removes.

## Non-goals

- More than one labeller, agreement statistics, or Kev's spot-check sampling and filters.
- Options past 8 per question: Kev allowed 29, click-only past the keys. A file with more refuses at import.
- Moving the experiment's analysis (step 12); it is not built.
- Editing the Kev checkout or the upstream Kev repository.

## Failure table

| Step | State moved | Failure | Recovery | Test |
| --- | --- | --- | --- | --- |
| Import a Kev file | Item, header note and question notes, one transaction (`review.file`) | A line is malformed, duplicated or over a limit | Refused before any write, naming the line; nothing filed | `TheKevImport`: a bad line in the middle files no item and no note |
| Import again | Nothing | Same file, same batch name | `review.file` finds the batch by digest and writes nothing | `TheKevImport`: filing twice returns one item |
| Import again | Nothing | Other content under the same batch name | Refused, naming the first difference | Existing `test_a_changed_option_is_refused...` |
| Label, drop or accept | Ruling note and resolved question, one transaction | Stale revision | 409; the page rereads | Existing `LabelAndWithdraw` and dashboard stale test |
| Undo | Withdrawal note and reopened question, one transaction | Ruling withdrawn already | Refused; nothing moves | Existing `LabelAndWithdraw` |
| Export as reviews | `<out>.part`, then the rename | The process stops before the rename | No `<out>`; the same command writes it again | `TheReviewsExport`: a failed write leaves no `<out>` |
| Export as reviews | Nothing | A blind batch's verdict | `relabel`, never `accept` | `TheReviewsExport` |
| Jev sheet: file | As "Import a Kev file" | The process stops before the commit | Nothing filed; the rerun files the batch | `test_jev_experiment`: a refused file leaves no batch, the rerun files one |
| Jev sheet: file | `sheet-<population>.json` after the batch commit | The process stops between the two | The rerun finds the batch by digest and writes the summary | `test_jev_experiment`: a stop after the file, then a rerun |
| Jev sheet: read back | Nothing | Questions still open | Nothing written; it prints the open count and exits 0 | `test_jev_experiment` |
| Jev sheet: close | Batch status done, one transaction | The process stops after the close, before the file | The rerun finds the batch closed and writes the file; a ruling or Undo between is refused | `test_jev_experiment`: a stop after the close, a refused ruling, then the rerun |
| Jev sheet: write | `hand-<population>.jsonl` through `.part` and a rename | The process stops before the rename | The rerun writes the same bytes: the lines hold no export time | `test_jev_experiment`: two runs a second apart write equal bytes |
| Jev sheet: close | Nothing | `hand-<population>.jsonl` holds other bytes | Refused inside the close transaction, so the batch stays open and Undo works | `test_a_hand_file_with_other_bytes_refuses_before_the_close` |
| Jev sheet: read back | Nothing | A batch closed by hand with a row unlabelled | Refused; no hand file | `test_a_batch_closed_with_an_unlabelled_row_refuses` |
| Older state | Nothing | Batches filed before this change | Read as today; `reviews` reads a batch with no proposal as `proposed_label: ""` | `TheReviewsExport` on a plain batch |
| Older state | Nothing | A sheet CSV or key CSV written by the old code | Refused before filing, naming the file | `test_jev_experiment`: an old `key-C1.csv` refuses and files nothing |
| Import | Nothing | A Kev subject a `Reject` label in the same queue would suppress | Not suppressed: the import passes `suppress=False` | `TheKevImport`: a second batch reusing a rejected subject files every question |
| Rollback | Nothing | The pack rolls back | Kev batches stay readable as plain batches; `--kev` and `--format` are gone | Covered by the old code's tests |

## Slices

1. Pack PR: `from_kev`, `reviews`, the two CLI options, tests. No schema change, so no migration and no dashboard schema move.
2. System PR: Details marks the proposed option; the Jev sheet files a blind batch; README lines; this page.
   The Jev sheet uses only `review.file`, `review.read`, `database.transaction` and `workflow.change_status`, which the pinned pack already has, so this PR needs no pin move.

## Acceptance criteria

- [ ] `python3 -m unittest tests.sd_db.test_review` in the pack passes, with the Kev import and the reviews export tests.
- [ ] A scripted run against a temp database files the 71-name AURA file (traces-research `fixtures/aura-name-labels.jsonl` at `sd-2213-aura-labels`) as one blind batch, the Review document shows 71 questions with no subject and no evidence, 71 rulings land, and `--format reviews` writes 71 lines.
- [ ] `sh local-project-dashboard/dashboard.sh test` and `make test SUITE=jev` pass in system.
- [ ] `sd-docs-lint` passes from the system root.

## Risks

- Blindness depends on the producer's text: the queue hides subject and evidence, not words in the question. The Jev sheet's tests check that no arm name reaches the text, as the CSV's did.
- The dashboard marks the proposal by parsing an evidence line prefix; a hand-written evidence line with that prefix would be marked too. Accepted: it only adds a mark in an open batch.
- The export binds each label to its question note and the SHA-256 of the question body, as today; the reviews shape drops those fields, so a caller that needs them reads the default format.

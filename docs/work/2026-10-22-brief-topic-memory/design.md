---
title: Review daily briefs, and keep memory and sources per topic across runs
created: 2026-10-22
item: sd:5778
---

# Design: brief-topic-memory

Write this page only when the shape needs agreement before the code.
The item's row holds status, progress and decisions; this page holds the shape.
It holds no code. It read `origin/main` fc6ea38b of this repository, the command pack at ae577c829, the writing
plugin's `sd-plugin.json`, the live workflow database read-only, and the `intel-brief` vault routine.

## What exists today

- **Briefs.** The `intel-brief` vault routine (daily) and `intel-weekly` (Saturday) file each brief as an `item` row,
  kind `report`, source `brief`, through `sd reports brief add` (sd:3459, `docs/work/2026-10-13-briefs-in-database/`).
  The pack's `routine_briefs.counts` keeps series, day, words, lead and sha256 in `fields.brief`; the frontmatter's
  `signal-topics` and `quiet-topics` stay in the text only. The database holds 158 brief rows.
- **The Briefs page.** `local-project-dashboard/sd_dashboard/briefs_screen.py:198` (`document`) lists them; Read opens
  `/item/<id>`; Details offers Publish to and Make task (`local-project-dashboard/docs/pages/briefs.md`).
  It keeps no read or reviewed state: `briefs_screen.py:50` (`WATCHDOG_REASON`) and
  `local-project-dashboard/sd_dashboard/v2/static/briefs.js:157` say no reader supplies one.
- **Topics.** The 11 topics are `sdw.topic` rows of the writing plugin (kind `idea`, source `vault`), 9 `active` and 2
  `parked`. Each body holds `Covers`, `Boundaries`, `Sweep notes`, `Ground truth`, `Feeds` and `Provenance`, the
  kind's declared sections. All 11 were created on 2026-08-05, and their `Ground truth` says it was last set then.
- **Managing topics.** Tasks lists them under Kind `base:topic` (`/tasks?base=topic`, `v2/static/tasks.js:487`).
  Details shows the stage select and edits the fields and body (`tasks_screen.py:442` `_base`, `server.py:306`).
  Stage moves are human-only.
- **Who reads topics.** The routine runs `sd store list sdw.topic --status active --full` at the start of every run.
  The pack's `sd-research`, `sd-tips` and `sd-topic-radar` skills read the same list. Today it prints 35,729 bytes.
- **What a run remembers.** Only its own previous brief, to skip repeats and carry up to 3 open items forward.
  No routine writes a topic row. What a run learns about a topic, such as a running story, a source that paid off
  or a source that went dead, lives only in that day's brief text.
- **Writers of a section.** `sd store set-section <kind> <title> --section NAME=TEXT` replaces declared sections
  in one atomic write. It refuses an undeclared section.
- **Notes.** `POST /api/items/<id>/notes` (`server.py:573`) adds a note to any item. The brief rows carry 158
  `status_change` notes and 8 `comment` notes, each a routine's `--extend` record. None comes from the operator.

## Problem

The operator cannot mark a brief as read, or say what in it was useful or noise, so the next run cannot learn.
Each run starts from the topic notes as they stood on 2026-08-05 plus yesterday's brief.
It cannot see a story it followed last week, nor which sources each topic has proved good or dead.
Interactive sessions that read topics, such as `sd-research`, start from the same stale notes.

## Approach

Reuse the topic rows as the per-topic store, and notes as the review record. Add no table, verb or state store.

1. **Two declared sections per topic.** Add `Memory` and `Sources` to the topic kind's `sections.order` and to
   `templates/topic.md` in the writing plugin. `Memory` holds at most 20 dated lines, newest first: open threads and
   settled facts, each line ending with the brief stem it came from. `Sources` holds at most 15 lines, one per
   source: `<source> · last signal <day> · <n> hits in 30 days`, and `dead since <day>` for one that went quiet.
   Every `--full` reader gets them with no change to its command.
2. **The routines write them.** At the end of a run, after `add --extend`, `intel-brief` re-reads each signal
   topic with `sd store get`, merges its findings into `Memory` and `Sources`, prunes to the bounds, and writes both
   with one `set-section` call per topic. A run first finds the briefs newer than the newest stem in each topic's
   `Memory` and folds those in too, so a stopped run is caught up by the next. `intel-weekly` prunes and settles
   `Memory` once a week. The routines live in the vault; the sitting edits them and records each edit with
   `sd task note 5778`.
3. **Review on the Briefs page.** Details gets Review: useful, mixed or noise, and an optional line of text. It
   posts a `comment` note on the brief's item through the existing notes route, with a body that starts
   `Review: <verdict>`. The document reads each brief's newest such note; the ledger shows a reviewed mark and the
   verdict, and the "no unread state" gap becomes "reviewed or not". The routine reads the review notes of the last
   7 days of briefs with `sd note list <item>` and applies them to `Memory` and `Sources`: a noise verdict on an
   item lowers that source, and a useful one keeps its thread open.
4. **Topics link.** The Briefs page header links Topics to `/tasks?base=topic`, where editing already works.

Rejected: memory as notes on the topic row. Notes never lose a concurrent write, but they grow without bound,
and the `--full` readers would not see them without a second read per topic.
Rejected: a topic table or a memory store in the pack. The topic rows already are that store.

## Non-goals

- No dashboard Topics view with charts of coverage per topic; that needs `signal-topics` in `fields.brief` (Q4).
- No change to how briefs are filed, rendered to the vault or published.
- No model call from the dashboard; the routine does all the merging.
- No change to the human-only stage moves on topics.

## Failure table

| Step | State moved | Failure | Recovery | Test |
| --- | --- | --- | --- | --- |
| Run: brief filed | the brief row | the run stops before any topic write | the next run folds in every brief newer than each topic's newest `Memory` stem | slice 2 check: stop a run after `add`; the next run's `Memory` names both stems |
| Run: topic A written, B not | A's sections | the run stops between topics | as above, per topic: B's newest stem is older, so B catches up | slice 2 check, as above |
| Run: one topic write | both sections, one atomic write | `set-section` fails or its answer is lost | the run reports the topic and moves on; a rerun re-reads, so a write that landed is not doubled (stems dedupe) | pack `set-section` tests cover the atomic write |
| Two runs write one topic | one run's lines | the hub and the work satellite replace the same section minutes apart | re-read right before the write narrows the window; the lost lines still stand in that run's brief and come back on the next catch-up | accepted risk (Q2) |
| Review written | one note | the post fails | the toast says not written and nothing changed; the note is one transaction | `test_v2_briefs`: a failed post shows the error and no mark, fail-first |
| Review read | nothing | the notes read fails | the mark is unknown with the cause, never "not reviewed" | `test_v2_briefs`: a failed read shows unknown, fail-first |

## Slices

1. Writing plugin: declare `Memory` and `Sources` on the topic kind and its template. Every reader works unchanged;
   the sections are empty.
2. Vault routines: `intel-brief` and `intel-weekly` read and write the sections and read review notes.
   Outside every repository; recorded by note on sd:5778.
3. This repository: Review in the Briefs page Details, the reviewed mark, and the Topics link.

## Acceptance criteria

- [ ] `sd store set-section sdw.topic "<title>" --section Memory=x` exits 0 after slice 1, and nonzero before it.
- [ ] After two scheduled runs, every signal topic's `Memory` names both runs' stems and holds at most 20 lines.
- [ ] `make test SUITE=dashboard` passes, with the two fail-first `test_v2_briefs` tests above.
- [ ] A review posted on the Briefs page shows on its row after a reread, and the next run's report names it.

## Risks

- The `--full` list grows by up to about 2 KB per active topic, about 18 KB today, in every reader's context.
- The bounds hold only by the routine's text; nothing in code refuses a 50-line `Memory`.
- A `Review:` prefix is a convention on a free-text note, not a note kind; adding a kind is a schema change.
- The routine's merge is a model's judgement. A wrong memory line misleads later runs until the weekly prune or
  the operator removes it on the topic's Details.

## Open questions

1. **What does "review them" mean?**
   - (Recommended) The operator marks each brief useful, mixed or noise, with a line of text, and the next run
     learns from it.
   - Show the adversarial gate's findings, already appended to each brief, as a review panel on the Briefs page.
   - Both.
2. **Who writes topic memory?**
   - (Recommended) The hub's routines only; the work satellite's run reads the sections and writes none.
     One writer, so no lost lines.
   - Both machines' routines, with the re-read narrowing the lost-write window.
   - Also interactive sessions such as `sd-research`: more learning, more writers.
3. **Where does memory live?**
   - (Recommended) Two bounded sections on each topic row, as above.
   - Append-only notes on the topic row: no lost writes, unbounded, and a second read per topic.
4. **Is a coverage view per topic wanted now?**
   - (Recommended) Not now: the Topics link and each topic's `Sources` section answer it.
   - Yes: the pack copies `signal-topics` into `fields.brief`, and the Briefs page gets a Topics view.

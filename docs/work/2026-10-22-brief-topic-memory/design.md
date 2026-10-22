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
- **Seen.** A brief's row on Today, Home, HOA and the realm places has a Seen action (sd:5752, 3f1fe863). It writes
  a snooze mark that holds until the brief changes, read through `today_reports.seen_row`
  (`local-project-dashboard/sd_dashboard/today_reports.py:86`). A brief seen in one place is seen in all of them.
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

## Problem

Each run starts from the topic notes as they stood on 2026-08-05 plus yesterday's brief.
It cannot see a story it followed last week, nor which sources each topic has proved good or dead.
Interactive sessions that read topics, such as `sd-research`, start from the same stale notes.
No page shows which topics produce signal and which have gone quiet.

## Approach

Reuse the topic rows as the per-topic store and Seen as the review. Add no table, verb or state store.

1. **Review is Seen.** Seen already marks a brief as read on every page that lists briefs. Nothing new is built
   for review.
2. **Two declared sections per topic.** Add `Memory` and `Sources` to the topic kind's `sections.order` and to
   `templates/topic.md` in the writing plugin. `Memory` holds at most 20 dated lines, newest first: open threads and
   settled facts, each line ending with the brief stem it came from. `Sources` holds at most 15 lines, one per
   source: `<source> · last signal <day> · <n> hits in 30 days`, and `dead since <day>` for one that went quiet.
   Each write rewrites both sections in place. Every `--full` reader gets them with no change to its command.
3. **The hub's routines write them.** At the end of a run, after `add --extend`, the hub's `intel-brief` re-reads
   each signal topic with `sd store get`, merges its findings into `Memory` and `Sources`, prunes to the bounds,
   and writes both with one `set-section` call per topic. A run first finds the briefs newer than the newest stem in
   each topic's `Memory` and folds those in too, so a stopped run is caught up by the next. `intel-weekly` prunes
   and settles `Memory` once a week. The work satellite's run reads the sections and writes none.
   The routines live in the vault; the sitting edits them and records each edit with `sd task note 5778`.
4. **Signal topics on each brief row.** Pack sd:5798 copies each brief's `signal-topics` into
   `fields.brief.signal_topics`, a list of slugs.
5. **A Topics view on the Briefs page.** It lists each topic row, active first: title, slug, stage, the last day
   it gave signal and its signal days in the page's window, read from `fields.brief`, then the newest `Memory` line
   and the number of `Sources` lines. A row opens `/tasks?row=<id>`, where editing already works.
   A brief row without the field counts as not recorded, never as quiet; the view says how many there are.

Rejected: memory as notes on the topic row. Notes never lose a concurrent write, but they grow without bound,
and the `--full` readers would not see them without a second read per topic.
Rejected: a topic table or a memory store in the pack. The topic rows already are that store.

## Non-goals

- No new review action, verdict or feedback note; Seen is the review.
- No change to how briefs are filed, rendered to the vault or published.
- No model call from the dashboard; the hub's routine does all the merging.
- No change to the human-only stage moves on topics.

## Failure table

| Step | State moved | Failure | Recovery | Test |
| --- | --- | --- | --- | --- |
| Run: brief filed | the brief row | the run stops before any topic write | the next run folds in every brief newer than each topic's newest `Memory` stem | slice 2 check: stop a run after `add`; the next run's `Memory` names both stems |
| Run: topic A written, B not | A's sections | the run stops between topics | as above, per topic: B's newest stem is older, so B catches up | slice 2 check, as above |
| Run: one topic write | both sections, one atomic write | `set-section` fails or its answer is lost | the run reports the topic and moves on; a rerun re-reads, so a write that landed is not doubled (stems dedupe) | pack `set-section` tests cover the atomic write |
| Satellite run | nothing | the work satellite's routine would write a topic | its routine text writes none, so the hub is the one writer | slice 2 check: the satellite's run leaves every topic's revision unchanged |
| Topics view read | nothing | the topic rows or the brief fields are not read | the view is unknown with the cause, never an empty list or "quiet" | `test_v2_briefs`: a failed read shows unknown, fail-first |

## Slices

1. Writing plugin: declare `Memory` and `Sources` on the topic kind and its template. Every reader works unchanged;
   the sections are empty.
2. Vault routines: the hub's `intel-brief` and `intel-weekly` read and write the sections; the satellite's run only
   reads them. Outside every repository; recorded by note on sd:5778.
3. Pack sd:5798: `signal-topics` in `fields.brief`.
4. This repository: the Topics view on the Briefs page. It lands first: until slice 1 a topic shows no `Memory` or
   `Sources` section, and until slice 3 signal shows not recorded.

## Acceptance criteria

- [ ] `sd store set-section sdw.topic "<title>" --section Memory=x` exits 0 after slice 1, and nonzero before it.
- [ ] After two scheduled hub runs, every signal topic's `Memory` names both runs' stems and holds at most 20 lines.
- [ ] A work satellite run leaves every topic row's revision unchanged.
- [ ] `make test SUITE=dashboard` passes, with the fail-first `test_v2_briefs` test above.
- [ ] The Topics view shows each active topic's last signal day, matching `sd reports brief list` for that topic.

## Risks

- The `--full` list grows by up to about 2 KB per active topic, about 18 KB today, in every reader's context.
- The bounds hold only by the routine's text; nothing in code refuses a 50-line `Memory`.
- The routine's merge is a model's judgement. A wrong memory line misleads later runs until the weekly prune or
  the operator removes it on the topic's Details.
- Briefs filed before sd:5798 carry no signal topics unless the pack backfills them; the view shows them as not
  recorded.

## Decisions

The operator ruled on 2026-10-22.

1. **Review is Seen**, which already exists on brief rows on Today, Home, HOA and the realm places (sd:5752).
   Reason: it already records that the operator read a brief, so nothing new needs building.
2. **Only the hub's brief routines write topic memory; the work satellite only reads.** Reason: one writer
   means no lost lines when a run rewrites a section.
3. **Memory lives in two bounded sections on each topic row, `Memory` and `Sources`, rewritten in place.**
   Reason: every `--full` reader sees them with no second read, and the bounds keep them small.
4. **Add a Topics view on the Briefs page now.** The pack copies each brief's `signal-topics` into
   `fields.brief` (pack sd:5798). Reason: the operator wants to see coverage per topic now.

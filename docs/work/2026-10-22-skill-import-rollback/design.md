---
title: Roll back a skill import, and find the skills it informed
created: 2026-10-22
item: sd:5779
---

# Design: skill-import-rollback

Write this page only when the shape needs agreement before the code.
The item's row holds status, progress and decisions; this page holds the shape.
It holds no code. It read `origin/main` fc6ea38b of this repository and the command pack at ae577c829.
The import code is the pack's; slices 1 and 2 land there, slice 3 here.

## What exists today

- **The import.** `sd skill intake <url|name> [--scope pack|user] [--approve REV]` is the pack's
  `bin/sd_skill_intake.py` (pack sd:3560). It fetches a GitHub skill as git blobs, scores risk, overlap and value,
  and prints a verdict: install, fold or decline. It installs only with `--approve` and the revision its report
  printed. The Skills page runs it through `local-project-dashboard/sd_dashboard/skill_intake.py`
  (Evaluate, Re-import, Approve; `local-project-dashboard/docs/pages/skills.md`).
- **Where it writes.** Pack scope writes `contrib/<name>` in the pack checkout that runs `sd` (`scope_roots`,
  `sd_skill.py` `checkout`); nothing commits it. User scope writes `~/.claude/skills/<name>`, outside any git tree.
- **Install in steps.** `install` stages the new copy beside the root, moves the old copy to `stage/old`, moves
  the new one in, then `_drop` deletes the stage and the old copy with it. `recover` finishes or undoes a stop
  between those steps. After a finished install, the previous copy is gone.
- **The record.** One line in the skill's `## Provenance` section names the intake, the day, the source URL and
  the commit (`PROVENANCE_LINE_RE`, `source_of`). The database holds only the tracked
  repositories and their last refresh (`skills.intake_repos`). No row records an install.
- **Fold.** A verdict of fold says the candidate overlaps an installed skill by 0.35 or more. Nothing performs a
  fold: a person edits the existing skill in a pack pull request.
- **Informed skills.** Five pack skills carry a `## Lineage` section that names an outside skill, its repository
  and a revision: `sd-receive-review`, `sd-debug`, `sd-grill`, `sd-tdd` and `sd-typed-holes`. They landed in three
  pull requests on 2026-09-04 to 2026-09-07, before the intake existed. Each Lineage says which parts came from
  outside and which are the pack's own. Nothing reads these sections.
- **Use so far.** No installed skill carries the intake's Provenance line, in the pack or in `~/.claude/skills`.

## Problem

An approved import cannot be undone. A re-import overwrites the previous copy and deletes it; a new import can only
be deleted by hand. For user scope no git history holds the old copy.
An outside skill can also inform edits to skills already installed, through a fold or a Lineage adaptation.
Nothing ties those edits to the import, so rolling the import back leaves them in place, unseen.

## Approach

Keep one previous copy, add rollback as a flag on the existing verb, and list informed skills from the text that
already records them. Add no table and no verb.

1. **Keep the previous copy.** `install` moves the old copy to `.sd-skill-intake/kept/<root>/<name>/` instead
   of deleting it, replacing any earlier kept copy. A new install keeps an `absent` marker instead. The kept
   folder sits in the staging folder the intake already owns, outside the skill roots, so `sd skill list` never
   reads it.
2. **`sd skill intake <name> --rollback`.** It reports what a rollback would do: the diff from the installed copy
   to the kept one, or "removes the skill" for `absent`, and a revision binding both copies. With
   `--approve <revision>` it swaps them through the same staged steps and `recover`, so the rolled-back copy
   becomes the kept one. A second rollback is then the undo.
   It refuses when the installed copy is not the one the intake wrote, which means a local edit since.
   For pack scope, it also refuses when the installed copy is committed and clean in git; it names `git revert`
   through the lane instead, since the lane is the only way the pack moves.
3. **Informed skills.** The rollback report and the Evaluate report list every pack skill whose `## Lineage` or
   `## Provenance` names the same `github.com/<owner>/<repo>`, with the commits that added that line
   (`git log -S`). Rollback changes none of them. The operator files a revert item for each one to undo, and it
   lands through the lane like any pack change. A failed `git log` reads as unknown with its cause, never as none.
4. **Skills page.** Details shows Rollback beside Re-import for a skill with a kept copy. It runs the report
   first, asks, then approves the printed revision, as Approve does today. The informed list shows under it.

Rejected: an automatic revert of informed skills. Each Lineage mixes the outside idea with the pack's own work,
so a mechanical revert would remove both; a builder must edit by hand anyway.
Rejected: a database row per install. The Provenance line and the kept copy carry what rollback needs (Q4).

## Non-goals

- No rollback for skills that `sd-skill-adopt` or a hand copy installed; only intake installs keep a copy.
- No history beyond one previous copy (Q1).
- No change to how the intake fetches, scores or approves.
- No new check that Lineage lines follow a format; the listing reads the repository URL they already name.

## Failure table

| Step | State moved | Failure | Recovery | Test |
| --- | --- | --- | --- | --- |
| Install: keep old | the old copy moves to `kept` | stop after the new copy is in, before the move | `recover` moves `stage/old` to `kept` instead of dropping it | pack test: stop between swap-in and keep; rerun finds the kept copy, fail-first |
| Install: replace an earlier kept copy | the kept folder | stop between removing the earlier kept copy and moving the new one | write the new kept copy under a temporary name, then rename over the old one | pack test: stop after the temporary write; the earlier kept copy stands |
| Install: new skill | an `absent` marker | stop before the marker | `recover` writes the marker when `stage/old` was never made | pack test: stop before the marker, fail-first |
| Rollback: stage | the kept copy is copied to `stage/new` | stop | `recover` drops an unfinished stage (today's rule) | existing `recover` tests |
| Rollback: swap out | the installed copy moves to `stage/old` | stop | `recover` moves it back when the destination is absent (today's rule) | existing `recover` tests |
| Rollback: swap in | the kept copy moves in; the rolled-back copy becomes kept | stop before the kept move | a rollback stage carries a `rollback` marker, so `recover` keeps the copy and skips the "not one this tool wrote" refusal | pack test: stop after swap-in of a hand-written kept copy, fail-first |
| Rollback: `absent` | the installed skill moves to `kept` | stop | as swap out, with the destination left empty | pack test, fail-first |
| Rollback: guard | nothing | the installed copy was edited, or the pack copy is committed | refused, naming the diff or `git revert` | pack tests, fail-first |
| Informed list | nothing | `git log` fails or the pack root is not a checkout | the list is unknown with its cause | pack test: a failing git reads unknown, fail-first |

## Slices

1. Pack: keep the previous copy on install. Every machine keeps working; rollback is not offered yet.
2. Pack: `--rollback` with its report, `--approve`, and the informed list in both reports.
3. This repository: Rollback and the informed list on the Skills page.

## Acceptance criteria

- [ ] After an approved re-import, `--rollback` then `--approve <rev>` restores the previous `SKILL.md` byte for byte.
- [ ] A rollback of a rollback restores the re-imported copy byte for byte.
- [ ] The pack tests stop the process between each pair of steps above, and the next run finishes or undoes it.
- [ ] `--rollback` on a pack skill whose copy is committed exits nonzero, names `git revert` and writes nothing.
- [ ] The Evaluate report for a URL in the same repository as a Lineage line lists that skill and its commit.
- [ ] `make test SUITE=dashboard` passes with a Skills page test for Rollback asking first.

## Risks

- Nobody has used an approved install yet, so this builds for a need not yet measured.
- A pack-scope install writes the pinned pack checkout without a commit; how `repo-sync.sh refresh` treats that
  dirty tree was not checked. Rollback inherits the risk; it does not add to it.
- The informed list matches a repository URL in prose. A Lineage that names its source another way is missed,
  and the list then says less than the truth.
- One kept copy means two re-imports in a row lose the oldest copy.

## Open questions

1. **How many previous copies does an install keep?**
   - (Recommended) One per skill, replaced at each install; a second rollback is the undo.
   - Every copy, with a prune; more disk, and a prune to schedule.
2. **What happens to skills an import informed?**
   - (Recommended) Rollback lists them with their commits; the operator files a revert item for each one to undo.
   - Rollback opens a revert branch per informed skill; a builder still has to separate the pack's own work.
3. **May rollback touch a pack copy that git already holds?**
   - (Recommended) No: refuse, and point to `git revert` through the lane.
   - Yes: roll back the working copy, which leaves the pinned checkout dirty.
4. **Should installs and rollbacks be recorded in the database?**
   - (Recommended) No new record: the Provenance line and the kept copy carry what rollback needs.
   - A note per install and rollback on one item per skill, so the Skills page can show a history.

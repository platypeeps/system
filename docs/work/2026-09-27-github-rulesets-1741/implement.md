---
title: GitHub rulesets and one required check name
created: 2026-09-27
item: sd:1798
---
# Implement — GitHub rulesets and one required check name

## Order

Code lands before settings change, so the readers can tell a migrated branch
from a broken one.

1. **This repository's pull request: S1, S2, S3, S5.** The readers learn the
   two flags and the `needs` closure. No setting changes.
   Check: the five tests in `design.md`, "Code changes", fail on
   `origin/main` and pass on the branch. `sd-db.sh test` and
   `dashboard.sh test` pass with zero skips.
2. **File the pack tasks P1 to P6** with `sd task add` in the pack checkout.
   P2 depends on step 1's merge commit through the pack's CI pin.
3. **Preconditions**, below. Each is checked, not assumed.
4. **Phase 1 for G1**, one repository first, then the other eight.
5. **Phase 1 for G2, G3 and the pack.**
6. **Phase 2 for `system` (S4), G3, G2, then the pack**, one repository at a time.
7. **Close**: acceptance criteria 1 to 5 for every repository in scope.

## Preconditions

- **A readable protection sync.** On 2026-09-27 all 64 `repo_protection` rows
  read `unknown`, `budget exhausted` (review item A2). Criterion 3 needs one
  sync that reaches the 20 managed rows. Run it before step 4, and record the
  per-repository status as the baseline.
- **`sd-status` baseline.** Run `sd-status --json` in each checkout before
  step 4. Keep the output beside the preimage. Criterion 4 diffs against it.
- **Decisions D1 to D5 recorded** in `prd.md`.
- **Open pull requests listed per repository.** Phase 2's swap blocks those
  branched before PR A until they update. Update each, Dependabot's included,
  after PR A merges.

## Phase 1, per repository

Run from any directory. `$R` is the slug, `$D` the preimage folder,
`/Volumes/local/repo-storage/system/rulesets-1741/preimage/<repo>`.

1. Save the preimage:

       gh api "repos/$R/branches/main/protection" > "$D/classic.json"
       gh api "repos/$R/rulesets?includes_parents=true" > "$D/rulesets.json"

2. Write `$D/ruleset.json` from the standard target in `design.md`. Put
   today's check names and strict value from `classic.json` in the
   `required_status_checks` rule. Keep `integration_id` 15368.
3. Create it:

       gh api -X POST "repos/$R/rulesets" --input "$D/ruleset.json" > "$D/created.json"

4. Verify, before anything is removed:

       gh api "repos/$R/rules/branches/main" --jq '[.[].type] | sort'

   Expect `deletion`, `non_fast_forward`, `pull_request`,
   `required_status_checks`. Anything else stops here.
5. Remove classic:

       gh api -X DELETE "repos/$R/branches/main/protection"

6. Verify: the classic `GET` answers 404 `Branch not protected`, and step 4's
   command still prints the four types.

## Phase 2, per repository

1. **PR A.** Add `ci` as `design.md`, "The `ci` job", shows. The PR body lists
   `ci`'s `needs` beside the preimage's check names. They must match (R4).
   Merge under today's rule.
2. **Swap.** Edit `$D/ruleset.json` to the target list. Then:

       gh api -X PUT "repos/$R/rulesets/<id>" --input "$D/ruleset.json"

3. **Probe.** Open a one-line probe pull request. Read its required checks:

       gh pr view <n> --json statusCheckRollup,mergeStateStatus

   Expect `ci` present and `BLOCKED` while it runs. Close the probe unmerged.
4. **PR B** for G2 and `fun-ai-stuff`, as the design says.

## Rollback

Per repository, from `$D`.

- **Phase 1.** Rebuild classic's `PUT` body from the saved `GET`, restore it,
  then remove the ruleset:

       jq '{required_status_checks: {strict: .required_status_checks.strict,
             checks: [.required_status_checks.checks[] | {context, app_id}]},
           enforce_admins: .enforce_admins.enabled,
           required_pull_request_reviews: (.required_pull_request_reviews |
             {required_approving_review_count, dismiss_stale_reviews,
              require_code_owner_reviews, require_last_push_approval}),
           restrictions: null,
           required_linear_history: .required_linear_history.enabled,
           allow_force_pushes: .allow_force_pushes.enabled,
           allow_deletions: .allow_deletions.enabled,
           required_conversation_resolution: .required_conversation_resolution.enabled,
           lock_branch: .lock_branch.enabled,
           allow_fork_syncing: .allow_fork_syncing.enabled}' \
         "$D/classic.json" > "$D/classic-put.json"
       gh api -X PUT "repos/$R/branches/main/protection" --input "$D/classic-put.json"
       gh api -X DELETE "repos/$R/rulesets/<id>"

  The filter was run on one saved `GET` body. It was not sent to the API;
  run it against the first G1 repository's preimage and inspect it before use.
- **Phase 2.** `PUT` the ruleset with the old list. After PR B, revert PR B first.

## Verification

Named before the work. Each is a command whose failing output is stated.

- **Every managed row is covered.** Enumerate from the database, not from
  this page. Every `yes` row of `sd-db.sh repo list` must appear in the
  inventory or a recorded exception. A row in neither fails.
- **No classic protection is left.** For every repository in scope, the
  classic `GET` answers 404. A 200 fails.
- **Every ruleset requires `ci`.** For each, the `rules/branches/main`
  output holds a `required_status_checks` rule whose contexts include `ci`
  with integration 15368. A missing pin fails.
- **No weaker gate.** For each, the rules' strict value and thread resolution
  equal the preimage's. A lower value fails.
- **Readers agree.** The sync files each row `protected`, source `ruleset`,
  both flags clear. `sd-status` shows no new gap against its baseline.
- `sd-docs-lint` clean; `python3 tests/test_citations.py` green.

## BLOCKING

Decisions D1 to D5 in `prd.md`. Step 1 can start before them; steps 4 on cannot.

## Log

2026-09-27 — planned from sd:1741 and the consistency review of 2026-09-26.
Plan only: no GitHub setting changed, no code written. The inventory was
measured read-only with `gh api -X GET`; `collect.py` and `jobs.py` in
`/Volumes/local/repo-storage/system/rulesets-1741/` reproduce it.
`sd_db.protection` (sd:1430) and the pack's stamp (sd:1655) already read
rulesets. The task's "update to read rulesets" therefore reduces to S1, S2
and P2.
`work register` from the planning worktree filed this folder as sd:1798,
under its first, undated folder name. `sd-docs-lint`
then refused that name: a work item folder is named `<YYYY-MM-DD>-<slug>`.
The folder moved to `2026-09-27-github-rulesets-1741`. `sd work relink`
checks the file under `~/repos/system`, so it cannot run before merge. After
merge, from `~/repos/system`, run
`sd work relink 1798 docs/work/2026-09-27-github-rulesets-1741/prd.md`.
sd:1741, the task row, points here.

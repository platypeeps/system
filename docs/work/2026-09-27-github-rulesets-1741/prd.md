---
title: GitHub rulesets and one required check name
created: 2026-09-27
item: sd:1798
---
# PRD — GitHub rulesets and one required check name

Task: sd:1741. Plan only: this item changed no GitHub setting.

## Problem

The managed repositories protect `main` in two ways, and require checks under
many names.

- `system` uses two repository rulesets and no classic protection.
- The pack and 16 other platypeeps repositories use classic branch protection only.
- Two repositories in an organisation the operator does not own (the employer's)
  use classic protection plus a ruleset that blocks force pushes and deletion.
- Required check names differ. Among the owned repositories, nine require `ci`, and four
  require `CI Result`. Five more require other sets: `node`+`python`, `check`,
  `writing acceptance`, `lint`+`unittest (...)`+`body-lint`, and `route`+4 legs.

Tooling cannot ask one question of every repository. A reader must know the
mechanism and the check name per repository before it can say "protected".
The consistency review of 2026-09-26 found this as items B18 and F5/F17.
Its evidence is in
`/Volumes/local/repo-storage/system/consistency-review-2026-09-26/`
(`REPORT.md`, `review-F.md`).

## Measured state, 2026-09-27

Read-only, with `gh api -X GET`, for every row that
`./local-sd-db/sd-db.sh repo list` marks managed (`yes`): 20 repositories.
The collector and its raw output are in
`/Volumes/local/repo-storage/system/rulesets-1741/` (`collect.py`,
`raw.json`, `jobs.py`, `jobs.txt`). The per-repository table is in
`design.md`, "Inventory".

- The operator's token has `admin` on all 20.
- 19 carry classic protection. All 19 have strict checks, 0 required approvals,
  `enforce_admins` on, and no push restrictions.
- Every classic required check is pinned to the GitHub Actions app (15368).
- `system` requires `route` plus four `system-native` legs, not strict. Its
  checks ruleset lets a deploy key bypass (sd:1417, `local-autocommit`).
- The operator is not the sole admin of the two repositories it does not own.
- `platypeeps` is on the `enterprise` plan, so rulesets work on private repositories.
- No organisation ruleset reaches any of the 20 (`includes_parents=true` returned none).

## What already exists

The task text asks to make `sd_db/protection.py` and the stamp read rulesets.
Both already do.

- `sd_db.protection` reads the branch's rules whatever classic answered, and
  layers them onto classic (sd:1430). A ruleset-only branch reads `protected`
  with `source: ruleset`.
- The pack's `sd fleet stamp` reads protection through `sd_protection`, the
  reader `sd-ship` and `sd-status` share (sd:1655, pack #1221). A
  ruleset-only branch reads `protected`.

So the code work here is narrower than the task text says. It has three
parts. Report the mechanism. Report the check name. Stop the aggregate
pattern from raising a false `produced_not_required` gap.

## Requirements

- **R1 — rulesets only.** Every managed repository in scope protects its
  default branch with repository rulesets that target `~DEFAULT_BRANCH`.
  Classic protection is removed, but only after the ruleset reads as active.
- **R2 — no weaker gate.** The ruleset imposes at least what classic imposed.
  It requires a pull request and forbids bypass, force push and deletion.
  Checks stay strict where classic was strict.
- **R3 — one shared check name.** Every repository in scope requires a check
  named `ci`, pinned to the GitHub Actions app.
- **R4 — `ci` gates exactly what is gated today.** `ci` is an aggregate job
  whose `needs` are the jobs required today. No job becomes gating or
  advisory through this item.
- **R5 — no window.** Each default branch stays protected at every moment.
  A pull request based on current `main` can merge once its checks pass.
- **R6 — rollback per repository.** Each repository's preimage is saved
  before its first change. One command sequence restores it.
- **R7 — readers report the baseline.** `sd_db.protection` and the pack's
  `sd-status` report two new flags. One marks a branch not protected by
  rulesets alone. The other marks a branch that does not require `ci`.
- **R8 — the aggregate is not a gap.** A job inside the `needs` closure of a
  required aggregate job is not reported as `produced_not_required`.

## Acceptance criteria

1. For each repository in scope, `gh api repos/<slug>/branches/main/protection`
   answers 404 `Branch not protected`.
2. For each, `gh api repos/<slug>/rules/branches/main` lists four rule types.
   They are `required_status_checks` with context `ci` and integration 15368,
   `pull_request`, `non_fast_forward` and `deletion`.
3. A fresh `sd_db.protection` sync files every row in scope as `protected`,
   source `ruleset`, with both baseline flags clear.
4. `sd-status` in each repository reports no new gap against its pre-change
   output, except the two baseline flags clearing.
5. One probe pull request per repository shows `ci` as required
   (`isRequired: true` in GraphQL) and `mergeStateStatus` `BLOCKED` while `ci` runs.
6. `sd-db.sh test`, `dashboard.sh test` and the pack's suite pass. Each new
   test fails with its checked behaviour removed.

## Out of scope

- Merge methods and squash message settings (review items B16, B17). The
  ruleset mirrors today and restricts no merge method.
- Secret scanning, Dependabot, Issues, Wiki (review items B14, B15, B20).
- Making `route` required in more repositories (review item F8).
- The `repo_protection` rows that read `unknown`, `budget exhausted` (review
  item A2). Criterion 3 depends on it; see `implement.md`, "Preconditions".
- Unmanaged repositories, such as the `no` rows of `repo list`.
- Repositories the operator does not own (employer org), per D1.

## Decisions for the operator

The operator accepted every recommendation on 2026-09-27. `design.md`, "Decisions", records each one.

- **D1** — repositories the operator does not own: in scope, or recorded as an exception?
- **D2** — `route`: stays required in `system` beside `ci`, or is dropped?
- **D3** — the pack's `body-lint`: stays a second required check, or folds into `ci`?
- **D4** — `system` strict policy: stays off (sd:1421), or turns on?
- **D5** — the baseline flag: `ci` must be present, or `ci` must be the only name?

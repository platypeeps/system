---
title: GitHub rulesets and one required check name
created: 2026-09-27
item: sd:1798
---
# Design — GitHub rulesets and one required check name

## Inventory

Measured 2026-09-27, read-only, for the 20 managed rows of
`sd-db.sh repo list`. "Today" is the classic `required_status_checks` list,
except for `system`, where it is the ruleset's list. "Producer" is the
workflow file that ran the check on the last merged pull request.
Every repository's token permission is `admin`.
Two of the 20 belong to an organisation the operator does not own (the
employer's). D1 puts them out of scope, so the table leaves them out.
Six research repositories derived from employer work, and the vault, are
shown by a placeholder name.

| Repository | Mechanism today | Required today | Producer | Group |
|---|---|---|---|---|
| `platypeeps/<vault>` | classic | `ci` | `ci.yml` | G1 |
| `platypeeps/<app-e>` | classic | `ci` | `ci.yml` | G1 |
| `platypeeps/<research-g>` | classic | `ci` | `ci.yml` | G1 |
| `platypeeps/<research-a>` | classic | `ci` | `ci.yml` | G1 |
| `platypeeps/<research-h>` | classic | `ci` | `ci.yml` | G1 |
| `platypeeps/<research-b>` | classic | `ci` | `ci.yml` | G1 |
| `platypeeps/<research-c>` | classic | `ci` | `ci.yml` | G1 |
| `platypeeps/<research-d>` | classic | `ci` | `ci.yml` | G1 |
| `platypeeps/<app-a>` | classic | `ci` | `ci.yml` | G1 |
| `platypeeps/<research-e>` | classic | `CI Result` | `ci.yml`, job `ci_result` | G2 |
| `platypeeps/<app-d>` | classic | `CI Result` | `ci.yml`, job `ci-result` | G2 |
| `platypeeps/<app-b>` | classic | `CI Result` | `ci.yml`, job `ci_result` | G2 |
| `platypeeps/<app-c>` | classic | `CI Result` | `ci.yml`, job `ci_result`, name is an expression | G2 |
| `platypeeps/<research-f>` | classic | `node`, `python` | `test.yml` | G3 |
| `platypeeps/writing-pack` | classic | `writing acceptance` | `writing.yml`, job `writing-acceptance` | G3 |
| `platypeeps/fun-ai-stuff` | classic | `check` | `sd-check.yml`, laid by the stamp | G3 |
| `platypeeps/system` | 2 rulesets | `route`, `system-native (shared, dashboard, runner, tools)` | `sd-review-route.yml`, `system-native.yml` | G4 |
| `platypeeps/sd-ai-command-pack` | classic | `lint`, `unittest (ubuntu-latest, 3.14)`, `body-lint` | `tests.yml`, `pr-body-lint.yml` | G4 |

Groups:

- **G1** (9) already require `ci`. Only the mechanism changes.
- **G2** (4) have a `CI Result` aggregate. It becomes `ci`.
- **G3** (3) require plain jobs from one workflow. A new `ci` aggregate needs them.
- **G4** (2) require checks from two workflows. `ci` covers one; decisions D2 and D3 cover the other.
- The two repositories the operator does not own form no group; D1 records them as out of scope.

Facts that shape the plan:

- Every classic check is pinned to app 15368, GitHub Actions. The ruleset keeps the pin.
- All 19 classic objects: strict, 0 approvals, `enforce_admins` on, no push
  restrictions, no linear history, no signatures, no lock.
- `system`'s checks ruleset is not strict and lets any deploy key bypass it.
  `local-autocommit` pushes through that bypass (sd:1417). sd:1421 tracks the
  strict question.
- The repositories the operator does not own have other admins, including service accounts.

## Target rulesets

### Standard (G1, G2, G3, and the pack)

One ruleset per repository. It copies `system`'s live `pull_request` shape,
which the API already accepted, and classic's strict policy.

```json
{
  "name": "main",
  "target": "branch",
  "enforcement": "active",
  "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
  "bypass_actors": [],
  "rules": [
    {"type": "deletion"},
    {"type": "non_fast_forward"},
    {"type": "pull_request", "parameters": {
      "required_approving_review_count": 0,
      "dismiss_stale_reviews_on_push": false,
      "require_code_owner_review": false,
      "require_last_push_approval": false,
      "required_review_thread_resolution": false,
      "allowed_merge_methods": ["merge", "squash", "rebase"]
    }},
    {"type": "required_status_checks", "parameters": {
      "strict_required_status_checks_policy": true,
      "do_not_enforce_on_create": false,
      "required_status_checks": [{"context": "ci", "integration_id": 15368}]
    }}
  ]
}
```

- `bypass_actors: []` binds administrators. It is classic's `enforce_admins: true`.
- `allowed_merge_methods` lists all three, so it restricts nothing. The
  repository's merge settings still decide. Review item B16 narrows them separately.
- During phase 1 the `required_status_checks` list holds today's names, not `ci`.

### Variants

- **`system`** keeps its two rulesets. `main integrity` is unchanged.
  `main required checks` keeps its deploy-key bypass and its strict `false`
  (D4). Its list becomes `ci` and, per D2, `route`.
- **The pack** lists `ci` and, per D3, `body-lint`.

## The `ci` job

`ci` is an aggregate. Its `needs` are exactly the jobs required today (R4).
So `sd-db-main-canary` in the pack stays advisory, and
`Windows collection (advisory)` in `<research-e>` stays advisory.

```yaml
  ci:
    name: ci
    needs: [lint, unittest]
    if: ${{ !cancelled() }}
    runs-on: ubuntu-latest
    timeout-minutes: 5
    steps:
      - name: Every needed job succeeded
        env:
          RESULTS: ${{ join(needs.*.result, ' ') }}
        run: |
          for result in $RESULTS; do
            [ "$result" = success ] || { echo "a needed job ended: $result"; exit 1; }
          done
```

Rules the job must keep, each learned in this fleet:

- **Never skippable by `if`.** GitHub counts a skipped required check as
  passing. The job runs on `!cancelled()` and decides in a step.
- **`!cancelled()`, not `always()`.** `always()` reports red on a superseded run.
  The comment in `<app-c>`'s `ci.yml` records the measurement.
- **One run per event carries the name.** GitHub resolves a required check to
  the newest run with that name. A label event that skips the real jobs must
  report under another name. `<app-c>` does this with a `name:`
  expression; its rename keeps the expression and swaps only the string.
- **Only one job in the repository is named `ci`.** A ruleset pins the app,
  not the workflow. A second `ci` from another workflow would satisfy the rule.
- G2 repositories keep their aggregate's own step logic, which already
  accepts `skipped` for path-classified jobs. Only the `name:` changes.
- G3 and G4 jobs are unconditional, so the template accepts `success` only.

## Sequence

Two phases per repository. Phase 1 changes the mechanism and no name.
Phase 2 changes the name and no mechanism. Each step leaves `main`
protected, and each requirement is one that pull requests on `main` produce.

### Phase 1 — mechanism (settings only, no pull request)

1. Save the preimage: `GET branches/main/protection`, `GET rulesets` and each
   ruleset, to `/Volumes/local/repo-storage/system/rulesets-1741/preimage/<repo>/`.
2. `POST repos/<slug>/rulesets` with the target ruleset, holding **today's**
   check names and today's strict value.
3. Verify: `GET rules/branches/main` lists the four rules. Classic still stands.
   Both mechanisms now protect `main`; GitHub applies the strictest.
4. `DELETE repos/<slug>/branches/main/protection`.
5. Verify: classic answers 404 `Branch not protected`; the rules still list all four.

No window: the ruleset is active before classic goes. The requirement is the
same, so no open pull request changes state.

`system` has no phase 1.

### Phase 2 — the name (G2 to G4)

Additive first, swap second, subtractive last.

1. **PR A, additive.** Add `ci` beside today's checks. G2 adds a one-step
   `ci` job that needs the aggregate and passes only on its `success`. G3 and
   G4 add the aggregate. Today's names still report, so PR A merges under
   today's rule.
2. **Swap.** `PUT repos/<slug>/rulesets/<id>` with the list changed to `ci`
   (plus D2 or D3 extras). The `PUT` replaces the list in one call. From here
   every pull request on `main` produces `ci`.
3. **PR B, subtractive.** G2 renames the aggregate to `ci` and drops the
   alias. G3 and G4 need no PR B: the old jobs stay as `ci`'s needs.

The only pull requests the swap blocks are those branched before PR A. They
lack `ci`. Strict policy already makes them update after PR A merges, and the
update brings `ci`. `system` is not strict (D4), so an old branch there needs
a manual update.

Per-group notes:

- **`fun-ai-stuff`**: the stamp laid `sd-check.yml`. PR A adds `ci` by hand
  beside `check`. After pack item P1, a re-stamp is PR B.
- **The pack**: the `ci` job of the pack's tests workflow needs `lint` and `unittest`. `body-lint`
  reruns on body edits, which that workflow does not; D3 keeps it separate.

## Rollback

Per repository, from its preimage.

- **Phase 1, after step 4:** `PUT branches/main/protection` with the saved
  body, then `DELETE rulesets/<id>`. Classic is back before the ruleset goes.
  The classic `PUT` body differs from the `GET` shape; `implement.md` carries
  the conversion.
- **Phase 2, after the swap:** `PUT rulesets/<id>` with the old list. Today's
  names still report until PR B, so the old list is satisfiable.
- **Phase 2, after PR B:** revert PR B first, then swap back.

## Code changes

### This repository, `platypeeps/system`

- **S1 — two baseline flags in `sd_db.protection`.** `classify` appends two
  entries to the list it returns as `merge_settings`, beside
  `squash_message` and `rebase_merge`:
  - `protection_source`, flagged when protection is classic or combined,
    value the source;
  - `required_check`, flagged when `ci` is not among the required contexts,
    value the list.
  They are flags, not gaps: an acknowledgement cannot silence them, as with
  `squash_message`. Both apply to owned repositories only (owners
  `platypeeps` and the operator's personal account, the stamp's `OWNERS`).
  A repository outside those owners shows them as not applicable (D1).
- **S2 — the `needs` closure in `produced_contexts`.** A job reachable through
  `needs` from a required job in the same workflow file is covered. It is
  not `produced_not_required`. Without S2, each migrated repository shows
  every inner job as a gap. `_jobs` already walks the job table; S2 reads
  each job's `needs` (scalar or list) the same way.
- **S3 — the dashboard.** `protection_screen` adds the two ids to `FLAGS`.
  It reads each flag from the row's `merge_settings` list, so this is one
  column each. The list's name no longer fits; renaming it is out of scope,
  because stored rows carry it.
- **S4 — `system` itself.** `system-native.yml` gains the `ci` aggregate,
  needing `system-native`. `CLAUDE.md` "Tests and CI" names the required
  checks in one line.
- **S5 — prose.** The `CI Result` example in `local-dependabot/ROUTINE.md`
  becomes "the aggregate check". It is an example, not a name the routine reads.

Tests, each red with its behaviour removed:

- a ruleset-only fixture reads `protected`, source `ruleset`, both flags clear;
- a classic fixture flags `protection_source`; a combined one does too;
- required `CI Result` flags `required_check`; `ci` plus `route` does not (D5);
- an aggregate whose `needs` cover every job reports no `produced_not_required`;
- a job outside the closure still reports it.

### The pack, `platypeeps/sd-ai-command-pack`

File each as an sd task against the pack after this plan is agreed.

- **P1** — the pack's `check_workflow_text` (its `sd_fleet` module) names its job `ci`,
  not `check`. Its test pins the name.
- **P2** — `sd-status` mirrors S1 and S2, with the same ids and sentences.
  `sd_lib.ACKNOWLEDGEABLE_GAPS` must not gain the two flag ids.
- **P3** — the pack's `branch_protection` (same module) reads the rules before it
  answers `unknown` on a non-admin 404. `sd_db.protection` already does. Low
  priority: the operator is admin on all 20.
- **P4** — the pack's own README paragraph "`main` carries classic branch
  protection" is rewritten for the ruleset, after the pack's phase 2.
- **P5** — `sd-ship`'s gate already reads rulesets after a classic 404. Add a
  regression test: a ruleset-only `main` requiring `ci` passes the gate.
  No code change expected.
- **P6** — `docs/spec/backend/quality-guidelines.md` names `CI Result` as the
  required context. It changes with the repositories it describes.

## Decisions

The operator accepted all five recommendations on 2026-09-27. Each "Recommend" below is the decision.

- **D1 — repositories the operator does not own.** Recommend: out of scope,
  recorded as an exception. The operator is not their sole admin, and their
  organisation's policy is not readable with this token. S1 treats them as
  not applicable.
- **D2 — `route` in `system`.** Recommend: keep it required beside `ci`. It
  is a different workflow, and `ci` cannot need it. D5 allows the extra.
- **D3 — `body-lint` in the pack.** Recommend: keep it required beside `ci`.
  Folding it in reruns the full suite on every body edit.
- **D4 — strict in `system`.** Recommend: unchanged here. sd:1421 owns it.
- **D5 — the flag rule.** Recommend: `ci` must be present; other names may
  stand beside it. "Only `ci`" would contradict D2 and D3.

## Risks

| Risk | Consequence | Guard |
|---|---|---|
| A skipped `ci` counts as passing | a failing pull request merges | never `if`-skip `ci`; decide in a step |
| A label event publishes `ci` | a later green run hides a red one | keep `<app-c>`'s name expression |
| Classic deleted before the ruleset is active | `main` unprotected | phase 1 step 3 verifies before step 4 |
| A second workflow names a job `ci` | a wrong job satisfies the rule | one `ci` per repository; S2 test fixture |
| `ci` needs fewer jobs than today requires | a gate weakens silently | R4; PR A lists the needs against the preimage |
| A repository the operator does not own is changed | another owner's settings change | D1; phases run on owned repositories only |
| Readers flag every inner job | noise hides a real gap | S2 and P2 |

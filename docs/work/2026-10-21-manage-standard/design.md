---
title: Turning Managed on applies the standard managed-repo setup, previewed and confirmed per step
created: 2026-10-21
item: sd:5610
---

# Design: manage-standard

Write this page only when the shape needs agreement before the code.
The item's row holds status, progress and decisions; this page holds the shape.

## Problem

The Management page's `managed` control calls `repos.set_managed` (`management_screen.SETTERS`) and flips one
column. Everything else a managed repository needs is set by hand, by one-off sweeps (sd:1669, sd:1670, sd:1672,
sd:1673) or by agents running `gh api` per repo. The result drifts: the four repos registered since 2026-10-17 carry
Actions on, Issues, Wiki and Projects on, and no `delete_branch_on_merge`; the newest has no branch protection, the
GitHub squash defaults and rebase merging allowed. The operator asked that turning Managed on for the first time
bring every setting to the established standard.

## The standard, from evidence

Read on 2026-10-21 from the 23 `repo` rows with `managed = 1`, their `repo_protection` rows, live `gh api` GETs and
`sd fleet stamp --dry-run`. Two of the 23 belong to an employer organization: `sd fleet stamp` already treats an
owner outside `sd.fleet_owners` as "adapted, never changed in its settings", so GitHub counts below read n/21 for the
owned repos where that matters. The outliers' names stay out of this public file.

| # | Setting | Standard value | Where it lives | Setter today | Holds |
| --- | --- | --- | --- | --- | --- |
| D1 | `managed` | yes | `repo.managed` | `repos.set_managed` | 23/23 |
| D2 | `runner_merge` | auto | `repo.runner_merge` | `repos.set_runner_merge` | 23/23 |
| D3 | `ci` | local | `repo.ci` | `repos.set_ci` (also inside `sd ci local`) | 23/23 |
| D4 | `review.copilot_review.automatic_deep` | false, merged into the row's review object | `repo.declarations` | `repos.set_declaration` | 17/23 |
| G1 | Default-branch protection | classic only, no repository ruleset; required check `sd/local-gate`, strict; `enforce_admins`; pull request required, 0 approvals | GitHub branch protection | none creates it; `sd_ci.classic_step` replaces the checks of an existing classic object | 22/23 |
| G2 | Squash commit title / message | `PR_TITLE` / `PR_BODY` | GitHub repo settings | none | 22/23 |
| G3 | `allow_rebase_merge` | false | GitHub repo settings | none | 22/23 |
| G4 | `delete_branch_on_merge` | true | GitHub repo settings | none | 19/23 |
| G5 | `has_issues` | equal to `repo.github_issues` | GitHub repo settings | none | 19/23 |
| G6 | `has_wiki`, `has_projects` | false | GitHub repo settings | none | 19/23 each |
| G7 | Dependabot alerts | on | `PUT repos/X/vulnerability-alerts` | none (enabled by hand on 3 repos on 2026-10-21, note #26287) | 23/23 live |
| G8 | Secret scanning and push protection | enabled, public repos only | `security_and_analysis` | none | 2/2 public; private n/a |
| G9 | Actions | private: off; public: either, file workflows disabled | GitHub Actions permissions | `sd_ci.actions_steps`, inside `sd ci local` | 17/21 owned |
| F1 | Fleet files: `.claude/settings.json` deny baseline, `.gitignore` dashboard lines, `CLAUDE.local.md` block, `docs/dashboard/` | as rendered | the checkout | `sd fleet stamp` | 11/23 with no change |
| F2 | Pre-push leak guard | installed, public repos only | the checkout's hooks | `leak-guard.sh install`; machine-setup `repos` stage nightly | 2/2 public |
| F3 | `CLAUDE.local.md` shared keys | present | the checkout | machine-setup `local_blocks` nightly | follows D1; not counted |

G1, G2, G3 and G9 are what Health's protection matrix already flags (`protection_screen.GAPS`, `FLAGS`). D4 is moot
while `sd.copilot_review` is `never`; it keeps the repo quiet if that setting changes. G1 is the sd:1741 baseline
after sd:3274 moved every repo back to classic protection. G5 and G6 are sd:1670; G2 is sd:1669.

Rejected as not standard, with the reason:

- `allow_auto_merge`: 14/23, mixed; nothing reads it (`sd-ship` merges through the API). sd:1672 set it once and nine
  repos have dropped it since, the pack and system among them.
- Dependabot security updates: 19/23; they open pull requests that only the Dependabot sweep lands, and the sweep's
  consent (`repo.dependabot`) is on for 1/23.
- `clone_profiles`, `dependabot`, `github_issues`, `lane_host`, `channels`, `jira_project`, `pieces_source`:
  per-repo choices (clone sets alone take four values across the 23). The preview shows them; nothing writes them.
- The `status.accepted_gaps` "reviews" entry: 5/23; its text carries the repo's own date and reasoning, and `sd-ship`
  lands the other 18 without it.
- `allow_merge_commit`, `mode`: no reader flags them.

## Approach

**One plan, read first, written by step.** A plan builder reads everything (row, checkout origin, GitHub repo,
classic protection and rulesets, Actions, alerts, open issue count, the checkout's fleet files and branch) and
returns one step per setting with a state: `ok` (already standard), `change` (before and after), `pending` (needs a
pull request), `skip` (with the reason), or `unknown` (a read failed). Each `change` step carries its own write,
decided by its own read, so a second run finds `ok` where the first one wrote. This is the `Step` and `Plan` shape of
`sd ci local` (`bin/sd_ci.py`).

**Builders, not verbs.** The plan reuses the pack's pure builders in process, never the `sd ci local` or
`sd fleet stamp` command lines, for three reasons the review found. Both commands refuse an unmanaged row
(`sd_ci.ci_step`, `sd_fleet.auto_rows`), so they cannot preview a first manage. `sd ci local --apply` re-plans after
the confirm and rewrites repository rulesets with no owner guard (`sd_ci.check_steps`, `ruleset_step`). It also
stops at the first exception before it prints any JSON, so a partial run reports nothing. The plan instead calls
`sd_ci.read_protection` and `actions_steps` and `sd_fleet.plan_repo` (with `ci="local"`, the projected value). It
keeps only the steps the standard allows, and runs each write inside its own `try`. A failed write re-reads that
setting and reports `failed: <reason>; live value <x>`.

**Order: automation last.** The lane picks a repo up as soon as `managed` is yes and `runner_merge` is auto
(`sd_lane`). So the GitHub steps and the checkout steps run first, then D4 and D3, and D1 and D2 last. A run that
stops part way leaves the repo unmanaged and outside the lane, and the Manage sheet still offers the plan. Apply on
a repo that is managed already (the Standard setup row) holds the repo's ship flock for the run, as `set_lane_host`
does, and refuses while a ship holds it.

**G1, case by case.** The fleet standard is classic protection alone.
- No classic object and no ruleset: one `PUT repos/X/branches/B/protection` with all four required fields:
  `required_status_checks {strict: true, checks: [{context: "sd/local-gate"}]}`, `enforce_admins: true`,
  `required_pull_request_reviews {required_approving_review_count: 0}`, `restrictions: null`.
- Classic object present: sub-object writes only. Required checks other than the gate: replace them, as
  `sd_ci.classic_step` does. `enforce_admins` off: `POST .../protection/enforce_admins`. Approvals above 0: `PATCH
  .../protection/required_pull_request_reviews` with a count of 0. No required checks, or no pull-request
  requirement: `skip`, because GitHub offers no write for an absent sub-object and a whole-object PUT from a read is
  lossy. The skip names the settings page and what to add.
- Any active repository ruleset on the branch: every G1 step reads `skip: a ruleset protects this branch; the
  standard is classic protection alone; remove it by hand`. Nothing writes to the ruleset.

**What it never does.**
- It deletes nothing and writes no ruleset.
- It changes no GitHub setting of a repo outside `sd.fleet_owners`; those steps read `skip: employer repository`.
- It turns Issues off (G5) only when the repo has no open issue; otherwise `skip: N open issues; convert them to sd
  items first (sd:1670)`. Hiding issues is the one setting that loses visible data.
- It turns public Actions neither on nor off: a public repo's Actions may be either; only its file workflows are
  disabled (G9, as `sd_ci.actions_steps` reads it).
- It writes tracked files only through a pull request. F1 plans against the checkout as it stands
  (`Tree(root, None)`, the same input the write uses). It writes only the untracked changes, and only when the
  checkout is on its default branch. Tracked changes read `pending`, with one sd item in that repo, "Stamp the fleet
  files", added only when no open item of that title exists. `pending` does not block "standard".

**Evidence ties.** The apply refuses unless three names agree on one repository: the row's `remote`, the checkout's
`origin` and the GitHub `full_name`, compared as lower-cased `owner/name`. The preview returns a digest of the
change steps (subject, before, after, and for F1 the rendered text) and of those names and the default branch. The
apply rebuilds the plan, refuses with the new preview when the digest differs, and runs only the steps the digest
covers. The operator confirms exactly what runs. A step that reads `unknown` never writes and never counts as
standard. Alert state reads from `gh api`'s outcome: success is on and 404 is off, since `sd_ci.gh_call` reports
any success as 200.

**First time.** The server reads past lane use from two existing records: a `runner_run` row for the path, or a
`status_change` note "by sd-ship" on an item of the path. Neither proves past management (`runner.py` claims without
reading `managed`), so the rule only chooses the dialog's default button. On today's data it reads 22 of the 23
managed rows as used before. The exception is the newest, the one repo never set up. Of the 67 unmanaged rows it
reads 1 as used before, a row that still holds `auto`/`local`. Every step is idempotent and previewed, so a wrong
guess costs one click, not a wrong write. No new column or store.

**Unmanage** stays one column: `set_managed no`, as today.

**Where the code lives.** Recommended: a pack verb, `sd fleet standard [--apply] [--json]`, beside `sd fleet stamp`
and `sd ci local`, whose builders it calls and whose fleet owner list it shares. The dashboard runs it the way it
runs `sd-ship` (`land.py`), renders its JSON, and runs F2 (`local-leak-guard`, a system tool) after it. It replaces
the hand-run sweeps and per-repo `gh api` calls. Rejected: a dashboard-only module, which would put a second GitHub
writer in system and leave the CLI without it. The new verb and the dashboard's preview route are new mechanisms; the
operator approves them first.

## UI

The existing `managed` segment control stays the one entry point; no new button.

- **no → yes** opens the Manage sheet instead of a bare proposal. It loads the preview and lists the steps in three
  groups (sd-db row, GitHub, checkout), each as `before → after`, `ok`, `pending` or `skip` with its reason. The
  sheet's risk is `confirm`. First manage: the primary button is "Manage and apply standard", the second "Manage
  only". Used before: the two swap, and a line says why ("this repo has lane history").
- **yes → no** stays the one-column proposal it is today.
- **Standard setup** row in the details pane of a managed repo: "Check" opens the same sheet with "Apply standard".
  It is the drift check for a repo managed before this change.
- The result replaces the preview in the sheet: one line per step (`changed`, `ok`, `pending`, `skipped`,
  `failed: <reason>; live value <x>`), and the item number when tracked fleet files were filed. Health's cells update
  at the next `sd shadow sync`; the sheet says so.

## Non-goals

- No change to unmanage, to the per-field controls, or to the nightly machine-setup stages.
- No edit of a per-repo choice (clone sets, lane host, channels, Dependabot sweep consent).
- No removal of rulesets, workflows files or open issues; no pull request opened by the dashboard.
- No standard for employer-owned repositories beyond the sd-db row.

## Failure table

Steps run top to bottom; D1 and D2 come last so a stop anywhere leaves the repo outside the lane.

| Step | State moved | Failure | Recovery | Test |
| --- | --- | --- | --- | --- |
| Preview | nothing | a GitHub, row or checkout read fails | that step reads `unknown`; the apply never writes it | failing fake call reads `unknown`; an unmanaged `manual` row previews with zero writes |
| Apply: identity and digest | nothing | row remote, origin and `full_name` disagree; plan changed since preview | refuse; return the new preview | mismatched origin refuses; stale digest refuses; a step the digest lacks never runs |
| Apply on a managed repo: ship flock | the flock, held for the run | a ship holds it | refuse: "a ship is running; try again" | held flock refuses before any read-decided write |
| G2–G6 repo PATCH | merge, issues, wiki, projects settings | 403, 422, timeout; open issues | `failed` with the live value re-read; the next run writes what is left | fake 422: later steps still run, result names it; open-issue guard skips G5 |
| G7 alerts | vulnerability alerts | 403 or timeout | `failed`; rerun reads it first (success on, 404 off) | fake 404, PUT, then success reads `ok` |
| G8 secret scanning (public) | `security_and_analysis` | any non-2xx | `failed` with GitHub's message; rerun reads it first | fake 422 reads `failed` and names the message |
| G1 create | classic protection | 403, 422 (nothing created) | `failed` naming the admin token or GitHub's message; rerun tries the PUT again | fake 403 then success; the PUT body holds all four fields |
| G1 create | classic protection | timeout after GitHub applied it | rerun reads protection and moves to the sub-object steps | fake timeout with the write applied: rerun issues no PUT |
| G1 sub-objects | checks, `enforce_admins`, approvals | 403, 404 for an absent sub-object | `failed` or `skip` with the settings page named | classic without checks reads `skip`; approvals 2 becomes 0; a ruleset makes zero writes |
| G9 Actions (private) | Actions off; file workflows (public) | 403, timeout | `failed`; rerun reads the permission first | fake failure on the second workflow disable: the first stays done, rerun finishes |
| F1 stamp | untracked files; one sd item | checkout missing, not on its default branch, stamp refused | `skip` with the reason; rerun files no second item | rerun with the open item adds none; a feature-branch checkout gets no write |
| D4 declaration | `repo.declarations` | refusal | `failed`; rerun writes it | other review keys survive the merge |
| D3 `ci` | `repo.ci` | refusal | `failed`; rerun writes it | stop after D4: rerun writes D3, D1, D2 only |
| D1, D2 | `repo.managed`, `repo.runner_merge` | `HubOnly`, stale row | repo stays unmanaged; the Manage sheet offers the plan again | stop between D1 and D2: rerun writes D2 only; lane picks nothing before D2 |
| F2 leak guard (dashboard, public) | the checkout's `pre-push` hook | a foreign hook, a failed install | `skip: hook differs` or `failed`; nightly machine-setup retries | foreign hook stays byte-identical |
| Unmanage | `repo.managed` | stale | as today | existing tests |

## Slices

1. Pack: `sd fleet standard` with the plan builder, `--json`, and the tests above that need no dashboard. Leaves the
   CLI working on every machine; the dashboard is unchanged.
2. System: the preview and apply routes, the Manage sheet and the Standard setup row, against the pinned pack.

## Acceptance criteria

- [ ] `sd fleet standard --json` on system and the pack reads every step `ok`, `pending` or `skip`, with zero `change`.
- [ ] On the newest managed repo it lists G1, G2, G3, G4, G5, G6 and G9 as `change`, and a second `--apply` run
      reads zero `change`.
- [ ] An unmanaged `manual` row previews the full plan and the preview writes nothing (row and fake GitHub unchanged).
- [ ] On an employer-owned repo, and on a repo with a ruleset, the fake GitHub records zero writes to the skipped steps.
- [ ] Each failure-table test fails on `origin/main` before the change and passes after it.
- [ ] `make test SUITE=local-project-dashboard` passes with the Manage sheet tests.

## Risks

- The dashboard runs as a launchd service; `gh` there must hold `admin` on the repo. `sd ci local` refuses without
  it, and the new steps refuse the same way, before any write.
- Classic protection with `enforce_admins` and a required `sd/local-gate` blocks direct pushes at once; that is the
  intent (sd:1673), but the repo's first pull request needs a gate `sd-check` can run.
- The "used before" rule reads lane history, not management; a wrong read costs the default button only.
- GitHub writes are not atomic. Accepted: each step is read-decided and reported, and Check is the retry.
- The evidence ties are the plan digest, and one `owner/name` agreed by the row's remote, the checkout's origin
  and GitHub's `full_name`, with the default branch.
- The pack builders are internal functions; a pack change can move them. The verb's tests pin their shapes.

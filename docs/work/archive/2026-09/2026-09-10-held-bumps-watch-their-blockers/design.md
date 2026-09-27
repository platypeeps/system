# Design — held bumps watch their blockers, on one page

The `prd.md` beside this file is the decision trail. This page is what the
watcher is checked against: each requirement in a few sentences. When the
two disagree, the `prd.md` is right and this page is fixed.

## 1. What a hold record is

A fenced `yaml` block in a comment on a Dependabot pull request, opening
with `sd-hold:`, carrying `since`, `reason` and a `lifts-when` list. The
comment is the registration; the pull request is where a person reads it;
no file in this repository names a held pull request. A block counts only
when its comment's author has write access to the repository, read from
GitHub at run time; anyone else's is logged and ignored. Three probe
kinds, each one read: `issue-closed`, `npm-version`, `npm-dep`. Any probe
true lifts the hold. A probe that does not parse is reported, not
guessed; one whose read fails is `unknown`, and a record with no true
probe and an unknown one is neither lifted nor carried that run.

## 2. What the watcher does

`dependabot-holds-weekly`, a launchd job in `local-cron-jobs/`, listed in
`personal.cron`. It enumerates the fleet the way `dependabot-daily` does
and applies the same two gates, then searches each admitted repository's
Dependabot pull requests, open and closed, for the block. Open records are
evaluated, probes run serially; closed records are read only to detect
supersession. Lifted: the record is edited to `lifting`, then one comment
with the evidence, `@dependabot rebase`, the record edited to `lifted`, a
`LIFTED` log line. Not lifted: a `HELD` line and silence on the pull
request. A record found at `lifting` is resumed, never repeated; one
already `lifted` is inert, logged `LIFTED-EARLIER`, and touched by
nothing, which is what makes a weekly run idempotent against a probe
that stays true. Never a merge. Exit 0 unless it could not run at all.

## 3. Supersession

Dependabot replaces a held pull request when the dependency releases
again, and the closed one is where the record is, which is why discovery
reads closed pull requests. The evidence is Dependabot's own `Superseded
by #new` comment on the closed pull request, and nothing looser: a
package name matches too many branches. A closed pull request with an
unlifted, uncarried record and that comment, where `#new` is open and
has no record, is carried: the block is posted on `#new` with `since`
preserved, the closed record gains `carried-to`, the log says `CARRIED
old -> new`, and the probes run against `#new` in the same pass.

## 4. The contract, with an executable reference for the half a test can pin

The contract in `local-dependabot/ROUTINE.md` is what is reviewed, written
in the same imperative voice as the merge conditions: what the job may
touch, what it may never do, what it prints. But acceptance criteria 2, 3, 4 and 6
assert the watcher's behaviour *against a GitHub double and a registry
double* -- edit order, comment text, `@dependabot rebase`, idempotence,
supersession -- and a prompt cannot be asserted against a double; only
code can. So the mechanical half has an executable reference, decided on
2026-09-11 when PR 1 was built: `local-dependabot/dependabot.sh holds`,
implemented in `local-dependabot/holds.py`, runs discovery through the two
gates, the writer check, the probes, the lift sequence and supersession as
the contract states them, with every GitHub and registry call behind a
runner the suite replaces (`SD_HOLDS_GH`, `SD_HOLDS_NPM`, the way
`local-sd-plan` replaces its runner). The job is that command and nothing
more -- a `JOB_COMMAND` in `local-cron-jobs`, not a prompt, decided on
2026-09-12 when PR 2 was built -- and its output is the log, in the
contract's own words. The judgement -- writing a hold in the first place,
wording it, choosing which probe names the blocker -- stays prose in the
daily sweep's section, and when the text and the reference disagree, the
text is the contract and the reference is fixed.

## 5. The <app-c> case is the first record and part of the build

`platypeeps/<app-c>` #308, #309 and #317 gain the block by hand in the
slice that lands the job. The installing pull request quotes a hand run
showing all three evaluated. That run is the acceptance test the operator
asked for on 2026-09-10, and the feature is not done without it.

## Landing order

Contract first, in `ROUTINE.md`, with the fixture tests for the probes and
the discovery gates; then, once the block format is on `main`, the three
comments on <app-c>, which need nothing else from this repository;
then the job file and the profile line, whose pull request carries the
hand run that discovers those three records. The comments come before the
hand run because a run that cannot find them is criterion 7 failing.

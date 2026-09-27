---
title: held bumps watch their blockers
created: 2026-09-10
status: done
item: sd:426
---

# PRD — held bumps watch their blockers

## Problem

`dependabot-daily` holds what it cannot justify merging and says why in the
log. That is the job working. But the log is the end of the road: a hold
whose reason is *upstream has not shipped the fix yet* sits until a person
remembers to look, and nobody remembers, because the daily sweep reports the
same hold every morning in exactly the same words until the words mean
nothing. A hold with a named blocker is a question with a known answer
source, and the backbone asks nobody.

The case that surfaced this, on 2026-09-10 in `platypeeps/<app-c>`:

- **#308 and #309**, `vitest` and `@vitest/coverage-v8` 4.1.11 → 5.0.0.
  Majors, so never auto-merged, and held on triage because vitest 5 changed
  `Assertion<T>` to `Assertion<R, T>` and `@testing-library/jest-dom` 7.0.1
  still augments the one-parameter shape, so every DOM matcher fails
  typecheck. The blocker is `testing-library/jest-dom#738`, open. The
  unblock is that issue closing, or a `@testing-library/jest-dom` release
  above 7.0.1.
- **#317**, `nodemailer` 9.1.1 → 10.0.1. Held because nodemailer 10 ships
  its own types and `@payloadcms/email-nodemailer` 3.89.0, the latest, still
  depends on `nodemailer@^9.0.1`, so `transportOptions` no longer typechecks
  at the `nodemailerAdapter` call in `apps/web/src/payload.config.ts`. The
  unblock is a
  `@payloadcms/email-nodemailer` release whose `nodemailer` dependency admits
  10.

Each hold is a comment on the pull request in prose. Each unblock is one API
call: an issue's state, a package's `dependencies` on the registry. Nothing
runs them. A cloud routine was proposed for this on 2026-09-10 and not
created, by the operator's decision, because the check belongs to the
backbone this repository already keeps: the fleet enumeration, the owner
gate, the launchd jobs and their failure banner, the log that is the product.
A routine outside it would be one more list nobody registered.

## What this changes

The Dependabot sweep gains a second verb. Today it *holds*; after this it
also *watches*: a hold with a named blocker carries a machine-readable record
of what would lift it, a scheduled job evaluates every such record across the
fleet, and when a blocker lifts the job says so on the pull request, asks
Dependabot to rebase, and puts the change in the log where a person reads
it. Holds without a named blocker, a major the owner has to decide, are
unchanged: there is nothing to watch.

Three requirements. The third is the operator's explicit condition on this
item: the feature is not built until the <app-c> check above is one of
the records it evaluates, and its first run evaluates it.

### Requirement 1 — a hold with a blocker is a record, not a paragraph

When the sweep, or a person, holds a Dependabot pull request on an upstream
blocker, the hold comment carries a fenced block the watcher can read:

```yaml
sd-hold:
  since: 2026-09-10
  reason: vitest 5 Assertion<R, T>; jest-dom 7.0.1 augments Assertion<T>
  lifts-when:
    - issue-closed: testing-library/jest-dom#738
    - npm-version: "@testing-library/jest-dom > 7.0.1"
```

- **The record lives on the pull request.** Not in a file in this
  repository and not, yet, in the database. The hold is made where the pull
  request is, the person who reads the pull request sees why it is held and
  what would lift it in one place, and the record survives this machine. A
  row can mirror it later, item B's database being the natural home; the
  comment stays the source until then.
- **Probes are a closed set with fixed semantics**, so the watcher never
  reasons about an unblock in prose. Three probes to start, each one
  request: `issue-closed: <owner>/<repo>#<n>`, true when the issue or pull
  request is closed; `npm-version: "<pkg> <op> <version>"`, true when the
  registry's latest satisfies the comparison; `npm-dep: "<pkg> <dep>
  <range>"`, true when the latest published `<pkg>` declares `<dep>` with a
  range that admits some version in `<range>`. A `lifts-when` list is a
  disjunction: any probe true lifts the hold. A probe the watcher cannot
  parse is `malformed`, reported and never treated as true or false; like
  `unknown` below, it does not stop a valid true probe beside it from
  lifting, and a record with no true probe and any malformed one is
  `MALFORMED` for the run, neither lifted nor carried. A probe whose read
  fails, a network error, a rate limit, an authentication failure, a
  package or issue that does not exist, is `unknown`, a third outcome
  beside true and false: it is logged with the error, and a record with
  no true probe and any unknown one is `UNKNOWN` for the run, not `HELD`,
  and is neither lifted nor carried until every probe reads. A true probe
  beside an unknown one still lifts, since true is true.
- **Discovery enumerates, never recites.** The watcher finds held pull
  requests by searching Dependabot pull requests, open and closed, for the
  fenced block, in every repository the daily sweep's two gates admit and
  no other. Open records are evaluated. Closed records are read for one
  purpose only, supersession below, and never evaluated or commented on.
  There is no list of watched pull requests anywhere; the block on the
  pull request is the registration, and removing the comment is the
  deregistration.
- **Only a writer's block counts.** A comment is user-controlled, and on a
  public repository anyone can paste a block whose probe is already true
  and have the watcher comment and ask for a rebase on their behalf. So a
  block is actionable only when its comment's author has write access to
  the repository, read from GitHub at run time,
  `repos/<slug>/collaborators/<login>/permission` answering `admin`,
  `maintain` or `write`, and never from `author_association` alone. A
  block from anyone else is logged `IGNORED #<n> <login> no write access`
  and neither evaluated nor carried.
- **A lifted record says so, and is inert.** When a probe lifts a hold the
  watcher first edits the record to say `lifting: <date> <probe>
  <evidence>`, then comments and asks for the rebase, then edits it again
  to `lifted:`. `lifting` is the pending state: a run that finds one
  resumes it, posting the comment if no comment of its own carrying
  `sd-hold-lifted #<n>` exists, asking for the rebase if no
  `@dependabot rebase` of its own follows the record, and only then
  writing `lifted`. A record carrying `lifted` is never evaluated,
  commented on, rebased or carried again; it stays on the pull request as
  the account of why the hold ended. This is what makes the weekly run
  idempotent and crash-safe: the probe stays true forever, the record is
  what says the watcher already acted on it, and a crash between the two
  edits loses nothing because the next run finishes the sequence.
- **`ROUTINE.md` writes the block, once.** The daily sweep's hold reports
  gain the block whenever the agent can name the blocker as one of the
  three probes, and the contract says so in the same imperative voice as
  the four merge conditions. A hold it cannot express as a probe stays
  prose, and the summary counts the two kinds separately, so a reader can
  see how many holds the watcher is not watching. A pull request that
  already carries a block gets no second one: an unlifted block is the
  live hold and the sweep reports it as `held, watched`; a lifted block is
  history, the sweep reports the pull request as `lifted <date>, awaiting
  owner`, and it writes a new block there only when it names a blocker
  none of the existing blocks' probes name, a genuinely new reason, never
  the one that already lifted.

### Requirement 2 — the probes run on a schedule and are loud only on change

- **A launchd job, weekly.** `dependabot-holds-weekly` in
  `local-cron-jobs/jobs/`, listed in `personal.cron`, so `machine-setup.sh`
  installs it and the watchdog notices when it goes quiet. Weekly matches
  the cadence of upstream releases and keeps the pull request threads
  quiet; the daily sweep already re-reports the hold every morning and does
  not need a second voice.
- **Serial, one call per probe, no fan-out.** Held pull requests across the
  fleet are tens, not thousands, and every probe is one GitHub or registry
  read. Nothing here justifies subagents.
- **On a lift, five things, in order.** The record is edited to
  `lifting`, so a crash after this step is resumed and not repeated; then
  a comment on the pull request, carrying `sd-hold-lifted #<n>`, naming
  the probe that lifted and the evidence, the closed issue's URL or the
  registry version and range seen; then `@dependabot rebase`, so the pull
  request comes back current; then the record is edited to `lifted`; then
  a line in the log, in the words a person would search for, `LIFTED #317
  platypeeps/<app-c> npm-dep @payloadcms/email-nodemailer nodemailer
  ^10 satisfied by 3.90.0`. The job does not merge. A lifted hold on a
  major is still a major, and the daily sweep's four conditions decide
  everything after the rebase. The next run finds the record `lifted`,
  logs `LIFTED-EARLIER #317 2026-09-24` and touches nothing; one that
  finds `lifting` finishes the sequence and logs `LIFTED (resumed)`.
- **On no change, one line per held pull request and nothing on the pull
  request.** `HELD #308 platypeeps/<app-c> 2 probes, none lifted`, so
  the log names what it watched and a silent week is distinguishable from
  a job that did not run.
- **Supersession carries the hold forward.** Dependabot closes a held pull
  request and opens a newer one when the dependency releases again, and
  the new pull request has no comment. This is why discovery reads closed
  pull requests. Supersession is established by Dependabot's own word and
  nothing looser: when it closes a pull request in favour of a newer one
  it comments `Superseded by #<new>` on the old one, and that comment,
  authored by `app/dependabot`, is the only evidence the watcher accepts.
  Branch names are not: this repository's own case has `vitest` and
  `vitest/coverage-v8` open side by side, and a group, a second base
  branch or a second manifest directory can each put two Dependabot pull
  requests on one package, so a match on the package name could carry a
  blocker to the wrong pull request and ask it to rebase. A closed pull
  request carrying an unlifted, uncarried record and a `Superseded by
  #<new>` comment from Dependabot, where `#<new>` is open and carries no
  record, is superseded. The watcher posts the same block on `#<new>`
  with `since` unchanged and a line saying which pull request it came
  from, edits the closed record to say `carried-to: #<new>` so it is
  never carried twice, logs `CARRIED #309 -> #<new>`, and evaluates the
  new record in the same pass. A closed record with no such comment, or
  one that is `lifted` or already `carried-to`, is skipped and logged as
  such. A record that travels with the dependency is the difference
  between this and a bookmark.
- **Exit 0 unless the job cannot run at all**, the same rule as the daily
  sweep and for the same reason: finding nothing lifted is the job
  working, and a banner every week on that would drain the banner.

### Requirement 3 — the <app-c> check ships inside the feature

This is the operator's condition, stated on 2026-09-10 when this item was
opened, and it is a requirement rather than a first use because a watcher
that has never watched anything is unproven, and this case is the one with
every answer already known.

- **Three records are written as part of building this.** The existing hold
  comments on `platypeeps/<app-c>` #308, #309 and #317 gain the fenced
  block, by hand, in the same pull request that lands the job, or a
  follow-up in the same slice. #308 and #309 carry `issue-closed:
  testing-library/jest-dom#738` and `npm-version: "@testing-library/jest-dom
  > 7.0.1"`; #317 carries `npm-dep: "@payloadcms/email-nodemailer nodemailer
  ^10"`. Every version and issue number above was read live on 2026-09-10
  and is recorded in the log below with the command that read it.
- **The first run evaluates them and its output lands here.** The pull
  request that installs the job includes one hand run,
  `cron-jobs.sh run dependabot-holds-weekly`, whose log shows all three
  pull requests `HELD` with their probe counts, or `LIFTED` if upstream has
  moved by then, and either is a pass. A run that finds fewer than three is
  a failure of discovery, not of upstream, and blocks the merge.
- **Supersession is proven on this case if it happens.** vitest and
  nodemailer both release often. If Dependabot supersedes any of the three
  before the job lands, the first run must `CARRIED` the record to the new
  number, and the pull request records which. If none has been superseded,
  the fixture test in criterion 6 stands in.

## Landing order

One slice, in this order: PR 1 in this repository, the contract and the
probes; then the three <app-c> comments, which need only the block
format on `main`; then PR 2, the job, whose hand run must discover those
three records, criterion 7. `implement.md` names them. Nothing here waits
on item B or item D: the watcher reads pull requests and the registry,
and writes only comments and the log.

## Acceptance criteria

1. `local-dependabot/ROUTINE.md` specifies the `sd-hold:` block, the three
   probes with their exact semantics, disjunction across `lifts-when`, and
   the rule that an unparseable probe is reported and never evaluated. The
   daily sweep's "What to print" section counts prose holds and record
   holds separately.
2. A held Dependabot pull request is discovered by the block on it and by
   nothing else: a test with a fixture GitHub double holding two
   repositories, one admitted by the gates and one denied, each with a
   pull request carrying the block, asserts the watcher evaluates exactly
   the admitted one and logs the denied repository as skipped by name. In
   the admitted repository a second block, in a comment whose author the
   double reports with `read` permission, is logged `IGNORED` and neither
   evaluated nor carried, and one whose author it reports with `write` is
   evaluated.
3. Each probe is tested true and false against recorded responses:
   `issue-closed` on an open and a closed issue, `npm-version` on a latest
   below and above the bound, `npm-dep` on a manifest whose range excludes
   and one whose range admits the version. A malformed probe line beside
   no true probe yields a `MALFORMED` log line, no edit, no comment and no
   rebase; the same record with a valid true probe beside it lifts, and
   the log still names the malformed line. A probe whose read fails, the
   double answering an error, yields an `UNKNOWN` line naming the error,
   no edit, no comment and no rebase; the same record with one true probe
   beside the failing one lifts.
4. On a lift the watcher edits the record to `lifting`, posts one comment
   carrying `sd-hold-lifted` and naming the probe and the evidence, then
   `@dependabot rebase`, then edits the record to `lifted`, then logs
   `LIFTED`; on no change it posts nothing and logs `HELD` with the probe
   count. Asserted against the double, including that a second run
   against the same double state, the probe still true, edits nothing,
   posts nothing, and logs `LIFTED-EARLIER`; and that a run starting from
   a record left at `lifting` with the comment posted and no rebase asked
   for posts no second comment, asks for the rebase, writes `lifted`, and
   logs `LIFTED (resumed)`.
5. `local-cron-jobs/jobs/dependabot-holds-weekly.job` exists,
   `personal.cron` lists it, `cron-jobs.sh verify dependabot-holds-weekly`
   reports it installed and current, and the job exits 0 on a run that
   lifts nothing.
6. A closed pull request carrying an unlifted record and a `Superseded by
   #<new>` comment from `app/dependabot`, where `#<new>` is open and has
   no record, is carried: the block is posted on `#<new>` with `since`
   preserved, the closed record gains `carried-to`, the log says `CARRIED
   <old> -> <new>`, and the new record is evaluated in the same run. A
   second run carries nothing. A closed record with no such comment is not
   carried even when an open Dependabot pull request names the same
   package, and the double holds exactly that pair to prove it. Asserted
   with discovery over open and closed pull requests.
7. **The <app-c> records exist and were evaluated.** #308, #309 and
   #317 on `platypeeps/<app-c>` each carry the block with the probes
   named in requirement 3, and the installing pull request quotes a hand
   run's log showing all three as `HELD`, `LIFTED` or `CARRIED`. Fewer than
   three lines is a failing criterion whatever the rest of the run says.
8. When upstream moves for any of the three, the run edits that record to
   `lifting`, comments on that pull request, asks for the rebase, edits
   the record to `lifted`, and then logs `LIFTED`, in that order, and does
   nothing else: no merge, no
   edit to the repository, and on the next run a `LIFTED-EARLIER` line for
   it, no second comment, and no new block from the daily sweep.

## Open questions

1. **A phase of `dependabot-daily`, or its own job?** Its own, weekly, as
   written: the daily sweep's contract is deliberately narrow and a
   watcher that writes comments and asks for rebases is a second kind of
   write. The operator may prefer one job with two verbs; the block format
   and the probes are the same either way.
2. **Should the record also be a row?** Item B's database would let the
   dashboard show held bumps beside everything else. Deferred: the comment
   is the source, and a mirror is a later slice once B's library is on
   `main`.

## Log

- **2026-09-10** — Item opened from the <app-c> session, on
  `docs/held-bumps-watch-their-blockers`. The operator asked for the
  periodic check to be filed as a backbone capability rather than created
  as a cloud routine, with the <app-c> case listed as a condition of
  building it; that is requirement 3 and criterion 7. Facts read live that
  day, for the records requirement 3 names:
  - `gh api repos/testing-library/jest-dom/issues/738 --jq .state` →
    `open`; title "Vitest 5: matcher types are lost — `Assertion<T>`
    augmentation no longer merges with Vitest's `Assertion<R, T>`".
  - `npm view @testing-library/jest-dom version` → `7.0.1`.
  - `npm view @payloadcms/email-nodemailer@latest version
    dependencies.nodemailer` → `3.89.0`, `^9.0.1`.
  - `for n in 308 309 317; do gh pr view $n --repo platypeeps/<app-c>
    --json state,headRefName; done` → all `OPEN`; head branches
    `dependabot/npm_and_yarn/vitest/coverage-v8-5.0.0`,
    `dependabot/npm_and_yarn/vitest-5.0.0`,
    `dependabot/npm_and_yarn/nodemailer-10.0.1`.
  - The hold comments the block will be added to: #308 and #309 comments
    5626389416 and 5626389235; #317 comment 5626981034.
- **2026-09-11** — PR 1 built on `feat/held-bumps-contract-and-probes`:
  the three `ROUTINE.md` sections, and the mechanical half as an
  executable reference (`local-dependabot/dependabot.sh holds`,
  `holds.py`) because criteria 2, 3, 4 and 6 are asserted against doubles
  and a prompt cannot be; `design.md` §4 records the decision. The suite
  (`dependabot.sh test`, 37 tests) covers those four criteria against a
  GitHub double and a registry double whose shapes were recorded read-only
  that day: Dependabot's own supersession comment is `Superseded by
  #<new>.` from `dependabot[bot]` (`gohugoio/hugo#13976`), while
  `platypeeps/<app-c>#307` carries the same words from a person and
  is the negative fixture for criterion 6. Discovery lists
  `repos/<slug>/issues?state=all&creator=dependabot[bot]` rather than
  `pulls`, for the comment count. A read-only dry run against
  `platypeeps/<app-c>` found 99 Dependabot pull requests and 0
  records, as expected before the three comments gain the block, and
  exited 0. Criteria 5, 7 and 8 remain PR 2's.
- **2026-09-12** — The three records, then PR 2 on
  `feat/held-bumps-weekly-job`. The hold comments gained the block by
  `gh api --method PATCH`, every existing word kept above it, `since:
  2026-09-10`, the probes requirement 3 names, and `parse_record` run on
  each body before and after the edit:
  - <https://github.com/platypeeps/<app-c>/pull/308#issuecomment-5626389416>
  - <https://github.com/platypeeps/<app-c>/pull/309#issuecomment-5626389235>
  - <https://github.com/platypeeps/<app-c>/pull/317#issuecomment-5626981034>

  Upstream, read live that day before the edits: `gh api
  repos/testing-library/jest-dom/issues/738 --jq .state` → `open`; `npm
  view @testing-library/jest-dom version` → `7.0.1`; `npm view
  @payloadcms/email-nodemailer@latest version dependencies.nodemailer` →
  `3.89.0`, `^9.0.1`; all three pull requests `open`, none superseded. So
  no probe was true and criterion 8's lift could not be exercised; the
  fixture tests stand in for it, as criterion 6's clause allows for a
  carry. The hand run, `dependabot.sh holds --repo platypeeps/<app-c>`
  from the worktree, twice, the second to show a run that lifts nothing is
  idempotent; both exited 0 and neither wrote to the pull requests:

      SCANNED platypeeps/<app-c> 99 Dependabot pull requests, 3 records
      HELD #308 platypeeps/<app-c> 2 probes, none lifted
      HELD #309 platypeeps/<app-c> 2 probes, none lifted
      HELD #317 platypeeps/<app-c> 1 probes, none lifted
      TOTAL 3 HELD

  Criterion 7 is met by that output. The job is a `JOB_COMMAND` job and
  not a prompt, a departure from what `implement.md` first said: the
  script already prints the contract's vocabulary and exits 0, so an agent
  between it and the log could only paraphrase or widen it. Monday 05:45,
  before the daily sweep. `cron-jobs.sh verify dependabot-holds-weekly` is
  the one check left to the primary checkout after the merge, because a
  plist installed from a worktree points into a directory that is deleted
  with it.
- **2026-09-12** — Delivered. Landed by platypeeps/system#274 (the
  contract, `dependabot.sh` and the probes' reference, 37 tests) and #276
  (the weekly job, the profile line, the three <app-c> records and the
  hand run). Each criterion re-checked on merged `main` (`7c60909b`) from a
  worktree: 1, the `sd-hold:` block, the probes and the watcher's contract
  are the sections at `local-dependabot/ROUTINE.md:207`, `:257` and `:328`,
  and the daily "What to print" counts prose and record holds separately
  (`:320`); 2, 3, 4 and 6, `dependabot.sh test` → `Ran 37 tests ... OK`,
  the tests named `test_c2_*` through `test_c6_*`; 5, the operator ran
  `cron-jobs.sh install dependabot-holds-weekly` from `~/repos/system` at
  `826b9d40` → installed (`45 5 * * 1`), `cron-jobs.sh verify` → `ok`, and
  `launchctl list` shows the label; 7, the hand run above found and held
  all three. Criterion 8 is not a build condition: upstream has not moved
  (jest-dom#738 open, jest-dom 7.0.1, email-nodemailer declares
  `nodemailer ^9.0.1`), so the first live lift is the weekly run's to
  show, and its `LIFTED` / `LIFTED-EARLIER` pair is what the suite
  asserts under criterion 4 in the meantime. This is the commit
  `sd work deliver` reads.

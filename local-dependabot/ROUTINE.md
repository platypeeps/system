# dependabot-daily

Resolve Dependabot pull requests across the repo fleet, every morning,
unattended.

This runs with `--dangerously-skip-permissions`, like every prompt-driven job
here. That is exactly why the contract below is narrow and written down: the
grant is broad, so the instructions have to be the thing that is not.

## What this job may touch

Dependabot-authored pull requests, and nothing else. It does not edit
application code, does not push commits directly to any default branch, does
not force-push, and does not open pull requests of its own. It does advance a
default branch -- squash-merging a pull request is the entire point -- but only
through GitHub's merge button, only on a Dependabot PR, and only after the four
conditions below all hold. If a bump needs a code change to
land, that is a finding to report, not work to do.

## The fleet, enumerated

    find "$HOME/repos" -mindepth 2 -maxdepth 3 -name .git

resolving each checkout's `origin` remote to an `owner/name` slug. Enumerate,
never recite: a list in this file would only ever name the repositories
somebody remembered to add, and the one nobody registered is the one quietly
accumulating unmerged bumps. Deduplicate the slugs — two checkouts can point
at one repository, and a duplicate would have two agents merging the same PR.

Note the local directory group is not the GitHub org. `~/repos/web/example-site`
can be `platypeeps/example-site`. Read the remote; never assemble a slug from the
directory path.

**Then drop everything you cannot merge into.** Most of what `~/repos` holds is
somebody else's project, cloned to read. Measured 2026-09-01: twelve
repositories had open Dependabot PRs and **eight were `READ`** —
`gohugoio/hugo`, `open-telemetry/opentelemetry-demo`,
`modelcontextprotocol/python-sdk`, and five more. Fanning out there spawns
agents that cannot merge anything, and fills the morning log with failures
that were never going to be anything else, which is how a log stops being
read.

    gh repo view <slug> --json viewerPermission --jq .viewerPermission

Keep `ADMIN`, `MAINTAIN`, `WRITE`. Skip `READ` and anything that errors, and
say the count in the summary — "8 of 12 skipped, no write access" — because a
skip nobody can see reads exactly like a repository that had nothing.

Ask GitHub rather than matching on the owner. An owner-prefix rule looks
equivalent and is not: the local directory group is not the org, a fork's
`origin` can point either way, and the rule breaks silently the first time
access is granted or revoked.

## Then the owner gate, which is the one thing here that is recited

Write access says a merge would *succeed*. It does not say one is *wanted*.
Employer repositories are the gap: this account holds `MAINTAIN` on work
repositories whose dependency decisions are not its owner's alone to make
unattended at 06:20.

So permission is necessary and not sufficient. Three rules, in order:

1. **Never these, whatever the rules below say.** Checked first, so the two
   that follow cannot re-admit them:

       platypeeps/Trellis
       platypeeps/google_workspace_mcp
       platypeeps/sd-github-review
       platypeeps/sd-github-review-pilot
       platypeeps/sd-review-test
       platypeeps/se-ai-command-pack

   Every one is `platypeeps/*` and would otherwise be swept by rule 2. They
   are denied for two different reasons, and the difference matters to anyone
   reading this later.

   The first two are already unreachable: discovery walks `~/repos` and reads
   each checkout's `origin`, and neither org copy has one pointing at it —
   `~/repos/ai/Trellis` is the operator's personal fork, and
   `~/repos/ai/google_workspace_mcp` is `taylorwilsdon/google_workspace_mcp`.
   So denying them changes nothing today, which is exactly why it is written
   down: the exclusion currently holds by accident of where somebody's
   checkouts point, an accident is not a decision, and cloning the org's copy
   or widening discovery to enumerate the org through the API would silently
   start merging into both.

   The other four **are** swept today, so denying them is a real change.
   Three of them — `sd-github-review`, `sd-github-review-pilot`,
   `sd-review-test` — carry no `.github/dependabot.yml` at all, so Dependabot
   opens nothing there and the sweep only ever found an empty list. The fourth
   is not like the others: `se-ai-command-pack` **does** configure Dependabot,
   weekly pip with patch and minor grouped, so bumps will genuinely accumulate
   there with nothing merging them. That is the intended trade and not an
   oversight; its own `dependabot.yml` notes each pip bump needs a `make lock`
   follow-up before it can go green, which is the kind of thing an unattended
   06:20 merge cannot do.

   Skip all six and name them in the log, like any other skip.
2. **`platypeeps/*` — every repository, always.** The owner's own org. New
   ones join automatically, which is the enumerate-never-recite principle
   still doing its job where it applies.
3. **Every other owner — only if named in `allowed-repos.conf`.** That file
   lives outside the checkout in `<config>/dependabot/allowed-repos.conf`
   (`<config>` is `$SYSTEM_TOOLS_CONFIG`, default `~/.config/system`;
   copy `allowed-repos.conf.example` there; `DEPENDABOT_ALLOWED_REPOS_FILE`
   names another file), and lists one `owner/name` per line. It
   holds the operator's own consent, so it is not published. Missing means
   none.

Anything else outside `platypeeps` is skipped and *named* in the log, so the
omission is visible and re-authorizing it is one line of that file.

**This list is deliberately a list, and that is not a contradiction of the
enumerate-never-recite rule above.** That rule governs *discovery* — which
repositories exist — where a stale list silently omits the repository nobody
registered, and the cost of being wrong is a missed bump. This governs
*authorization* — which repositories may be written to without a human — and
authorization cannot be derived from the filesystem, because the filesystem
does not know what the owner consented to. Here the failure directions are not
symmetric: a stale allow-list means a repository goes unswept and shows up in
the log saying so, while a derived one means an unattended agent merges into
an employer's repository nobody asked it to touch. Fail closed, and make the
closure loud.

A repository gaining `WRITE` for this account is therefore **not** consent.
Access changes for reasons that have nothing to do with this job — a team
grant, an org-wide policy, an added collaborator — and none of them are the
owner deciding that unattended dependency merges are appropriate there.

## Fan out: one agent per repository, serial within each

Repositories are independent — different PRs, different checks, no shared
state — so **spawn one subagent per repository with Dependabot PRs, in
parallel**. That is the whole point of doing it this way.

Within a repository the merges are **serial**. Dependabot PRs in one repo
share a base branch: merging the first can leave the second conflicted or
stale, so one agent merges one repo's PRs one at a time, in ascending PR
number. Never two agents in one repository.

Give every agent an explicit budget and require a report. An agent that
returns nothing is a failure, not a success — respawn it once, then record
that the repository was not covered this run. Do not report a repository as
clean because its agent went quiet.

Skip the fan-out entirely when only one repository has Dependabot PRs; a
single agent for a single repo is overhead with no parallelism to buy.

## What may be merged

All four must hold. Any one failing means report, not merge.

1. **Author is Dependabot.** `app/dependabot`, and not a draft.
2. **The bump is patch or minor.** A major bump is a breaking change by the
   same semver contract that makes the others safe, so it is always the
   owner's call.
3. **Checks ran and passed — and "ran" needs proving.** Every check passing,
   none failing, none pending.

   **"No checks reported" is not "checks passed."** A repository with no CI
   configured gets its PRs listed and never merged: green there would only
   mean nothing was asked. Say which of the two it was.

   **A green aggregate is not proof either, and this has already bitten.**
   A Dependabot PR routinely carries *two* workflow runs on one head SHA —
   the push, and a second triggered by Dependabot's `labeled` event. In the
   second, a path-filter or `changes` job can skip everything, after which the
   aggregate roll-up check passes having executed nothing. On at least one
   repository in this fleet that aggregate is the *only required check*, so
   `gh pr checks` showed almost every job as `skipping` beside the one green
   aggregate check and the PR read as fully tested. It was not.

   So before merging, confirm at least one **substantive** job actually
   succeeded — a real lint, typecheck, test, or scan job, not an aggregate
   and not a skip. When the newest run is all skips, look at the other run on
   the same SHA before concluding anything. If no run on the head SHA shows a
   substantive job succeeding, report it as unproven and do not merge.

   A roll-up can also fail for reasons that are not the PR's: a *cancelled*
   check run from a superseded workflow counts toward the roll-up and shows as
   `BLOCKED`. That is still a hold — never merge past it — but say so in the
   report rather than calling the PR broken.
4. **GitHub says `CLEAN`.** `mergeStateStatus` from
   `gh pr view <N> --json mergeStateStatus`. Anything else — `BLOCKED`,
   `DIRTY`, `BEHIND`, `UNKNOWN` — is reported with the status named.

Merge with:

    gh pr merge <N> --repo <slug> --squash --delete-branch

## Classifying a grouped bump

A grouped PR — "bump the next group with 3 updates" — is classified by its
**widest member**, never by its title. Dependabot writes one
``Updates `pkg` from A to B`` line per package in the body; a group whose
title reads minor can carry a major inside it. Read the body, enumerate every
package, and take the widest bump found.

A pair that is not two semver versions — a Docker image digest such as
`` bump node from `d649c27` to `83f487e` `` — is **unknown**, and unknown is
never merged. There is no version arithmetic that can call it safe. Report it
so a human can look.

## After a merge

Later PRs in the same repository may go stale or conflicted. Comment
`@dependabot rebase` on them and report that you did. **Do not resolve a
dependency conflict by hand** — a hand-edited lockfile in an unattended job is
how a broken dependency tree reaches main at 06:00 with nobody watching.

## A hold with a named blocker is a record, not a paragraph

A hold whose reason is *upstream has not shipped the fix yet* names a
question with a known answer source, and prose asks nobody. So when the
blocker can be expressed as one of the three probes below, the hold comment
carries a fenced block the watcher can read, and the summary counts it
separately from a hold that stays prose.

    ```yaml
    sd-hold:
      since: 2026-09-10
      reason: vitest 5 Assertion<R, T>; jest-dom 7.0.1 augments Assertion<T>
      lifts-when:
        - issue-closed: testing-library/jest-dom#738
        - npm-version: "@testing-library/jest-dom > 7.0.1"
    ```

- **The block opens with `sd-hold:` and carries three fields.** `since` is
  the date the hold was made and is never rewritten, not even when the
  record travels to a superseding pull request. `reason` is one line a
  person reads. `lifts-when` is a list of probes, any one of which lifts the
  hold. Two-space indentation, a `yaml` fence, nothing else in the block.
  A block without `since`, `reason`, or at least one probe is `MALFORMED`:
  named per missing field, no probe read, neither lifted nor carried.
- **The watcher adds the rest; the sweep never writes these.** `lifting:
  <date> <probe> <evidence>` is the pending state while the watcher is
  mid-sequence; `lifted: <date> <probe> <evidence>` is the account of why
  the hold ended; `carried-to: #<new>` on a closed pull request's record
  says the block was posted on `#<new>`, and `carried-from: #<old>` on that
  new record says where it came from. A record with none of them is a live
  hold.
- **Write the block once, on the hold comment, and once per pull request.**
  A pull request that already carries a block gets no second one. An
  unlifted block is the live hold: report the pull request as `held,
  watched` and leave the comment alone. A lifted block is history: report
  the pull request as `lifted <date>, awaiting owner`, and write a new block
  there only for a blocker none of the existing blocks' probes name -- a
  genuinely new reason, never the one that already lifted.
- **A hold you cannot express as a probe stays prose.** Do not stretch a
  probe to approximate a judgement; the count in the summary is what tells a
  reader how many holds the watcher is not watching.

An example for the nodemailer case, the other shape a blocker takes:

    ```yaml
    sd-hold:
      since: 2026-09-10
      reason: nodemailer 10 ships its own types; @payloadcms/email-nodemailer 3.89.0 depends on nodemailer@^9.0.1
      lifts-when:
        - npm-dep: "@payloadcms/email-nodemailer nodemailer ^10"
    ```

## The three probes, and only these

Each probe is one read with fixed semantics, so the watcher never reasons
about an unblock in prose. The set is closed: a kind not named here is
malformed.

- **`issue-closed: <owner>/<repo>#<n>`** -- true when the issue or pull
  request is closed, read from `repos/<owner>/<repo>/issues/<n>`; false
  while it is open. The evidence on a lift is the issue's URL and its
  state.
- **`npm-version: "<pkg> <op> <version>"`** -- true when the registry's
  latest published version of `<pkg>` satisfies the comparison; `<op>` is
  one of `>`, `>=`, `<`, `<=`, `=`, and `<version>` is a full semver
  version. Quote the value: it has spaces. The evidence is the latest
  version seen.
- **`npm-dep: "<pkg> <dep> <range>"`** -- true when the latest published
  `<pkg>` declares `<dep>` in its `dependencies` with a range that admits
  some version in `<range>`: `^9.0.1` does not admit `^10`, `^9.0.1 ||
  ^10.0.0` and `>=9` do. A `<pkg>` that does not declare `<dep>` at all is
  false. The evidence is the version of `<pkg>` seen and the range it
  declares. Only release versions count, as in npm. A prerelease in
  `<range>` makes the probe `MALFORMED`; one in the declared range makes
  it `UNKNOWN`. Neither lifts.
- **`lifts-when` is a disjunction.** Any probe true lifts the hold; the
  first true probe in list order is the one named in the comment and the
  log. Probes are read serially, one call each, and every probe is read so
  the log names all of them.
- **A probe the watcher cannot parse is `MALFORMED`.** It is reported with
  the line as written, never evaluated, and never treated as true or false.
  It does not stop a valid true probe beside it from lifting -- true is
  true -- but a record with no true probe and any malformed one is
  `MALFORMED` for the run, neither lifted nor carried, and gets no `HELD`
  line: the record is wrong, not the world. Decided without a read, so it
  blocks a carry the way it blocks a lift.
- **A probe whose read fails is `UNKNOWN`.** A network error, a rate limit,
  an authentication failure, a package or issue that does not exist: a
  third outcome beside true and false, logged with the error, never treated
  as false. A record with no true probe and any unknown one is `UNKNOWN`
  for the run, not `HELD`, and is neither lifted nor carried until every
  probe reads; a true probe beside an unknown one still lifts.

## Exit code

Exit 0 whether or not anything merged, and whether or not anything was held
back. Finding PRs you could not merge is this job working correctly, and
failing on it would fire the failure banner every morning until the signal
meant nothing. `notify_failure` is for the job being unable to run at all —
no `gh`, no authentication, no fleet.

If `gh` is missing, note that launchd's own PATH lacks the Homebrew prefix and
`cron-jobs.sh` is what supplies `JOB_PATH`.

## What to print

The log is the whole product — nobody is watching this run. Print, in order:

- one line per repository **not** scanned, with which gate stopped it — `no
  write access` or `owner not authorized` — because these two are the ones a
  reader would otherwise mistake for a repository that had nothing;
- one line per repository scanned, with its PR count;
- one line per PR: number, bump level, title, and either `MERGED` or `SKIP`
  with the specific reason ("major bump", "2 of 6 checks failing",
  "no CI configured", "mergeStateStatus=DIRTY"); a hold that carries an
  `sd-hold:` block says so -- `held, watched` for a live record, `lifted
  <date>, awaiting owner` for one the watcher has already lifted;
- a final total: PRs seen, merged, held back -- **prose holds and record
  holds counted separately**, "5 held (2 watched, 3 prose)", so a reader
  can see how many holds the watcher is not watching -- repositories
  covered, and repositories skipped by each gate.

Name what you did not cover and why. A silent omission in an unattended log is
indistinguishable from nothing being there.

# dependabot-holds-weekly

Evaluate every `sd-hold:` record across the fleet, weekly, unattended, and
say on the pull request when a blocker has lifted. This is the daily sweep's
second verb: it *holds*; this *watches*. It never merges.

The mechanical half of this contract has an executable reference:
`dependabot.sh holds` in this folder runs discovery through the gates, the
writer check, the probes, the lift sequence and supersession exactly as
written here, and `dependabot.sh test` is the suite that pins it against a
GitHub double and a registry double. The job runs `dependabot.sh holds` and
acts on its log; the judgement -- writing a hold in the first place, wording
it, choosing which probe names the blocker -- stays with the agent in the
daily sweep above. When this text and the reference disagree, this text is
the contract and the reference is fixed.

## What this job may touch

Comments on Dependabot pull requests, and only two kinds: the record's own
comment, which it edits to add `lifting`, `lifted` or `carried-to`, and
comments of its own that it posts -- the `sd-hold-lifted` comment, the
`@dependabot rebase` request, and a carried block on a superseding pull
request. Nothing else: no merge, no edit to any repository, no comment on a
pull request that carries no record, no comment on a closed pull request
beyond its record's `carried-to` line. A lifted major is still a major; the
daily sweep's four conditions and the owner decide everything after the
rebase.

## Discovery enumerates, never recites

The fleet and the two gates are the daily sweep's, above: enumerate
`~/repos` for checkouts, resolve each `origin` to a slug, keep `ADMIN`,
`MAINTAIN` and `WRITE`, then apply the owner gate -- the six denied by name
first, then `platypeeps/*`, then the named exceptions. A repository refused
by either gate gets one line naming the gate, `SKIP <slug> no write access`,
`SKIP <slug> owner not authorized` or `SKIP <slug> denied by name`, and is
not read.

In each admitted repository, list every Dependabot pull request, open and
closed (`repos/<slug>/issues?state=all&creator=dependabot[bot]`, which
carries the comment count the `pulls` listing lacks), read the comments of
each that has any, and take every comment carrying the block. There is no
list of watched pull requests anywhere; the block is the registration, and
removing the comment is the deregistration. Open records are evaluated.
Closed records are read for one purpose, supersession, and never evaluated
or commented on.

**Only a writer's block counts.** A comment is user-controlled, and on a
public repository anyone can paste a block whose probe is already true and
have the watcher comment and ask for a rebase on their behalf. So before a
record is evaluated or carried, read the author's access from GitHub at run
time, `repos/<slug>/collaborators/<login>/permission`, and keep the record
only when it answers `admin`, `maintain` or `write` -- never from
`author_association` alone. Anyone else's block is logged `IGNORED #<n>
<slug> <login> no write access` and neither evaluated nor carried.

## The lift sequence, in order, once

On a record with a true probe, five things in this order, so a crash between
any two is resumed and never repeated:

1. edit the record to add `lifting: <date> <probe> <evidence>`;
2. post one comment on the pull request opening `sd-hold-lifted #<n>:`,
   naming the probe that lifted and the evidence -- the closed issue's URL,
   or the registry version and range seen;
3. post `@dependabot rebase`, so the pull request comes back current;
4. edit the record again: `lifting` becomes `lifted`, same value;
5. log `LIFTED #<n> <slug> <probe> <evidence>`.

On no true probe: nothing on the pull request, and one line, `HELD #<n>
<slug> <k> probes, none lifted`, so a silent week is distinguishable from a
job that did not run.

**A record found at `lifting` is resumed.** Post the comment only if no
comment of the watcher's own carrying `sd-hold-lifted #<n>` follows the
record; ask for the rebase only if no `@dependabot rebase` of its own follows
the record; then write `lifted` and log `LIFTED (resumed) #<n> <slug>
<value>`. The probe is not re-read: the record already says what lifted it.

**A record carrying `lifted` is inert.** Never evaluated, commented on,
rebased or carried again; logged `LIFTED-EARLIER #<n> <slug> <date>` and
touched by nothing. The probe stays true forever; the record is what says
the watcher already acted, and that is what makes a weekly run idempotent.

## Supersession carries the hold forward

Dependabot closes a held pull request and opens a newer one when the
dependency releases again, and the new one has no comment. Supersession is
established by Dependabot's own word and nothing looser: the `Superseded by
#<new>` comment it writes on the pull request it closes, authored by
`dependabot[bot]`. A person writing the same words is not evidence, and a
branch or package name matching is not either -- a group, a second base
branch or a second manifest directory can each put two Dependabot pull
requests on one package, and a match on the name could carry a blocker to
the wrong pull request and ask it to rebase.

A closed pull request carrying an unlifted, uncarried record from a writer
and that comment, where `#<new>` is an open Dependabot pull request carrying
no record, is superseded: post the same block on `#<new>` with `since`
unchanged, `carried-from: #<old>` added and a line saying where it came from;
edit the closed record to add `carried-to: #<new>` so it is never carried
twice; log `CARRIED #<old> -> #<new> <slug>`; and evaluate the new record in
the same pass. A closed record with no such comment, or one that is `lifted`
or already `carried-to`, or whose successor is not open or already carries a
record, is skipped and logged as such with the reason.

## Exit code

Exit 0 whether or not anything lifted, for the daily sweep's reason: finding
nothing lifted is the job working, and a banner every week on that would
drain the banner. Non-zero only when the job cannot run at all -- no `gh`,
no authentication, no fleet.

## What to print

One line per repository skipped, naming the gate; one `SCANNED <slug>` line
per repository read, with its Dependabot pull request count and the number of
records found; then one line per record, in this vocabulary and no other:

- `HELD #<n> <slug> <k> probes, none lifted`
- `LIFTED #<n> <slug> <probe> <evidence>`
- `LIFTED-EARLIER #<n> <slug> <date>`
- `LIFTED (resumed) #<n> <slug> <value>`
- `CARRIED #<old> -> #<new> <slug>`
- `UNKNOWN #<n> <slug> <probe>: <error>` -- one per probe that did not read
- `MALFORMED #<n> <slug> <line>` -- one per probe that did not parse, or per
  field the block lacks (`missing reason`, `lifts-when names no probe`)
- `IGNORED #<n> <slug> <login> no write access`
- `SKIP #<n> <slug> closed, <reason>` -- a closed record not carried
- `UNREADABLE <slug>: <error>` -- a repository whose read failed; the pass goes on

and a `TOTAL` line counting each word. With `--dry-run` the reference reads
everything and writes nothing, printing `WOULD-LIFT` and `WOULD-CARRY` where
it would have acted. These are the words a person searches the log for; do
not paraphrase them.

---
title: implement — one slice, two pull requests, three comments
created: 2026-09-10
---

# Implement

One slice, in one order: PR 1 in this repository; then the three
comments on `platypeeps/<app-c>`, which are part of the slice and
not a follow-up and need only PR 1's block format on `main`; then PR 2,
whose hand run must find those three records. Nothing here builds on item
B or item D and nothing waits for them; the watcher reads GitHub and the
npm registry and writes comments and a log.

## PR 1 — the contract and the probes

`local-dependabot/ROUTINE.md` gains three sections: the `sd-hold:` block
and when the daily sweep writes one; the three probes with their exact
semantics and the unparseable-probe rule; and the watcher's own contract,
what it may touch, the lift sequence, supersession, exit code, and what it
prints. The "What to print" section of the daily sweep counts prose holds
and record holds separately.

Beside it, the mechanical half as an executable reference, because the
criteria below are asserted against doubles and only code can be
(`design.md` §4): `local-dependabot/dependabot.sh`, the folder's one
entrypoint, with `holds [--dry-run] [--repo <owner/name>]...` for the
watcher's pass and `test` for the suite; and `local-dependabot/holds.py`,
the implementation, every GitHub and registry call behind `SD_HOLDS_GH`
and `SD_HOLDS_NPM`. Then `local-dependabot/tests/` with a GitHub double
and a registry double (`tests/doubles/`) whose shapes were recorded from
real responses and which journal every write, covering `prd.md` criteria
2, 3, 4 and 6: discovery through the gates, each probe true and false, the
lift sequence and its idempotence, and supersession. The tests exercise
the probe and discovery rules as the contract states them; they are what
a reviewer reads to check the contract says what the tests assert. A
`run_suite` line in `.github/workflows/system-native.yml` runs them.

Lands alone. It changes what the daily sweep prints and nothing it merges.
Built 2026-09-11 on `feat/held-bumps-contract-and-probes`.

## PR 2 — the job, the profile line, and the first run

`local-cron-jobs/jobs/dependabot-holds-weekly.job`, running
`dependabot.sh holds` for the mechanical pass, Monday early morning, clear
of `repo-sync-nightly` and the daily sweep.
`local-machine-setup/profiles/personal.cron` gains the line.
`cron-jobs.sh verify` passes for it, criterion 5.

This first said "prompt-driven like `dependabot-daily.job`". Built as a
`JOB_COMMAND` job instead, on 2026-09-12: the script prints the contract's
own vocabulary and exits 0 on a week that lifts nothing, so an agent
between it and the log would have nothing to add and one more place to
widen a rule. The judgement the daily sweep needs -- reading Dependabot's
prose, deciding whether CI ran -- has no counterpart here; every step of
the watcher is one read or one write with fixed semantics, which is why PR
1 could pin it against doubles at all.

**The <app-c> records are made before this pull request's hand
run, and the run is what proves them.** Between PR 1 and PR 2 the three
hold comments on `platypeeps/<app-c>` #308, #309 and #317 are edited
to carry the block with the probes `prd.md` requirement 3 names, and PR
2's body links the three edited comments. Then one hand run,
`cron-jobs.sh run dependabot-holds-weekly`, and its log goes in the pull
request body: three lines, `HELD`, `LIFTED` or `CARRIED`, one per pull
request. Fewer than three fails criterion 7 and the pull request does not
merge until discovery finds all three.

If upstream has moved by then and a line reads `LIFTED`, the comment and
the `@dependabot rebase` it triggered are linked from the pull request
body too, criterion 8, and the rebased pull request is left for the daily
sweep and the owner: a lifted major is still a major.

Landed 2026-09-12 on `feat/held-bumps-weekly-job` as #276. The three
comments were edited the same day and the hand run found all three
records, `HELD`; upstream had not moved, so criterion 8 was not exercised
live. `prd.md`'s Log has the comment links, the facts read, and the run.

## What is deliberately not here

- No merge path. The watcher lifts holds; the daily sweep's four
  conditions and the owner decide what merges.
- No list of watched pull requests. The block on the pull request is the
  registration.
- No database row. Item B's database can mirror the records in a later
  slice; the comment is the source until then.
- No cloud routine. The 2026-09-10 proposal for one was set aside so that
  this check lives with the fleet enumeration, the owner gate and the
  failure banner the backbone already has.

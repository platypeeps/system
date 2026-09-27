---
title: a file-intake convention, written down from local-drive-intake
created: 2026-09-19
item: sd:1096
---
# PRD — a file-intake convention

## Problem

`local-drive-intake` works. It walks two mounted Google Drive roots against a
state file, routes every new or changed file by path rules, appends one row per
arrival to an append-only log, and hands a digest line to whoever asks. It has
been in daily use since 2026-09-19 and it is the second-largest inbound channel
on this machine after Gmail.

Nothing describes how to build the next one.

The fold-in PRD names this as F7:

> A file-intake convention: a mounted folder walked against a state file, path
> rules to routes, a digest line per new file, nothing acted on without
> approval. `local-drive-intake` is the instance, section 5.7.

The cost of leaving it as folklore is not hypothetical. Three decisions inside
the instance are load-bearing, none is obvious, and each was arrived at by
getting it wrong first:

1. **A missing mount is an error, not a quiet day.** The walker distinguishes
   "the root is mounted and holds nothing new" from "the root is not there".
   Its own comment says why: reporting the second as the first would be a
   silent failure on the one day it matters.
2. **The arrivals log is append-only and carries a delivered stamp**, so a
   hand-run fetch cannot consume rows a digest has not yet reported. The
   consumer stamps `--through` a cutoff *after* delivery, never before.
3. **Routing has a default, not a catch-all row.** Anything no rule matched
   is `noise`, counted and not listed. The reference has no final catch-all
   entry on purpose, so an unrecognised kind surfaces as a noise count. A
   convention that omits the default produces a digest that grows until nobody
   reads it.

A fourth is newer and belongs in the write-up because it is the kind of thing
every instance will meet: **the same file can arrive many times without
changing.** A Drive-hosted Doc syncs as a ~179-byte `.gdoc` stub whose content
never changes while every sync touches its mtime. On 2026-09-19 eighteen
correspondence arrivals were two documents; sixteen were one notice re-syncing.
The instance logs them faithfully, and the *consumer* must collapse them —
`collapse`, in the `site` repo, at tools/morning-digest.py. Where that
responsibility sits is a convention decision, not an implementation detail.

## Why now

`local-mission` is the next instance and is scheduled for step 7 of the
fold-in plan. It will walk an export directory rather than a Drive mount, which
is exactly the variation that shows whether the convention generalises or
whether `local-drive-intake` merely works.

## Acceptance criteria

The deliverable is `docs/conventions/file-intake.md`. Each criterion names the
check that settles it; every command runs from this repository's root.

- **The document exists and has the sections.** `grep -c` finds each of these
  `## ` headings in it: `The source`, `The state file`, `The arrivals log`,
  `Routes`, `Duplicate arrivals`, `Verbs`, `Exit codes`,
  `The approval boundary`. Fail: any heading missing.
- **The state-file contract.** The State file section says what the file keys
  on, that an absent file is a baseline exiting 3, and that it is replaced
  atomically. Fail: any of the three missing.
- **The arrival-log schema, delivered stamp included.** Every column name in
  `ARRIVAL_COLUMNS`
  (`source:local-drive-intake/drive_intake.py::ARRIVAL_COLUMNS`) appears
  backticked in the document, and the log section says the consumer stamps
  `--through` a cutoff after delivery. Fail: a column the module defines is
  absent.
- **The route table shape and the default.** The Routes section says rules are
  ordered, first match wins, and a path no rule matches is `noise`, counted
  and not listed, with no catch-all row. Fail: the document calls for a
  catch-all row, or lists `noise`.
- **A quiet day and an unavailable source are different exits.** The Exit
  codes table has one row for a missing root (1) and one for a reachable
  source with nothing new (3). A reader answers implement.md's verification
  question from that table alone. Fail: either row missing or both on one
  code.
- **Duplicate collapsing is decided, with reasons.** The Duplicate arrivals
  section states where collapsing belongs and why the rejected places were
  rejected; design.md records the same decision. Fail: either page still
  lists it as open.
- **The approval boundary.** The document has a section stating that intake
  reports and nothing it finds is acted on without a person saying so. Fail:
  no such statement.
- **Cross-referenced both ways.** The document names `local-drive-intake` as
  the reference instance, and `grep -c 'docs/conventions/file-intake.md'
  local-drive-intake/README.md` prints at least 1. Fail: 0.
- **The gates stay green.** `JEV_SD_DOCS_LINT=0 <pack>/bin/sd-docs-lint`
  prints `sd-docs-lint: clean`, and `python3 tests/test_citations.py` passes.

## Out of scope

Changing `local-drive-intake`'s code or behaviour. This item writes down what
exists. The one edit to that folder is the cross-link line in its README. Any
defect it uncovers gets its own row. It uncovered one: the convention has
`peek` and `report` collapse repeated arrivals of one file, and the instance
does not yet.

## Not verified

Whether `local-mission` will in fact reuse the convention, since it is unbuilt.
That is the test of this item and cannot be run yet.

---
title: Secret scan masks by hand and scans weekly
created: 2026-10-07
item: sd:1254
---
# PRD — secret scan masks by hand and scans weekly

## Problem

The weekly `critical` scan exits 2 every Monday, and the cron job turns exit 2 into a phone push.
The hits that cause it are key values inside AI session logs under `~/.claude/projects` and `~/.codex/sessions`.
The operator accepted those values on 2026-09-21 (`local-scan-for-secrets/README.md`, "Accepted exposure").
On 2026-09-30 the operator ruled to clear them with `mask --apply`.

`mask --apply` rewrites a file in place on the same inode, so a live session's open handle keeps working.
An in-place rewrite has a window between its last check and its truncate.
A line a session appends inside that window is lost.
The guards shipped in #208 shrink the window; they cannot close it without a lock the sessions do not take.

## Goal

Masking is safe to run, by hand, at a time the operator picks.
The weekly job scans and pages; it never rewrites a file.

## Requirements

1. Masking never loses a byte that a live session appended before its last check (shipped in #208).
2. The weekly job runs `critical` only; it never calls `mask`.
3. `mask` with no key-like export still masks the known credential patterns.
4. `mask` with no target present searches nothing; it never falls back to the current directory.
5. The operator clears the backlog by hand: a dry run, read, then apply.

## Acceptance criteria

- [x] A file that grows between the read and the write keeps every appended byte, and the run counts it as busy (#208).
- [x] The weekly job's command calls `critical` and never `mask`; its exit code is the scan's.
- [x] `mask` with no export masks a pattern hit in a fixture session log.
- [x] `mask --apply` on a fixture home with no target leaves a `~/repos` file untouched.
- [x] `make check` passes.

## Out of scope

- Stopping secrets from entering the shell environment that sessions echo; that is the root cause.
  Until it is fixed, `critical` exits 2 again after each mask.
- Rotating keys; the operator accepted them.
- Unattended masking in any job.

## Log

- 2026-10-07 design record written on a satellite; open questions sent to the hub lead for the operator.
- 2026-10-07 operator accepted Q1 to Q5. Pull request 1 (the append race) shipped as #208.
- 2026-10-07 pull request 2 built a weekly mask before the scan, plus a settling class in `critical`.
- 2026-10-07 the hub's lane review blocked it: an unattended in-place rewrite can lose a resumed session's append.
  Operator ruling (option 1): the job scans and pages only; masking stays a manual step. Settling was cut.
- 2026-10-08 the settle age was cut (sd:3011): with masking manual, compare-before-write covers a live append.
  The #209 restore stays: lane review found a full disk mid-write still leaves a half-masked file.

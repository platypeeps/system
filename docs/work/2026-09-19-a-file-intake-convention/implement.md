---
title: a file-intake convention, written down from local-drive-intake
created: 2026-09-19
item: sd:1096
---
# Implement — a file-intake convention

## Steps

1. Read `local-drive-intake` end to end and list every decision that is not
   forced by the problem. Those are the convention.
2. For each, find the commit or comment that explains it. Several carry their
   rationale inline; those are quotable.
3. Draft `docs/conventions/file-intake.md` in the five sections from the
   design.
4. Settle the two open questions in the design, in the document, with reasons.
5. Cross-link: the convention names `local-drive-intake` as the instance, and
   `local-drive-intake/README.md` names the convention.

## Verification

- A reader who has not seen `local-drive-intake` can answer, from the document
  alone: what exit code does an unreachable source produce, and what does an
  empty one produce?
- `sd-docs-lint` stays clean from this repository's root.

## BLOCKING

None. This item needs no code and no decision from anyone else.

## Log

2026-09-19 — filed. `local-drive-intake` is in daily use; the convention is
the only missing half.

2026-09-23 — prd acceptance criterion corrected (sd:1416, from #452's
review). Goal 3 already said routing has a default and no catch-all row;
the criterion still asked for "the route table shape including the
catch-all". It now asks for the table shape and its default, matching goal 3
and `local-drive-intake/README.md`.

2026-09-23 — wrote `docs/conventions/file-intake.md` from `local-drive-intake`
and `local-mail-intake`, and added the cross-link line to
`local-drive-intake/README.md`. Settled both open questions in design.md:
collapsing happens in every reader and never in the log, and `peek` is
mandatory. The reference instance's `peek` and `report` do not collapse yet;
that gap needs its own row.

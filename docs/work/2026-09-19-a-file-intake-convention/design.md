---
title: a file-intake convention, written down from local-drive-intake
created: 2026-09-19
item: sd:1096
---
# Design — a file-intake convention

## Shape

One document, `docs/conventions/file-intake.md`, written by reading
`local-drive-intake` and stating each decision as a rule plus the failure that
produced it. The failures are the valuable half: a rule with no scar attached
gets discarded by the next person who finds it inconvenient.

## The five sections

| Section | States |
|---|---|
| The source | What a source is (a mounted root, an export directory), and that unreachable is distinct from empty |
| The state file | Where it lives, what it keys on, what happens when it is absent — first run must not report every existing file as new |
| The arrivals log | Append-only, one row per arrival, the delivered stamp, and why the consumer stamps after delivery rather than the walker stamping at write |
| Routes | The table shape, that rules are ordered, and that anything no rule matches falls through to `noise`, counted and never listed. The default is implicit: `local-drive-intake/README.md` says there is deliberately no catch-all row, so a new kind of file shows up as a noise count before anyone writes a rule |
| Exit codes | 0 changed, 3 nothing to do, 1 a real error. The convention's whole point is that 3 and 1 are never confused |

The written convention also carries a Verbs section, a Duplicate arrivals
section and an approval-boundary section beside these five. The two decisions
below needed a home, and the prd's acceptance criteria name the boundary.

## Decisions (settled 2026-09-23)

Both were open questions the assistant was free to settle. Each is stated in
`docs/conventions/file-intake.md` with the same reason.

**Where does duplicate collapsing belong? Decided: the log keeps every row, and
every reader collapses on the identity (`root` plus `rel_path`), the
instance's own `peek` and `report` included.** The question was framed as
instance versus consumer. Neither alone holds up:

- In the log: rejected. The log is evidence, and `stamp` marks rows. A log
  that merged detections could not say which of them a digest delivered.
- Only in consumers: rejected. Every consumer has to remember, and one that
  forgets mails sixteen identical lines. A person running `report` by hand is
  a consumer too, and today sees every row.
- In each reader, the instance's views included: chosen. `report` already
  owns presentation (it collapses above 40 rows to folders), so one line per
  identity with a count sits beside that rule. A consumer that reads the CSV
  directly, as the site morning digest does, keeps its own collapse, because
  presentation differs: that digest lets `new` win over `modified` and counts
  `moved` rows.

This leaves the reference instance short of the convention: its `peek` and
`report` list every row. That is a defect in `local-drive-intake`, which this
item may not change (prd, Out of scope), so it gets its own row.

**Does the convention mandate a `peek` verb? Decided: yes.** `fetch` advances
the state, so a look-only verb is the only safe way to ask what is new. The
design asked for confirmation against a second instance. `local-mail-intake`
is one: its README says its verbs, exit codes, state layout and log are
deliberately identical to `local-drive-intake`, and it carries `peek` with the
same meaning on a source that is not a folder. An export-directory importer,
built later outside this repo, remains the test of that variation.

## Rejected

A shared library. Two instances is too few to know what is common, and a
premature base class would force an export-directory walk into a
mount-shaped hole. Write the convention first; extract code when a third
instance repeats the same twenty lines.

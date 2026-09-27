---
title: re-scope F2, the plugin-generic vault importer
created: 2026-09-19
item: sd:1098
---
# PRD — re-scope F2: plugins declare their dated kinds at install

## Decision, 2026-09-23

**The hardcoded table is not the permanent shape.** The operator ruled on
2026-09-23 (note 3727 on sd:1098). This item designs the install-boundary
answer now: a plugin declares its dated kinds in its manifest, registration
checks the declaration, and the vault importer reads it instead of the table.
F2 stays open, re-scoped to that design.

This supersedes the 2026-09-19 recommendation on this page. That one was to
accept the table plus its drift test and reopen the question when a second
plugin needed it. The table is a copy of another repository's facts, and a
drift test only finds the gap after it opens. The operator chose to remove
the copy rather than guard it.

## Problem

F2 of the fold-in PRD reads:

> Make the vault importer plugin-generic, so any registered plugin's kinds
> with a due date reach Today and the dashboard.

with the rationale:

> Today it imports only Blog Ideas and Topics, by a hardcoded table.

The rationale still describes the code. The vault importer,
`local-sd-db/sd_db/sources/vault.py`, holds two tables:

- `BASES` names the writing pack's two imported kinds and their vault paths.
  Those paths repeat the manifest's `store.bases`.
- `STAGES` maps each `(kind, ladder word)` to one of the six item statuses.
  It also holds rows for `tip`, which nothing imports, and three `blog-idea`
  words the real manifest does not have yet. Both exist only to satisfy the
  drift test.

The table's own comment names the constraint: the library installs into two
virtualenvs, and the manifest is a third repository's file. So nothing at
runtime reads the manifest. A test reads the manifest and asserts the table
covers it. That catches drift and keeps the duplication.

**The constraint is real, and it is about install time, not run time.** The
library cannot carry the manifest when it installs. It can ask, at run time,
which plugins the machine has registered. The pack already stores exactly that
fact: `sd plugin add` writes the plugin's checkout path to the machine config,
and every reader opens the manifest when it needs it.

The site migration of 2026-09-19 took the second expected consumer away. The
`site` plugin writes rows directly and registers no kinds. That removes the
urgency. It does not change the operator's ruling that the copy should go.

## Terms

A **dated kind** is a kind a plugin declares for import into `item` rows. Its
notes reach Today and the dashboard as rows. The declaration may name a due
field. When it does, the note's date lands in `item.due`, and Today shows the
row when it falls due. A declared kind without a due field still imports. Its
rows reach Today while in progress, which is what `blog-idea` and `topic` do
now.

## Requirements

1. **R1 — the plugin declares.** A plugin names each kind it wants imported, in
   its own `sd-plugin.json`. For each kind it gives the status each ladder word
   maps to. It may also name the frontmatter field that holds a due date.
2. **R2 — registration checks.** `sd plugin add` refuses a malformed
   declaration by name. `sd plugin list` re-checks it on every read and reports
   a declaration that has gone bad since registration.
3. **R3 — the importer reads the declaration.** `sd-db.sh import vault` gets
   the declaration from the registered plugins at import time. No table of
   kinds, paths or ladder words remains in `local-sd-db`.
4. **R4 — no silent failure.** Every missing or malformed input names the
   plugin, the kind and the word or value at fault. The import then writes
   nothing. A plugin that declares no dated kinds is reported in the import's
   notes, not refused.
5. **R5 — no fallback.** The hardcoded table is removed in the same change
   that switches the importer. It does not survive as a default.
6. **R6 — stable rows.** Rows the vault import already wrote keep their
   `source` and `external_id`. A re-import after the switch reports them as
   unchanged unless a note changed.

## Acceptance criteria

- `sd plugin add` refuses each of these by name, in the pack's own suite: a
  status word the kind's ladder can hold but the declaration does not map; a
  mapped word the ladder cannot hold; a status outside the six; a kind that is
  not declared or has no `store.bases` entry; a due field that is not one of
  the kind's `fields`; an unknown key.
- `sd plugin list --json` carries each plugin's checked declaration, or a
  `manifestError` naming what is wrong with it.
- The writing pack's manifest declares `blog-idea` and `topic`, and
  `sd plugin list` shows the declaration with no error.
- `local-sd-db/sd_db/sources/vault.py` defines no `BASES`, `STAGES`,
  `MANIFEST_RELATIVE`, `manifest_path` or `manifest_targets`.
- Against a fixture vault and a fixture registry, the import lands the same
  rows the table produced, and a second run reports zero inserted.
- A note whose declared due field holds `2026-10-01` lands with
  `item.due = '2026-10-01'`. A value that is not a `YYYY-MM-DD` date refuses,
  naming the note, the field and the value.
- Each failure in the design's failure table has a test that asserts its
  message names the plugin and the fault, and that no row was written.
- The drift test `TheMappingTableCoversTheManifest` is gone, replaced by the
  test the design names. `system-native` still runs no skipped test.
- `sd-docs-lint` prints `clean` from this repository's root.

## Out of scope

- Scheduling the import. `sd-db.sh import vault` runs by hand today. A due
  date set in the vault reaches Today at the next import. A job that runs it
  is a separate item.
- The item kind. Every imported note lands as item kind `idea`, as now. A
  plugin that needs another kind needs a schema change first.
- `local-sd-db/sd_db/writing.py`, which carries its own copy of the writing
  ladder for the publication flow. It is a sibling duplication and a candidate
  for the same treatment, not part of this item.
- Stores other than the vault. `vault` is the only driver the pack accepts.

## Not verified

- Whether a future domain will want vault-mediated import rather than direct
  rows. The site case chose direct rows because its records carry names and
  addresses and must not sit in a synced vault.
- Whether running the pack's `bin/sd` from the importer holds under launchd.
  The import is run from a shell today, so nothing has tested it there.

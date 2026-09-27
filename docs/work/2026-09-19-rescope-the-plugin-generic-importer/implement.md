---
title: re-scope F2, the plugin-generic vault importer
created: 2026-09-19
item: sd:1098
---
# Implement — plugins declare their dated kinds at install

## Steps

Steps 1 and 2 happen in other repositories. This item records them and does
not make them. Step 3 starts only when both have merged.

1. **Pack (`sd-ai-command-pack`).** Add `workflow` to `MANIFEST_KEYS`. Add
   `validate_workflow` with the refusals in design section 3. Call it from
   `add` after `validate_store`, and from `describe` in the same `try` block.
   Emit `entry["workflow"]`. Write the decision record for the new top-level
   key. Add the `workflow` row to `docs/domain-packs.md`, and fix that page's
   stale claim that the top level is unchecked. Test each refusal.
2. **Writing pack (`writing-pack`).** Declare `blog-idea` and `topic` in a
   `workflow` block, with the status maps design section 2 shows. Do not
   declare `tip`. Confirm `sd plugin list --json` shows the block with no
   error.
3. **System, importer.** In `local-sd-db/sd_db/sources/vault.py`, build the
   bases from the pack's `sd plugin list --json`, reached through
   `pack.installed()`. Map ladder words with each kind's declared `status`.
   Read `due-field` into `item.due`, and add `due` to the frozen payload and
   to `rows()` so verify compares it. Add every refusal in design section 4.
   Delete `BASES`, `STAGES`, `MANIFEST_RELATIVE`, `manifest_path` and
   `manifest_targets`. Keep `SOURCE`, `WHO`, `DEFAULT_VAULT` and
   `vault_root`, which `publication_render.py` imports. Rewrite the module
   docstring. It explains the table, and there is no table.
4. **System, tests.** In `local-sd-db/tests/test_sources_vault.py`, replace
   `TheMappingTableCoversTheManifest` with the integration test design
   section 6 describes. Build `Reader` from a fixture `plugin list` payload in
   the unit tests. Add one test per row of the failure table. Drop the
   fixture manifest under `local-sd-db/tests/fixtures/` if nothing else reads
   it.
5. **System, CI.** Move the pack pin in `.github/workflows/system-native.yml`
   to a commit that holds step 1. Refresh `.github/fixtures/writing-sd-plugin.json`
   and its `.source.json` from a writing commit that holds step 2. Remove
   `SD_WRITING_MANIFEST` and its `test -f` line from the suite step. Update
   `.github/fixtures/README.md` to say what now reads the fixture.
6. **site.** Annotate F2 in the fold-in PRD with the re-scope, its date and
   this row. That is an edit in the `site` repository, made there.

## Verification

- `sd-db.sh test` passes, and `system-native` passes on the pull request with
  no skipped test.
- Enumerate from the tree, not from memory:
  `git grep -n -e STAGES -e BASES -e manifest_targets -e SD_WRITING_MANIFEST -- local-sd-db .github`
  prints nothing that belongs to the vault importer. Any hit from it means a
  reader of the old table was missed. `STAGES` in
  `local-sd-db/sd_db/writing.py` is a different tuple and is out of scope.
- On this machine, after steps 1 and 2: `sd-db.sh import vault` reports every
  existing row unchanged and zero inserted.
- `sd-docs-lint` prints `clean` from this repository's root.

## BLOCKING

None for this page. Step 3 waits on steps 1 and 2, which are ordinary
dependencies in other repositories, not open questions.

## Log

2026-09-19 — filed as a re-scope. F2's justification was that a registered
plugin's dated kinds should reach Today through the vault importer. The site
domain, which was the second plugin, writes rows directly instead and
registers no kinds, so the importer's hardcoded table now covers every
consumer it has.

2026-09-23 — the operator ruled in note 3727: do not accept the hardcoded
table as permanent. Design the install-boundary answer now. F2 stays open,
re-scoped to that design. The pages were rewritten to that design: a
`workflow` block in the manifest, checked by `sd plugin add` and
`sd plugin list`, read by the importer through `sd plugin list --json` at
import time. The table goes with no fallback.

---
title: re-scope F2, the plugin-generic vault importer
created: 2026-09-19
item: sd:1098
---
# Design — plugins declare their dated kinds at install

Decided 2026-09-23, from the operator's ruling in note 3727: design the
install-boundary answer now, and do not keep the hardcoded table. The
2026-09-19 version of this page recommended keeping it. That recommendation
is withdrawn; its reasoning is under **Rejected**.

## 1. The boundary, stated precisely

Three repositories are involved:

| Repository | Holds | Installs as |
|---|---|---|
| `writing-pack` | `sd-plugin.json`: kinds, ladders, `store.bases` | a checkout, registered by path |
| `sd-ai-command-pack` | `sd plugin add` and `list`, the registry, the manifest validators | a checkout, named by the install receipt |
| `system` | `local-sd-db`, which holds the vault importer | a wheel, copied into two virtualenvs |

The wheel cannot carry the manifest, because it installs without the writing
checkout. That is the whole of the constraint. It says nothing about run time.
At run time the machine already knows its plugins. `sd plugin add` writes each
checkout's path to `plugins` in the pack's machine config. `describe` in the
pack's `bin/sd` opens each manifest again on every read. The pack's own
docstring gives the rule: the registry stores a path, never a copy.

So the answer is to move the read from install time to run time. The plugin
declares at install. The pack checks the declaration at install and on every
read. The importer asks the pack at import time.

## 2. Where a plugin declares: the `workflow` block

A new top-level manifest block, `workflow`, sits beside `store`:

```json
"workflow": {
  "blog-idea": {
    "status": {
      "inbox": "planning", "accepted": "ready", "drafting": "in_progress",
      "published": "done", "declined": "done"
    }
  },
  "topic": {
    "status": {
      "candidate": "planning", "active": "in_progress",
      "parked": "blocked", "retired": "done"
    }
  }
}
```

Each key is a kind the plugin wants imported into `item` rows. Each value has
two keys, and no others:

- `status` (required) maps every word the kind's ladder can hold to one of the
  six item statuses. The ladder words are `initial-status`, every
  `transitions` key and every transition target. This map is the one place a
  ladder word becomes a status. It moves out of `STAGES` in `local-sd-db`.
- `due-field` (optional) names the frontmatter field that holds the due date,
  as `YYYY-MM-DD`. A kind with it is dated in the strict sense. A kind without
  it imports with `item.due` left `NULL`.

A plugin that declares no `workflow` block imports nothing. That is the `site`
plugin's case, and it needs no edit.

**Why a top-level block, not a ninth `kinds.*` key.** The eight kind keys
describe what a kind is. `store` is a separate block because where a kind is
kept does not change what it is. Whether a kind becomes a workflow row is a
third fact of the same sort. It is about the backbone's database, not about
the note. A ninth kind key would also need a decision record under R11-D14.
A new top-level key needs one too, because `MANIFEST_KEYS` in the pack's
`bin/sd` is a closed set of eight. The pack change records that decision.

**Why the status map lives in the plugin.** The ladder is the plugin's
vocabulary. The mapping changes when the ladder changes, so both belong in one
file and one commit. The writing pack's pending item C adds `researching`,
`review` and `ready` to `blog-idea`. Today `STAGES` carries those words ahead
of time, because the table and the ladder land in different repositories.
With the map in the manifest, item C adds the three mappings in the same
commit as the three rungs.

## 3. Who reads it, and when

**At install: the pack validates.** `sd plugin add` gains
`validate_workflow(manifest, kinds, store)`. It runs after `validate_store`,
because it needs both results. It refuses:

- a `workflow` key that is not a declared kind;
- a declared kind with no `store.bases` entry, because the importer reads the
  base;
- a ladder word with no `status` entry;
- a `status` entry for a word the ladder cannot hold;
- a status outside `ROW_STATUSES`, the six the pack already spells in
  `bin/sd_lib.py`;
- a `due-field` that is not one of the kind's `fields`;
- an unknown key, at either level;
- an empty block or an empty `status`, as the pack already refuses `"kinds": {}`.

`describe` runs the same validator in its second `try` block, next to
`validate_store`. It adds `entry["workflow"]` to the output of
`sd plugin list --json`. A declaration that has gone bad since registration
becomes a `manifestError`, as every other block does.

**At import: the importer asks the pack.** `sd-db.sh import vault` finds the
installed pack through the receipt, with the existing
`source:local-sd-db/sd_db/pack.py::installed`. It runs the checkout's
`bin/sd plugin list --json` with the current interpreter. From each entry it takes `prefix`, `store` and
`workflow`, and builds one base per declared kind. It resolves `store.root`
the way `publication_render.py` already does: `$OBSIDIAN_VAULT` goes through
`vault_root()`, and any other variable is read from the environment.

**One validator, not two.** The importer does not parse the registry or the
manifests itself. It trusts only what the pack's `list` has checked. A second
validator in `local-sd-db` would be a copy of the pack's rules in another
repository. That is the same kind of copy this item removes. The importer
still checks two things the pack cannot know, because they need the notes:
each note's ladder word is in the declared map, and each due value parses.

The cost is that the import now needs an installed pack. That is a small
cost: a plugin exists on a machine only through the pack's registry, so a
machine without the pack has no plugins to import from.

## 4. Failures

Each failure refuses the whole sitting with `MigrationRefused`, and the import
writes nothing. The message names what is at fault. A partial import that
skipped one plugin would be the silent failure the operator ruled out.

| Case | Outcome |
|---|---|
| No install receipt, or no checkout, or no `bin/sd` in it | refuse, quoting the reason `pack.installed()` gives |
| `sd plugin list --json` exits non-zero or prints what is not JSON | refuse, with its exit code and the first line of its stderr |
| A registered root that is not readable | refuse, naming the root and the pack's `why` |
| An entry with `manifestError` or `dashboardError` | refuse, naming the prefix and the error. `describe` stops at the first error, so the declaration cannot be known |
| An installed pack that predates `workflow` | the writing pack's entry carries an error naming `workflow` as outside the vocabulary; refuse as above |
| A plugin with no `workflow` block | not a failure. One note line: `<prefix>: declares no dated kinds; nothing imported` |
| No registered plugin declares any kind | refuse: an import of zero kinds is the silent case, as an absent base already is |
| Two plugins declare the same kind name | refuse, naming both prefixes. Row identities are `<kind>:<path>`, and a prefix is not added, so existing rows keep their ids |
| `store.root` names an unset variable other than `$OBSIDIAN_VAULT` | refuse, naming the variable and the prefix |
| A declared base that is not a directory | refuse, as today |
| A note with no `status:`, or a word not in the declared map | refuse, as today, naming the note, the word and the plugin |
| A due value that is not `YYYY-MM-DD` | refuse, naming the note, the field and the value. An absent or empty value lands as `NULL` |

## 5. Migration: no fallback

**Decision: remove the table in the same change that switches the importer.**
Do not keep it as a fallback until every plugin declares.

A fallback answers a missing declaration with the table's answer. That turns
the failure R4 names into a success nobody sees. It also keeps the copy the
operator ruled against, with no date on which it goes. The fallback would
serve one plugin, the writing pack. Its declaration is one manifest edit that
the same operator makes.

Order of landing, each step safe on its own:

1. **Pack.** Add `workflow` to `MANIFEST_KEYS`, add `validate_workflow`, and
   emit the block from `describe`. Nothing declares it yet, so nothing
   changes.
2. **Writing pack.** Declare `blog-idea` and `topic`. The status maps are
   `STAGES` for the words its ladders hold today. `tip` is not declared.
   `sd plugin list` must show no error. This must follow step 1: under an
   older pack the unknown key is an error, and the writing tabs disappear
   from the dashboard.
3. **System.** Switch the importer, delete the table, replace the drift test
   and move the pack pin in `system-native`. If this lands before step 2, the
   import refuses by name, because no plugin declares a kind. It writes
   nothing.

The rows already landed are unaffected. `source` stays `vault`,
`external_id` stays `<kind>:<vault-relative path>`, and `fields` keeps the
same keys. Only `due` is new, and it stays `NULL` for kinds without a
`due-field`. A re-import therefore reports every row unchanged.

## 6. The drift test

Today the drift test is `TheMappingTableCoversTheManifest` in
`local-sd-db/tests/test_sources_vault.py`. It asserts `STAGES` covers a
manifest's ladder words. It runs against `tests/fixtures/sd-plugin.json` and
against the writing manifest that `SD_WRITING_MANIFEST` names. CI pins that
manifest as `.github/fixtures/writing-sd-plugin.json`.

After the change there is no table to drift, so the test cannot stay. Its job
splits in two:

- **Coverage moves to the pack.** Ladder-to-status coverage, in both
  directions, is a registration check. The pack's suite tests each refusal in
  section 3. It is enforced where the ladder and the map share one file.
- **System keeps an integration test.** It builds a temporary checkout that
  holds the pinned writing manifest as `sd-plugin.json`, plus an empty file at
  each `sections.template` path so registration passes. It registers that
  checkout with the pinned pack in an isolated `XDG_CONFIG_HOME`, then imports
  a fixture vault. It asserts both kinds land, and that the pinned pack
  accepts the real declaration. The unit tests build `Reader` from a fixture
  `plugin list` payload and cover every row of the failure table.

The pinned fixture, its `.source.json` digest and its verify step in
`system-native.yml` stay. They now feed the integration test.
`SD_WRITING_MANIFEST` goes: `manifest_path()` was its only reader, and the
test passes the fixture path directly. The fixture must be refreshed from a
writing commit that carries the `workflow` block.

## 7. Edits in other repositories

This item edits only this repository's pages. The other edits are listed here
for their own items.

- **`sd-ai-command-pack`:** step 1 above, a decision record for the ninth
  top-level key, and a `workflow` row in the manifest table in
  `docs/domain-packs.md`. That page also still says the top level is
  unchecked. `validate_manifest_keys` now checks it, so the paragraph is
  stale.
- **`writing-pack`:** step 2 above. Item C's pull request adds the mappings
  for its three new rungs.
- **`site`:** the F2 row in the fold-in PRD gets a dated re-scope note. The
  `site` manifest needs no edit.

## Rejected

- **Keep the table plus its drift test indefinitely.** This was the
  2026-09-19 recommendation. It keeps a copy of another repository's facts,
  and the test finds a gap only after it opens. Overruled on 2026-09-23.
- **Keep the table as a fallback until every plugin declares.** See section 5.
- **Copy the declaration into the database or the machine config at
  `sd plugin add`.** The pack rejects this for the manifest as a whole: a copy
  is derived state that goes stale while the plugin repository edits the
  original. The same holds for one block of it.
- **Parse the registry and the manifests in `local-sd-db`.** This gives two
  validators in two repositories, and the second drifts from the first. The
  import already cannot run usefully without the pack's registry.
- **Build nothing until a second plugin needs it.** The operator ruled the
  copy should go regardless of a second consumer.

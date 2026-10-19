# Settings

Settings is at `/settings` (sd:3438). Its Fleet section (sd:3989) comes
first: the `sd.*` keys one value serves on every machine, `sd_lib.FLEET_KEYS`
in the pack (`fleet_owners`, `copilot_review`). `GET /api/settings`
(`settings_screen.document`) runs `sd config list sd --json` and shows each
key's value, its source (workflow database, `config.json` on this machine, or
unset) and its description; a key whose pattern is a list of words gets a
select. A change saves at once: `POST /api/settings/fleet {key, value}` runs
`sd config set sd.<key> <value>` and answers the value it replaced, and the
page names that value when it is not the one it showed. A refusal keeps what
was typed and gives sd's reason. A set that runs past its bound may have
committed, so the page says the outcome is unknown and reads the values
again. A failed list names its reason and draws no row.

Sweeps (sd:5109) follows: the `sweep.<realm>.*` settings the Work and
Personal Run sweep on Tasks reads (`realm_sweep.SETTINGS`), such as its Gmail
search, the shared Drives it skips and the Jira, GitHub and Notion scopes.
They are workflow database rows (`sd_db.writes.set_setting`), never a config
file. `GET /api/settings` reads them on the page's connection. A change posts
`/api/settings/row {key, value, before}`, `before` being the value shown;
an empty value unsets the key. A row changed since the page read it refuses
with 409, and the page reads the values again.

Dashboard (sd:5109) holds the lists other pages read, one workflow database
row each, and the adversarial review's model and effort:

- `dashboard.realms`: Tasks' and Writing's realm views, one
  `realm|<id>|<label>|<glob>` line each, such as
  `realm|work|Work|~/repos/example-work/*`. A task is in the first realm whose
  glob matches its repo path; no match is Personal.
- `dashboard.report_families`: Reports' families, one
  `family|<key>|<label>|<icon>|<job>,<job>,...` line each, such as
  `family|backups|Backups|hard-drive-download|db-backup-nightly`. Families
  `hoa` and `home` are the HOA and Home pages' Reports and Briefs.
- `dashboard.hoa_thresholds`: HOA's trend levels, one
  `<series>|below|<number>` or `<series>|above|<number>` line each, series
  `tank`, `runtime` or `aquifer`.
- `dashboard.disk_storage`: Health's storage folders, one `storage|<path>`
  line each, such as `storage|/Volumes/change-me/repo-storage`; `~` and
  `$HOME` expand. Disk sizes the folders inside each.
- `adversarial.model` and `adversarial.effort`: unset means `gpt-6-astra` at
  `high`.

A row wins: once the database holds one, the old file under
`<config>/project-dashboard/` is not read, and a cleared row stays cleared.
Until then the old file answers, and the page names it as the source, so the
first Save is the import. A list saves with `POST /api/settings/list {key,
text, before}`, `before` being the digest of the source and text shown. A text
the parser cannot read is refused with each problem; a list changed since the
read is a 409. A row that does not read leaves each page unknown, never "not
configured", and no file is read in its place.

The rest is what this browser keeps. The theme is Dark, Light or System
(`theme.js`); Dark is the default. The sections rail is Narrow, Default, Wide
or Widest, or any width from 176 to 384 px dragged at its edge
(`shell.railWidth`, sd:3445). A change saves at once and says Saved. Both live
in the browser's storage, so another browser keeps its own.

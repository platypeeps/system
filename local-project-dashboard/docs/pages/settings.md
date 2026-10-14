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

The rest is what this browser keeps. The theme is Dark, Light or System
(`theme.js`); Dark is the default. The sections rail is Narrow, Default, Wide
or Widest, or any width from 176 to 384 px dragged at its edge
(`shell.railWidth`, sd:3445). A change saves at once and says Saved. Both live
in the browser's storage, so another browser keeps its own.

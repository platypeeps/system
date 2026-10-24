# local-statusline

The Claude Code statusline. Runs the [claude-hud] plugin, then adds what the
plugin cannot render on its own:

- the **login** at the end of the Tokens line (line 1 until a session has
  tokens) — claude-hud's `auth.ts` strips the domain and only ever prints the
  local part. A login named in `STATUSLINE_ACCOUNT_LABELS` shows as its short
  label instead of the email;
- the **system load** on line 1 — the 1, 5 and 15 minute averages; the 1 minute
  value is yellow at or above the logical CPU count and red at twice that;
- an **allowance warning** on line 2 — red, once a plan or model-scoped window
  has less than `FABLE_WARN_PCT` percent (default 35) remaining;
- the **compactions line folded** onto the line above it — claude-hud appends
  it outside its mergeGroups system, so no config can merge it.

Line 1 is already the widest line in the HUD, so the warning rides line 2 (the
context bar), just left of the Cache segment. It falls back to line 1 if the
HUD renders only one line.

## Usage

```sh
./statusline.sh render     # what Claude Code calls on every refresh
./statusline.sh install    # point statusLine in ~/.claude/settings.json here
```

`local-machine-setup`'s `tooling` stage installs the plugin and runs `install`
on every profile, so a new machine needs neither step by hand.

`install` backs up `settings.json` to `settings.json.bak` before rewriting it,
and sets:

```json
"statusLine": {
  "type": "command",
  "command": "sh <this folder>/statusline.sh render",
  "refreshInterval": 5
}
```

### Environment

| Variable | Default | Meaning |
| --- | --- | --- |
| `CLAUDE_CONFIG_DIR` | `~/.claude` | Claude config dir — plugin cache, settings |
| `BUN_BIN` | `~/.bun/bin/bun`, then `$PATH` | bun binary used to run claude-hud's TypeScript entrypoint |
| `FABLE_WARN_PCT` | `35` | remaining percent below which the allowance warning shows |
| `STATUSLINE_ACCOUNT_LABELS` | unset | space-separated `email=label` pairs; read from `<config>/statusline/.env` (copy `.env.example`) |

machine-setup's `envs` stage installs `<config>/statusline/.env` from the
config folder's `envs/<profile>/local-statusline.env`, else `envs/common/`, so
each machine gets the same labels.

## Gotchas

- Claude Code indents the statusline, so `render` hands claude-hud
  `COLUMNS - 4`; without that the HUD truncates its own last segment.
- claude-hud is resolved as the highest-numbered version under
  `~/.claude/plugins/cache/*/claude-hud/*/` — a plugin update needs no change
  here, but an uninstall makes `render` exit 1 with a message.
- HUD display options (which lines exist at all) live in
  `~/.claude/plugins/claude-hud/config.json`, not here. The machine-setup
  `tooling` stage seeds it from `claude-hud.config.example.json` when the file
  is absent and never overwrites one (sd:2929).

[claude-hud]: https://github.com/jarrodwatts/claude-hud

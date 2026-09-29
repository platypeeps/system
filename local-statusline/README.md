# local-statusline

The Claude Code statusline. Runs the [claude-hud] plugin, then adds what the
plugin cannot render on its own:

- the **full login email** on line 1 — claude-hud's `auth.ts` strips the domain
  and only ever prints the local part;
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

## Gotchas

- Claude Code indents the statusline, so `render` hands claude-hud
  `COLUMNS - 4`; without that the HUD truncates its own last segment.
- claude-hud is resolved as the highest-numbered version under
  `~/.claude/plugins/cache/*/claude-hud/*/` — a plugin update needs no change
  here, but an uninstall makes `render` exit 1 with a message.
- HUD display options (which lines exist at all) live in
  `~/.claude/plugins/claude-hud/config.json`, not here.

[claude-hud]: https://github.com/jarrodwatts/claude-hud

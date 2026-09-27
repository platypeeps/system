# local-ai-apps

One place to inventory, compare, and align the AI coding apps — claude-code,
claude-desktop, codex, copilot, opencode, antigravity. Tracks what each app
carries (MCP servers, skills, agents, plugins) as **names only**, so the
captured manifests are safe to commit; secrets stay in each app's own config.

## Usage

```sh
./ai-apps.sh status              # apps installed + per-kind counts
./ai-apps.sh capture [profile]   # snapshot inventory to profiles/<p>.inv
./ai-apps.sh compare             # cross-APP matrix on this machine
./ai-apps.sh compare personal work   # cross-PROFILE diff
./ai-apps.sh setup [profile] [--apply]  # align machine to a manifest
./ai-apps.sh adopt <kind> <name> <from> <to> [--apply]  # move one item
./ai-apps.sh update              # brew upgrade for just these six apps
./ai-apps.sh nightly             # update + capture, email on change (cron)
```

## Manifest format

One row per item: `app|kind|name`. A row whose name is derived, and so not
unique, carries a fourth field: `app|kind|name|key`. Today that is an
opencode plugin, whose name comes from its path. Its key is the first 12 hex
of the SHA-256 of its spec, with a leading home written as `~` first so machines agree.
The spec itself is never recorded, since it can carry a URL password or a
token. The key tells two rows apart; the name is the label the matrix and
`adopt` use. Two plugins that derive one name stay two rows,
so removing either one shows in the capture diff.
A `|` or `%` in a name is written as `%7C` or `%25`, so every row keeps its field count.
Every output decodes it back: the capture report, both `compare` forms, and `setup`.
The name reads a spec's path only, never its userinfo, query or fragment.
A fourth field that is not a digest comes from an earlier capture that kept the spec.
The reports print it as `(old key withheld)`; the next capture replaces it.

## Cross-app adopt semantics

- **skills** — SKILL.md directories are the same format everywhere; `--apply`
  copies the directory.
- **agents** — markdown-compatible between claude-code and opencode only;
  codex agents are TOML and need a manual rewrite.
- **MCP servers** — never written automatically. The recipe for the target
  app is printed with every env/header value REDACTED; copy the secrets by
  hand from the printed source path. This is deliberate: config files embed
  tokens (the reason the old local-claude snapshots were purged).
- **plugins** — installed through each app's own manager; prints the command.

## Cross-machine flow

`capture` on each machine writes `profiles/personal.inv` / `profiles/work.inv`
(profile auto-detected from `~/.config/machine-setup/profile`, else work if
`AI_APPS_WORK_ROOT` names an existing directory, else personal; override with
`AI_APPS_PROFILE`). The `.inv` files are a machine's own inventory, so they are
gitignored here and `nightly` skips its commit step for them;
`profiles/example.inv` shows the format. A private fork that tracks them keeps
the commit. Then `compare personal work` shows the drift anywhere,
and `setup <profile> --apply` pulls a machine toward the chosen manifest.

## Cron

`ai-apps-nightly` (04:30, both machines via common.cron) runs `nightly`:
upgrades the six apps through brew and re-captures the inventory, emailing
via local-notify only when an app was upgraded or the inventory moved.
The `.inv` diff lands in the repo working tree — commit it when it looks right.

## Where inventories come from

| app | mcp | skills | agents | plugins |
|---|---|---|---|---|
| claude-code | `~/.claude.json` | `~/.claude/skills` | `~/.claude/agents` | `~/.claude/plugins/installed_plugins.json` |
| claude-desktop | `claude_desktop_config.json` | — | — | `Claude Extensions/` |
| codex | `~/.codex/config.toml` | `~/.codex/skills` | `~/.codex/agents` (toml) | — |
| copilot | `~/.copilot/mcp-config.json` | `~/.copilot/skills` | — | `~/.copilot/config.json` |
| opencode | `opencode.json` | `skills/` | `agents/` | `opencode.json` plugin list |
| antigravity | `~/.gemini/antigravity/mcp_config.json` | `~/.gemini/skills` | — | `~/.gemini/extensions` |

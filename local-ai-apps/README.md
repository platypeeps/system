# local-ai-apps

One place to inventory, compare, and align the AI coding apps — claude-code,
claude-desktop, codex, copilot, opencode, antigravity. Tracks what each app
carries (MCP servers, skills, agents, plugins) as **names only**, so the
captured manifests are safe to commit; secrets stay in each app's own config.

## Usage

```sh
./ai-apps.sh status              # apps installed + per-kind counts
./ai-apps.sh capture [profile]   # snapshot inventory to <config>/ai-apps/profiles/<p>.inv
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
A manifest whose `# format:` line lacks `name[|key]` predates the encoding, so its names are read as written.
The next capture rewrites such a file, encoded, if a name in it holds a `%`.
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

`capture` on each machine writes `personal.inv` / `work.inv` into
`<config>/ai-apps/profiles/`, outside the checkout (`<config>` is
`$SYSTEM_TOOLS_CONFIG`, default `~/.config/system`; `AI_APPS_PROFILES_DIR`
names another folder). The profile is auto-detected from
`~/.config/machine-setup/profile`, else work if the work root names an
existing directory, else personal; override with `AI_APPS_PROFILE`. The work
root is `AI_APPS_WORK_ROOT`, else `SYSTEM_TOOLS_WORK_ROOT`; `local-repo-sync`
reads the same meaning from `REPO_SYNC_WORK_ROOT`, so the shared name drives
both. The `.inv` files are a machine's own inventory and keep no git history;
`nightly` writes them and commits nothing. `profiles/example.inv` in this
folder shows the format. To compare machines, copy the other
machine's `.inv` into the same folder. Then `compare personal work` shows the drift anywhere,
and `setup <profile> --apply` pulls a machine toward the chosen manifest.

## Cron

`ai-apps-nightly` (04:30, a job in `<config>/cron-jobs/jobs/`; examples in `local-cron-jobs/examples/`) runs `nightly`:
upgrades the six apps through brew and re-captures the inventory, emailing
via local-notify only when an app was upgraded or the inventory moved.
The `.inv` diff lands in `<config>/ai-apps/profiles/`.
Each brew call names itself in the job log as it starts.
A query stops after 600 s and an upgrade after 1800 s; `AI_APPS_STEP_TIMEOUT` sets one bound in seconds for both.
A hung call then ends and names its step, instead of running until the job's limit.
A brew step that fails or times out fails the night with exit 1, so cron-jobs raises its failure banner and push.
A night with nothing outdated and no inventory change stays quiet.

## Where inventories come from

| app | mcp | skills | agents | plugins |
|---|---|---|---|---|
| claude-code | `~/.claude.json` | `~/.claude/skills` | `~/.claude/agents` | `~/.claude/plugins/installed_plugins.json` |
| claude-desktop | `claude_desktop_config.json` | — | — | `Claude Extensions/` |
| codex | `~/.codex/config.toml` | `~/.codex/skills` | `~/.codex/agents` (toml) | — |
| copilot | `~/.copilot/mcp-config.json` | `~/.copilot/skills` | — | `~/.copilot/config.json` |
| opencode | `opencode.json` | `skills/` | `agents/` | `opencode.json` plugin list |
| antigravity | `~/.gemini/antigravity/mcp_config.json` | `~/.gemini/skills` | — | `~/.gemini/extensions` |

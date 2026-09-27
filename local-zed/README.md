# local-zed

Where Zed `settings.json` snapshots for each setup are kept. This folder has no
entrypoint; it holds no config itself.

## Where the snapshots live

Zed's live config is `~/.config/zed/settings.json` plus `~/.config/zed/prompts`.
When present, it embeds a GitHub PAT and MCP auth tokens, so no copy goes in git.

Keep snapshots in `<config>/zed/`, outside the checkout, one file per setup:

- `<config>/zed/settings.json.personal`
- `<config>/zed/settings.json.work`

`<config>` is `$SYSTEM_TOOLS_CONFIG`, default `~/.config/system`.
Give the folder mode 700 and each file mode 600.

A snapshot becomes safe to share only after every
`github_personal_access_token`, `Authorization` and `AUTH_HEADER` value is an
environment reference.

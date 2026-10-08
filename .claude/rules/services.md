---
paths:
  - "local-*/**"
  - "network-testing/**"
---

# Gotchas: services, ports and launchd

## Ports

- No two `local-*` services share a port; `README.md` § Ports is the one table. Move the non-canonical holder and update that table.
- `8767` is the workflow dashboard's loopback port (`dashboard.sh serve --port`); its explicit configuration must use the same backend port.
  - Private Tailscale access uses HTTPS on 8443; optional IP access binds the Tailscale address on 8768 and authenticates its TCP peer; both leave public 443 alone.

## launchd plists hold absolute paths

- Every installed LaunchAgent passes `SYSTEM_TOOLS_CONFIG` in its environment (`@CONFIG@` in a `*.plist.template`, or the plist a tool builds): launchd gives the agent only the environment the plist names.
- The label prefix is `SYSTEM_TOOLS_LABEL_PREFIX` (default `local.system-tools`); keep it identical for every tool on a machine.
- Renaming a script or folder a plist references requires reinstalling the plist.
- The pack's registry holds this repo's root, and `sd-plugin.json` points at the dashboard entrypoint.
- Renaming `local-project-dashboard` requires both a LaunchAgent and a plugin update.
- `<prefix>.sd-serve` (example plist in `local-machine-setup/examples/launchagents/`) names `local-sd-db/sd-db.sh`; renaming either means editing and reloading it.

## Wrappers and vendored code

- Some wrappers point at checkouts elsewhere (for example `~/repos/ai/llama.cpp`); this repo holds only the wrapper.
- `local-milvus/standalone_embed.sh` is vendored upstream code; refresh it with `./milvus.sh install`, never hand-edit.

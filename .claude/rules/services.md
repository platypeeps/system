---
paths:
  - "local-*/**"
  - "network-testing/**"
---

# Gotchas: services, ports and launchd

## Ports

- No two tools share a port; `README.md` § Ports is the one table, one row per folder. `tests/test_ports.py` (preflight) fails on a clash or a table that differs from the scripts; move the non-canonical holder and update the table.
  - A tool that only points at another's port (a client) gets an `ALLOWED` entry with its reason.
- `8767` is the workflow dashboard's loopback port (`dashboard.sh serve --port`); its explicit configuration must use the same backend port.
  - Private Tailscale access uses HTTPS on 8443; optional IP access binds the Tailscale address on 8768 and authenticates its TCP peer; both leave public 443 alone.

## launchd plists hold absolute paths

- Every installed LaunchAgent passes `SYSTEM_TOOLS_CONFIG` in its environment (`@CONFIG@` in a `*.plist.template`, or the plist a tool builds): launchd gives the agent only the environment the plist names.
- The label prefix is `SYSTEM_TOOLS_LABEL_PREFIX` (default `local.system-tools`), kept once in `<config>/shared.env`; read it with `st_label_prefix` (shell) or `label_prefix()` (Python), never from a tool's own `.env`.
- Renaming a script or folder a plist references requires reinstalling the plist.
- The pack's registry holds this repo's root, and `sd-plugin.json` points at the dashboard entrypoint.
- Renaming `local-project-dashboard` requires both a LaunchAgent and a plugin update.
- `<prefix>.sd-serve` (example plist in `local-machine-setup/examples/launchagents/`) names the pack's `bin/sd-db`; moving the pack checkout means editing and reloading it.

## Wrappers and vendored code

- Some wrappers point at checkouts elsewhere (for example `~/repos/ai/llama.cpp`); this repo holds only the wrapper.
- `local-milvus/standalone_embed.sh` is vendored upstream code; refresh it with `./milvus.sh install`, never hand-edit.

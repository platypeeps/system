---
paths:
  - "local-*/**"
  - "network-testing/**"
---

# Gotchas: services, ports and launchd

## Ports

- No two `local-*` services collide; all can run at once.
- Same-role pairs moved the non-canonical holder:
  - falkordb on **6380**, graphiti's bundled falkordb on **6381** (redis keeps 6379);
  - graphiti's UI on **3004** (falkordb keeps 3003);
  - jaeger's OTLP on **4327/4328** (the collector keeps 4317/4318);
  - the clickhouse MCP on **8002** (redisinsight keeps 8001).
- Remaining overlaps are with software outside this repo:
  - `8080-8083`: jaeger HotROD;
  - `8084`: llama-cpp, overridable via `PORT`;
  - `8766`: task-actions, overridable via `TASK_ACTIONS_PORT`.
- graphiti's MCP HTTP sits on `8085` to avoid 8083/8084.
- `local-kev` serves on loopback `8009`, Kev's documented port, overridable via `KEV_PORT`.
- `8767` is the workflow dashboard's loopback port, served by `local-project-dashboard/dashboard.sh serve --port`.
  - Its explicit configuration must use the same backend port.
  - Private Tailscale access uses HTTPS on 8443.
  - Optional IP access binds the node's Tailscale address on 8768 and authenticates its TCP peer.
  - Both leave public 443 alone.
- `local-postgres` sits on **5434** and `local-qdrant` on **6337**, both overridable, to leave the conventional ports to other software.

## launchd plists hold absolute paths

- Tools that install a LaunchAgent fill a committed `*.plist.template` at install time.
- The label prefix is `SYSTEM_TOOLS_LABEL_PREFIX` (default `local.system-tools`); keep it identical for every tool on a machine.
- Renaming a script or folder a plist references requires reinstalling the plist.
- The pack's registry holds this repo's root, and `sd-plugin.json` points at the dashboard entrypoint.
- Renaming `local-project-dashboard` requires both a LaunchAgent and a plugin update.

## Wrappers and vendored code

- Some wrappers point at checkouts elsewhere (for example `~/repos/ai/llama.cpp`); this repo holds only the wrapper.
- `local-milvus/standalone_embed.sh` is vendored upstream code; refresh it with `./milvus.sh install`, never hand-edit.

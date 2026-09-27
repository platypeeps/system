# local-mock-mcp

Runs `mock-mcp-service` — a mock MCP server used to benchmark agents on
root-cause analysis — in docker on `:9992`, endpoint `/mcp`. It serves the
real MCP tool surface but answers from a planted failure scenario instead of
real backends, so an agent under test cannot tell the difference and an eval
harness holding the scenario's ground truth can score what it concluded.

The image is built from the checkout named by `MOCK_MCP_SRC`; this repo holds
only the wrapper.

## Configuration

Set values in `$SYSTEM_TOOLS_CONFIG/mock-mcp/.env` (copy `.env.example`) or
export them. `MOCK_MCP_SRC` is required by `build`, `start`, `scenarios` and
`update`; `MOCK_SCENARIO` is required when `start` names no scenario.
`MOCK_MCP_NAME`, `MOCK_MCP_IMAGE` and `MOCK_MCP_PORT` are optional.

## Usage

```sh
./mock-mcp.sh build                        # docker build from the checkout
./mock-mcp.sh start [scenario]             # run it (default $MOCK_SCENARIO)
./mock-mcp.sh scenarios                    # list scenarios, * marks the active one
./mock-mcp.sh scenario dns-partial-outage  # serve a different one (restarts)
./mock-mcp.sh status                       # container, scenario, health, Claude Code entry
./mock-mcp.sh logs                         # follow container logs
./mock-mcp.sh register [scope]             # add to Claude Code (default scope user)
./mock-mcp.sh stop
./mock-mcp.sh update                       # stop, drop the image by ID, rebuild
```

## Gotchas

- **Switching scenario restarts the container.** The server picks its default
  scenario from `MOCK_SCENARIO` at startup, so `scenario <name>` is a restart,
  not a live swap. Claude Code keeps working on the same URL; reconnect with
  `/mcp` if the session was mid-call.
- **Which scenario is live is read back from docker**, not from a state file —
  `status` and `scenarios` inspect the running container's env.
- `mock-scenarios/` is bind-mounted read-only from the checkout, so editing a
  scenario YAML takes a `restart`, not a `build`.
- Per-call selection exists too: the server reads `X-Mock-Scenario` and pins an
  independent timeline per `X-Mock-Session` tag. Claude Code only sends static
  headers, which is why the wrapper switches the container's default instead.
  Add headers to the Claude Code entry by hand if a fixed session tag is wanted.
- `register` defaults to **user** scope (available in every project). Use
  `register local` to keep it to the current project only.
- Not in a service profile — this is a bench rig you start for a run, not
  always-on infrastructure.
- The image is built locally, so `update` rebuilds rather than pulling.
  If the checkout is gone, this folder is dead weight: delete it.

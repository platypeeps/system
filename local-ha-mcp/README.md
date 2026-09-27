# local-ha-mcp

Bridges Home Assistant's MCP endpoint to stdio so an MCP client can talk to it
without the connection going stale.

## Usage

```sh
./ha-mcp.sh          # serve (the default; what an MCP client invokes)
./ha-mcp.sh check    # handshake + tools/list, prints server version and count
./ha-mcp.sh --help
```

`check` on a healthy system:

```
server : home-assistant 1.26.0
tools  : 22
url    : http://homeassistant.local:8123/mcp_server/sse
OK
```

## Why this exists

Home Assistant 2026.9.x serves MCP over **SSE only**. There is no
streamable-HTTP endpoint — eleven candidate paths were probed on 2026-09-06 and
every one returned `404`; `POST` to the SSE endpoint itself returns `405`.

An SSE session is a ULID minted per `GET /mcp_server/sse` that lives only as
long as the stream. A Home Assistant restart, a network blip or a laptop sleep
kills it. Claude Code's native `"type": "sse"` client held the id from its last
successful connection and kept POSTing to it rather than re-handshaking, so
every call failed with:

```
Error POSTing to endpoint (HTTP 404): Could not find session ID '01M1SS...'
```

That is **structural, not intermittent** — guaranteed to recur after any
interruption. `/mcp` reconnects but does not fix it.

Running the bridge as a **stdio** server fixes it: the client launches this as a
child process, so the handshake happens fresh every time. The bridge itself is
[`mcp-remote`](https://www.npmjs.com/package/mcp-remote), pinned.

## Wiring it into Claude Code

`bin-links.sh install` puts `ha-mcp` on `PATH`, then in `~/.claude.json`:

```json
"home-assistant": { "type": "stdio", "command": "<HOME>/bin/common/ha-mcp" }
```

A wrapper rather than the `npx` command inline: nothing else in `~/.claude.json`
uses `${VAR}` inside `args` — only inside `env` and `headers` — so there is no
evidence the client expands variables there. This script takes `HA_TOKEN` from
the environment it inherits, which is the mechanism the previous config already
relied on.

## The no-argument deviation

Every other multi-action tool here prints usage to stderr and exits 1 when run
with no arguments. This one **serves**. An MCP client invokes the command with
no arguments and expects a live stdio server; printing usage would break it.
`serve` is named only so `check` has something to contrast with.

## Gotchas

- **The endpoint is plain HTTP on the LAN**, so the bearer token crosses the
  network in clear and `mcp-remote` needs `--allow-http`. This is not new — the
  direct SSE client config this replaced sent the same token the same way, as
  does any REST call to the same host. Revisit if Home Assistant is ever given
  a certificate.
- `HA_TOKEN` and `HA_MCP_URL` come from `~/.config/system/ha-mcp/.env`
  (`$SYSTEM_TOOLS_CONFIG/ha-mcp/` when that is set; copy `.env.example` there)
  or the environment; `serve` and `check` fail naming whichever is missing.
- `npx -y` re-resolves the package on each launch. It is cached after the first
  run; install `mcp-remote` globally if start-up latency ever matters.
- `mcp-proxy` was tried first and did not work: its cached
  build raises `ImportError: cannot import name 'request_ctx'` against the
  installed MCP SDK.
- If a later Home Assistant release serves streamable HTTP, this whole module
  can go and the client can point straight at it.

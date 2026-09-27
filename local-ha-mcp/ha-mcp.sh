#!/bin/sh
# Home Assistant MCP bridge: the remote SSE endpoint, spoken as local stdio.
#
# Why this exists. Home Assistant 2026.9.x serves MCP over SSE only -- there is
# no streamable-HTTP endpoint; eleven candidate paths were probed on 2026-09-06
# and every one returned 404. An SSE session is a ULID minted per GET that lives
# only as long as the stream, so a Home Assistant restart, a network blip or a
# laptop sleep kills it. Claude Code's native "type": "sse" client kept POSTing
# to the id from its last successful connection instead of re-handshaking, and
# every call failed with:
#
#   Error POSTing to endpoint (HTTP 404): Could not find session ID '01M1SS...'
#
# Running the connection as a stdio server fixes it structurally: the client
# launches this as a child process, so the handshake is fresh every time.
#
# Settings come from <config>/ha-mcp/.env (lib/config.sh) -- see .env.example.
# HA_TOKEN is
# normally already exported by ~/.config/shell/env.sh.
set -e

# Resolve symlinks with relative targets against the link's own directory --
# a bare readlink walk breaks for the relative links bin-links installs. This
# script sources ../lib/config.sh from beside itself, so without the walk it
# would find nothing when invoked through ~/bin/common/ha-mcp.
SELF="$0"
while [ -L "$SELF" ]; do
  target="$(readlink "$SELF")"
  case "$target" in
    /*) SELF="$target" ;;
    *)  SELF="$(dirname "$SELF")/$target" ;;
  esac
done
DIR="$(cd "$(dirname "$SELF")" && pwd)"

# shellcheck source=../lib/config.sh
. "$DIR/../lib/config.sh"

# <config>/ha-mcp/.env provides defaults only -- values already in the
# environment win.
ENV_HA_TOKEN="${HA_TOKEN:-}"
ENV_HA_MCP_URL="${HA_MCP_URL:-}"
ENV_MCP_REMOTE_VERSION="${MCP_REMOTE_VERSION:-}"
ENV_HA_MCP_NODE="${HA_MCP_NODE:-}"
st_source_env ha-mcp
[ -n "$ENV_HA_TOKEN" ] && HA_TOKEN="$ENV_HA_TOKEN"
[ -n "$ENV_HA_MCP_URL" ] && HA_MCP_URL="$ENV_HA_MCP_URL"
[ -n "$ENV_MCP_REMOTE_VERSION" ] && MCP_REMOTE_VERSION="$ENV_MCP_REMOTE_VERSION"
[ -n "$ENV_HA_MCP_NODE" ] && HA_MCP_NODE="$ENV_HA_MCP_NODE"

MCP_REMOTE_VERSION="${MCP_REMOTE_VERSION:-0.8.3}"

usage() {
  cat <<'HELPEOF'
usage: ha-mcp.sh [serve|check|-h]

  serve   run the bridge: remote Home Assistant SSE <-> local stdio, speaking
          JSON-RPC on stdin/stdout. This is the default, and the no-argument
          form is what an MCP client invokes.
  check   one-shot health check -- handshake, initialize, tools/list -- and
          print the server version and tool count. Exits non-zero on failure.

Unlike the other multi-action tools in this repo, a bare invocation does NOT
print usage and exit 1: an MCP client runs the command with no arguments and
expects a live stdio server on the other end. `serve` is spelled out only so
`check` has something to contrast with.

environment (<config>/ha-mcp/.env, or already exported; the environment wins;
<config> is $SYSTEM_TOOLS_CONFIG, default ~/.config/system):
  HA_TOKEN             required. Home Assistant long-lived access token.
                       Normally exported by ~/.config/shell/env.sh.
  HA_MCP_URL           required. The SSE endpoint, e.g.
                       http://homeassistant.local:8123/mcp_server/sse
  MCP_REMOTE_VERSION   default 0.8.3
  HA_MCP_NODE          node binary to run the bridge with. Default: the first
                       `node` on PATH. npx is invoked from beside it and that
                       directory is prepended to PATH, so one Node serves the
                       whole call -- see resolve_node() for why that matters.

Needs `npx` (Node). --allow-http is passed because the endpoint is plain HTTP
on the LAN, so the bearer token crosses the network in clear. That is not new:
the direct SSE client config this replaced sent the same token the same way.
HELPEOF
}

# Pin the Node that runs the bridge.
#
# `npx` is not a binary -- /opt/homebrew/bin/npx is a `#!/usr/bin/env node`
# script, so the interpreter is whatever PATH resolves at spawn time, not
# anything fixed by the symlink. A machine can have two Homebrew Nodes (`node`
# and the keg-only `node@22`), each ad-hoc signed with its OWN identifier, so
# macOS tracks them as two separate entries in Privacy > Local Network. Home
# Assistant is a same-link peer, which that permission gates: a bridge that
# silently switched Node would fail with
#
#   fetch failed: connect EHOSTUNREACH <ha-host>:8123
#
# while curl to the same host returns 200, because the gateway and everything
# routed through it stay reachable. Resolving Node once, here, keeps the
# binary that a permission was granted to the binary that actually runs.
#
# HA_MCP_NODE overrides the choice (give it the node binary, not a directory).
resolve_node() {
  if [ -n "${HA_MCP_NODE:-}" ]; then
    NODE_BIN="$HA_MCP_NODE"
    if [ ! -x "$NODE_BIN" ]; then
      echo "ha-mcp.sh: HA_MCP_NODE is not an executable: $NODE_BIN" >&2
      exit 1
    fi
  else
    NODE_BIN="$(command -v node 2>/dev/null || true)"
    if [ -z "$NODE_BIN" ]; then
      echo "ha-mcp.sh: node not found on PATH. Install Node (brew install node)," >&2
      echo "  or set HA_MCP_NODE to the binary to use." >&2
      exit 1
    fi
  fi

  NODE_DIR="$(cd "$(dirname "$NODE_BIN")" && pwd)"
  NPX_BIN="$NODE_DIR/npx"
  if [ ! -e "$NPX_BIN" ]; then
    echo "ha-mcp.sh: no npx beside $NODE_BIN" >&2
    exit 1
  fi

  # npx re-resolves `node` through PATH from its shebang, so the chosen Node
  # has to win there too -- otherwise the pin holds for one hop and is lost.
  PATH="$NODE_DIR:$PATH"
  export PATH
}

require_token() {
  if [ -z "${HA_TOKEN:-}" ]; then
    st_missing HA_TOKEN ha-mcp .env
    echo "  (the export is normally done by ~/.config/shell/env.sh)" >&2
    exit 1
  fi
}

require_url() {
  case "${HA_MCP_URL:-}" in *change-me*) HA_MCP_URL="" ;; esac
  if [ -z "${HA_MCP_URL:-}" ]; then
    st_missing HA_MCP_URL ha-mcp .env
    echo "  (HA_MCP_URL is the Home Assistant SSE endpoint)" >&2
    exit 1
  fi
}

serve() {
  require_token
  require_url
  resolve_node
  echo "ha-mcp.sh: node $("$NODE_BIN" -v) at $NODE_BIN" >&2
  exec "$NPX_BIN" -y "mcp-remote@${MCP_REMOTE_VERSION}" "$HA_MCP_URL" \
    --header "Authorization: Bearer ${HA_TOKEN}" \
    --transport sse-only \
    --allow-http
}

check() {
  require_token
  require_url
  resolve_node
  out="$(mktemp)"
  trap 'rm -f "$out"' EXIT
  {
    printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"ha-mcp-check","version":"1"}}}'
    printf '%s\n' '{"jsonrpc":"2.0","method":"notifications/initialized"}'
    printf '%s\n' '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'
    sleep 25
  } | "$NPX_BIN" -y "mcp-remote@${MCP_REMOTE_VERSION}" "$HA_MCP_URL" \
        --header "Authorization: Bearer ${HA_TOKEN}" \
        --transport sse-only --allow-http >"$out" 2>/dev/null || true

  OUT="$out" URL="$HA_MCP_URL" NODE="$NODE_BIN" NODEV="$("$NODE_BIN" -v)" python3 -c '
import json, os, sys
server = tools = None
for line in open(os.environ["OUT"]):
    line = line.strip()
    if not line:
        continue
    try:
        msg = json.loads(line)
    except ValueError:
        continue
    result = msg.get("result") or {}
    if "serverInfo" in result:
        server = result["serverInfo"]
    if "tools" in result:
        tools = result["tools"]
if server is None or tools is None:
    print("ha-mcp check: FAILED -- no handshake or no tool list", file=sys.stderr)
    sys.exit(1)
print("server : %s %s" % (server.get("name"), server.get("version")))
print("tools  : %d" % len(tools))
print("url    : %s" % os.environ["URL"])
print("node   : %s %s" % (os.environ["NODE"], os.environ["NODEV"]))
print("OK")
'
}

case "${1:-serve}" in
  serve)          serve ;;
  check)          check ;;
  -h|--help|help) usage; exit 0 ;;
  *)              echo "ha-mcp.sh: unknown argument: $1" >&2; usage >&2; exit 1 ;;
esac

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
# HA_TOKEN is normally already exported by ~/.config/shell/env.sh.
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
st_source_env ha-mcp

MCP_REMOTE_VERSION="${MCP_REMOTE_VERSION:-0.8.3}"

usage() {
  cat <<'HELPEOF'
usage: ha-mcp.sh [serve|check|status|-h]

  serve   run the bridge: remote Home Assistant SSE <-> local stdio, speaking
          JSON-RPC on stdin/stdout. This is the default, and the no-argument
          form is what an MCP client invokes.
  check   one-shot health check -- handshake, initialize, tools/list -- and
          print the server version and tool count. Exits non-zero on failure,
          naming the cause on stderr.
  status  the same probe, one line: 0 healthy, 3 nothing to check (no
          HA_MCP_URL or HA_TOKEN), 1 broken, with the cause on the line, such
          as macOS Local Network refusing the node.
          local-health-check reads these codes.

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
  HA_MCP_NODE          node binary to run the bridge with. Default:
                       ~/.local/bin/node, the nodejs.org build machine-setup
                       links for a `.node` `<major> link` line, else the first
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
# Home Assistant is a same-link peer, which macOS's Privacy > Local Network
# permission gates, and macOS keys that grant to the binary's code signature.
# A node the grant does not cover fails with
#
#   fetch failed: connect EHOSTUNREACH <ha-host>:8123
#
# while curl to the same host returns 200, because the gateway and everything
# routed through it stay reachable. Homebrew's node is ad-hoc signed with an
# identifier built from its build hash, so each rebuild or upgrade is a new
# binary to macOS and silently loses the grant (sd:10894). nodejs.org's
# builds carry the Node.js Foundation's Developer ID, one signer for every
# version, so a grant given once holds across upgrades. machine-setup's
# tooling stage installs them with fnm and, for a `.node` `<major> link`
# line, links ~/.local/bin/node to the newest; that link is the default.
#
# `npx` is not a binary -- it is a `#!/usr/bin/env node` script, so the
# interpreter is whatever PATH resolves at spawn time. Resolving Node once,
# here, and putting its folder first on PATH keeps the binary that a
# permission was granted to the binary that actually runs.
#
# HA_MCP_NODE overrides the choice (give it the node binary, not a directory).
OFFICIAL_NODE="$HOME/.local/bin/node"
resolve_node() {
  if [ -n "${HA_MCP_NODE:-}" ]; then
    NODE_BIN="$HA_MCP_NODE"
    if [ ! -x "$NODE_BIN" ]; then
      echo "ha-mcp.sh: HA_MCP_NODE is not an executable: $NODE_BIN" >&2
      exit 1
    fi
  elif [ -x "$OFFICIAL_NODE" ]; then
    NODE_BIN="$OFFICIAL_NODE"
  else
    NODE_BIN="$(command -v node 2>/dev/null || true)"
    if [ -z "$NODE_BIN" ]; then
      echo "ha-mcp.sh: no $OFFICIAL_NODE and no node on PATH: add a \`<major> link\`" >&2
      echo "  line to the machine-setup profile's .node file and run" >&2
      echo "  machine-setup.sh update tooling --apply, or set HA_MCP_NODE." >&2
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

# Runs the bridge once with initialize and tools/list on its stdin, for at
# most $1 seconds; its stdout lands in $PROBE_OUT, its stderr in $PROBE_ERR.
# mcp-remote exits when its stdin closes, so stdin stays open until the tool
# list arrives, the bridge has exited, or the time runs out. A fixed wait
# made every run, good or bad, take that long, and the health check's sweep
# allows a status 30 seconds.
probe() { # seconds
  PROBE_OUT="$(mktemp)"
  PROBE_ERR="$PROBE_OUT.err"
  trap 'rm -f "$PROBE_OUT" "$PROBE_ERR" "$PROBE_OUT.done"' EXIT
  {
    printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"ha-mcp-check","version":"1"}}}'
    printf '%s\n' '{"jsonrpc":"2.0","method":"notifications/initialized"}'
    printf '%s\n' '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'
    # `"tools":[` is the tool list; initialize's capabilities hold `"tools":{`.
    waited=0
    while [ "$waited" -lt "$1" ] && [ ! -e "$PROBE_OUT.done" ] \
        && ! grep -q '"tools":\[' "$PROBE_OUT"; do
      sleep 1
      waited=$((waited + 1))
    done
  } | {
    "$NPX_BIN" -y "mcp-remote@${MCP_REMOTE_VERSION}" "$HA_MCP_URL" \
      --header "Authorization: Bearer ${HA_TOKEN}" \
      --transport sse-only --allow-http >"$PROBE_OUT" 2>"$PROBE_ERR" || true
    : > "$PROBE_OUT.done"
  }
}

# The server and tool-count lines of a probe that got both, or exit 1.
probe_result() {
  OUT="$PROBE_OUT" python3 -c '
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
if server is None or not isinstance(tools, list):
    sys.exit(1)
print("server : %s %s" % (server.get("name"), server.get("version")))
print("tools  : %d" % len(tools))
'
}

# One line naming why a probe failed. Only the bridge's stderr is read, and
# never printed: it can echo the request. EHOSTUNREACH with a host that
# answers curl is macOS refusing this node the LAN. /usr/bin/curl is an
# Apple binary, which that permission leaves alone; a Homebrew curl is
# gated like node. HA_MCP_CURL exists for the tests.
probe_cause() {
  origin=$(printf '%s\n' "$HA_MCP_URL" | sed -E 's#^([A-Za-z][A-Za-z0-9+.-]*://[^/]+).*#\1#')
  if grep -q EHOSTUNREACH "$PROBE_ERR" 2>/dev/null; then
    code=$("${HA_MCP_CURL:-/usr/bin/curl}" -sS -o /dev/null --max-time 5 -w '%{http_code}' "$origin/" 2>/dev/null) || code=000
    case "$code" in
      [1-5][0-9][0-9])
        echo "macOS Local Network blocks $NODE_BIN: node gets EHOSTUNREACH from $origin while curl gets HTTP $code; allow it under System Settings > Privacy & Security > Local Network" ;;
      *)
        echo "Home Assistant does not answer at $origin (node: EHOSTUNREACH; curl: no answer)" ;;
    esac
  else
    echo "no handshake or no tool list from $HA_MCP_URL"
  fi
}

check() {
  require_token
  require_url
  resolve_node
  probe 25
  if ! result="$(probe_result)"; then
    echo "ha-mcp check: FAILED -- $(probe_cause)" >&2
    exit 1
  fi
  printf '%s\n' "$result"
  echo "url    : $HA_MCP_URL"
  echo "node   : $NODE_BIN $("$NODE_BIN" -v)"
  echo "OK"
}

# Convention 6 for the nightly sweep: a machine with no endpoint or no token
# has nothing to check. 20 seconds of probe leave the sweep's 30-second
# bound room for npx to start.
status() {
  case "${HA_MCP_URL:-}" in
    ''|*change-me*) echo "ha-mcp: nothing to check: no HA_MCP_URL"; exit 3 ;;
  esac
  if [ -z "${HA_TOKEN:-}" ]; then
    echo "ha-mcp: nothing to check: no HA_TOKEN"
    exit 3
  fi
  resolve_node
  probe 20
  if result="$(probe_result)"; then
    echo "ha-mcp: ok, $(printf '%s\n' "$result" | sed -n 's/^tools  : //p') tools through $NODE_BIN"
    exit 0
  fi
  echo "ha-mcp: $(probe_cause)"
  exit 1
}

case "${1:-serve}" in
  serve)          serve ;;
  check)          check ;;
  status)         status ;;
  -h|--help|help) usage; exit 0 ;;
  *)              echo "ha-mcp.sh: unknown argument: $1" >&2; usage >&2; exit 1 ;;
esac

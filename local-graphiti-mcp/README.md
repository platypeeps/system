# local-graphiti-mcp

Graphiti knowledge-graph MCP server with bundled FalkorDB via docker compose.
FalkorDB on `:6381` + UI on `:3004`, MCP server HTTP on `:8085` (8083 is google_workspace_mcp on the host, 8084 is local-llama-cpp). Graph data in
`./falkordb_data`, logs in `./mcp_logs` (both gitignored).

## Usage

```sh
mkdir -p ~/.config/system/graphiti-mcp
cp .env.example ~/.config/system/graphiti-mcp/.env   # once, fill in GOOGLE_API_KEY / OPENAI_API_KEY
./graphiti-mcp.sh start|stop|update
```

## Gotchas

- Published on `127.0.0.1` only. These are local dev services, several with a
  credential written into this repo, so nothing off this machine can reach
  them. Change the bind in the script if you deliberately want LAN access.

- This stack bundles its own FalkorDB, so it publishes on `6381`/`3004` to
  stay clear of `local-redis` (`6379`) and `local-falkordb` (`6380`/`3003`).
  Container-internal ports are still `6379`/`3000`. Override the host side
  with `GRAPHITI_FALKORDB_PORT` / `GRAPHITI_UI_PORT`.
- API keys live only in `~/.config/system/graphiti-mcp/.env` (`$SYSTEM_TOOLS_CONFIG/graphiti-mcp/` when that is set), outside the
  checkout. The script passes that path to compose (`--env-file` and
  `GRAPHITI_ENV_FILE`) and refuses to start without it; a `./.env` here is ignored.

## Auth

`FALKORDB_PASSWORD` in that `.env` protects the embedded FalkorDB (published on
host `:6381`). The upstream image only feeds it to the MCP client, so
`docker-compose.yml` overrides the entrypoint to patch `--requirepass` into
the server at container start. Empty password = no auth. Connect with
`redis://default:<password>@localhost:6381`; UI on `:3004` takes the same.

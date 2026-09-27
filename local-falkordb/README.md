# local-falkordb

FalkorDB (Redis-protocol graph database) in docker: server on `:6380`, browser UI on `:3003`.

## Usage

```sh
./falkordb.sh start|stop|update
```

## Auth

The server starts with `--requirepass`. Environment variables (defaults match
`local-postgres` / `local-clickhouse`):

- `FALKORDB_USER` — extra ACL user to create at start (default: `default`,
  meaning no extra user — just password auth on the `default` user)
- `FALKORDB_PASSWORD` — password (default: `verysecure`)

Both can also live in a gitignored `./.env` (see `.env.example`); exported
values win over the file.

Connect with `redis://default:$FALKORDB_PASSWORD@localhost:6380`; the browser UI on
`:3003` takes the same credentials.

## Gotchas

- Published on `127.0.0.1` only. These are local dev services, several with a
  credential written into this repo, so nothing off this machine can reach
  them. Change the bind in the script if you deliberately want LAN access.

- The server is on `6380`, not Redis's `6379`, so `local-redis` can run
  alongside it; `local-graphiti-mcp` bundles its own FalkorDB on `6381`/`3004`
  for the same reason. Override with `FALKORDB_PORT` / `FALKORDB_UI_PORT`.
- No volume mount: data is gone when the container stops. `local-graphiti-mcp` runs its own persistent FalkorDB (whose credentials live in its own `.env`, independent of these).

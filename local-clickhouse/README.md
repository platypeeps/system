# local-clickhouse

ClickHouse server in docker (`:8123` HTTP, `:9000` native; data in `./storage`,
logs in `./logs`, both gitignored) plus the ClickHouse MCP server (SSE on `:8002`).

## Usage

```sh
./clickhouse.sh start|stop|update
./clickhouse.sh client     # clickhouse-client inside the server's network
```

User/password default to `default` / the local dev value `verysecure`
(matches local-postgres); override with `CLICKHOUSE_USER` / `CLICKHOUSE_PASSWORD`
in the environment or in a gitignored `./.env` (see `.env.example`). Exported
values win over `.env`.

## Gotchas

- Published on `127.0.0.1` only. These are local dev services, several with a
  credential written into this repo, so nothing off this machine can reach
  them. Change the bind in the script if you deliberately want LAN access.

- The MCP server is on `8002`, not `8001`: `8001` is the redis-stack
  convention that RedisInsight in `local-redis` publishes. Override with
  `CLICKHOUSE_MCP_PORT`.
- `start` launches both containers; `stop` stops both.

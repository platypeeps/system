# local-redis

Redis Stack in docker: server on `:6379`, RedisInsight UI on `:8001`.

## Usage

```sh
./redis.sh start|stop|update
```

## Gotchas

- Published on `127.0.0.1` only. These are local dev services, several with a
  credential written into this repo, so nothing off this machine can reach
  them. Change the bind in the script if you deliberately want LAN access.

- Redis keeps the registered `6379` and the redis-stack `8001`. The services
  that used to fight it for them moved instead: `local-falkordb` to `6380`,
  `local-graphiti-mcp`'s bundled FalkorDB to `6381`, and the ClickHouse MCP to
  `8002`. All of them can run at once.
- No volume mount: data is gone when the container stops.

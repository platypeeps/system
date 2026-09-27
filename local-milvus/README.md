# local-milvus

Milvus standalone via the upstream `standalone_embed.sh` (vendored here,
refreshed with `install`) plus the Attu web UI on `:8000`. Data lives in
`./volumes` (gitignored).

## Usage

```sh
./milvus.sh start|stop|update
./milvus.sh install    # re-download upstream standalone_embed.sh
```

## Gotchas

- Published on `127.0.0.1` only. These are local dev services, several with a
  credential written into this repo, so nothing off this machine can reach
  them. Change the bind in the script if you deliberately want LAN access.

- `standalone_embed.sh` hardcodes `sudo docker` (Linux assumption); `milvus.sh` no-ops it with a PATH shim (`.sudo-shim/`, gitignored), so no sudo prompt on macOS.
- It also regenerates `embedEtcd.yaml` / `user.yaml` on every start; both are gitignored.
- In Attu connect to `172.17.0.2:19530` (container IP), not localhost.
- Milvus ports: `19530` (grpc), `9091` (metrics), `2379` (etcd).

# local-prometheus

Prometheus in docker on `:9090`, data persisted in `./storage` (gitignored).

## Usage

```sh
./prometheus.sh start|stop|update
```

## Gotchas

- Published on `127.0.0.1` only. These are local dev services, several with a
  credential written into this repo, so nothing off this machine can reach
  them. Change the bind in the script if you deliberately want LAN access.

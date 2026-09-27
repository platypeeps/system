# local-grafana

Grafana Enterprise in docker on `:3000`, data persisted in `./storage` (gitignored).
Runs as the host user id so `./storage` stays writable.

## Usage

```sh
./grafana.sh start|stop|update
```

## Gotchas

- Published on `127.0.0.1` only. These are local dev services, several with a
  credential written into this repo, so nothing off this machine can reach
  them. Change the bind in the script if you deliberately want LAN access.

- Port `3000` is a popular default (dev servers etc.) — check for conflicts.

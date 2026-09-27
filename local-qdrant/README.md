# local-qdrant

Qdrant vector database in docker on `:6337`, data persisted in `./storage` (gitignored).

Not qdrant's usual `:6333`. Two other apps on this machine bundle their own
qdrant and run it at login — OpenWhispr on 6333/6334 and Miyo on 6335/6336 —
so the whole normal range is taken. Override with `QDRANT_PORT`; the container
still listens on 6333 internally.

## Usage

```sh
./qdrant.sh start|stop|update
```

## Gotchas

- Published on `127.0.0.1` only. These are local dev services, several with a
  credential written into this repo, so nothing off this machine can reach
  them. Change the bind in the script if you deliberately want LAN access.

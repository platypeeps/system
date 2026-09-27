# local-postgres

PostgreSQL in docker on `:5434`, data persisted in `./storage` (gitignored).

Not 5432: other projects commonly run their own postgres containers on 5432
and 5433, so the published port stays clear of both. Override it with
`POSTGRES_HOST_PORT` — the server inside the container still listens on 5432.

## Usage

```sh
./postgres.sh start|stop|update
```

Default local dev credentials are `admin` / `verysecure`; override with
`POSTGRES_USER` / `POSTGRES_PASSWORD` env vars before `start` or a
gitignored `./.env` (see `.env.example`; exported values win). Never reuse
these values anywhere non-local.

## Gotchas

- Published on `127.0.0.1` only. These are local dev services, several with a
  credential written into this repo, so nothing off this machine can reach
  them. Change the bind in the script if you deliberately want LAN access.

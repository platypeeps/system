# local-kev

Run Kev on this Mac: a small open-weights model with the same System One API
as TypeSafe's hosted model.

Kev is Jared Palmer's open reproduction. It answers typed questions about a
state (`noul`, `choice`, `score`) with probabilities, as its hosted peer does.
`local-jev` sends it a copy of every live call, so the two answer the same
question and the ledger can compare them. Kev itself is not part of this
repository: `install` clones it to `~/repos/ai/kev`, and this folder holds the
wrapper and the LaunchAgent.

## Usage

```sh
./kev.sh install          # clone or fast-forward Kev, then uv sync --extra serve
./kev.sh serve            # run the server in the foreground
./kev.sh agent-install    # run it as a LaunchAgent, started at login
./kev.sh agent-uninstall  # stop and remove the LaunchAgent
./kev.sh status           # 0 healthy, 3 not installed or not running, 1 broken
./kev.sh test
```

The server listens on `127.0.0.1:8009` only. The first `serve` downloads the
checkpoint into the Hugging Face cache. On a machine
with `/Volumes/models/huggingface` the cache goes there; otherwise it is the
library default, `~/.cache/huggingface`.

One request by hand:

```sh
curl -s http://127.0.0.1:8009/v1/systemone -H 'Content-Type: application/json' -d '{
  "model": "kev-latest",
  "state": {"format": "text", "content": "The deploy failed twice tonight."},
  "questions": [{"id": "q", "type": "noul", "question": "Does this need a human?"}]
}'
```

The answer carries `latency_ms`, the server's own time for the request.

## Configuration

Every value is optional. Copy `.env.example` to `<config>/kev/.env` (`<config>`
is `$SYSTEM_TOOLS_CONFIG`, default `~/.config/system`); an exported value wins
over the file.

| Variable | Default | Meaning |
| --- | --- | --- |
| `KEV_MODEL` | `jaredpalmer/kev-4b@v1.0` | The checkpoint: a Hub id, optionally `@revision`, or a run directory |
| `KEV_PORT` | `8009` | The loopback port |
| `KEV_DIR` | `~/repos/ai/kev` | The checkout `install` makes and `serve` runs |
| `KEV_REPO_URL` | Kev's GitHub repository | Where `install` clones from |
| `KEV_API_KEY` | unset | A bearer token the server then requires |
| `HF_HOME` | see above | The Hugging Face cache |
| `SYSTEM_TOOLS_LABEL_PREFIX` | `local.system-tools` | The LaunchAgent label is `<prefix>.kev` |

Kev pins Python 3.13 in its own `.python-version`; `uv` fetches it.
The server runs on MLX, so it needs Apple Silicon.

## The LaunchAgent

`agent-install` renders `kev.plist.template` into
`~/Library/LaunchAgents/<prefix>.kev.plist` and starts it with `RunAtLoad` and
`KeepAlive`. A crash restarts it after at most 60 seconds (`ThrottleInterval`).
Its output goes to `logs/kev.log` and `logs/kev.err.log` in this folder.
The agent runs `kev.sh serve`, so a change to `.env` takes effect at the next
restart: `agent-install` again.

## Status

`status` asks `GET /v1/models`. An answer with a model list is healthy (0).
No checkout, or nothing listening, is nothing to check (3), so a machine that
never installed Kev stays silent in `local-health-check`. A port that answers
something else, or an error, is broken (1).

## Tests

`./kev.sh test` runs the suite in `tests/` with `launchctl`, `uv` and `git`
stubbed on `PATH` and a loopback stub for `/v1/models`. Nothing reaches
launchd or the network. `make check` runs it in the tools leg.

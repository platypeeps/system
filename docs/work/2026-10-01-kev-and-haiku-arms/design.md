---
title: Run a local Kev and a cheap frontier model beside every live Jev call
created: 2026-10-01
item: sd:2366
---
# Design — Kev and Haiku arms beside every live Jev call

## Shape

```
caller ── jev noul ... ──> jev.py ── post() ──> Jev (TypeSafe)       ──> stdout, exit code
                              │
                              └─ spawn, detached ──> jev_compare.py ─┬─> Kev   (127.0.0.1:8009)
                                                                     └─> Haiku (one transport)
                                                                     └─> one judgment row per arm
```

`jev.py` stays stdlib-only. The arms live in a second stdlib module,
`local-jev/jev_compare.py`, which runs as its own process.

## Not delaying the caller: detach, not a bounded wait

Two options were on the table.

1. Start the arms on threads beside the Jev call, and after Jev answers wait
   at most a bounded extra time.
2. Start a detached child process before the Jev request goes out. The parent
   does not wait for it at all.

The design takes option 2, for three reasons.

- **A bounded wait censors the data.** `claude -p` takes seconds and Kev-4B
  takes about 700 ms cold on an M5. A bound short enough not to hurt a caller
  cuts off exactly the slow answers, so the latency column would hold only the
  calls that beat the bound. The paper needs the whole distribution.
- **A bounded wait is a cost on every call.** Any bound above zero is time
  added to a judgment that already finished, and a cron job or a hook makes
  many of these calls.
- **The arms do not need Jev's answer.** Agreement is computed at report time,
  from rows that share a pair id. Nothing in the arm depends on the parent
  staying alive.

The child is started with `subprocess.Popen`: a fresh interpreter, a new
session (`start_new_session=True`), and stdin, stdout and stderr on
`/dev/null`. A caller that reads `jev` through `$(...)` or a pipe waits for
end of file on stdout. A child that inherited stdout would hold that pipe open
and delay the caller by the arm's whole run; that is the failure the test
`a hung arm does not delay a piped caller` pins. The new session also keeps a
caller's process-group kill from taking the arms with it.

`fork` without `exec` was rejected. On macOS a forked child that makes an HTTPS
call runs CoreFoundation proxy lookup in a process that forked, which is the
known Objective-C fork-safety crash. A fresh interpreter has no such state.

The request reaches the child as its stdin: a `0600` temporary file, unlinked
before the child starts, so no name is left behind if the child dies before
reading it. A pipe was rejected: a payload over the pipe buffer would block
the parent's write until the child had started, which is time the caller
waits. The file holds the payload after redaction, exactly the bytes that
leave the machine, and the disk space goes when the child closes it.

The child bounds itself. Each arm has `JEV_COMPARE_TIMEOUT` seconds (default
60), and the child exits when every arm has finished or timed out.

The cost to the caller is one `posix_spawn` and one small file write, a few
milliseconds. The meter's own row is still written after the answer is printed.

## When the arms run

Only on a call that sends to Jev. The hook is in `post`, after redaction and
refusal and before the first attempt, so the arms see exactly the bytes Jev
sees. `status` sends a probe through `post` but is not measured, and the arms
are not started for it. A call declined before `post` (switched off, unkeyed,
a refused key) starts no arm.

The arms run only when the meter is on. An arm exists to produce a row; with
`JEV_METER=0` nothing would record it, so running it would only spend money.
This also keeps the `local-jev` suite, which pins the meter off, from fanning
out.

Retries do not start a second set of arms. The arms start once per call.

## Switches

| Variable | Meaning | Default |
| --- | --- | --- |
| `JEV_COMPARE_KEV` | Kev arm; an off-word switches it off | on |
| `JEV_COMPARE_HAIKU_VIA` | `anthropic`, `openrouter`, `claude-cli`, `baseten`, or an off-word | `anthropic` |
| `JEV_COMPARE_TIMEOUT` | seconds per arm | 60 |

`JEV_COMPARE_KEV` is read with `stage_off`, the existing off-word reader.
`JEV_COMPARE_HAIKU_VIA` is off when `stage_off` says so; any other unknown word
records an `invalid` decline rather than guessing a transport. A switched-off
arm writes no row; only an arm that tried and could not answer records a
decline.

## The Kev arm

The same System One request, sent to `KEV_URL` (default
`http://127.0.0.1:8009/v1/systemone`), with `model` set to `kev-latest`.
Kev's server accepts `kev-latest` and `jev-latest`; a pinned `JEV_MODEL` such
as `jev-1.13.0` would be refused there. `KEV_API_KEY`, when set, goes in a
`Bearer` header. The response has the System One shape plus `latency_ms`,
which becomes the row's `server_ms`.

The row records `provider=local`, `model` from `KEV_MODEL` (default
`jaredpalmer/kev-4b@v1.0`) and `usd=0`. A refused connection is the decline
`unavailable`; a timeout is `timeout`.

## The Haiku arm

### One request per question

Jev answers each question of a request in isolation: questions in one request
cannot see each other. A prompt that held all of them would let the model read
one answer into another. So the adapter sends one model request per question,
concurrently, and sums the tokens and the cost. The row's latency is the
arm's wall clock, which is the slowest question. Most calls carry one question,
so this costs nothing extra for them.

### The prompt

The system prompt fixes the role: a classifier that returns only JSON. The
user message carries the state (text, or JSON rendered with `json.dumps`) and
the question, then asks for a probability per option:

- `noul`: `{"probability": p}`, the probability that the condition holds.
- `choice`: `{"probabilities": {"<key>": p, ...}}` over the caller's keys,
  with their descriptions.
- `score`: `{"probabilities": {"0": p, ...}}` over the levels, low first.

Where the transport supports a JSON schema the request carries one: Anthropic's
`output_config.format`, OpenAI-compatible `response_format` for OpenRouter and
Baseten, and `--json-schema` for `claude -p`. The parser also strips a code
fence, because a schema is a request and not a guarantee on every transport.

### Back to the System One shape

The probabilities are clipped to `[0, 1]` and normalised. The answer then
mirrors Kev's `to_answers`, which mirrors TypeSafe's reference adapter:

- `noul`: `noul = p`.
- `choice`: the argmax key, `confidence = (p_max - 1/K) / (1 - 1/K)`.
- `score`: `score = Σ i·p_i`, `confidence = max(0, 1 - E|i - mode| / D)`.

So the three arms' confidences are the same function of a distribution, and a
difference between them is a difference in the distribution.

A self-reported probability is not a calibrated one. The README says so; the
paper reads Haiku's Brier score with that caveat.

### Transports

| `VIA` | Endpoint | Key | Model default |
| --- | --- | --- | --- |
| `anthropic` | `https://api.anthropic.com/v1/messages` | `JEV_COMPARE_ANTHROPIC_KEY` | `claude-haiku-4-5` |
| `openrouter` | `https://openrouter.ai/api/v1/chat/completions` | `JEV_COMPARE_OPENROUTER_KEY` | `anthropic/claude-haiku-4.5` |
| `baseten` | `https://inference.baseten.co/v1/chat/completions` | `JEV_COMPARE_BASETEN_KEY` | `JEV_COMPARE_BASETEN_MODEL`, required |
| `claude-cli` | `claude -p --model haiku` | none (the CLI's own login) | `haiku` |

The Anthropic key has its own variable. The operator's shell profile unsets
`ANTHROPIC_API_KEY` on purpose, so the arm never reads that one.
`JEV_COMPARE_HAIKU_MODEL` overrides the model of the selected transport.
A transport with no key, or a placeholder key, records `unkeyed` and sends
nothing. Baseten with no model also records `unkeyed`: it is not configured.

`claude -p` runs in an empty temporary folder, with `--setting-sources project`
so no user hooks or plugins run, `--strict-mcp-config` so no MCP server
starts, `--tools ""`, and `--no-session-persistence`. Its tokens include
Claude Code's own system prompt and its latency includes process start-up;
the README names both, because they make this transport's numbers
incomparable to the other three.

### Cost

`usd` is the transport's reported cost when it reports one (OpenRouter's
`usage.cost`, `claude -p`'s `total_cost_usd`). Otherwise it is
`tokens_in × JEV_COMPARE_HAIKU_USD_IN + tokens_out × JEV_COMPARE_HAIKU_USD_OUT`,
per million tokens, defaults 1 and 5, Haiku 4.5's list price.
Baseten serves no Haiku, so it has no default price: without both variables
its rows carry tokens and no cost, rather than a cost that is Haiku's.

## Privacy

The arms receive the payload after `redacted_payload`, so a key that matches a
redaction pattern refuses the whole call before any arm starts. The Kev arm
stays on this machine. The Haiku arm sends the same state to Anthropic,
OpenRouter or Baseten: a second third party beside TypeSafe. That is the cost
of the comparison, and the README section "What it never does" says it. A
caller that must not send its state to a second party switches the Haiku arm
off with `JEV_COMPARE_HAIKU_VIA=off`.

The rows hold the same shapes as every other row: numbers and identifiers.
The per-option probabilities are numbers in the caller's criteria order, never
the keys.

## Ledger: migration 017

`arm` gains two values, `kev` and `haiku`. SQLite cannot alter a CHECK
constraint, so the table is rebuilt as 007 rebuilt `state`. Two columns join
it:

- `server_ms INTEGER`: the latency the model reported, beside the wall clock
  in `duration_ms`. Kev reports `latency_ms`; the Jev row records it when the
  response carries it.
- `probabilities TEXT`: the distribution as numbers in option order,
  `0.47,0.28,0.25`. Shaped like `ordering`, with decimals: numbers and commas
  only. Brier scores need the distribution, and agreement on a noul needs the
  probability, which `answer` already holds.

The arm names are roles in the comparison, not vendors: `kev` is a local
System One server, `haiku` is a frontier model through a prompt adapter. The
transport is the row's `provider`, so `judgments compare` splits the Haiku arm
by transport when more than one has run.

The existing `judgments` report keeps its meaning: its counts and its paired
samples read only the `jev` and `baseline` arms.

A library at schema 17 refuses to write to a database at 16, and a library at
16 refuses a database at 17. After the merge the operator migrates
(`sd-db.sh migrate`) and reinstalls `sd_db` where `jev` reads it, the command
pack's venv. Until then the meter's rows are dropped, as for any other
unmigrated store; no caller is affected.

## The comparison report

`sd-db.sh judgments compare [--since S] [--until S] [--stage NAME] [--json]`.

Per stage, one line per arm (`jev`, `kev`, `haiku`, split by provider when
the stage has more than one):

- calls, `ok`, declines by cause;
- p50 and p95 of `duration_ms`, and p50 of `server_ms`;
- tokens in and out, and the summed cost;
- agreement with the Jev row of the same pair: the share of pairs whose
  answer equals Jev's (for a noul, both on the same side of 0.5; for a score,
  the rounded recorded score, never the distribution's mode), and for a
  noul the mean `|Δp|`;
- on pairs whose Jev row carries a label: accuracy, and the Brier score from
  `probabilities` (a noul's `answer` is its probability).

A label lives on the Jev row. A pair shares one decision, so the report reads
the label from the pair's Jev row for every arm, and ignores one found on an
arm row. `judgments label` keeps labelling one row, and refuses a `kev` or
`haiku` row.

Percentiles are computed in Python: SQLite has none, and the read is per
stage.

## `local-kev`

`kev.sh` copies the harnesses in this repository: the config pattern of
`local-task-actions` (`.env` provides defaults, the environment wins), its
`launchctl bootstrap` and `bootout` sequence, and a committed
`kev.plist.template` filled with `@LABEL@`, `@DIR@` and `@HOME@`. The label
is `$SYSTEM_TOOLS_LABEL_PREFIX.kev`. `RunAtLoad` and `KeepAlive` start it at
login and restart it after a crash; `ThrottleInterval` keeps a crash loop from
spinning while weights are missing.

- `install`: clone or fast-forward `KEV_REPO_URL` into `KEV_DIR` (default
  `kev/` beside `kev.sh`), then `uv sync --extra serve`. The root
  `.gitignore` excludes that checkout, so none of Kev enters this repository.
- `serve`: `exec uv run --extra serve python -m kev.serve --run "$KEV_MODEL"
  --host 127.0.0.1 --port "$KEV_PORT"`, in the foreground, as the agent runs it.
- `status`: 3 when the checkout is missing or nothing listens; 0 when
  `GET /v1/models` answers with a model list; 1 when the port answers anything
  else.
- `HF_HOME` defaults to `/Volumes/models/huggingface` when that folder exists,
  the rule `local-llama-cpp` uses.

Port 8009 is Kev's documented port and no service in this repository uses it.
It joins the ports list in `.claude/rules/services.md`.

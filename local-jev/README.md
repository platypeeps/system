# local-jev

Ask Jev one typed question from the shell, and print the answer.

Jev is TypeSafe's System One model. It does not write prose and does not
explain itself: it answers a narrow question about some state with a type and
a probability. `noul` prints a probability, `choice` prints the name it picked,
`score` prints a number. Code decides what that means.

## Usage

```sh
./jev.sh noul   "Does this need a human tonight?" --state finding.txt --gate 0.8
./jev.sh choice "Which change type is this?" --criteria 'feat,fix,chore'
./jev.sh score  "How urgent is this?" --levels "can wait,this week,today"
./jev.sh ask    --questions questions.json --state thread.json --state-format json
./jev.sh status
./jev.sh test
```

State comes from stdin unless `--state FILE` says otherwise, so the natural
form is a pipe:

```sh
git log -1 --format=%s | jev choice 'Which change type is this?' \
    --criteria 'feat=new capability,fix=corrects behaviour,chore=housekeeping'
```

`--criteria` splits on commas and on the first `=` of each entry. A description
that needs a comma goes in `--criteria @file.json` instead; inventing a quoting
dialect for a shell argument is how a criteria string starts meaning something
other than what it reads.

`ask` is the one to reach for when there is more than one question about the
same state. Questions in a single request run in parallel and cannot see each
other's answers, which is both why they are cheap and why a question that
depends on an earlier answer needs a second call.

## Why a command and not an MCP server

TypeSafe ships an SDK and an agent skill, and no CLI. Every caller here is a
shell: a cron `JOB_COMMAND`, a git hook, an agent running Bash. A command costs
a session nothing, where an MCP server puts a tool schema in every session that
loads it and still cannot be called by cron. Claude Code and Codex both reach
this through Bash, and `local-bin-links` puts it on `PATH` as `jev`.

The Claude Code skill `typesafe-ai` is for *writing* the questions — the
primitives, the state shaping, the confidence guidance. This is what the
questions then run on.

## Why a command and not a `claude -p` job

A judgment made here does not depend on an agent session. Thirteen
agent-driven cron jobs died on 2026-09-19 when that session expired, and a
`JOB_COMMAND` calling this one would have survived it. That is the argument for
moving a nightly job whose real work is one judgment out of `JOB_PROMPT`.

## Nothing may depend on it

Jev is experimental. Every caller keeps the mechanism it had, and reaches for
Jev only when Jev can answer. Two shapes make that the easy path:

```sh
if jev enabled JEV_MY_STAGE; then verdict=$(jev noul ... )
else verdict=$(the_old_way); fi

verdict=$(jev noul 'Is this a real defect?' --state f --gate 0.8 --fallback yes)
```

`jev enabled` exits `0` when Jev can answer here and `3` when it cannot. It
costs nothing and calls nothing, so it is safe in a hot path and in a hook.

Given a stage name it answers both halves at once: can Jev answer on this
machine, **and** has that variable been used to switch this one stage off. The
name is optional, so the fleet question stays askable on its own. A caller
passes its own variable and never reads it itself, so the vocabulary below is
defined in one place instead of once per caller.

`--fallback ANSWER` prints `ANSWER` and exits 0 when Jev is switched off,
unkeyed, or failing, and writes the reason to stderr. It is never silent: a
lane that quietly stops running is the bug this shape exists to avoid.

It goes after the verb, and every verb that calls out takes it, `ask`
included — for a batch, pass the JSON a caller can parse as an empty answer
set:

```sh
jev ask --questions q.json --state s.json --state-format json --fallback '{}'
```

`status`, `enabled`, `on` and `off` take no fallback. The first is the
diagnostic that reports an outage and the other three never call out.
`local-jev/tests/test_jev.py` reads that split off the parser rather than
listing the verbs, because the list that did missed `ask`.

**A machine with no key and a machine with the switch off behave
identically.** That is deliberate. It means a caller written against the
switch is already correct on a machine that was never keyed, and one test
covers both.

Two more machines join them, both from review of this branch. One whose
`TYPESAFE_API_KEY` is still `change-me` — a straight copy of `.env.example` —
is **unconfigured, not broken**: sending `Bearer change-me` earns a 401, and
`status` reporting 1 is a nightly health-check finding saying the service is
down when nobody has configured it yet. And one whose `JEV_TIMEOUT` or
`JEV_RETRIES` does not parse, which used to be an uncaught traceback out of
every verb — `enabled` included, the one call a caller makes to find out
whether it should call at all. Both now exit 3, send nothing, and honour a
`--fallback`.

`jev on` and `jev off` consult none of this on purpose. The remedy for a
misconfigured machine must not be unreachable because the machine is
misconfigured.

**Only one option may read stdin per call.** `--state` defaults to `-`, so
`jev ask --questions -` must give one of the two a file. It used to consume
stdin for the questions and read EOF for the state, sending an empty state and
reporting the answer that came back: a judgment about nothing, presented as a
judgment.

## The switch

```sh
jev off          # stop using Jev on this machine, everywhere, now
jev on           # resume
jev enabled --why
jev enabled JEV_HEALTH_CHECK --why   # ...and is that one stage still on?
JEV_ENABLED=1 jev noul ...    # one call, against the file's setting
```

The switch is a file — `~/.config/jev/enabled`, or `JEV_FLAG_FILE` — because
the callers that matter are cron and launchd, and neither reads a shell
profile. `JEV_ENABLED` overrides it for one call or one session.

**Absent means enabled.** A switch that defaulted to off would make every
integration added after it silently never run, which is the failure this
repository already knows by name. The key is the other half: no key is off.

### Per-stage switches

Each integration has its own variable, read by `jev enabled STAGE`:

```sh
jev enabled JEV_ADVERSARIAL_GATE     # 0 = this stage is on, 3 = it is off
JEV_ADVERSARIAL_GATE=0 adversarial-gate.sh rank ...   # this stage off, for one command
jev enabled JEV_ADVERSARIAL_GATE --why   # says which half said no
```

**Unset means on**, for the same reason the file's absence does. `0`, `off`,
`false`, `no` and `disabled` switch a stage off, in any case; **any other
value leaves it on** — including the `1` these variables used to require. A
typo is not an outage, which is the rule `read_flag` already follows for the
file, and these follow it too. There is no value meaning "ask me later".

**A stage variable only ever subtracts, and cannot switch a stage on.**
`cmd_enabled` asks `unusable()` before it looks at the variable, so `jev off`,
a machine with no `TYPESAFE_API_KEY`, a key still reading `change-me`, and a
`JEV_TIMEOUT` that does not parse each answer `3` however the variable is set.
`JEV_ADVERSARIAL_GATE=1` on an unkeyed machine is still off. The fleet switch and
the key decide whether Jev can be asked at all; a stage variable only takes
one caller out of the set that asks.

That direction is deliberate. Switches that could each contradict the
machine-wide answer would be as many ways to leak a call off a machine
somebody had already switched off — and it is why a caller makes one call here
rather than checking a variable itself and then probing.

The variable is named after the tool — `local-adversarial-gate` uses
`JEV_ADVERSARIAL_GATE` — and **the list of them is not written down here**, because
a list in prose goes stale. `KNOWN_CALLERS` in the repository's
`tests/test_jev_contract.py` is the enumeration, and it is enforced: a folder
that starts or stops calling Jev fails that suite naming itself, so it cannot
drift the way this paragraph could.

To ask the machine which variable a given caller reads, ask the caller:

```sh
local-adversarial-gate/adversarial-gate.sh help | grep JEV_
```

**Do not reach for `grep -rho 'JEV_[A-Z_]*'` across the repo.** It answers a
different question and gets this one wrong in both directions: it returns
`JEV_GATE`, `JEV_MODEL`, `JEV_TIMEOUT` and other variables that are not stage
switches, and grepping for `enabled JEV_` instead misses every caller that
passes the name through a constant. Two of the nine appear in neither result.

## Exit codes

`status` answers `0` ok, `3` no key on this machine, `1` configured and
failing. `local-health-check` reads these codes, and the sweep finds this tool
because its `help` says that sentence — there is no list of tools to add one
to.

The `status` probe is a real request: one question, a handful of tokens. That
is what makes it able to tell `3` from `1`. A status that only looked for the
environment variable could not, and `local-health-check` runs nightly, so the
cost is a handful of tokens a night.

The other verbs answer `0` or `1`. They do not overload the exit code with the
answer, because a probability is not an exit status. Gate it instead:

```sh
if [ "$(jev noul 'Is this a real defect?' --state finding.txt --gate 0.8)" = yes ]; then
```

## Configuration

`TYPESAFE_API_KEY` is required and normally already exported from
`~/.config/shell/env.sh`. A machine without it is not broken, it is
unconfigured, and `status` says `3`.

A `.env` in `<config>/jev/` works too — copy `.env.example` there;
`<config>` is `$SYSTEM_TOOLS_CONFIG`, default `~/.config/system` — but it
provides defaults only: a value already in the environment wins, so
`JEV_MODEL=jev-1.13.0 jev noul ...` pins a model for one call.

`JEV_URL`, `JEV_MODEL`, `JEV_TIMEOUT` and `JEV_RETRIES` override the endpoint,
the model, the per-attempt timeout and the retry count. Retries cover 429, 529
and 5xx with a doubling backoff, and honour `Retry-After` when it is a number.
A 401 and a 422 are not retried: the key or the request is the problem, and
retrying spends the budget twice to learn the same thing.

## What a call is worth

Every call is measured, and the measurement can never cost the caller
anything. One `judgment` row goes into `sd.db` after the answer has been
printed: who asked, which stage, which model, the primitive, the input and
output tokens the response has always carried, the wall clock the caller
waited, the judgment, its confidence, how the call ended, and whether it
changed anything. A machine with no database, an unmigrated one or a
read-only one records nothing and behaves exactly as it did before; there is
no path through the recorder that raises. `JEV_METER=0` switches it off and
`JEV_METER_DB` points it somewhere else. A locked ledger is waited on for
250 ms and then the row is dropped; `JEV_METER_BUSY_MS` sets another bound.
The row needs `sd_db`, so `jev.sh` runs the interpreter `sd-db.sh` would:
`PYTHON`, `SD_DB_PYTHON`, then the command pack's venv, then `python3`.

Name yourself, or the row says `unknown`:

    jev noul 'is it?' --caller local-adversarial-gate --stage JEV_ADVERSARIAL_GATE

`JEV_CALLER` and `JEV_STAGE` do the same, for a caller that is a shell and
reaches this script through a wrapper that already exports its own variables.

**The same row can go to a trace collector.** With `JEV_TRACES_URL` set,
`jev_trace.py` posts it as one OTLP/HTTP JSON span, service `jev`, after the
answer is printed. Point it at `local-genai-traces`
(`http://127.0.0.1:4338/v1/traces`) to see Jev calls in Phoenix beside the
other experiments. It carries the ledger's fields and nothing else, never
raises, and waits `JEV_TRACES_TIMEOUT` seconds (default 0.5) at most.
Unset sends nothing.

**Nothing you submit is recorded.** No prompt, no state, no path, no subject,
no body. The ledger holds identifiers and counts, and the two columns that
could carry content by accident are **shaped**, not merely capped: a length
cap keeps a paragraph out and lets `/Users/someone/private.txt` straight in. The
judgment is a number -- a probability, a score, or which of your own criteria
won, counting from 1 -- and an ordering is positions into your own input. A
row that breaks either shape is refused rather than truncated; a truncated
value is a prefix of content, stored.

The names you choose are shaped as well. `--caller`, `--stage`, `--id` and
`--pair` are identifiers, so `--id 'the quarterly numbers'` and `--stage
/Users/someone/private.txt` never reach the ledger. `jev` drops the field and
keeps the row -- filed under `unknown` where the ledger needs a name -- rather
than losing the stage, the arm and the timing over the one field you got
wrong.

`jev choice` therefore records the **position** of the criterion that won,
never its key. A key is text you wrote, so it can be a path or a subject, and
you already have the list to read the position back against.

**A busy ledger loses the row, not your time.** The recorder opens the
database with no lock wait at all, so contention drops the measurement
immediately. Five seconds waiting for a lock would be five seconds added to a
decision that already finished, because a caller running this as a subprocess
waits for the process to exit.

### The old mechanism is the control arm

A row for the model alone cannot say whether it helped. Every caller here
still has the mechanism it used before, and that mechanism runs whenever this
one declines. So it is measured too, with the same fields and under the same
stage key, in the same table:

    jev enabled JEV_NOTIFY --record --caller local-notify   # the decline
    jev record --caller local-notify --stage JEV_NOTIFY \
        --decline unkeyed --answer 2 --duration-ms 3        # what you did

`--record` is opt-in because `enabled` promises to cost nothing and every
caller asks it on every run. `jev record` sends nothing, needs no key, prints
nothing and always exits 0.

Those are two rows for one decision, and the report counts it once. The
`enabled` row is a **gate event**: the gate saying the old path is about to
run, which is a different fact from the old path having run. The per-stage
comparison leaves gate events out of the calls and the declines and counts
them on their own line, `never reached a decision`. A stage whose callers only
ever decline still has a number there, which is the only number it ever had.

The decline reason is a value and not a boolean: `switched-off`, `unkeyed`,
`no-path`, `timeout`, `invalid`, `unavailable`, `budget`. They are separate
because the repairs are separate, and `no-path` in particular has already
caused a silent outage here -- every gate on, the key working, the probe
answering `0.98`, and every PATH consumer skipping its step.

A lane that only reorders a list records the ordering on both arms, as
positions into its own input (`--positions 0,1,2`). Positions, never the
things at them; the ledger refuses anything that is not whole numbers and
commas.

### Shadow mode

    jev choice 'which route?' --criteria desk,phone --shadow desk --shadow-ms 4

Ask anyway, record both answers, and print the caller's own. The stage
behaves exactly as it did, including on the run where the call failed, and it
produces a paired sample -- which is the only thing a delta can be computed
from. It costs a real call, so it is off unless the flag is given, and the
report counts it separately.

### Reading it back

    local-sd-db/sd-db.sh judgments [--since 2026-09] [--json]

One block per stage: both arms, their calls, outcomes, tokens, cost and
latency, the decline reasons by name, and how many paired samples exist. A
stage with none is named, because a reader who is not told will assume a
delta.

### Was it right? Labels from later outcomes

Reported confidence is not accuracy, so a row can carry a label: the answer
an authoritative later source says it should have been, written through
`sd-db.sh judgments label` (see `local-sd-db/README.md`). `sd-db.sh judgments`
then reports how many labelled rows were right, by stage and by reported
confidence.

A caller whose judgment can be labelled names the judged thing with
`--subject NAME`: an identifier, recorded as the row's question id and never
sent. `--id` stays a key of the request, so a subject must not go there.

No labeller ships yet. A label must come from independent evidence of the
right answer, never from the prediction under test; a later fix in a shared
file is not that evidence for a review tier (sd:2107).

### The comparison arms

Two comparison arms can ask other models the same question as each live
Jev call, so the ledger holds paired answers for the Jev evaluation. **Both
are off unless switched on**, one at a time:

- **Kev**, the open-weights model `local-kev` serves on `127.0.0.1:8009`.
  Its rows record provider `local`, the checkpoint from `KEV_MODEL`, and a
  cost of 0.
- **Haiku**, Claude Haiku 4.5 through one transport that
  `JEV_COMPARE_HAIKU_VIA` names: `anthropic` (keyed by
  `JEV_COMPARE_ANTHROPIC_KEY`), `openrouter`, `claude-cli` (`claude -p
  --model haiku`, under the operator's own `claude` login), or `baseten`, which
  serves no Haiku and so needs `JEV_COMPARE_BASETEN_MODEL` and its own
  prices. The row records the transport as provider and the model name.
  Haiku's list price is the default only for the transport's own Haiku model;
  `JEV_COMPARE_HAIKU_MODEL` naming another model needs
  `JEV_COMPARE_HAIKU_USD_IN` and `_OUT`, or its rows carry no cost.

The arms get the request after redaction, exactly as Jev gets it. They run in
a detached child process that `jev.py` starts once per call, before the first
attempt, and does not wait for: the caller's output, exit code and timing are
Jev's alone, and a hung arm costs the caller nothing. The child writes one
judgment row per arm, with arm `kev` or `haiku`, the same pair id as the Jev
row, the answer, the probabilities, the model's own latency where it reports
one, tokens and cost. An unreachable or unkeyed arm records a decline, and
so does a reply that does not carry exactly the options asked about.
Before any arm calls out, the child checks that the ledger would take its
row: `sd_db` installed and naming the arm, and the database at the
library's schema. Without that, the arm does not run, so no paid call goes
unrecorded.

Haiku answers one question per request, with a JSON schema for the answer's
probabilities, so no question sees another's answer: the isolation Jev's
batch gives. A batch of more than 8 questions (`MAX_HAIKU_QUESTIONS`) is
declined as `invalid` before any request, so one call never fans out into a
pile of paid ones. So is a question with more than 255 options, the most the
ledger records. An exported empty switch (`JEV_COMPARE_HAIKU_VIA=`) is off,
and it beats an on-value in `<config>/jev/.env`. `--fallback`, `enabled`, a meter that is off, and a call Jev
never gets start no arm.

Switch the Kev arm on with an on-word (`JEV_COMPARE_KEV=1`) and the Haiku arm
by naming a transport (`JEV_COMPARE_HAIKU_VIA=anthropic`). Unset or an
off-word is off: unlike a Jev stage, where unset means on, an arm sends every
live request to a second endpoint, so it is opt-in. An off arm starts no
child and writes no row. Install the Kev server (`local-kev/kev.sh install`,
then `agent-install`) when you switch the Kev arm on, not before. The rest of
the settings are in `.env.example`. Read the comparison with:

    local-sd-db/sd-db.sh judgments compare [--stage S] [--since 2026-10] [--json]

## What it never does

It never prints the key, never logs it, and never puts it in an error message.

**Nothing sensitive should be piped into it.** Every call leaves the machine.
That is why `local-scan-for-secrets` is not a caller: triaging its hits is a
textbook Noul, and it would mean posting candidate credentials to a third
party, which is the one thing that scanner exists to prevent.

**Redaction is the backstop, not the permission.** Before a request leaves,
every string in its state and questions passes credential shapes (GitHub,
AWS, Slack, OpenAI-style and TypeSafe keys, JWTs, `Bearer` values, private
key blocks, `NAME_TOKEN=value` assignments, long hex and mixed base64 runs)
and the operator's `<config>/privacy-patterns`, the file `local-leak-guard`
reads, with the same `grep -E` meaning, line by line. Each match becomes
`[REDACTED]`, and stderr says how many were taken out. A key that matches,
in JSON state or among a choice's criteria, is not renamed: the request is
refused whole, nothing is sent, and a `--fallback` is honoured. A pattern file that cannot be read or
compiled is a setting that does not parse: nothing is sent, `enabled` exits 3,
and a `--fallback` is honoured. Patterns catch shapes and listed values; they
do not make a sensitive input safe to send.

**The comparison arms widen who sees a request.** With the Haiku arm on, every
redacted request Jev gets also goes to Anthropic, or to OpenRouter and
Anthropic, or to Baseten, depending on the transport. The Kev arm stays on
this machine. Switch the Haiku arm off where that second recipient is not
acceptable; it never receives a request that redaction refused.

## Tests

`./jev.sh test` runs the suite against a stub of the API on loopback, never
against TypeSafe. A suite that called the real endpoint would spend tokens on
every `make check` and go red when someone else's network did, and `tests/ci-native.sh`
treats a skipped test as a failure, so "skip when offline" is not an option.
The stub also lets a test assert the exact payload that went out, which is the
half of the contract a live call cannot show.

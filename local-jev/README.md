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

### Per-stage budgets

```sh
JEV_HEALTH_CHECK_MAX_CALLS=10       # requests per UTC day
JEV_HEALTH_CHECK_MAX_TOKENS=20000   # input plus output tokens per UTC day
```

A stage can carry a ceiling for each UTC day (sd:1239). A spent budget is a
decline with cause `budget`, exactly like Jev off: `jev enabled STAGE` exits
3, a `--fallback` is printed, and a call without one exits 3. The caller's
old path runs, and the ledger counts the decline under that cause.

- Unset means no ceiling. A stage with none reads and writes no file.
- A call counts itself after its last local check and before it is sent,
  so two callers cannot share the last call. A call refused here (bad
  arguments, a redacted key) counts nothing, and neither does `enabled`.
- Tokens are known only after the answer, so a token ceiling stops the call
  after the one that crossed it.
- A ceiling that is not a whole number declines, and so does a counter that
  cannot be read or locked within a second, or that is not a budget book. A
  limit you set is never lifted silently.
- A token charge that cannot take the lock is left beside the counter as a
  `pending-*.json` file; the next call that holds the lock adds it.
- Tokens count toward the UTC day their call was counted on. A call that
  crosses midnight charges nothing to the new day.
- Every counter in the book must be a whole number from 0; a book holding
  anything else declines, like a book that does not parse.
- The counter is one file, `~/.local/state/jev/budget.json`
  (`XDG_STATE_HOME`, or `JEV_BUDGET_DIR`), locked through `budget.json.lock`
  beside it. It holds the current UTC day only, and each write replaces it
  whole, so a failed write leaves the last counts.
- `status` and the comparison arms are not counted; `JEV_COMPARE_STAGES`
  bounds the arms.

Put the ceilings in `<config>/jev/.env`; an exported value wins over it.

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
and 5xx with a doubling backoff, and honour `Retry-After` when it is a finite,
non-negative number, capped at 60 seconds.
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
`JEV_METER_DB` points it somewhere else. The row's cost comes from the
`price` that `providers.yaml` beside the database gives a `typesafe` entry;
with no such entry the row carries tokens and no cost
(`local-sd-db/README.md`, section `judgments`). Leave the entry's `model` out
to price every Jev model; the row records the model the response names.
A locked ledger is waited on for 250 ms and then the row is dropped; `JEV_METER_BUSY_MS` sets another bound.
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

**Nothing you submit reaches the ledger or a span.** No prompt, no state, no
path, no subject, no body. The [trace corpus](#trace-corpus) is the one place
that keeps them, in a private file on this machine. The ledger holds
identifiers and counts, and the two columns that
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

A shadow `choice` records the caller's answer as its position in the
criteria, the same shape as the judgment's. A `noul` records `yes` as 1 and
`no` as 0, the words `--gate` prints. Under a `--gate` other than 0.5 it
records no number: the report reads every noul at 0.5 and no row keeps the
gate, so that pair is counted and not compared.

### Live paired mode

    jev choice 'which route?' --criteria desk,phone \
        --fallback "$(old_way)" --baseline "$(old_way)" --baseline-ms 4

`--baseline ANSWER` is the caller's own answer for this decision, and
`--baseline-ms N` is how long the caller's own path took. Every judgment verb
takes both: `noul`, `choice`, `score` and `ask`.

The call behaves exactly as without the flags. The judgment is printed, the
exit codes are the same, and `--fallback` works as before. Two rows go into
the ledger under one pair id:

- the judgment's row, with `changed` comparing the printed answer with the
  baseline, not with the fallback;
- a baseline row: `arm=baseline`, `shadow=0`, `primitive=baseline`,
  `provider=local`, with `--baseline-ms` as its duration.

The baseline is stored in the judgment's shape. For `choice` that is its
position in the criteria, counting from 1. A baseline that is not a number,
or not one of the criteria, is dropped from the row and the row is kept.
Two numbers are compared as numbers, so `2` and `2.0` agree.

Use `--baseline` when the fallback is not the answer the caller would have
used. `sd-review` passes a sentinel as `--fallback`, and every one of its
rows compared against that sentinel said `changed=yes`.

`--baseline` and `--shadow` together are refused with exit 1, before anything
is sent. One prints the judgment and the other prints the caller's answer.
With `--json`, `changed` is `unknown`, because no single answer is printed.

### The shadow switch

    jev shadow on        # every call on this machine runs in shadow mode
    jev shadow off       # back to normal
    JEV_SHADOW=1 jev ... # one call, against the file's setting

The switch is a file beside the kill switch (`~/.config/jev/shadow`). **Absent
means off**, unlike `enabled`: it costs a real call per decision and changes
what callers see. On, every call is still sent and recorded, and the caller
is answered as if Jev were down:

- a call given `--fallback X` behaves as `--shadow X`: it prints `X` and
  records both answers under one pair id;
- a call with no `--fallback` prints nothing and exits 3, which every caller
  already reads as "take the old path";
- a call given `--shadow` is unchanged.

So every caller runs its old mechanism with no edit, and the comparison arms
still get the request. `jev status` prints `shadow=on`, and `jev enabled --why`
says so too.

**A satellite runs shadow by setup.** Its rows reach the hub's ledger, and a
missing file means live, so a satellite once ran `sd-review` on Jev's answer
(sd:2838). `machine-setup.sh update satellite` reports a missing shadow file
as `MISSING` and, with `--apply`, runs `jev shadow on`. It leaves a written
`off` alone, sets no comparison arm, and skips silently where `jev` is not on
`PATH`. The hub has no `.satellite`, so the stage never touches its file.

**The agreement needs the old answer, and a fallback is a marker.**
`tests/test_jev_contract.py` keeps every `--fallback` distinct from a real
answer. A switched call given `--baseline B` records `B` as the pair's old
answer, and `changed` compares the judgment with it. A switched call without
one records no old answer, and its `changed` is `unknown`.
`judgments compare` reports agreement for the first kind and counts the second.

### Local-only mode

    jev noul 'Is this a real credential?' --local-only --stage JEV_SECRET_SCAN \
        --gate 0.5 --shadow yes --state-format text < hit.txt

`--local-only` is for text that may not leave this machine. The contract:

- The request goes to the local Kev alone, at `JEV_COMPARE_KEV_URL`
  (default `http://127.0.0.1:8009/v1/systemone`). It never goes to Jev or to
  a comparison arm, whatever `JEV_COMPARE_KEV`, `JEV_COMPARE_HAIKU_VIA` and
  `JEV_COMPARE_STAGES` say.
- A Kev URL whose host is not a literal loopback address is refused before
  anything is sent; `localhost` is refused too, since a name is resolved. The
  request uses no proxy and follows no redirect.
- No `TYPESAFE_API_KEY` is needed and none is sent; `KEV_API_KEY` is sent when
  set. Privacy redaction is skipped, because the text stays here.
- The row is written under the stage with `arm=kev`, `provider=local`.
- Kev down, slow, or the URL refused is a decline: the `--fallback` or
  `--shadow` answer is printed, or exit 3, exactly as when Jev is down.
- The kill switch (`jev off`, `JEV_ENABLED=0`) and the shadow switch apply.
- `jev enabled STAGE --local-only` answers for this path: no key check, and
  exit 3 for a refused URL.

A stage in `LOCAL_ONLY_STAGES` in `jev.py` is local-only without the flag.
`JEV_SECRET_SCAN` is one.

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

**Do not compare `claude-cli` latency or tokens with the other transports.**
Its latency includes the start-up of a `claude` process, and its input tokens
include Claude Code's own system prompt. `judgments compare` reports each
provider on its own line; use `anthropic`, `openrouter` or `baseten` when
those numbers matter.

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
and it beats an on-value in `<config>/jev/.env`. A fallback answer, `enabled`, a meter that is off, and a call
Jev never gets start no arm.

Switch the Kev arm on with an on-word (`JEV_COMPARE_KEV=1`) and the Haiku arm
by naming a transport (`JEV_COMPARE_HAIKU_VIA=anthropic`). Unset or an
off-word is off: unlike a Jev stage, where unset means on, an arm sends every
live request to a second endpoint, so it is opt-in. An off arm starts no
child and writes no row.

**An arm runs only for a stage `JEV_COMPARE_STAGES` lists**, stage names
separated by commas: `JEV_COMPARE_STAGES=JEV_SD_REVIEW`. Unset or empty lists
none, so a machine with an arm on and no list sends nothing extra. Until
sd:2824 an arm ran for every stage, so set the list when you upgrade, or the
arms stop. A local-only stage stays out of every arm even when listed.

**Put a comparison key in `<config>/jev/.env`.** `jev.sh` reads that file on
every call, so a key there reaches every caller at once. A key exported from
the shell profile (`~/.config/shell/env.sh`) reaches only shells started after
the edit; a caller started before it records the Haiku arm as `unkeyed`.

A Kev row arrives when Kev answers or when `JEV_COMPARE_TIMEOUT` (60 s by
default) runs out, whichever is first. A Kev that hangs leaves its `timeout`
row a minute after the Jev row, not beside it. Install the Kev server (`local-kev/kev.sh install`,
then `agent-install`) when you switch the Kev arm on, not before. The rest of
the settings are in `.env.example`. Read the comparison with:

    local-sd-db/sd-db.sh judgments compare [--stage S] [--since 2026-10] [--json]

### Trace corpus

Every call also writes what it sent and what came back, so the experiment
can be rerun and relabelled from stored data when the success criteria
change. One JSON line per call per arm, appended to
`~/.local/share/sd/jev-corpus/YYYY-MM-DD.jsonl` (the UTC day), or under
`JEV_CORPUS_DIR`. The folder is held to 0700 and each file to 0600, and a
symlink, a FIFO or another user's file is refused. Keep it on the
system disk: a volume mounted `noowners` ignores both modes. The corpus is
never committed, and nothing here sends it anywhere.

**It keeps 30 days.** A day's first line removes the day files more than
`JEV_CORPUS_DAYS` UTC days older than today's (default 30; a value that is
not a positive whole number means 30). The sweep runs on that one append, so
nothing else is scheduled, and it removes only this user's regular files
named `YYYY-MM-DD.jsonl`. Raise the value before a run whose corpus must
outlive the bound, or copy those files elsewhere.

A record carries:

- `schema`, `id` and `time`, and `call`: one id shared by the Jev record,
  the baseline's and each arm's, with or without a pair;
- `caller`, `stage`, `arm`, `provider`, `model`, `primitive`, `pair`,
  `shadow`, and the ledger's fields: answer, confidence, distribution,
  outcome, cause, tokens, duration, `changed`;
- `request`: the payload as sent, after redaction. A local-only call sends
  its payload unredacted, so the corpus redacts its copy. The Haiku arm
  adds `prompts`, the message and schema each question became;
- `response`: the whole parsed response, every distribution included; for
  the Haiku arm, each question's reply text. Redacted, since a model can
  echo what it was asked;
- `settings`: the flags that shape the printed answer (`CORPUS_SETTINGS`
  in `jev.py`), such as `--gate`, `--unsure-below` and `--model`, the one
  that takes text and is redacted; the instructions and criteria are in
  `request`, redacted;
- `printed`: exactly what reached the caller's stdout, without its final
  newline, or null when nothing did. In shadow mode that is the caller's
  own answer; the judgment is `answer`;
- `fallback` and `baseline`: the `--fallback` marker and the caller's own
  answer;
- `ledger`: the `judgment` row's id, or null when the meter wrote none.

**A call that sent nothing stores no request.** Switched off, unkeyed, a
setting that does not parse, or a redaction that refused the request: the
record is written with `request` null, because the corpus keeps what was
sent and nothing was. Building the request anyway would read stdin after
the caller's answer is printed, which blocks a caller whose stdin is open.
`jev enabled --record` and `jev record` store nothing here: they send
nothing, and the ledger already holds all they know.

**Every content field is redacted, or for the secret scanner hashed.**
`request`, `response`, `prompts`, `answer`, `printed`, `fallback`,
`baseline`, `model` and `settings.model` (`CORPUS_CONTENT` in `jev.py`;
`--model` takes any text) go through the redaction a
hosted request gets, on every record, so a local-only call stays
replayable. A field the pass refuses, or every one when the pattern file
does not load, is stored as null. A stage in `CORPUS_HASHED_STAGES`
(`JEV_SECRET_SCAN`) stores each of those fields as `{"sha256": …}`
instead, and the request adds `state_sha256`. A candidate credential
copied into a file is what the scanner exists to find. Equal states hash
equal, so a hit can be found again and labelled.

A record that waits more than a second for another writer's lock is
dropped, so a stuck writer never holds up a call.

`JEV_CORPUS=0` (or another off-word) stores nothing, and redacts nothing
first; unset means on. The
arms write their records from their own child, so they need the meter on
as before. A folder that cannot be written loses the record and changes
nothing a caller sees.

Read it with `jq`; for example, every call of one stage with its arms:

    jq -c 'select(.stage == "JEV_NOTIFY") | {call, arm, answer, outcome}' \
        ~/.local/share/sd/jev-corpus/*.jsonl

## What it never does

It never prints the key, never logs it, and never puts it in an error message.

**It keeps what it sends.** Every call's request and response go to the
[trace corpus](#trace-corpus), a private file on this machine. That copy is
local, but it is a copy: switch it off with `JEV_CORPUS=0` where it is not
wanted.

**Nothing sensitive should be piped into it.** Every call leaves the machine,
except a `--local-only` one. That is why `local-scan-for-secrets` calls only
through `--local-only`: triaging its hits is a textbook Noul, and sending
candidate credentials to a hosted model is the one thing that scanner exists
to prevent. Its hits go to the local Kev, or nowhere.

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

# local-mail-intake

Notice what arrived in shared mail aliases, and say who owes a reply.

It was built for two distribution aliases of a volunteer board. Every board
member gets every message, so the mailbox is
a firehose and the signal in it is "something changed since yesterday", not
"here is your inbox". This module reports that delta.

## Usage

```sh
./mail-intake.sh peek     # say what a fetch would find, change nothing
./mail-intake.sh fetch    # search both aliases, append arrivals, advance state
./mail-intake.sh report   # print undelivered arrivals
./mail-intake.sh stamp    # mark arrivals delivered, --through TIMESTAMP
./mail-intake.sh status   # config, server reachability, undelivered count
./mail-intake.sh test     # the unittest suite
```

Verbs, exit codes, state layout and the append-only log are deliberately
identical to `local-drive-intake`. Read that README for the reasoning; only the
differences are written down here.

**0** something changed, **3** nothing to do, **1** a real error. A quiet day
must be 3 and not 1.

## It never sends, replies, files or labels

The rule it was built under is that reading Gmail is allowed and any external
change needs the owner first. This module calls exactly two Gmail tools:
`search_gmail_messages` and `get_gmail_messages_content_batch`.

That is asserted by a test rather than by inspection, because a helpful edit
adding one draft call would otherwise pass every other test. `TestNeverSends`
greps the module source and fails if `send_gmail_message`,
`draft_gmail_message`, `modify_gmail_message_labels` or `manage_gmail_filter`
appears in it at all.

## Headers, not bodies

The batch call is made with `"format": "metadata"`, so the server returns
From, To, Cc, Subject and Date and never the message body. The same test that
guards against sending also fails if `"format": "full"` appears anywhere.

Two reasons, and the second is the one that matters. Subject lines and
addresses are enough to answer "who wrote last"; pulling bodies would put
private correspondence through a module that writes CSV to disk, and keeping
that out is the point.

## It is a command job, not an agent job

`workspace-mcp` speaks JSON-RPC over HTTP at `127.0.0.1:8083`, so a shell
script can reach Gmail with `curl` and no model in the loop. The handshake is
`initialize`, keep the `mcp-session-id` header, `notifications/initialized`,
then `tools/call`. `Accept: application/json, text/event-stream` is required
or the server answers 406.

This is not a detail. On 2026-09-19 the Claude CLI OAuth session expired and
**13 cron jobs failed in one morning** — every one of them a `JOB_PROMPT` job.
The 21 `JOB_COMMAND` jobs were untouched. The morning digest reads mail through
this path, so it keeps working through an outage that stops the agent jobs.

## How it decides what is new

Gmail is searched once per run with a single query, `newer_than:{window}d`
across `to:`, `from:` and `cc:` for both aliases, paged until exhausted.
Messages are grouped by thread and only the newest message in each thread is
kept. That newest message id is compared against
`~/.local/share/mail-intake/threads.csv`.

A thread whose newest message has not changed is not reported, however much
older traffic it holds. **The first run is a baseline**: it records the threads
and reports nothing.

Measured against the real mailboxes on 2026-09-19: **104 messages in 40
threads over a 21-day window, 9.5 s**.

## A batch fetch gets rate-limited, and a dropped message is a false alarm

The server turns one `get_gmail_messages_content_batch` into that many
concurrent Gmail calls, and Gmail answers the overflow with
`HttpError 429 ... Too many concurrent requests for user.` It puts the failure
in the reply where the headers would have gone, as
`⚠️ Message <id>: <HttpError 429 ...>`, and still says "Retrieved 25 messages".

At a batch of 25 that cost **eight to fifteen of 104 messages per run, a
different set each time**. The symptom was a thread count that flapped between
39 and 40 across identical runs.

The consequence is worse than a missing line. A thread whose newest message was
throttled away looks absent, and absent used to mean deleted from the state
file, so the next run reported that thread as newly arrived. A digest that
invents arrivals is worse than one that misses them.

Three changes, because one was not enough:

- **The batch is 10, not 25.** Measured: three consecutive runs at 10 returned
  104 messages and 0 throttled ids, every time.
- **Throttled ids are collected and retried**, in smaller batches, backing off
  2 s and doubling, up to four attempts.
- **State is merged, never replaced.** A thread this run did not see keeps its
  row. Absence is not evidence that a thread is gone, and the only cost of
  keeping a stale row is one line of CSV.

If ids are still throttled after the retries, the run says so and carries on.
A transient rate limit must not exit non-zero: the same alarm-fatigue rule that
governs quiet days governs this.

## `waiting_on` is a coarse signal, and the number says so

A thread is marked `you` when its newest message is not from a `me` address, and `them`
when it is. That is the whole rule.

Measured on the live backlog: **37 of 40 threads came back `you`**. Most of
those are not obligations. A vendor's mowing confirmation and a broadcast
notice both land as "waiting on you" because nobody on the `me` list answered them, and
neither wants an answer.

The label is honest inside a daily delta, which is the only place the digest
uses it: a message arrived today, addressed to a watched alias, and you did not
write it. As a standing worklist it would be wrong, so nothing presents it as
one. Fixing it properly needs the body text and an intent judgement, which is
an agent's job and not this module's.

## `asks` is a second signal, and it never touches `waiting_on`

`waiting_on` keeps meaning exactly what it meant: who spoke last. The failure
above is not that the rule is wrong, it is that "you did not answer" and
"somebody wants something from you" are two different questions being asked
of one field. So the second question got its own field rather than a smarter
version of the first.

`asks` is a Jev `noul` over one thread: *does the newest message in this email
thread ask its recipients for a decision or an action?* Gated at 0.7, it is
`yes` or `no`; `unknown` is the fallback word, and means Jev did not answer. `report` uses it for one thing — ordering. Threads
that ask for something print first **inside the group they were already in**,
marked `(asks)`. Nothing is dropped, nothing is hidden, and no thread moves
between `WAITING ON YOU` and `waiting on them`. A wrong answer therefore costs
a line of reading order and cannot cost a missed message, which is the only
reason a probabilistic signal is allowed near this report at all.

### It is on unless something switches it off

    JEV_MAIL_INTAKE=0 ./mail-intake.sh report   # today's report, for one run

`local-jev/jev.sh enabled JEV_MAIL_INTAKE` decides, and it exits 0 only when a
key is present, the fleet switch is on, and this stage has not been switched
off. With any of those missing nothing is spawned and the report is
byte-for-byte what it was before this existed — asserted by capturing that
output and comparing every other case against it, not by inspection.

**Unset means on.** `0`, `off`, `false`, `no` and `disabled` switch this stage
off, in any case; every other value leaves it on, the `1` this used to require
included. It only ever subtracts: `jev enabled` reads the fleet switch and the
key first, so this variable can take the stage out and can never put it back in
on a machine that cannot answer.

`jev.sh` is reached **by path**, resolved from this module's own directory as
`../local-jev/jev.sh`. `local-bin-links` puts `jev` on an interactive shell's
`PATH` and neither cron nor CI has it, so a caller written against the bare
name is a caller that quietly stops running in the place this actually runs.

Every failure — Jev off, no key, a timeout, a non-answer, the folder missing —
falls back to today's ordering, prints the reason on **stderr**, and leaves
the exit code alone. Stderr rather than stdout so a degraded run's report is
identical to a clean one's, and loud rather than silent because a lane that
quietly stops running is the defect this repo has already been bitten by.
It is all or nothing: one thread without an answer drops the answers already
given, so a report is never half in Jev's order and half in today's (sd:2551).
`report --all` stops asking after 25 questions and leaves the rest in today's
order.

Threads are asked in batches of up to eight, one `jev ask` per batch (sd:1160);
a batch of one is the `noul` above, gated the same way. The state is each
thread's own payload under the keys `q1`, `q2`, ..., so a batch sends what one
question per thread sent. A batch answer counts only when every thread in it
has a probability and nothing else came back; anything less is a failure, and
the all-or-nothing rule above puts the whole report back in today's order.

### What leaves the machine

Every Jev call goes to a third party, and this mailbox carries real names and
addresses that the repo's rules keep out of git. Exactly two things are sent
per thread: **the subject line**, whitespace-collapsed, and **the single word
`inbound` or `outbound`**. No address, no display name, no body, no snippet,
no thread id and no message id — bodies are never fetched in the first place.

`state_for_jev` builds that whole string and nothing else contributes to it,
which is what makes the sentence above checkable rather than a promise:
`TestNothingPrivateLeavesTheMachine` runs a report through a stubbed `jev` and
fails if an address, a name or an id appears in any argument or any payload.

**Ledger subject and run (sd:2953).** Each call passes `--subject mail-intake:<16 hex>`: the first 16 hex of the sha256 of the asked threads' `message_id` values, sorted, one per line. That is one id for a `noul` and the batch for an `ask`. An outcome recomputes it from the report rows with `printf '%s\n' ID... | LC_ALL=C sort | shasum -a 256 | cut -c1-16`. No subject line or address leaves in it. Every call of one run shares `JEV_RUN=mail-intake-<UTC yyyymmddThhmmss>-<4 hex>`, or the run's inherited one.

## Config

The conf is private: it names real addresses. It lives outside the checkout, at
`<config>/mail-intake/mail-intake.conf`. `<config>` is `$SYSTEM_TOOLS_CONFIG`,
default `~/.config/system`. Start from `mail-intake.conf.example`.
`MAIL_INTAKE_CONFIG` names another file.

Pipe-separated, same reason as `drive-intake.conf`.

```
account|<gmail address the MCP server is authorised for>
me     |<an address that counts as you writing>    (repeatable)
alias  |<label>|<alias address>                    (repeatable)
window |<days back to search>
```

`me` is repeatable because a reply can leave from more than one address, and
every one of them has to read as "you answered" or the thread bounces back
into the digest the day after you clear it.

## Environment

| Variable | Default |
|---|---|
| `MAIL_INTAKE_CONFIG` | `<config>/mail-intake/mail-intake.conf` |
| `MAIL_INTAKE_STATE` | `~/.local/share/mail-intake` |
| `WORKSPACE_MCP_URL` | `http://127.0.0.1:8083/mcp` |
| `JEV_MAIL_INTAKE` | unset means on — `0`/`off`/`false`/`no`/`disabled` stops `report` asking Jev for the `asks` ordering |

## Where it fits

Third section of the morning digest, after what is due and what arrived in
Drive. The digest script, in a separate private repository, runs a `fetch`, reads the
undelivered rows, and stamps them only after the mail is actually sent — the
same two-phase delivery the Drive half uses, with its own cutoff file so one
half cannot stamp the other's rows.

Its plan lives in that private repository. The plan assumed Gmail had no shell
path and scoped this as an agent job; the `curl` handshake above disproved
that. Nothing it finds leaves the digest without the owner.

### A quiet day is 3, including when the log does not exist yet

`report` and `stamp` used to exit **1** whenever `mail-arrivals.csv` was absent, printing
"run fetch first". That is right only before the first fetch. After a baseline,
an absent log means nothing has changed since — a quiet day, which this
module's own contract says must be 3.

The state file is what separates the two. With `threads.csv` present, a fetch has
run, so an absent arrivals log reports "nothing undelivered" and exits 3. With
neither file, the advice to run a fetch is still correct and still exits 1.

Found 2026-09-19: every `report` on this machine had been failing since the
baseline run, telling the caller to run the command it had just run.

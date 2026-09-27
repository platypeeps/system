# local-notify

General-purpose notification wrapper: local macOS banner plus ntfy push to the
phone, optional iMessage. Replaces the old `~/bin/common/sendalert.sh` /
`sendalert.osa` pair (iMessage-only, hardcoded phone number).

## Usage

```sh
./notify.sh [-t title] [-k kind] [-F] [-p priority] [-c channels] [-f format] [-b] message...

./notify.sh "build done"                        # banner + phone push
./notify.sh -t Backup -p urgent "disk full"     # urgent push
./notify.sh -c local "quiet, mac only"
./notify.sh -c ntfy,imessage "phone + iMessage"
./notify.sh -b -c ntfy,email "push must land, email is a bonus"
./notify.sh -t "health check: 2 findings" -k status -F -c email "..."
```

## Subject labels (`-k`, `-F`)

`-k` prefixes the **email subject only** — `brief` gives `Brief: <title>`,
`status` gives `Status: <title>`. Briefs are curated content (digests, review
queues, the weekly summary); status is machine state (health, maintenance,
backups, repo-sync, upgrades, the cron watchdog). The point is a mailbox that
sorts, so the ntfy push and the macOS banner keep the bare title: a phone
notification is not a mailbox. Omitting `-k` sends the title unprefixed.

`-F` marks a status mail **Important** in Gmail and adds the `!Followup`
label — for a report of something actually worth opening, not a routine
receipt. `-F` with `-k brief` is rejected rather than ignored: a caller
passing both has its categories confused, and dropping the flag silently
would bury a real alert.

### The inbox half lives in a Gmail filter

`-F` also re-adds `INBOX`, which only makes sense alongside a Gmail filter in
the receiving account:

    from:<EMAIL_FROM address> subject:"Status:"
      -> +Briefs/Status Updates, -INBOX, -SPAM

The filter archives every `Status:` mail on arrival, so routine receipts
(brew-doctor warnings, maintenance findings, ai-apps changes) never reach the
inbox; `-F` puts the ones reporting a real problem back. The split is
deliberately *not* expressed in the filter, because a filter cannot test for
`!Followup` — that label is applied by `notify.sh` after the message has
already been delivered and filtered. Racing it would be unreliable; re-adding
after the fact cannot lose.

Consequence worth remembering: the filter matches on **sender**. Re-authorizing
google_workspace_mcp against a different Google account changes the From
address and silently orphans it: the filter keeps pointing at the old sender
long after the mail stopped coming from there. Briefs are routed by six equivalent subject filters
with the same sensitivity.

The label is resolved **by name at send time** (`FOLLOWUP_LABEL`, default
`!Followup`), not stored as an id. A hardcoded id worked until the mail
turned out to be landing in a different account than the label lived in, and
the call failed `labelId not found` on a label that plainly existed. Labelling
is decoration on a mail that already landed, so every failure past the send —
no message id in the reply, label absent from this mailbox, the modify call
refused — warns on stderr and still exits 0. `IMPORTANT` is a system label
and needs no lookup, so a missing `!Followup` still leaves the mail flagged.

A channel this machine has no credentials for is **skipped**, not failed —
`notify.sh: not configured here, skipped: email`. Only a channel that was
configured and could not deliver counts as a failure. If nothing is configured
at all, notify.sh exits 1 with `no notification channel is configured on this
machine`, `-b` or not: the point of `-b` is that another channel carried the
message, and there nothing did.

`-b` (best effort) exits 0 as long as one requested channel delivered. Without
it a single dead channel fails the whole call: every nightly cron job asked for
`-c email` alone, so when google_workspace_mcp was down they exited 1 and
reported failure even though the ntfy push had landed. The failed channel is
still named on stderr either way, and `-b` still exits 1 when nothing got
through.

Priorities (ntfy): `min|low|default|high|urgent`. Also linked as `notify` in
`~/bin/common` via `local-bin-links`.

## Channels

- `local` — macOS notification banner via `osascript`.
- `ntfy` — POST to `$NTFY_SERVER/$NTFY_TOPIC` (default server `https://ntfy.sh`).
  Subscribe to the topic in the ntfy iPhone app to receive pushes.
  `NTFY_TOKEN` only for protected topics / self-hosted servers.
- `imessage` — Messages.app via AppleScript; needs `IMESSAGE_TO`.
- `email` — **only ever mails `NOTIFY_EMAIL_TO`.** The recipient is pinned
  per machine in `.env`, not an overridable default: every job here reports
  on one person's machine, and a stray `EMAIL_TO` (a copied `.env`, an exported
  var left over from a shell experiment) would quietly redirect machine
  inventories, repo lists and health findings wherever it pointed. An
  `EMAIL_TO` that disagrees fails the channel with rc=1 rather than rc=2, so it
  reads as a misconfiguration to fix and not a channel to skip. An unset
  `NOTIFY_EMAIL_TO` skips the channel (rc=2) and names the variable. `EMAIL_FROM`
  stays free — which Google account does the sending is a separate question.
  Gmail through the local `google_workspace_mcp` HTTP server
  (launchd, port 8083); needs `NOTIFY_EMAIL_TO` and `EMAIL_FROM` (the Google
  account authorized in that server). Title becomes the subject; `-f html`
  sends the body as HTML (obsidian-tasks uses this for its action
  buttons — the other channels always get the body as-is). Used by
  the nightly report jobs (brew-doctor, repo-sync, obsidian-tasks).

Configuration comes from exported variables or `<config>/notify/.env`,
outside the checkout (`<config>` is `$SYSTEM_TOOLS_CONFIG`, default
`~/.config/system`; start from `.env.example`).

## Jev routing (`JEV_NOTIFY`)

On by default, and additive. One call decides it — the sibling
`../local-jev/jev.sh enabled JEV_NOTIFY` — and it answers both halves at once:
whether Jev can answer on this machine, and whether `JEV_NOTIFY` has been used
to switch this stage off. It costs nothing and calls nothing; a machine with no
`TYPESAFE_API_KEY` answers it the same way as one with `jev off`.

**Unset means on.** Set `JEV_NOTIFY` to `0`, `off`, `false`, `no` or `disabled`
(any case) to switch this stage off; any other value leaves it on, so a typo
cannot silently stop the lane. A per-caller switch that defaults to off makes
every integration added after it silently never run, which is the failure
`jev off` — the machine-wide kill switch — exists for.

When it is on, and **only when the caller passed neither `-c` nor `-p`**, Jev
answers two questions from the title and the message:

- a `choice` between `desk` (channels `local`) and `phone` (channels
  `local,ntfy`, today's default);
- a `score` on `min,low,default,high,urgent`, rounded and clamped back onto
  that same list, which becomes the ntfy priority.

An explicit `-c` **or** `-p` skips the call entirely — either flag means the
caller has already decided the route, and half-deciding it is worse than the
answer it asked for.

Two things Jev may never do here. It may never add `imessage`: reaching
someone's phone messages is a decision a human makes, and an ordinary push is
the ceiling — `imessage` is not among the criteria it is offered, and an
answer naming it is discarded rather than pasted into `CHANNELS`. And it may
never suppress a notification: both routes deliver, and every answer outside
the two names above (`unsure` included) leaves the default in place.

Every failure degrades to today's default channels and priority and says so on
stderr: the switch off, no key, `jev.sh` missing, a non-zero exit, an empty
answer, or no answer inside `JEV_TIMEOUT` seconds (default 5, enforced here by
a wall-clock cap around the call, because jev's own timeout is per attempt and
retried). None of them is fatal, and the notification still goes out.

`jev.sh` is resolved as `$DIR/../local-jev/jev.sh`, from the directory
notify.sh actually lives in, not from `PATH` — notify.sh is linked into
`~/bin/common` by `local-bin-links`, and a `jev` on `PATH` is the same link
farm one `install` out of date.

### What leaves the machine

Every Jev call is a network call to a third party. Exactly two values are
sent, as the question's state:

    Title: <the -t title, or "Alert">
    Message: <the message argument>

and the fixed question text (the instructions, the two criteria names and
their descriptions, the five priority level names). **Nothing else.** The ntfy
topic, `NTFY_TOKEN`, `NTFY_SERVER`, the iMessage recipient, `EMAIL_TO`,
`EMAIL_FROM`, `WORKSPACE_MCP_URL` — nothing read from `.env` or from the
environment is in the state, and a test asserts it.

That still leaves the alert text itself, and this tool carries the alert text
of every other tool in the repo — including tools nobody has written yet, so
what turns up in a message is not enumerable in advance. This is the one place
where the default-on flip changed what leaves the machine rather than only
when it leaves: before the flip no message body took this path unless someone
asked for it, and after it every body does unless someone says not to.

Three things bound that.

**The state is the title and the message and nothing else**, asserted by a
test.

**The payload is redacted before it leaves.** `jev_write_redactions` builds a
`sed` program and the state passes through it on the way to `jev`, so a
credential shape or a machine identifier in the message becomes `<redacted>`
or `<path>`. Two classes: credential shapes copied from
`local-scan-for-secrets`' `PATTERNS` — copied, not sourced, because that tool
must never become a Jev caller — and machine identifiers (`$HOME`, `/Users`,
`/private`, IPv4, IPv6, `*.ts.net`, `*.local`, the hostname), which is
`local-health-check`'s set. A path is redacted to the end of its line, because
a folder name can hold a space. **Only the payload.** The notification the person
receives carries the raw text, and a test asserts that too — a redaction that
reached the delivery would pass every other assertion here. It **fails
closed**: if the program cannot be built, `notify.sh` says so on stderr and
asks nothing, because a routing nobody got is a notification on the defaults
and a payload sent raw is a secret on somebody else's server.

Redaction is pattern matching and catches shapes, not meaning. A password in
prose, a customer name, the contents of a private file — none of those have a
shape. So it is the second line, not the first.

**The first line is the caller.** A tool that handles secret material and
wants a notification should pass `-c`/`-p`, which skips the call entirely, or
export `JEV_NOTIFY=0` around it. And `local-scan-for-secrets` is not a caller
and must not become one — triaging its hits would post candidate credentials
to a third party, which is why it stays out of `KNOWN_CALLERS` in
`tests/test_jev_contract.py`.

## Tests

    python3 tests/test_notify.py -v
    python3 tests/test_email_retry.py -v

`test_email_retry.py` runs real curl against a stub workspace-mcp on a local
port. It checks that a failed session setup is retried in a fresh session and
that a failed send is never repeated.

Offline and sends nothing: each case builds a throwaway `local-notify` +
`local-jev` pair in a temp directory and puts stub `osascript` and `curl` on
`PATH`, so a delivery is a line in a log file. The jev stub records its argv
and the state it was handed on stdin, which is what makes "the topic never
left the machine" an assertion and not a claim.

## Gotchas

- The public `ntfy.sh` server means anyone who knows the topic name can read
  and post to it — treat the topic like a password (hence `.env`).
- First `local` send may prompt for notification permission (Script Editor /
  osascript) in System Settings.
- `imessage` needs Messages.app signed in and automation permission.
- `email` works only on a machine that runs the google_workspace_mcp HTTP
  server on :8083; a machine without it has nothing listening on the port. Email is absent there by design,
  not misconfigured, which is why cron jobs request `-c ntfy,email -b`: ntfy
  carries the alert on every profile and email is a bonus where it exists.
  Set the addresses in `.env` only on the machines that should send mail.
- `email` needs the google_workspace_mcp launch agent running; Mail.app is
  deliberately not used (unconfigured here, and headless osascript
  automation hangs on TCC).

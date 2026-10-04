# local-obsidian-review

Daily digest of the vault's decision queues, emailed as HTML with signed
one-tap decision buttons. Where `local-obsidian-tasks` covers due TaskNotes
tasks, this covers the five `System/Databases/*` folders whose notes wait on
a status call from the operator; the nightly accept routines (blog-idea-accept,
tips-accept) then act on whatever the buttons set. Skill Proposals keeps its
Accept/Decline buttons and no routine behind them: since 2026-08-31 the
decision is the whole record, and adoption happens through `sd-skill-adopt`.

## Queues and buttons

| Database | digest picks up | buttons (status written) |
|---|---|---|
| Blog Ideas | `status: inbox` | Accept (`accepted`) / Decline (`declined`) |
| Skill Proposals | `status: proposed` | Accept / Decline |
| Tips and Tricks | `status: inbox` | Accept / Decline |
| Topics | `status: candidate` | Adopt (`active`) / Park (`parked`) |
| Market Watch | `status: candidate` | Track (`active`) / Pause (`paused`) / Decline |

Every card also gets an Open button (https `/open` redirect to the
`obsidian://` deep link — Gmail strips custom schemes). Tips already in
`ready` are only counted in the footer; that final call happens in Obsidian.

## Usage

```
./obsidian-review.sh list   # print pending decisions per database, no email
./obsidian-review.sh run    # list + email the digest (obsidian-review-daily cron job)
```

No pending decisions = no email; `run` exits 1 only when the email itself
failed, so the cron failure push covers a lost digest, not findings.

## How it works

- Buttons are HMAC-signed URLs from `local-task-actions` (`url -b <db>`),
  served through its Tailscale Funnel, expiring after 7 days; GET shows a
  confirmation page, POST flips the one `status:` field. See that folder's
  README for the security model.
- Scanning and the email body are built by an embedded python3 heredoc
  (python has the TCC grant for `~/Documents`; plain shell does not), sent
  through `local-notify`'s email channel.
- Cron: `<config>/cron-jobs/jobs/obsidian-review-daily.job` (examples in `local-cron-jobs/examples/`), 07:30 daily —
  after market-watch (03:00) and vault-cleanup (06:52) refresh the queues.
- Environment: `OBSIDIAN_VAULT` (default `~/Documents/Obsidian Vault`; exported, or set in `<config>/obsidian-review/.env` outside the checkout (`<config>` is `$SYSTEM_TOOLS_CONFIG`, default `~/.config/system`), see `.env.example`),
  `JEV_OBSIDIAN_REVIEW` (on unless switched off, below),
  `OBSIDIAN_REVIEW_TODAY` (the `YYYY-MM-DD` note ages count from;
  default today, and the suite pins it),
  `OBSIDIAN_REVIEW_SIGN_TIMEOUT` (seconds one signed link may take, default
  15; a link that runs out is tried once more, then the digest keeps
  Open-only links and writes why to stderr, with the load averages).

## Ordering, and the optional Jev ranking

The digest is grouped by queue in a fixed order, and scored highest-first
inside each one. That says nothing about which decision is worth making
today. The digest asks Jev to score the cards, prints each queue's cards in
that order, and puts the queue holding the best card first — so the top of the
mail is the decision to make now.

It is **on by default and additive**, and one call decides it:
`local-jev/jev.sh enabled JEV_OBSIDIAN_REVIEW` exits 0 when a key is present,
the fleet switch is on, and this stage has not been switched off. With any of
those missing the digest is the byte-for-byte digest it was before this
existed.

**Unset means on.** `0`, `off`, `false`, `no` and `disabled` switch this stage
off, in any case; every other value leaves it on, the `1` this used to require
included. It only ever subtracts: `jev enabled` reads the fleet switch and the
key first, so this variable can take the stage out and can never put it back in
on a machine that cannot answer.
 Jev is experimental, so
nothing here depends on it: a switched-off, unkeyed, failing or half-
answering Jev falls back to today's order, writes the reason to stderr, and
leaves the exit code alone. A run that was ranked says so, in one line of
the text body and one sentence in the mail's footer.

**The twelve-card cap runs first, and Jev never sees past it.** Each queue
picks its twelve by `score:` exactly as before, and only those cards are
ranked, so this can move a card up the mail and can never put one in it or
take one out. It never touches the buttons, their URLs or their signatures,
and never writes to the vault. `jev.sh` is called by path from this folder
(`../local-jev/jev.sh`), not through `PATH`, because cron and launchd have
neither this repo nor `~/bin/common` on theirs.

Every shown card goes out as a question in **one** `jev ask` request, which
is how they run in parallel; one request per note would pay a round trip per
card in the mail.

### What leaves the machine

Every Jev call leaves this machine, so the request carries the smallest span
that can answer "which of these decisions is worth making today":

- each note's **title** (its file stem, which is what the card shows)
- the **queue** it sits in, by its display name (`Blog Ideas`, `Topics`, …)
- its **`dateCreated`** date, and today's date

Nothing else: no `description`, no `score`, no note body, no file path, no
vault name, no `obsidian://` link, no action URL. `tests/` asserts that on
the exact payload the tool hands to `jev.sh`.

## Tests

```sh
./obsidian-review.sh test
```

Runs offline against a fake repo root: a copy of the entrypoint with stubbed
`local-jev`, `local-notify` and `local-task-actions` siblings and a fixture
vault whose Blog Ideas queue is deliberately over the twelve-card cap.
Nothing reaches the network or `~/Documents`. The golden files in
`tests/fixtures/` are the digest this tool produced before Jev existed, with
the dates templated so they do not go stale; three of the tests assert the
mail is still byte-for-byte that.
The harness passes its own date to the script in
`OBSIDIAN_REVIEW_TODAY`. Without that, a run that crossed midnight
counted every age one day older than the golden and failed.

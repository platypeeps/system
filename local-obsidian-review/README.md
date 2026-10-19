# local-obsidian-review

Daily digest of the decision queues, filed as the day's `Review Queue`
brief on the dashboard's Today. It reads the base rows of five `sdw` kinds from
the workflow database (`sd_db.records`), their source since pack sd:4627; it
reads no vault note (sd:5134). Each card's Open link is the row on the
dashboard's Tasks page (`/tasks?row=<id>`), where its stage moves; the nightly
accept routines (blog-idea-accept, tips-accept) then act on that stage.

## Queues

| Queue | kind | digest picks up |
|---|---|---|
| Blog Ideas | `sdw.blog-idea` | stage `inbox` |
| Skill Proposals | `sdw.skill-proposal` | stage `proposed` |
| Tips and Tricks | `sdw.tip` | stage `inbox` |
| Topics | `sdw.topic` | stage `candidate` |
| Market Watch | `sdw.market` | stage `candidate` |

Tips already in `ready` are only counted in the footer.

## Usage

```
./obsidian-review.sh list   # print pending decisions per queue, files nothing
./obsidian-review.sh run    # list + file the digest as a brief (obsidian-review-daily cron job)
```

No pending decisions = no brief; `run` exits 1 when the base rows cannot be
read or `sd reports brief add` failed, so the run's report on Today covers a
lost digest, not findings. A failed read is never an empty queue.

## How it works

- The rows come from `sd_db.records.list_rows` over a read-only connection,
  imported from the command pack's `lib/` (`SD_PACK_ROOT`, default
  `~/repos/platypeeps/sd-ai-command-pack`).
- The Markdown brief is built by an embedded python3 heredoc and filed
  with `sd reports brief add --replace`, so a rerun the same day replaces it.
- Cron: `<config>/cron-jobs/jobs/obsidian-review-daily.job` (examples in `local-cron-jobs/examples/`), 07:30 daily —
  after market-watch (03:00) and vault-cleanup (06:52) refresh the queues.
- Environment: `SD_PACK_ROOT`,
  `JEV_OBSIDIAN_REVIEW` (on unless switched off, below),
  `OBSIDIAN_REVIEW_TODAY` (the `YYYY-MM-DD` row ages count from;
  default today, and the suite pins it).

## Ordering, and the optional Jev ranking

The digest is grouped by queue in a fixed order, and scored highest-first
inside each one. That says nothing about which decision is worth making
today. The digest asks Jev to score the cards, prints each queue's cards in
that order, and puts the queue holding the best card first — so the top of the
brief is the decision to make now.

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
the text body and one sentence in the brief's footer.

**The twelve-card cap runs first, and Jev never sees past it.** Each queue
picks its twelve by `score:` exactly as before, and only those cards are
ranked, so this can move a card up the brief and can never put one in it or
take one out. It never touches the links, and never writes a row. `jev.sh` is called by path from this folder
(`../local-jev/jev.sh`), not through `PATH`, because cron and launchd have
neither this repo nor `~/bin/common` on theirs.

Every shown card goes out as a question in **one** `jev ask` request, which
is how they run in parallel; one request per note would pay a round trip per
card in the brief.

### What leaves the machine

Every Jev call leaves this machine, so the request carries the smallest span
that can answer "which of these decisions is worth making today":

- each row's **title** (what the card shows)
- the **queue** it sits in, by its display name (`Blog Ideas`, `Topics`, …)
- its **`dateCreated`** date, and today's date

Nothing else: no `description`, no `score`, no body, no path, no item id,
no link. `tests/` asserts that on
the exact payload the tool hands to `jev.sh`.

**Ledger subject and run (sd:2953).** The `ask` and its baseline row pass `--subject obsidian-review:<16 hex>`: the first 16 hex of the sha256 of the shown rows' titles, sorted, one per line. An outcome recomputes it with `printf '%s\n' STEM... | LC_ALL=C sort | shasum -a 256 | cut -c1-16`. The script exports `JEV_RUN=obsidian-review-<UTC yyyymmddThhmmss>-<4 hex>` once per run, unless it inherited one.

## Tests

```sh
./obsidian-review.sh test
```

Runs offline against a fake repo root: a copy of the entrypoint with a stubbed
`local-jev` sibling, a stub `sd` on PATH, and a fixture workflow database under a
temporary HOME, built with the pack's `sd_db`, whose Blog Ideas queue is
deliberately over the twelve-card cap. Nothing reaches the network or a real
store. The golden files in `tests/fixtures/` are the digest with Jev off, with
the dates templated so they do not go stale; three of the tests assert the
brief is still byte-for-byte that.
The harness passes its own date to the script in
`OBSIDIAN_REVIEW_TODAY`. Without that, a run that crossed midnight
counted every age one day older than the golden and failed.

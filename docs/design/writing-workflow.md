---
title: The writing workflow
eyebrow: From an idea to a published piece
stand: A piece moves through six stages. Readiness is not an opinion — it is a recorded decision pinned to one exact draft, and editing that draft revokes it.
---

## The stages

Eight stage names exist; six of them are a path, and two are ends.

| Stage | What it means |
|---|---|
| `inbox` | An idea exists. Nothing has been committed to it. |
| `accepted` | Worth writing. **Your** decision — a session cannot make it. |
| `researching` | Sources are being gathered into `research.md`. |
| `drafting` | The piece itself is being written. |
| `review` | The three gates are being run and recorded. |
| `ready` | Gates pass. A readiness digest is pinned. |
| `published` | A real URL is recorded. Terminal. |
| `declined` | Terminal — but see below; existing pieces are **parked** instead. |

@diagram writing-stages

Each stage has exactly **one** normal successor. Moving anywhere else requires
an explicit correction, and a correction has three constraints:

1. It may only target `researching`, `drafting` or `review`.
2. It may only move **backwards**.
3. Only the human operator may make one. An `actor` of `session` is refused.

A correction also needs a stated reason. That is not decoration — the reason is
written into the item's notes, so the record shows why a piece went back.

Two transitions are not corrections and are refused outright:

- **You cannot return a piece to `inbox`**, and you cannot `decline` one that
  already exists. An existing piece is **parked** instead. Parking is
  reversible and keeps the row; declining would pretend the work never
  happened.
- **`published` needs `ready` plus explicit confirmation**, and it needs at
  least one actual recorded publication URL. A piece with no URL cannot be
  published, whatever the stage says.

## The three gates

Reaching `ready` means three artifacts exist beside the draft and agree with
it: `research.md`, `fact-check.md` and `adversarial.md`.

**`research.md` is checked by stamp, not by presence.** It must carry exactly
one reconciliation stamp naming the current draft's digest, and that stamp must
also match the current gate *generation*. One stamp, not zero and not two — a
second stamp means two reconciliations disagree about which draft this is.

**`fact-check` and `adversarial` are checked by record.** For each, the piece
must carry a gate record whose:

- `draft_digest` and `generation` match the draft as it is now;
- `evidence_sha256` matches that gate's own file, **and** `research_sha256`
  matches `research.md`;
- `verdict` is `pass`.

That second bullet is the subtle one. It means editing `research.md` after
recording the fact-check invalidates the fact-check, even though the
fact-check's own file did not change. The gate was recorded against a body of
evidence; changing the evidence unrecords it.

**Open findings block differently by gate.** An open `fact-check` finding
blocks at any confidence. An open `adversarial` finding blocks only at
`CERTAIN`. A fact-check finding says something in the piece may be untrue; an
adversarial finding may be a judgement call.

## Readiness is pinned, and it expires

When a piece reaches `ready`, the draft's digest is stored as `ready_digest`.
Publication then checks two separate things:

1. The gates still pass **now**.
2. `ready_digest` still equals the draft's current digest.

Edit one character of the draft after granting readiness and the second check
fails with *"draft differs from the one granted readiness"*. This is the whole
point of the design. A readiness decision that survived arbitrary later edits
would be a decision about a document nobody reviewed.

The dashboard's Writing screen shows this directly: the **Readiness decision**
column reads *Recorded* or *Not recorded*, and the screen says in plain text
that a recorded decision is the saved review, to be checked against the current
draft rather than trusted on sight.

## Where the pieces live

The database is not necessarily the owner. Each repository carries a
`pieces_source`, and a repository whose source is `retiring` refuses stage
changes entirely — as does any parked piece. The dashboard's own stage controls
additionally require database ownership, so a repository mid-migration cannot
be driven from the web UI while its files are still authoritative.

Ideas imported from files map their frontmatter `status` onto a stage, with
`idea` mapping to `accepted`. The stages then map onto the generic item
statuses the rest of the system reads — `inbox` is `planning`, `ready` is
`ready_to_send`, `published` and `declined` are both `done` — which is how a
writing piece appears in the same queues as everything else.

## Status

**Verified in this build.** The eight stage names and their generic-status
mapping; the single-successor rule and the three constraints on a correction;
the refusal of `inbox` and `declined` as targets for an existing piece; the
`research.md` single-stamp rule; the digest, generation, evidence and research
hash conditions on the other two gates; the differing block rules for open
findings; that publication re-checks both the gates and `ready_digest`.

**Not verified here.** How any particular piece's gates currently stand — that
is per-piece state. Ask the Writing screen, or the piece's own readiness
report.

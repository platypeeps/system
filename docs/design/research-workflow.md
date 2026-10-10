---
title: The research workflow
eyebrow: One standard, every research repo
stand: A research repo is a fixed shape plus a renderer. The shape is enforced by a command, the rendering is idempotent, and nothing publishes until it has been argued against.
---

## What a research repo is

The research repos follow one standard, though the set is not recorded
anywhere and should not be: which repos follow it, and where each publishes, are per-repo
bindings that live in each repo's own `research.conf.py`. A table of them in a
shared document has to be edited in a seventh place the first time a seventh
repo appears, and is wrong from the moment one is renamed.

The standard itself lives in the pack, at
`skills/sd-research-repo/references/conventions.md`, and the tooling that
enforces it lives beside it as `bin/sd-research-kit`.

They sit together deliberately. A standard versioned in one repository and
enforced from another drifts the moment either moves — which is exactly why the
kit stopped living in this repository on 2026-09-03.

Print the standard's path at any time:

```sh
sd-research-kit conventions
```

@diagram research-flow

## The layout

| Path | Holds |
|---|---|
| `00-overview/` | `understanding.md`, `open-questions.md`, `next-research.md` — fixed names |
| `10-sources/` | `registry.md` (what was read, when, status, corrections on record), `references.md` |
| `20-map/` | `MAP-*.md` — ledgers, crosswalks, comparisons |
| `30-brief/` | `BRIEF-*` positions · `REVIEW-*` assessments · `SUMMARY-*` condensations |
| `40-docs/` | `PRD-` `DESIGN-` `PLAN-` `SPIKE-` `SURVEY-` `DISCOVERY-` `BENCHMARK-` `DECK-` `OUTREACH-` |
| `90-scratch/` | Throwaway and superseded. Never cited. |
| `docs/dashboard/` | Rendered HTML. Generated, gitignored, served. |

Every `registry.md` carries a `## Corrections on record` section: claims that
reading the source overturned, so nobody re-derives the old answer. An empty
section is a statement that none is recorded yet; delete no entry.

The numbers are **reading order, not workflow**. `00` is where a newcomer
starts and `90` is where nothing is cited from.

Three type prefixes carry real meaning: `SURVEY-` mirrors an external system
and must record what it was read at; `SPIKE-` is a measurement with its gates
declared up front; `REVIEW-` assesses someone else's work where `BRIEF-` argues
our own.

## Exactly one START HERE

Every project has one entry document, titled `START HERE — <title>`. The same
title appears in the Markdown H1, in `research.conf.py` as both `title` and
`h1`, and in every mirror.

`sd-research-kit review` checks the two surfaces inside the checkout — each
document's H1, and the config's `title`/`h1`. Missing, duplicate and mismatched
all fail.

It does **not** check the README entry link, or any mirror's title and parent,
because those are in no file the kit reads. The review's `ok main document`
line names what it checked, precisely so that a pass is not misread as a claim
about the two surfaces it could not reach.

If the role moves to another document, move the marker and update the entry
links. Rename no file and re-parent no mirror — a role change edits titles and
links, never an identity.

## The repo's own CLAUDE.md

Each research repo carries a short-form copy of the standard, for an agent
working there with no pack checkout to read.

**Laying the first copy is supported; re-laying it is not.**

```sh
sd-research-kit init-claude-md    # from inside a repo that has none
```

That verb refuses outright if a copy already exists, and the refusal is the
design. A repo legitimately states parts of the template differently — one
whose Notion folder is a team space replaces the personal `file:///` Source
header, because its readers have no such checkout. A writer that merged the
template back over that copy would silently put an absolute local path on a
shared page.

So after the first copy, the file is **checked** rather than rewritten.
`review` compares the repo against the template one-directionally: the template
is a floor, not a ceiling. Anything the repo *adds* is the repo doing its job
and is never reported.

Deliberate differences are declared under a `## Local overrides of the shared
template` heading, each naming an exact template heading in backticks and
stating a reason. A missing reason fails review; an unknown section fails
review. `review` prints each accepted override with its reason and the count of
findings it suppressed.

A repo with no `CLAUDE.md` at all fails, and so does a pack install whose
`skills/` is missing from beside its `bin/`. A gate that cannot run has not
passed.

## The three mechanical checks

```sh
sd-research-kit review        # provenance, Status, build freshness, START HERE
sd-research-kit checklinks    # every relative link and in-repo path resolves
sd-research-kit pins          # has upstream moved past the sha we assert?
```

`checklinks` resolves markdown links relative to the containing file, and
backticked paths carrying an `NN-dir/` segment against the repo root. A
backticked bare filename is treated as prose — documents legitimately name
files in other repositories or files that do not exist yet.

`pins` reads the asserted shas out of the documents on every run. The list is
never maintained by hand, so it cannot go stale in the way a table would.

## Rendering and publishing

```sh
sd-research-kit render
```

Every verb acts on the repository you are standing in, and **no verb takes a
repository path**. That is a deliberate rule: a command that can be pointed at
a checkout the caller is not in will eventually be pointed at the wrong one.

Each repo supplies a `research.conf.py` naming `PROJECT` and a `DOCS` list. The
visual identity lives in the renderer, not per repo, so all six render the same
way. One form is written per document: a standalone
`docs/dashboard/<name>.html` that opens with `file://` and is what the
dashboard lists and serves.

Rendering ends by writing each document's Markdown into the Obsidian vault,
registering `docs/dashboard/` with the dashboard, and queueing a sync for every
document that designates an outward destination. Every step is idempotent.

Add or edit pages in `research.conf.py` — never by editing a generated file.

## The adversarial pass

Nothing is published until it has been reviewed **against itself**. Two halves,
both required.

The mechanical half is `sd-research-kit review`. Exit 1 means fix it first.

The half no script can do has eight steps, and its stance is the whole point:
**the job is to refute the document, not to confirm it.**

1. List the load-bearing claims — those whose removal changes the conclusion.
2. Open each cited source again and read it. Default to refuted: a claim stands
   only if the source *says* it, not merely that it is consistent with it.
   Second-hand support is not support.
3. For numbers, check the unit, the date and the denominator — not the digits.
   A rate without its base has not been checked.
4. A claim no source supports is cut, or moved into Status as explicitly
   unverified. It never stays in the body, where a reader assumes it was
   checked.
5. Say what you could not check and why. A stated gap is useful; a silent one
   is a defect.
6. Read the rendered page as someone who has not seen the source material. Does
   the conclusion follow from what is on the page?
7. Find the load-bearing assumption the document never states. There is usually
   one.
8. After mirroring, check the published page against the source — and check
   that handling restrictions survived the mirror. A document that may not be
   shared externally may not become a shared page.

The outcome goes in the Status section. A review that found nothing says so,
and says what it checked: *"reviewed"* without a record of what was examined is
indistinguishable from not reviewing.

Reviewing your own work is the weak form, and it is the one that ships most
often — so it is the one to be disciplined about. Where the document carries a
decision someone will act on, get a second reader who was not involved in
writing it.

**That second reader is a CLI, not a plugin.** Its stance, four-part finding
format and confidence tags live in `local-adversarial-gate/core.md` in this
repository, shared with the writing pack.

```sh
adversarial-gate render --lens research-brief   # the focus text
adversarial-gate run --lens research-brief \
    --repo <checkout> --out <result.md>          # the whole scripted pass
```

`run` refuses to start without `--lens`, `--repo` and `--out`.

## The dependency runs both ways

The pack is public and its research skill tells readers to run
`adversarial-gate` — a command that lives *here*, in this repository. So
this repository owes the pack a `local-bin-links` row, added after the move
shipped an instruction nothing on any machine could execute.

The general rule: when a document moves, check what it tells the reader to
**run**, not only what it says. A path that leaves with the prose becomes a
command that has to be on `PATH` everywhere that prose is read.

## Status

**Verified in this build.** The directory table and prefix meanings; the
one-`START HERE` rule and the two surfaces `review` checks versus the two it
cannot; that `init-claude-md` refuses an existing file; the one-directional
template comparison and the override declaration rules; the three mechanical
verbs and what each resolves; that no verb takes a repository path; the eight
steps of the adversarial pass and the two `adversarial-gate` invocations.

**Not verified here.** Which repositories currently pass `review`,
and whether any asserted pin is behind — both are per-repo, per-run answers.
Run the verbs.

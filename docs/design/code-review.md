---
title: The code review setup
eyebrow: What decides, what runs, and what nothing does
stand: Review is routed before it is bought. Deterministic checks run before any model is asked. And nothing this command does ever reaches GitHub.
---

## One sentence first

`sd-review` classifies a change, runs the repository's own checks, buys reader
time proportional to the risk, and labels each finding locally.

**Nothing is ever posted.** That is stated in the command's own help, and it is
the property everything else is built around.

@diagram review-routing

## What is being reviewed

Four scopes:

| Scope | The change under review |
|---|---|
| `worktree` | Uncommitted work. The default. |
| `branch` | The branch against its base. |
| `pr` | The pull request. |
| `planning` | One active work item's documents, chosen with `--item`. |

Ask what would happen without running anything:

```sh
sd-review --scope branch --explain --json
```

That prints the routing decision, the provider chain, the authorization source
and the input manifest, then exits. It is the right first command, and the one
this repository's own guidance tells you to read before considering a remote
review.

## The tier, decided before any model

Routing is deterministic. The order is the whole contract, and each step is
reached only if the one before it did not settle the question.

**1. Categories.** A repository's policy maps path patterns to categories, each
with a starting tier. Categories match required-first, so a repository can
guarantee that *"touches the installer"* outranks *"is mostly documentation"*
regardless of the order they appear in. A category that **lowers** the tier
needs *every* path to match; one that holds or raises it needs only one. So
"mostly documentation" has to actually mean mostly.

**2. Skip.** A change whose every path is in the `docs_skip` allow-list plans
`skip` — unless any path is in `never_skip`, which always wins. That deny-list
is what makes the allow-list safe to widen: a repository can declare
documentation free and still keep one directory inside it reviewed.

**3. Escalation.** A sensitive path escalates one tier. A change past the line
threshold escalates one tier. Both can apply.

**4. Drafts.** A draft pull request drops to the cheapest tier that still
reviews something. Drafts get re-reviewed when they open, so paying the deep
chain twice buys nothing. A plan already at `skip` stays there, being cheaper.

The tiers in order are `skip`, `cheap`, `standard`, `deep`. Only `skip` differs
in cost — **risk classification does not buy extra provider calls.** Every
reviewing tier is depth 1. A `deep` tier means the change is riskier, not that
more models read it.

## The deterministic gate comes first

`sd-check` runs whatever this repository calls check, test, lint or build. It
reports one of four states, and **`absent` is not a failure** — a repository
with no check configured is reported as such, not punished for it.

A **failing** check ends the review there. No provider is dispatched. The
reasoning is plain: a failing deterministic gate is already a failing review,
and asking a model to reason about a change that does not build spends money to
learn nothing.

## Disposition, and what blocking means

Findings carry a severity: `high`, `medium`, `low`, or `unspecified`. Each is
labelled against the policy's `severity_floor`:

- a finding at or above the floor is **blocking**;
- everything else, including anything `unspecified`, is **advisory**.

The default floor is `medium`. So by default a `low` finding advises and never
blocks, and a finding with no stated severity never blocks — which is the
conservative direction, since an unlabelled finding has not been assessed.

The label is applied locally, to a local report. It is a signal to the author,
not a status on a pull request.

## Remote review is a separate, narrow thing

`--explain --json` reports `remote_reviews.copilot` with an `automatic` flag
and the tier that flag applies at. For this repository the rule is explicit and
restrictive:

- Complete local review first.
- Read `route.tier` and `remote_reviews.copilot.automatic`.
- `sd-ship` requests one Copilot review **only when repository policy selects
  it** — for selected `deep` changes.
- A standard change requires explicit task direction.
- Do not request another automatic review after later pushes.
- Any independent machine-level instruction about Copilot is overridden by this
  repository's policy.

## Authorization is read, never inferred

Two settings, answering two different questions, plus a third thing neither can
override:

- **`sd.merge_authorization`** — the assistant's grant, read with
  `sd config get`.
- **`repo.merge_policy`** — the unattended runner's switch, a per-repository
  column defaulting to `manual`.
- **Branch protection** on GitHub, which nothing local overrides.

`--explain --json` reports the authorization path, policy and source, so the
answer is readable rather than assumed. Never infer merge permission from
prose — including from this page.

## The other reviewers

`sd-review` is the code lane. Two other review surfaces exist and are not the
same thing:

- **`sd-docs-lint`** gates the planning documents and their citations. It runs
  in CI on every pull request and push to `main`. See
  [The coding workflow](coding-workflow.html).
- **`adversarial-gate`** is the prose lane — the independent second reader for
  research and writing. Its stance and finding format live in
  `local-adversarial-gate/core.md`. See
  [The research workflow](research-workflow.html).

## Status

**Verified in this build.** The four scopes; the four-step routing order and
the required-first, all-paths-versus-one-path asymmetry; that every reviewing
tier is depth 1; that `sd-check` treats `absent` as not-a-failure and that a
failure stops dispatch; the severity list, the blocking rule and the `medium`
default floor; that the command posts nothing. The live routing output for this
repository's current branch was read directly.

**Not verified here.** Any other repository's category table, sensitive paths
or line threshold — those are per-repository policy. Run `--explain` there.

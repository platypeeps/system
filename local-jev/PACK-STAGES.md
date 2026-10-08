# Pack Jev stages

The command pack's prose skills once carried optional Jev passes.
sd:3040 and sd:3001 removed them from the pack; the text below moved here as written.
Nothing calls these stages now: the model that read each skill was the caller.
So `KNOWN_CALLERS` in `tests/test_jev_contract.py` does not list them.
Use this page to run a pass by hand.

Stages: `JEV_SD_FACT_CHECK`, `JEV_SD_PUBLISH`, `JEV_SD_PROSE_SCORE`.
Step, section and pattern numbers refer to the pack skill each part came from, at pack commit `fcf0832f7`.
`references/prose-score-dimensions.md` in the moved text means the "Prose scores" section of this page.

## sd-fact-check (`JEV_SD_FACT_CHECK`)

From `skills/sd-fact-check/SKILL.md`.

### Argument

- `jev=on|off` — default `off`; opt in to the optional typed verdict pass in
  `## Optional typed verdicts`. Off, absent, or unavailable, the audit runs
  exactly as described here.

### Optional typed verdicts

This section is optional and off by default. Skip it unless the invocation
passes `jev=on` **and** `jev enabled` exits `0`. Both conditions are required;
either one absent means this section does not run and the audit is unchanged.

`jev` is a shell command that asks a System One model one narrow typed question
about supplied state. It is not part of this skill and is absent on most
machines. Without it the audit is complete: the workflow above assigns every
verdict on its own, and nothing in this section changes the scope, the verdict
ladder, or the `## Final report` contract.

#### Why a typed question fits here

Step 6 ends in one of five named verdicts per claim. That is a fixed-set
classification over an evidence span, so a `choice` question returns the same
vocabulary as a typed answer with a confidence attached. Use it as a second
reading that flags disagreement. It never assigns a verdict.

#### Before the first call

- Run `jev enabled JEV_SD_FACT_CHECK --record --caller sd-fact-check`. It exits
  `0` when the command can answer here and `3` when it cannot. It calls nothing and costs nothing, so it is safe to run first.
- Confirm the claim and its evidence span may leave the machine. Every call is
  a network request to a third party. Skip this pass for confidential,
  embargoed, or personal material.
- Send the claim text, the as-of date, and the evidence span, and nothing else.
  No file path, locator, internal URL, host name, repository name, or
  credential belongs in the state. The question does not need them.
- Ask once per claim, after step 5 gathers the evidence and before step 6
  writes the verdict.

#### The criteria

Each criterion describes one concrete situation and stands on its own. The
model reads these descriptions and not this page, so none of them refers to
another criterion or to the workflow above. Keep them in a scratch JSON
file of their own, outside the audited material and outside the working
directory:

```json
{
  "supported": "The evidence states the claim as written, or directly implies that it is true.",
  "partially_supported": "The evidence supports a narrower or qualified version of the claim, but not its full scope, magnitude, or certainty.",
  "unverified": "The evidence does not address what the claim asserts, either way, so nothing in it settles the claim.",
  "contradicted": "The evidence states the opposite of the claim, or implies that the claim is false.",
  "outdated": "The evidence shows the claim held at an earlier date, and that a later fact replaced it before the as-of date.",
  "not_a_factual_claim": "The text is an opinion, a value judgment, a prediction, or rhetoric, so no evidence could settle it."
}
```

The first five names carry the five verdicts of step 6, spelled with
underscores. `not_a_factual_claim` is the no-match option: it catches an input
the five verdicts cannot describe, so the model never forces one of them onto
an opinion or a prediction.

Give the run its own scratch directory with `scratch=$(mktemp -d)`. Install
`trap 'rm -rf "$scratch"' EXIT` in that shell, immediately after. Write the
criteria to `$scratch/criteria.json` and the state to `$scratch/claim.json`.
Neither file lands in the working directory, so neither reaches a repository,
a diff, or the audited material. Run the call below in the same shell. The
trap removes the directory when that shell exits, so an interrupted or failed
audit leaves nothing behind.

Pass the criteria file as `@"$scratch/criteria.json"`. The inline
`--criteria 'name=text,...'` form splits entries on commas and on the first
`=` of each entry, so these descriptions cannot be written inline.

#### The call

State is the claim and its span, one claim per call:

```json
{
  "claim": "<exact original wording>",
  "as_of": "<audit date>",
  "evidence": "<the evidence span read for this claim>"
}
```

```sh
jev choice 'How does the evidence relate to the claim as written?' \
    --state "$scratch/claim.json" --state-format json \
    --criteria @"$scratch/criteria.json" \
    --unsure-below 0.8 --fallback not_asked \
    --caller sd-fact-check --stage JEV_SD_FACT_CHECK \
    --subject "sd-fact-check:$key"
```

`--caller` and `--stage` name this pass in the judgment ledger, and
`JEV_SD_FACT_CHECK=0` switches it off.

`--subject` names the claim so a later verdict can join the judgment. `$key`
is the first 16 hex of the SHA-256 of the claim's exact original wording plus
a newline: `printf '%s\n' "$claim" | shasum -a 256 | cut -c1-16`. Never pass
the wording itself. Before the first call of an audit, export one run id,
`JEV_RUN=sd-fact-check-<UTC yyyymmddThhmmss>-<4 hex>`, and keep it for every
claim, so the ledger groups the audit as one run.

`--fallback not_asked` prints `not_asked` and exits `0` when the command is
switched off, unkeyed, or failing, and writes the reason to stderr. A failed
call is therefore never a stalled audit.

The token is deliberately not `unsure`. `--unsure-below` prints `unsure` for a
real answer under the threshold, so reusing that word would make a pass that
judged nothing read exactly like a pass that judged every claim and was
uncertain about all of them. Exit `0` does not mean the model judged anything
either, because a fallback also exits `0`. Read the printed name, not the exit
status.

#### What the answer changes

The command prints one criterion name, or `unsure` when its confidence is under
`0.8`. Confidence is the shape of the distribution: one peak is high, spread
across several names is low.

- Answer `unsure`: ignore it. Assign the verdict from the evidence, as step 6
  already requires.
- Answer equal to the verdict you reached: change nothing. It is corroboration
  and not evidence.
- Answer different from the verdict you reached: re-read the evidence span
  once, then write the verdict the evidence supports. A disagreement prompts a
  second look and decides nothing.
- Answer `not_a_factual_claim`: check whether the item belongs under
  **Non-fact-checkable items** instead of the claim ledger. Decide from the
  wording of the claim, not from this answer.
- Answer `not_asked`: the call reached no judgment. It is neither a verdict nor
  a low-confidence answer. Record nothing from it and assign the verdict from
  the evidence, as step 6 already requires.

Count the claims sent and the claims answered. A `not_asked` claim was sent and
not answered. When the two counts differ, say so once in the session response,
outside the deliverable, and give both numbers. When nothing comes back
answered, stop the pass for this audit instead of calling once per remaining
claim.

You own every verdict. A probability is not evidence, so never cite this
command or its output in the claim ledger, a rationale, a confidence value, an
evidence column, or the methodology section. The delivered report is
byte-for-byte the report this skill would produce with the pass switched off.

## sd-publish (`JEV_SD_PUBLISH`)

From `skills/sd-publish/SKILL.md`.

### Argument

- `judge=off|jev` — default `off`; an optional destination-fit judgment run
  before the preview, and only when the `jev` command is available; and

### Workflow, steps 11 and 12

11. Judge destination fit when `jev` is available, before the preview. This
    step is optional and off by default. Run it only when the user sets
    `judge=jev` and `jev enabled JEV_SD_PUBLISH --record --caller sd-publish`
    exits 0. That probe costs nothing and makes no request. Exit 3 means the judgment is unavailable; continue unchanged
    and record it as not run. Leave the probe unrun when `judge` is off, and
    record availability as `not checked`. Ask both questions in one `ask` request, over
    the same state:
    - a `score` for how well the draft matches the destination's register and
      length expectations. Describe each level as a concrete situation: wrong
      register or length for this channel; recognizable for it but fighting
      it in several places; matching its register and sitting inside its
      length, with rough spots; and reading as written for it, with nothing
      left to change; and
    - a `noul` for whether the draft still says what the source said. The
      condition holds when every load-bearing claim is stated or directly
      implied by the source span. It fails when the draft contradicts the
      source, and it fails when the draft adds or strengthens a claim the
      source does not carry. Name both failure shapes in the instructions, so
      a low probability is readable.
    Adaptation drift is the failure this skill exists to prevent, so the
    second question is a source-faithfulness check and is written as one.
    Both questions share one state, so send it once as named JSON fields and
    reference them from the questions with backticked paths such as
    `draft.body`. The question ids are the keys of the questions object; they
    are for your code and never reach the model, so put the whole meaning in
    the question:

    ```sh
    jev ask --questions q.json --state s.json --state-format json \
        --caller sd-publish --stage JEV_SD_PUBLISH \
        --subject "sd-publish:$(shasum -a 256 s.json | cut -c1-16)"
    ```

    `--subject` names the judged state by the first 16 hex of its SHA-256,
    never by its text, so a later outcome can join the judgment by hashing
    the same file. Export one `JEV_RUN=sd-publish-<UTC yyyymmddThhmmss>-<4
    hex>` before the first call of a run and keep it for every call in it.

    State carries the exact draft, the source spans it was adapted from, the
    destination name, and the supplied length or register constraints. Send
    nothing else. Never send file paths, repository names, credentials,
    destination account details, or profile content. Every call leaves the
    machine, so do not send a source the user marked confidential at all;
    leave `judge=off` and say so in the report.
12. Act on the two numbers, and treat neither as permission to publish.
    - A low fit score is a rewrite, not a note. Rework the draft against the
      destination contract in step 5, then judge the new draft before the
      preview. Below the top two levels is a reasonable starting threshold;
      evaluate it on real drafts.
    - A low faithfulness probability is a stop-and-ask. Name the drifted claim
      and its source-ledger ID, and ask the user before continuing. Below 0.8
      is a reasonable starting threshold. A probability near the middle means
      yes and no are similarly likely, not that the draft is half faithful.
      Stop there too.
    - A high score and a high probability change nothing about publication.
      This skill still does not send, publish, or schedule anything, and the
      user's approval is still required.
    - When a request fails, a fallback keeps the lane moving. The flag goes
      after the verb, and an empty answer set is the batch form:
      `jev ask --questions q.json --state s.json --state-format json
      --fallback '{}' --caller sd-publish --stage JEV_SD_PUBLISH`. It prints that answer, exits 0, and writes the reason
      to standard error. Read the reason. Record the answer as not run, and
      never as a pass.
    - Count the answers you got back, not the questions you sent, and report
      both numbers. Exit 0 says the call returned, not that Jev judged
      anything: a fallback answers nothing and exits 0 too. A judgment
      missing either answer is not run. A line that reads the same whether
      Jev answered or not is how a dead check stays green forever.

### Safety rule

- The `jev` judgment is optional, additive, and off by default. It reads the
  draft and the source span, returns numbers, and writes nothing. It cannot
  approve, send, publish, widen an audience, or supply a claim, and a high
  number is not approval.

### Final report bullet

- **Destination-fit judgment** — judge mode, whether `jev` was available, or
  `not checked` when judging was off and the probe never ran,
  questions sent and answers returned as separate numbers, the fit score,
  the faithfulness probability, the action each number triggered, and
  `not run` when the judgment was off, unavailable, or answered by a
  fallback; and

## Prose scores (`JEV_SD_PROSE_SCORE`)

From `skills/_shared/references/prose-score-dimensions.md`.
`sd-humanizer` and `sd-prose-lint` both read it.


A prose impression does not survive a revision. "Reads hedgy" cannot be
compared against yesterday's draft, thresholded, or handed to another
reviewer. This reference defines an optional scoring pass that turns the
judgment half of a prose review into numbers a caller can keep.

The pass is optional and off by default. A reader without the scorer reads a
complete skill: everything the owning skill does without this pass, it still
does, unchanged.

### What runs the pass

The scorer is `jev`, TypeSafe's System One model, reached through a command of
that name on `PATH`. It answers a narrow question about supplied state with a
type and a probability. It writes no prose and explains nothing.

Run the pass only when both conditions hold:

1. The user asks for prose scores, by name, in this session. Nothing infers
   the request, and no earlier session's request carries over.
2. `jev enabled JEV_SD_PROSE_SCORE --record --caller <skill>` exits `0`,
   where `<skill>` is the skill running the pass: `sd-prose-lint` or
   `sd-humanizer`. That command calls nothing and costs nothing, so it is
   safe to run first every time. `JEV_SD_PROSE_SCORE=0` switches the pass off.

If `jev` is absent from `PATH`, or `jev enabled` exits `3`, say so in one
plain sentence and finish the review without scores. A missing scorer is a
reported gap, never a failure and never a stall.

### What leaves the machine, and what must not

Every scored call posts the draft text to a third party. Nothing else is sent:
no file path, no repository name, no branch, no surrounding conversation, no
credential, and no environment value.

**Do not score a confidential draft.** An unpublished security finding, an
embargoed announcement, a draft naming a customer, and anything carrying a
secret stay unscored. Lint them with the deterministic pass and the reading
pass alone, and say in the report that scoring was withheld.

The user's request to score is a request to score the text in front of you. It
is not permission to send anything that text does not contain.

### Keep the countable in code

Ask the model only for what code cannot count. A question a `grep` answers
spends tokens to learn what a pipe already knows, and it answers less reliably.

Code counts, and the scoring pass never asks about:

- words per sentence, with `wc -w` on a split, and the distribution across the
  draft;
- occurrences of an em dash, an en dash, a curly quote, or an emoji;
- runs of bold spans, heading capitalization, and list shape;
- every watched word and phrase the owning skill lists, as a literal match;
- hyphenated pairs, and whether each sits before or after its noun.

The model judges, and only the model can:

- whether a long sentence carries one idea or several joined ones;
- whether a hedge names a real limit or pads a claim the writer could state;
- whether a passive hides an actor the reader needs, or drops one nobody wants;
- whether a watched word is being used or is being quoted and discussed;
- whether a promotional adjective is supported anywhere in the draft.

The division is the point. A count tells you a word is present. A score tells
you whether its presence is a defect here.

### The dimensions

Each dimension is one question about the whole draft. Each is scored on the
ordered levels below, numbered from `0`. Levels describe situations rather
than degrees, because "moderately hedged" is not something two readers agree
on and "qualifiers stack inside single sentences" is.

Keep the dimensions independent. A level that mixes two of them produces a
number that means neither.

| Dimension | Question |
| --- | --- |
| `hedging` | How much of the draft is padded with qualifiers that narrow nothing? |
| `filler` | How many words could be cut without losing a claim? |
| `passive_actor` | Does the passive voice hide an actor the reader needs? |
| `signposting` | Does the draft announce what it will do instead of doing it? |
| `puffery` | Does the draft rate its subject's importance instead of describing it? |
| `overload` | Does each sentence carry a single idea? |

### The request

The dimensions are independent questions over one state, so they belong in a
single `ask` request. Questions in one request run in parallel and cannot read
each other's answers, which is what makes them cheap. A question that needed
an earlier answer would need a second call, and none of these does.

Write the draft to a file, write the block below to a file, then run:

```sh
jev ask --questions questions.json --state draft.txt \
    --caller <skill> --stage JEV_SD_PROSE_SCORE \
    --subject "<skill>:$(shasum -a 256 draft.txt | cut -c1-16)"
```

`--subject` names the draft by the first 16 hex of its SHA-256, never by its
text, so a later outcome can join the scores by hashing the same draft. Export
one `JEV_RUN=<skill>-<UTC yyyymmddThhmmss>-<4 hex>` before the first call of a
run and keep it for every call in it.

Both `--questions` and `--state` read stdin when given `-`, so at most one of
them can be `-` in a single call. Keep the draft in a file and pipe the
questions, or keep both in files as above.

`ask` takes a shared `--state` and nothing per question. It has no `--id`, no
`--json` and no `--gate`, and the question ids are the keys of the object
below. The model never sees them; only your code does.

This pass branches on `jev enabled` rather than passing `--fallback`. A
fallback prints an answer you supplied, and a supplied score is a number
nobody measured. An unscored review is honest; an invented score is a defect
that reads exactly like a measurement.

```json
{
  "hedging": {
    "type": "score",
    "instructions": "How much of this draft is padded with qualifiers that narrow nothing?",
    "criteria": [
      "Claims stand unqualified. Where a qualifier appears it names a real limit: a date, a sample size, or a condition the reader can check.",
      "One or two claims carry a softener the writer could have dropped. The draft still commits to its main points.",
      "Most paragraphs soften a claim without naming what limits it. Words like 'may', 'often' and 'in many cases' appear where nothing narrows.",
      "Qualifiers stack inside single sentences, as in 'could potentially possibly'. No sentence states anything a reader could disagree with."
    ]
  },
  "filler": {
    "type": "score",
    "instructions": "How many words in this draft could be cut without losing a claim?",
    "criteria": [
      "Every phrase carries a claim. Sentences open on their subject.",
      "A few stock phrases restate a shorter form, such as 'in order to' or 'at this point in time'.",
      "Sentences routinely open with a throat-clearing phrase before the claim, such as 'It is important to note that'.",
      "Whole sentences carry no claim. Cutting them would lose nothing a reader needs."
    ]
  },
  "passive_actor": {
    "type": "score",
    "instructions": "Does the passive voice in this draft hide an actor the reader needs?",
    "criteria": [
      "Passive appears only where the actor is unknown or beside the point, and naming one would add nothing.",
      "One or two passive sentences drop an actor the reader can still infer from the surrounding text.",
      "Several main claims name no actor. The reader has to guess who performs the action.",
      "Agentless passives carry the argument throughout, as in 'The results are preserved automatically' or 'The decision was made last quarter'."
    ]
  },
  "signposting": {
    "type": "score",
    "instructions": "Does this draft announce what it will do instead of doing it?",
    "criteria": [
      "Sections open on their content. A heading is followed by the first real claim.",
      "One transition narrates the structure rather than carrying the argument forward.",
      "Several sections open with a line that restates the heading before the content starts.",
      "The draft repeatedly announces itself, with phrases such as 'Let us dive in' or 'Here is what you need to know'."
    ]
  },
  "puffery": {
    "type": "score",
    "instructions": "Does this draft rate the importance of its subject instead of describing it?",
    "criteria": [
      "Claims are specific and checkable. Nothing in the draft rates its own significance.",
      "One sentence calls the subject important without evidence, and the rest describes it plainly.",
      "Promotional adjectives recur, such as 'vibrant' or 'renowned', and nothing in the draft supports them.",
      "The draft frames its subject as a milestone throughout, with phrases such as 'stands as a testament' or 'marks a pivotal moment'."
    ]
  },
  "overload": {
    "type": "score",
    "instructions": "Does each sentence in this draft carry a single idea?",
    "criteria": [
      "Each sentence states one idea. A long sentence is long because its one idea is long.",
      "A few sentences join two ideas, and a reader can still separate them.",
      "Most sentences carry two or more ideas, often joined by a trailing clause beginning with an '-ing' word.",
      "Ideas chain across clause after clause. No single claim is stated on its own anywhere in the draft."
    ]
  }
}
```

`jev ask` prints the whole response. Each dimension answers with a `score`, a
probability distribution over the levels, a `confidence`, and a `legend`
mapping each level number back to its description.

### Count the answers, not the questions

Report how many dimensions answered and how many were asked, as two numbers,
every time the pass runs. Then name any dimension that did not come back.

A summary counting what you sent says the same thing whether every dimension
answered or none did. That line stays green after the pass has stopped
working, which is how a dead check goes unnoticed for months. Counting what
came back cannot do that: a call that answered nothing reports `0`, and a
reader sees it.

A response missing a dimension is not a zero on that dimension. Leave it out
of the composite and say it is missing, because a score of `0` on `hedging`
means the draft hedges nothing, and that is the opposite of what happened.

### Record the raw scores, decide policy separately

Put every dimension's raw `score` and `confidence` in the report, unrounded.
Those numbers are the expensive part, and they are reusable: a reader who
disagrees with a threshold or a weight can change it and re-read the same
numbers without paying for inference again.

Do not report a single verdict in place of the dimensions. A composite that
hides its inputs cannot be argued with, and the argument is the value.

Normalize before combining. Divide each raw score by the highest level number,
so every dimension lands on `0` to `1` and a dimension with more levels does
not outweigh one with fewer.

Weights and thresholds are policy, and policy belongs to whoever owns the
prose. State the weighting you used beside the composite, so a reader can
apply different weights to the same scores. A release note and an internal
design page do not deserve the same weighting, and neither deserves one baked
into this file.

A low `confidence` on a dimension means the model spread its probability
across levels. Read it as "this dimension did not separate here", not as a
middling score, and say so rather than reporting the number alone.

### Tracking a draft across revisions

Scores earn their cost on the second run. Record the dimension scores against
the revision they describe, then score the revision that follows. A dimension
that moved is evidence the edit worked. A dimension that did not move is the
finding the reading pass should have caught and did not.

Compare only scores taken with the levels above unchanged. Editing a level
changes what the number means, so a comparison across an edit is a comparison
of two different measurements.

## sd-humanizer (`JEV_SD_PROSE_SCORE`)

From `skills/sd-humanizer/SKILL.md`.

### Under "Your Task"

The loop judges by eye, so it tells you a rewrite reads better without showing
that it is. An optional pass scores the source and the rewrite on the same
dimensions, from `references/prose-score-dimensions.md`, and the difference is
the evidence. OPTIONAL SCORES states when that pass runs, and this paragraph
states no condition of its own. The pass is off by default; without it this
skill behaves exactly as it does above.

### OPTIONAL SCORES

The dimensions, the levels, the single request that runs them in parallel, and
the privacy rule all live in `references/prose-score-dimensions.md`. Read that
file before scoring anything. What follows is only how this skill uses it.

**Run it when all three hold.** The mode is pasted text or file, the user
asked for prose scores in this session, and `jev enabled` exits `0`. Otherwise
skip the pass. Embedded mode never scores, because it reports no numbers.
Say nothing when the user never asked. In pasted-text and file modes, say so
in one sentence when the user asked and `jev` cannot answer here. Embedded
mode reports neither the scores nor their absence, because it outputs prose
only. A reader without `jev` still gets the whole loop above.

**Score twice, before and after.** Score the source text, run the loop, then
score the final rewrite. A dimension that dropped is evidence the edit landed.
A dimension that held is a pattern you looked at and did not fix, which is
worth one line in the summary. Scoring only the rewrite proves nothing,
because there is nothing to compare it against.

**The scored dimensions map onto the patterns above.** `hedging` reads §24,
`filler` reads §23, `passive_actor` reads §13, `signposting` reads §28,
`puffery` reads §1 and §4, and `overload` reads §3's trailing participles.
The model scores the whole draft on each, not individual sentences.

**Do not hand the model a job a `grep` already does.** The watched-word lists
in §§1 to 33 are literal matches, and so are em dashes, en dashes, curly
quotes, emoji, bold runs, and heading capitalization. Count those. The model
judges what counting cannot reach: whether a watched word is being used or
quoted, whether a hedge names a real limit, whether a passive hides an actor
the reader needs, and whether a long sentence carries one idea.

**Report the raw numbers.** Give each dimension's score and confidence for
both passes, and keep any weighting separate from them, so a reader can apply
their own threshold to the same numbers. A single blended verdict throws away
the part that cost something to produce.

**Count the answers, not the questions.** Say how many dimensions answered and
how many were asked, as two numbers, and name any that did not come back. A
line counting what you sent reads identically whether every dimension answered
or none did, and that is how a pass that quietly stopped working keeps looking
fine. A missing dimension is missing, never a zero: zero on `hedging` means
the draft hedges nothing.

**Never score a confidential draft.** Every scored call posts the text to a
third party. Run the loop without scores on an embargoed draft, a draft naming
a customer, or anything carrying a secret, and say in the summary that scoring
was withheld. Send the draft text alone: no file path, no repository name, no
credential.

**Voice outranks the scores.** A writing sample the user supplied still beats
every rule in this skill, and a score is a rule. Do not flatten an author's
habits to move a number.

### Invocation Modes, embedded mode

Never score in this mode. The mode reports no numbers, so a scored call would post the draft to a third party for nothing.

### Process and Output

5. Score the source and the final rewrite only in pasted-text or file mode, and only when the user asked for scores and `jev enabled` exits `0` (see OPTIONAL SCORES). Report both sets of raw numbers. Skip this step otherwise. Embedded mode never reaches it, so an embedded draft is never sent.

Scores, where they ran, go in the summary in pasted-text and file modes; embedded mode never runs the scoring step, so it has none.

## sd-prose-lint (`JEV_SD_PROSE_SCORE`)

From `skills/sd-prose-lint/SKILL.md`.

### Intro

The judgment pass returns an impression, and an impression cannot be
thresholded or compared against the previous draft. When the user asks for
prose scores and `jev` is available, `references/prose-score-dimensions.md`
turns that pass into a number per dimension. It is optional, off by default,
and nothing else in this skill changes when it does not run.

### Workflow, step 7

7. Score the judgment dimensions only when the user asked for scores in this
   session **and** `jev enabled` exits `0`. Read
   `references/prose-score-dimensions.md` first: it holds the dimensions, the
   levels, the single `ask` request that runs them in parallel, and the rule
   that keeps the countable in code. Ask the model nothing a `grep` or a
   `wc -w` already answers; the deterministic pass owns every literal match
   and every length. Report each dimension's raw score and confidence, and
   state any weighting separately, so a reader can rethreshold without paying
   for inference again. Report how many dimensions answered against how many
   were asked, as two numbers, and name any that did not come back: a count of
   what you sent reads the same whether the pass worked or returned nothing.
   Do not run the step when the user did not ask, when `jev` cannot answer
   here, or when the draft is confidential. Name the reason in the **Scores**
   bullet of the final report, in one sentence, and say nothing more about it
   anywhere else. The report always accounts for an absent scoring pass.

### Safety rules

- Never score a confidential draft. A scored call posts the draft text to a
  third party, so an embargoed, customer-naming, or secret-bearing draft is
  linted without scores and the **Scores** bullet says scoring was withheld.
- Send the draft text and nothing else. A file path, a repository name, a
  branch, or a credential never belongs in a scoring request.

### Final report bullet

- **Scores** — when the scoring step ran, each dimension's raw score and
  confidence, how many dimensions answered out of how many were asked, and the
  weighting used to combine them; otherwise one sentence naming the reason it
  did not run: the user did not ask, `jev` could not answer here, or the draft
  is confidential and scoring was withheld;

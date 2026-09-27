# local-adversarial-gate

The hostile second-reader pass, shared. One prompt core, one lens per document type,
one wrapper — for the two repos that each built this separately and then had to keep
two copies of the same careful prose in sync by hand.

## Why it is here

The writing pack ran a Codex hostile read from `pack.py review adversarial`, against
`templates/adversarial-review.md`. `local-research-kit` ran one through the Codex
plugin, with the framing spelled out in `CONVENTIONS.md` and again in `reviewcheck.py`.
(That kit left this repo on 2026-09-03; it is `sd-research-repo` and `bin/sd-research-kit`
in `platypeeps/sd-ai-command-pack` now, and reads its framing from here.)
Different invocation, different scope, same idea — and the part that matters most was
duplicated: that the gate produces *hypotheses* rather than findings, that it sees the
repo and not the cited sources, that an honestly empty axis beats a manufactured
finding. Three copies of that, with nothing keeping them in agreement.

The stance and the output contract are `core.md` now, written once. What genuinely
differs between a blog draft and a research brief is a lens.

## Use

```sh
adversarial-gate lenses                        # what is available
adversarial-gate render --lens writing-draft \
    --set DRAFT_PATH=content/2026/x/index.md \
    --set RESEARCH_PATH=content/2026/x/research.md
adversarial-gate run --lens research-brief --repo . --out adversarial.md --timeout 900
adversarial-gate rank --in adversarial.md     # order an existing result (see below)
adversarial-gate test -v                      # the unittest suite under tests/
```

`render` composes `core.md` + `lenses/<name>.md` to stdout, substituting `{KEY}` for each
`--set KEY=VALUE`. `run` pipes that to `codex exec` in a read-only sandbox, and ends
it at `--timeout` seconds (default 1800) with **exit 124**, `timeout`(1)'s own status:
macOS ships no `timeout`(1), so the bound is `perl`, which every Mac has, doing what
it does -- `codex` runs in a process group of its own and the whole group is killed
at the bound, TERM then KILL, so the sandbox and the reviewer under the launcher go
with it and nothing keeps the pipe open. `--out` must name a file; an empty or absent
one is refused before `codex` is reached. `test` runs the suite in `tests/`, against a
fake `codex` on a private PATH, so no test reaches the real CLI; the `tools` leg of
`system-native` runs it.

**`render` is the one most callers want.** A caller that already owns its invocation
should compose through `render` and keep its own `codex` call: the writing pack does,
because the draft-digest stamping and confidence-tag counting wrapped around that call
are its own business and do not belong here. `run` exists for the scripted path a caller
does not otherwise have.

`--lens` also takes a path, so a repo with a document type of its own can keep its lens
in its own checkout and still get the shared core.

## Contents

| File | What it is |
|---|---|
| `core.md` | The stance, the four-part finding format, the confidence tags, and the caveats. Shared by every lens. |
| `lenses/writing-draft.md` | Blog/article drafts with a `research.md` behind them. Takes `{DRAFT_PATH}` and `{RESEARCH_PATH}`. |
| `lenses/research-brief.md` | Research briefs: load-bearing claims, numbers, the unstated assumption. |
| `adversarial-gate.sh` | Entrypoint — `render`, `run`, `rank`, `lenses`, `test`. |
| `rank.py` | The optional Jev ordering: parse the findings, ask one question each, reorder and label. Never removes one. |
| `tests/test_run.py` | The `run` regression suite (sd:790): the bound fires, an empty or missing `--out` is refused. |
| `tests/test_rank.py` | The ordering contract, against a stub `jev`: on unless switched off, degrades to today's output, and the same set of findings either way. |

## Order the findings by a Jev judgment

The reviewer ranks its own findings by severity. `rank` asks a second, narrower
question per finding — *is this a real defect in the document under review, as
opposed to a stylistic preference or a restatement of what the document already
says?* — and uses the answer to order them, so the reader meets the strongest
objection first. Each finding gets a visible label with its probability, and the
result gets a note at the top saying what the order means.

**It never drops, hides or filters a finding.** Ordering and a label, nothing
else. The whole value of a hostile read is that it says the unwelcome thing, and
a confidence filter that quietly removes one defeats it — the low-scored finding
is the one to read last, not the one to skip. The set of findings in the file
after `rank` runs is the set that was in it before, and
`test_the_set_of_findings_is_identical_with_and_without_jev` is what holds that.
Only a run of findings sitting back to back is reordered, so a finding cannot be
carried out from under the prose that introduces it.

### It is on unless you switch it off

Jev is experimental, and CLAUDE.md's rule is that nothing may depend on it. So
this is additive, and it subtracts only: one call decides it.

```sh
jev enabled JEV_ADVERSARIAL_GATE   # 0: keyed, switched on, and this stage is on
export JEV_ADVERSARIAL_GATE=0      # today's output, byte for byte
```

**Unset means on.** `0`, `off`, `false`, `no` and `disabled` switch this stage
off, in any case; every other value leaves it on, the `1` this used to require
included. It only ever subtracts: `jev enabled` reads the fleet switch and the
key first, so this variable can take the stage out and can never put it back in
on a machine that cannot answer.

`run` calls `rank` on its own output under exactly that condition and no other.
`rank --in FILE` is the same thing on a result you already have, and it reads
the same switch: an explicit verb does not override it.

Every failure — the stage switched off, Jev off or unkeyed, no `jev.sh` beside this folder, a
request that fails, a response that does not cover every finding, an output this
cannot parse — leaves the file exactly as the reviewer wrote it and says why on
stderr. It is never silent and never fatal: **`run`'s exit code does not change**,
because ordering is a courtesy and the reviewer's result is the thing that matters.
A file that was ordered once is not ordered again.

The sibling entrypoint is reached by path — `<repo root>/local-jev/jev.sh`,
resolved from this script's own real directory — and not by `jev` being on
`PATH`. This script is invoked through the `~/bin/common/adversarial-gate`
symlink, and through the symlink sibling files are missing. `ADVERSARIAL_GATE_JEV`
overrides the path; the suite drives a stub across it, so no test reaches the
network.

### What leaves the machine

Every Jev call leaves the machine, and this tool reads documents that may be
unpublished drafts. What is sent is **the text of each finding, truncated to
1500 characters, and nothing else** — as one `ask` request, so N findings cost
one round trip rather than N. A finding quotes a line of the document under
review, because `core.md` requires it to, so that line goes with it. The
document itself is never sent, and neither is the repository, the path, or
anything the reviewer did not write into a finding.

**Reviewing a confidential draft? Set `JEV_ADVERSARIAL_GATE=0` before the
run.** The finding text carries quoted spans of the draft out to a third party,
and no ordering is worth that. Doing nothing sends it: the stage is on unless
that variable says otherwise, so keeping a draft off the wire is an action you
take, not one you omit. `jev off` and an unkeyed machine stop it too.

## What this does not do

It does not decide whether a claim is true. It reads the repository, not the sources the
repository cites, so a citation-strength finding from it is a question addressed to a
human, not an answer. Whoever calls it is responsible for saying so wherever the output
lands — `pack.py` stamps that sentence into the head of every `adversarial.md` it writes.

It is also not a gate by itself: nothing here blocks a publish. The calling repo decides
what an unresolved finding means. In the writing pack it blocks `review -> ready`; in the
research repos it is a line in the document's Status section.

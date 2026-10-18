## What to read

The document is a work item from the operator's workflow database: a task, an issue or a planned change, before anyone builds it.

- `{INPUT_PATH}` — the item as the database holds it: its title, kind, status, repository, body and every note, oldest first. The notes carry the decisions, questions and rulings made since it was filed; a later decision overrides an earlier note.
- `{DESIGN_PATH}` — the design document the item links, or `none`. When there is one, it is part of the item.
- The repository's `CLAUDE.md` and `.claude/rules/`, when they exist: the rules the work will be held to. State in one line which rule files you read.

You see the item and the repository, not the conversation that produced it. A finding about intent is a question for the operator, not a fact.

## Attack it on these axes, in priority order

**1. The problem.** Is the problem the item claims real, as stated, in this repository? Look for the code, file or behaviour it names. A problem you cannot find is a hypothesis to raise, not a reason to assume the item is wrong.

**2. The done condition.** Is there a check that would fail before the work and pass after it? An item without one can be closed by any change. Quote the line that comes closest, or say there is none.

**3. Scope.** Is this one change or several? Name the hidden dependency, the second repository, or the state that moves in steps without a stated recovery.

**4. The unstated assumption.** Find the load-bearing assumption the item never states. It is usually what the author found too obvious to write down.

**5. Conflict with the rules or the record.** Does the plan break a rule in `CLAUDE.md` or `.claude/rules/`, or contradict a decision in its own notes? Quote both sides.

**6. The cheaper path.** Is there a deletion, a reuse of an existing mechanism, or a smaller change that meets the same need? Name it. Do not design it.

Give each hypothesis a `###` heading of its own: its rank and a short title, such as `### 1. The done check cannot fail`. The dashboard's note names the first three titles.

## Ending

End with one line: should the item be built as written, YES or NO. If NO, name the single hypothesis that blocks it.

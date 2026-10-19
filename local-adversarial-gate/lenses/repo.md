## What to read

The document is a whole repository, `{REPO_NAME}`, the checkout you are in. Read it as its next maintainer would, before trusting it.

- `README.md` and `CLAUDE.md`, and `.claude/rules/` when it exists: what the repository says it is and the rules it says it keeps. State in one line which of them exist.
- The tree: run `git ls-files` and read the layout. Sample the code the README names; do not try to read every file.
- The recent history: `git log --oneline -n 40` and `git log --stat -n 10`. Look for reverts, fixes of fixes, and files that change in most commits.
- The tests: how they run, what they cover, and what the README says they cover.

The quoted line in each finding is a line from a file, with its path. You see this checkout only; a finding about a service, a host or another repository is a question, not a fact.

## Attack it on these axes, in priority order

**1. Documents that are wrong.** A README, `CLAUDE.md` or rule claim the code contradicts: a command that does not exist, a default that differs, a file that is gone. Quote the claim and the code.

**2. Rules the code breaks.** A rule in `CLAUDE.md` or `.claude/rules/` that a file in the tree does not keep. Name the rule, the file and the line.

**3. Failure handling.** A failed probe, read or write that the code treats as a negative answer, a pass or exit 0. Name the call and what the caller does with its failure.

**4. Tests that do not test.** A skipped or unwired test, a test that cannot fail, or a feature the README promises that no test exercises.

**5. Secrets and personal values.** A credential, token, email address, hostname or private path in a tracked file. Quote only enough to locate it; never repeat a secret in full.

**6. Weight.** A duplicated mechanism, dead code or a second way to do one thing that the history shows nobody uses. Name it; do not propose the replacement.

Give each hypothesis a `###` heading of its own: its rank and a short title, such as `### 1. The done check cannot fail`. After its four parts, add three kinds of line, each at the start of a line:

- `Recommendation:` one sentence, the action a person would take if the hypothesis holds, short enough to be a task title.
- `Evidence:` one file path and line, or one quoted line, that you read; at most five, one per line.
- `Repo:` the repository the fix belongs in, as a path, only when it is not the one you are reading.

The dashboard files each hypothesis as a recommendation, and the operator picks which become tasks.

After the hypotheses, add three short sections with no `###` heading and no confidence tag: `## Patterns`, one line per mistake repeated in several places, with its hypothesis numbers; `## Missing`, one line per thing a project like this needs and this one lacks; `## Fix first`, up to 10 hypothesis numbers, most impact for least effort first, with one reason each.

## Ending

End with one line: can the next maintainer trust this repository's documents and tests, YES or NO. If NO, name the single hypothesis that breaks that trust.

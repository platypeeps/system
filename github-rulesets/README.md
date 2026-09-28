# github-rulesets

The repository's GitHub rulesets, kept as code. Each ruleset is one JSON file
under [`.github/rulesets/`](../.github/rulesets/), so a change to branch
protection is a reviewed diff, and a fresh repository can be given the same
rules in one command.

## Usage

```sh
github-rulesets/github-rulesets.sh diff   OWNER/REPO            # compare, GET only
github-rulesets/github-rulesets.sh apply  OWNER/REPO            # print the planned calls
github-rulesets/github-rulesets.sh apply  OWNER/REPO --apply    # make them
github-rulesets/github-rulesets.sh export OWNER/REPO --dir DIR  # live rulesets as files, GET only
github-rulesets/github-rulesets.sh test
```

All calls go through `gh api`, so `gh` must be authenticated for the target.
`diff` exits 0 when every file matches a live ruleset of the same name and no
live ruleset lacks a file, 1 on any drift, and 2 when it cannot read.

`apply` is a dry run unless given `--apply`. It creates (`POST`) a ruleset that
has a file but no live counterpart and updates (`PUT`) one that differs. It
never deletes: a live ruleset with no file is reported as `extra` and left
alone, because removing protection should be a deliberate act in the web UI or
a hand-made call.

## The files

A file holds only the fields a create or update takes: `name`, `target`,
`enforcement`, `conditions`, `rules` and `bypass_actors`. Ids, node ids,
timestamps, `_links` and `source` are what GitHub adds on a read, and are
dropped. Rulesets match by `name`; the file name is only a slug of it.

The repository is public, so no file names a person. `export` drops bypass
actors of type `User` or `Team` and names each on stderr. Roles, apps and
deploy keys stay. An `integration_id` of 15368 on a required check is the
GitHub Actions app, which pins the check to Actions rather than to any app
that posts a status of the same name.

The required-check contexts must be job names the workflows produce, or
`sd/local-gate`, the status `sd-ship merge` posts after `make check` passes at
the head. `tests/test_rulesets.py` expands the `system-native` matrix and fails
on any other context. `sd/local-gate` carries no `integration_id`: the
maintainer's account posts it, not an app.

The two rulesets:

- `main integrity` — no force push and no deletion of the default branch; no
  bypass.
- `main required checks` — a pull request (0 approvals, since one maintainer
  cannot approve their own pull request) and the check `sd/local-gate`; no
  bypass. Strict (up-to-date) checks are on, so the gate always ran on the
  base a pull request lands on.

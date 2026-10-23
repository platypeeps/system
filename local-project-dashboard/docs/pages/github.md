# GitHub

GitHub is at `/github` (sd:5781), in the rail's Work group, in three views.
They were Today's GitHub and Issues views until sd:5781; an old
`/today#view=github` or `#view=issues` address opens here with its query.

Pull requests (sd:5608) lists the operator's open pull requests, and those
merged or closed in the last 7 days, from `GET /api/today/github`
(`today_github.document`), which reads only what `sd shadow sync` and the
Contributions collector stored. It is one table, 25 rows a page (sd:5899).
Show mine, the default, keeps rows assigned to the operator or opened by
them; Show all adds the rest. State open, the default, hides merged and
closed rows; with closed shows them. A pull request row opens its Details:
Open on GitHub, Open item, Adversarial review, Copilot review, Merge
(sd:5668) and Close original (sd:5669), each through the routes the README
names.

Your issues (sd:4628) lists each repo's open Jira and GitHub issues assigned
to or filed by the operator, and their GitHub issues in other repos, which
Pull requests listed until sd:5899. Its State filter adds their GitHub
issues closed in the last 7 days; closed Jira issues are not read. Intake (sd:4840) lists each `github_issues`
repo's open third-party issues no item carries; File as item posts
`/api/items` with the issue's `github:<owner>/<repo>#N` ref. Both are one read,
`GET /api/github/issues` (`issues.document`, `issues.intake`), made when either
view first shows and on Read again. A failed read is a row naming it, never an
empty list. An issue row of either view opens its Details (sd:5877): key,
title, state, tracker, assignee, reporter, opened and updated, and the item
filed under its ref, from the `shadow` row `sd shadow sync` stored; no tracker
is called. The sync stores no labels, priority or description, and Details
say so. Its actions are Open in Jira or Open on GitHub, and Open item; an
Intake row keeps File as item.

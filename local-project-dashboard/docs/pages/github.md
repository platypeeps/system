# GitHub

GitHub is at `/github` (sd:5781), in the rail's Work group, in three views.
They were Today's GitHub and Issues views until sd:5781; an old
`/today#view=github` or `#view=issues` address opens here with its query.

Pull requests (sd:5608) lists the operator's open pull requests and issues,
and those merged or closed in the last 7 days, from `GET /api/today/github`
(`today_github.document`), which reads only what `sd shadow sync` and the
Contributions collector stored. A pull request row opens its Details: Open on
GitHub, Open item, Adversarial review, Copilot review, Merge (sd:5668) and
Close original (sd:5669), each through the routes the README names.

Your issues (sd:4628) lists each repo's open Jira and GitHub issues assigned
to or filed by the operator. Intake (sd:4840) lists each `github_issues`
repo's open third-party issues no item carries; File as item posts
`/api/items` with the issue's `github:<owner>/<repo>#N` ref. Both are one read,
`GET /api/github/issues` (`issues.document`, `issues.intake`), made when either
view first shows and on Read again. A failed read is a row naming it, never an
empty list.

---
title: an HTML report convention, from the site reports instance
created: 2026-09-19
item: sd:1097
---
# Design — an HTML report convention

## Shape

One document, `local-project-dashboard/docs/html-reports.md`, plus a decision
recorded in it on the tracked-or-ignored question.

## The tracked-or-ignored question

Three options, and the convention must pick one:

| Option | Cost |
|---|---|
| Track the generated HTML, ban date-derived text | Reports lose "starts in 5 days", which is the phrasing a reader actually understands |
| Track it, accept the churn | Every working day produces a diff that says nothing; `git status` stops meaning anything |
| Ignore the generated HTML, build on demand | The dashboard and the mailer must both build before reading, and a report cannot be reviewed in a pull request |

**Decided by the operator, 2026-09-23 (note on sd:1097), superseded:** track
it, ban date-derived text in tracked output, and let the mailer compute
relative phrasing at send time. The first draft of the convention was written
to that decision. It also banned live-store reads (`sd store`) from tracked
output, an extension made by the assistant for the same reason, churn.

**Decided by the operator, 2026-09-24, in force: the third option.** The
operator's global rule for generated HTML wins. Reports go to
`<repo>/docs/dashboard/`, which is gitignored, and are built on demand or by
a scheduled job. This supersedes the 2026-09-23 decision.

- **The date ban is lifted, for its own reason.** It existed because tracked
  output churned in git. Untracked output has no diff to churn. The
  live-store extension goes with it, for the same reason.
- **What an untracked page needs instead is a build stamp.** A page on disk
  can be days old, and relative text in it is true on its build day only.
  Each page states the date and time it was built, at the top, so a stale
  page is visible. Relative text is computed against that same instant.
- **The third option's costs, as they fall.** The dashboard reads the last
  build and does not build; the stamp says how old it is. The mailer already
  read its inputs on the send day, so it builds nothing new. A report is not
  reviewed in a pull request; its builder is.

## Where the files live

`<repo>/docs/dashboard/<name>.md` and `<repo>/docs/dashboard/<name>.html`,
gitignored. The dashboard finds any checkout under `REPO_ROOT` (default
`~/repos`, at the root or one group deep) that holds `docs/dashboard`, so the
folder needs no line in `documents.conf`. A `label|<key>|<label>` line renames
a found root. `site`'s `root|` line into its tracked `reports/` becomes
`label|site|site`.

The 2026-09-23 pages named a conflict here, between the tracked decision and
the global rule, and left it to the operator. The 2026-09-24 decision settles
it: one location, `docs/dashboard/`, for reports and for everything else the
global rule names.

## Interfaces

A domain repo supplies a script that writes `docs/dashboard/<name>.md` and
`docs/dashboard/<name>.html`, and a `docs/dashboard/` line in its
`.gitignore`. Within a found root the dashboard lists the regular `.html`
files on disk rather than naming them: a list of documents drifts from the
directory beside it.

A report is a Documents entry, not a Resources view. A Resources view runs a
tile and passes its output through the markup filter, which drops the
stylesheet and the SVG. The Documents screen serves the page whole.

The mailer computes relative text for the mail from its own inputs on the send
day. For `site` that is `tools/morning-digest.py`, whose output is not tracked.

## Rejected

A shared chart library, for now. See the PRD's out-of-scope note.

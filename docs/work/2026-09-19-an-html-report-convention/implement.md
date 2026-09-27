---
title: an HTML report convention, from the site reports instance
created: 2026-09-19
item: sd:1097
---
# Implement — an HTML report convention

## Steps

1. Create `local-project-dashboard/docs/`. It does not exist.
2. Read `site`'s report builder and list the decisions it makes that are not
   forced by the problem.
3. Settle the tracked-or-ignored question and record it with reasons.
4. Write `html-reports.md`.
5. Cross-link from `local-project-dashboard/README.md`.
6. Rewrite to the 2026-09-24 decision: reports in gitignored
   `docs/dashboard/`, the date ban lifted, a build stamp required. Change
   `site`'s `documents.conf` line from `root|` to `label|`, and drop the
   `site` example from the dashboard README and from `documents.py` comments.

## Verification

- The document answers, without reading any source: why is the mailed HTML
  styled inline, and why does the markdown twin exist?
- `sd-docs-lint` stays clean from this repository's root.

## BLOCKING

None.

## Log

2026-09-19 — filed. The instance is thirteen generated files in the `site`
repo, in use and mailed.

2026-09-23 — the operator decided the tracked-or-ignored question (note on
sd:1097): tracked, with date-derived text banned and computed by the mailer.
Wrote `local-project-dashboard/docs/html-reports.md` to that decision and
linked it from `local-project-dashboard/README.md`. Measured `site`: fourteen
tracked files, all with a `Generated 2026-09-21` stamp, and the brief also
carries a countdown, relative due text, an overdue count, a `TODAY` timeline
marker and live `sd store` rows. `site` is not changed here; the fix is a
follow-up row. The convention names a conflict with the operator's global
`docs/dashboard/` rule and leaves it for the operator.

2026-09-24 — the operator settled the conflict the 2026-09-23 pages named:
the global rule wins. Generated HTML reports go to gitignored
`<repo>/docs/dashboard/`, built on demand or by a scheduled job, and the
dashboard finds them with no line. This supersedes the 2026-09-23 decision
(note on sd:1097) that reports are tracked. The date-derived-text ban and its
live-store extension are lifted, because both existed to stop tracked output
churning in git. Each page now states when it was built, so a stale page is
visible. Rewrote `html-reports.md` to that decision. `documents.conf` now
carries `label|site|site` in place of the `root|` line into
`reports/`, so the Documents screen lists no `site` root until `site`
builds into `docs/dashboard/`. The `site` follow-up row changes from
stripping date text to moving its output and removing the tracked copies.

---
title: an HTML report convention, from the site reports instance
created: 2026-09-19
item: sd:1097
---
# PRD — an HTML report convention

## Problem

The fold-in PRD names this as F4:

> An HTML report convention: markdown plus inline-SVG HTML from a domain
> repo's own script, mailed by `local-notify`, mountable as a dashboard
> Resources view.

with the rationale:

> The four SVG chart functions are bound to dashboard row shapes and cannot be
> called from outside.

That rationale is the whole item. `local-project-dashboard` can draw charts.
It draws them from its own row shapes, so a domain repo that wants a chart
cannot ask for one — it must write its own. The `site` repo did exactly that:
`tools/build-reports.py` emits thirteen files, each report as a markdown twin
and an HTML page with inline SVG, and the HTML is mailed by `local-notify` and
mounted in the dashboard's Documents screen.

So the instance exists and works. What is missing is the convention that makes
the next domain repo's reports arrive the same way instead of inventing a
fourth chart style.

## What the instance already settled

Three decisions worth keeping, each of which cost something:

1. **Markdown and HTML are twins, and the markdown is what publishes.** Drive
   turns markdown into a real Doc with headings and tables; an HTML page
   arrives as flattened text with its stylesheet dropped. The `site` publish
   tooling refuses `.html` as a source for this reason.
2. **Inline styles only in the mailed HTML.** Gmail strips a `<style>` block in
   a forwarded mail, so a report that looks right in a browser and wrong in the
   one place it is read is the default outcome, not an edge case.
3. **Inline SVG, no chart library.** The reports are mailed and archived. A
   chart that needs a script to render is a blank rectangle in both.

## What was not settled, and the decision

**Date-dependent output dirties the repository.** `build-reports.py` renders
`starts in 5 days` from today's date and a count from the live store, so the
generated HTML changes on any day the calendar advances or the store moves,
whether or not a fact changed. The convention had to say whether generated
reports are tracked, and if they are, whether date-derived text is allowed in
them.

**Operator decision, 2026-09-23 (note on sd:1097), superseded on
2026-09-24:** generated reports are tracked, and date-derived text is banned
from them; the mailer computes such text at send time. That decision
conflicted with the operator's global rule for generated HTML, which sends
reports to `<repo>/docs/dashboard/`, gitignored. The 2026-09-23 pages named
the conflict and left it open.

**Operator decision, 2026-09-24, in force: the global rule wins.** Generated
HTML reports go to `<repo>/docs/dashboard/`, which is gitignored, and are
built on demand or by a scheduled job. The sd dashboard discovers any
`docs/dashboard` under `~/repos`, so no `documents.conf` line registers
one. The date-derived-text ban existed because tracked output churned in git.
Untracked output does not churn, so the ban is lifted for that reason. What
an untracked page needs instead is a build stamp: each page states when it
was built, so a stale page is visible.

Measured 2026-09-23, all fourteen tracked files in `site/reports/` carry a
`Generated <date>` stamp in their footer. Under the 2026-09-24 decision that
stamp is close to what a page needs. What `site` breaks is the location: its
reports are tracked in `reports/`. Moving them is a change in that repository
and gets its own row.

## Acceptance criteria

The deliverable is `local-project-dashboard/docs/html-reports.md`. Each
criterion names the check that settles it; commands run from this
repository's root.

- **It exists and covers the five topics.** It has a section for each of:
  the location decision and the build stamp, where generated files live, the
  markdown twin, styles, and inline SVG. Fail: a topic has no section.
- **The decision is stated as the operator made it.** The document states
  that reports go to `<repo>/docs/dashboard/`, gitignored, built on demand or
  by a scheduled job, and found by the dashboard. It states that the
  date-derived-text ban is lifted because untracked output does not churn,
  and that each page states when it was built. Fail: any of these is missing
  or softened.
- **The location rule is checkable on a real repository.** The document gives
  a check that nothing under `docs/dashboard` is tracked, one that the
  folder is ignored, and a grep for the build stamp in each page. Fail: any
  of the three is missing.
- **The superseded decision is recorded, not erased.** The document and
  design.md both name the 2026-09-23 decision, say that the 2026-09-24
  decision supersedes it, and say why the ban goes. Fail: either page omits
  it.
- **The Documents route is concrete.** The document says that
  `docs/dashboard` is found with no line, and gives the `label|` line shape,
  the served address `/documents/<key>/<name>.html`, the file-name pattern,
  and why a report is not a Resources view. A reader can list the steps
  without opening the dashboard's source. Fail: any of the five missing.
- **`site` is the reference instance, with its current breaches listed.** Fail:
  the document names `site` without saying it breaks the location rule today.
- **Cross-linked.** `grep -c 'docs/html-reports.md'
  local-project-dashboard/README.md` prints at least 1. Fail: 0.
- **The gates stay green.** `JEV_SD_DOCS_LINT=0 <pack>/bin/sd-docs-lint`
  prints `sd-docs-lint: clean`, and `python3 tests/test_citations.py` passes.

## Out of scope

Extracting the dashboard's four SVG chart functions into a callable library.
That is the obvious next thought and it is a separate item: it needs a stable
input shape, and two instances is too few to know what that shape is.

Changing `site`. This item writes the convention; bringing `site`'s output into
line with it is a follow-up row against that repository. This item does
change `site`'s line in `local-project-dashboard/documents.conf`, from `root|`
into `reports/` to `label|`, because that file lives here.

## Not verified

That a second domain repo would find the convention sufficient. There is no
second domain repo yet.

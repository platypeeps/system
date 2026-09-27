# The HTML report convention

A domain repository publishes a report as two files from its own script: a
markdown twin and a standalone HTML page with inline SVG charts. Both go into
`<repo>/docs/dashboard/`, which is gitignored. A build writes them, on demand
or from a scheduled job. The dashboard's Documents screen finds the folder on
disk and serves the HTML whole. Every page states when it was built.

The operator's first domain repository to publish reports is the reference
instance: a `tools/build-reports.py` that writes several stems, the index
`README` included.

Written from item `sd:1097`.

## Untracked, and it says when it was built

**Operator decision, 2026-09-24: generated HTML reports go to
`<repo>/docs/dashboard/`, which is gitignored. The sd dashboard finds any
`docs/dashboard` under `~/repos`.** The operator's global rule for generated
HTML wins. This supersedes the decision of 2026-09-23 (note on sd:1097) that
reports are tracked in git with date-derived text banned from them.

- **Untracked.** A build writes the files and git never holds them. A page is
  updated by running the builder again, not by a commit. Review the builder
  in a pull request; its output is not reviewed.
- **Built on demand or by a scheduled job.** The builder runs when somebody
  wants a current page, or from a job on a schedule. Nothing reads a report
  that no build has written.
- **The date-derived-text ban is lifted.** It existed because tracked output
  churned in git. Text computed from today's date changed the file every day
  the calendar moved, and the diff then said nothing. Untracked output has no
  diff, so the reason is gone. A report may say `starts in 5 days` or
  `3 days late`, count what is overdue, and place a `TODAY` marker on a
  timeline.
- **The live-store ban is lifted with it.** The 2026-09-23 page also kept
  reads from a live store, such as `sd store items --open --json`, out of the
  tracked writer. That extension had the same reason, churn, and it goes for
  the same reason.
- **What an untracked page needs instead: a build stamp.** A page read from
  disk can be days old, and relative text in it is true on its build day
  only. So each page, HTML and markdown twin alike, states the date and time
  it was built, at the top, as an absolute value:
  `Built 2026-09-24 07:15 -0700`. A reader then sees a stale page as stale.
  Compute every relative phrase and every count against the same instant the
  stamp names, never against a second reading of the clock.
- **Absolute dates are facts, and they stay.** A due date, a window start, a
  meeting date: each comes from an input, not from the clock.

**Check.** From the domain repository's root:

- `git ls-files docs/dashboard` prints nothing. Any line fails.
- `git check-ignore -q docs/dashboard/<name>.html` exits 0. Exit 1 fails.
- `grep -c 'Built [0-9]\{4\}-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]'`
  prints at least 1 for each built `.html` and `.md` file. A 0 fails.

## Where the files live

`<repo>/docs/dashboard/<name>.md` and `<repo>/docs/dashboard/<name>.html`. One
stem, two extensions. The script that writes them lives in `<repo>/tools/`. A
repository without the folder gets one, and a `docs/dashboard/` line in its
`.gitignore`.

**No line registers the folder.** The Documents screen finds every checkout
under `REPO_ROOT` (default `~/repos`, at the root or one group deep) that
holds `docs/dashboard`. The key is the checkout's directory name, and the
folder must resolve inside that checkout. A symlink out of it is refused.

`local-project-dashboard/documents.conf` still says what a directory cannot.
A `label|<key>|<label>` line names a found root better than its directory
name does, for example `label|mcp-research|MCP`. A
`root|<key>|<label>|<directory>` line is for a root that lives outside any
checkout's `docs/dashboard`. A report built to this convention never needs
one. The vault is the one such root today, because it is not a checkout under
`~/repos`: `root|vault|Vault|~/Documents/My Vault/docs/dashboard`.
`documents.conf` is gitignored; copy `documents.conf.example` to start one.

## The markdown twin publishes

**Each report is a markdown file and an HTML file with the same stem.** The
markdown is what publishes to Drive. Drive turns markdown into a real Doc with
headings and tables. An HTML page arrives there as flattened text with its
stylesheet dropped. The reference instance's publish tooling refuses `.html`
as a source for this reason.

**Assemble the facts once, render them twice.** Two renderers that compute
their own numbers will one day disagree about a date, a count or an owner.
The reference instance's `brief_data` builds one dictionary of values, and the markdown and HTML
renderers only format it. Keep prose as markdown inside that dictionary: both
renderers can take markdown, only one can take HTML.

**Links between reports** are written to `<name>.md` in the markdown and
rewritten to `<name>.html` in the page.

## Styles: a `<style>` block when served, inline when mailed

**A served page may carry one `<style>` block in its head.** The Documents
screen serves a report under its own policy,
`default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:`.
That permits an inline stylesheet and nothing fetched from elsewhere. So: no
`<link rel="stylesheet">`, no web font by URL, no image by URL, no script.

**Mailed HTML uses `style=""` attributes only.** Gmail strips a `<style>` block
in a forwarded mail. A report that looks right in a browser and wrong in the
one place it is read is the default outcome, not an edge case.

The reference instance meets both. Its report pages use a `<style>` block and
are served, not mailed. What it mails is its morning digest, which is
inline-only.

**Check.** For each HTML body passed to `local-notify -f html`,
`grep -c '<style'` prints 0.

## Charts: inline SVG, no library

**Draw every chart as inline `<svg>` in the page.** A report is served,
archived, and sometimes mailed. The Documents policy runs no script, and
neither does a mail client. A chart that needs a script to render is a blank
rectangle in all three places.

- Give each chart a `viewBox`, `role="img"` and a text label, so it scales and
  reads without the picture.
- An image that is not SVG is a `data:` URI; the policy refuses anything else.
- A colour taken from a CSS variable works in the served page only. A chart
  that is also mailed spells its colours out.

The dashboard's own four SVG chart functions are bound to its row shapes and
cannot be called from a domain repository. Write the chart in the domain
repository's builder. Extracting a callable chart library is a separate item.

## How a report becomes a Documents entry

1. The builder writes `docs/dashboard/<name>.md` and
   `docs/dashboard/<name>.html`. The file name must match
   `[A-Za-z0-9][A-Za-z0-9._-]*\.html`; the server refuses any other name with
   a 404.
2. `docs/dashboard/` is in the repository's `.gitignore`. Commit nothing the
   build wrote.
3. Optionally, add a `label|` line to `local-project-dashboard/documents.conf`,
   once per repository, when the directory name reads badly.
4. Open the dashboard's Documents screen. The page is listed under the
   checkout's name or its label and served at `/documents/<key>/<name>.html`,
   unchanged. Only regular `.html` files are served; the markdown twin is not.

**A report is a Documents entry, not a Resources view.** A Resources view runs
a tile through `sd_tile.py`, and the dashboard passes its output through a
markup filter that keeps headings, paragraphs, tables, lists and links. That
filter drops the stylesheet and the SVG, which are the point of a report. The
Documents screen hands the finished page over instead.

## The builder

- One script in the domain repository, standard library only.
- It fails with a named error on a missing column or a malformed row, rather
  than writing a report from half the data.
- It writes into `docs/dashboard/` and nowhere else in the repository.
- Every report starts with its build stamp and ends with a provenance line:
  the script and its input files, and the words "Do not hand-edit; rerun the
  script." Git records nothing for an untracked file, so the stamp is the only
  record of when the page was true.

## The mailer

- It reads the same inputs on the day it sends, not a built page. A built
  page's relative text is true on its build day, and the mail is read on the
  send day.
- It computes every relative phrase and every count measured against today.
- It uses inline styles only, and sends through `local-notify -c email -f html`.
- Its output is not tracked. Write the digest under `~/.local/share/<name>/`,
  outside the repository, and mail it from there.

## Checklist for a new domain repository

1. A builder in `tools/` writing `docs/dashboard/<name>.md` and
   `docs/dashboard/<name>.html`.
2. `docs/dashboard/` in `.gitignore`; `git ls-files docs/dashboard` prints
   nothing.
3. Facts assembled once and rendered twice.
4. A build stamp at the top of each page, and relative text computed against
   that instant.
5. Charts as inline SVG; no script, no external resource.
6. The builder run on demand or by a scheduled job.
7. A `label|` line in `documents.conf` only if the directory name reads badly.
8. Mailed HTML computed on the send day and inline-styled.

## The reference instance

The reference instance predates the location rule: it tracks its output in a
`reports/` folder and stamps the build date at the bottom, without a time.
Until it builds into `docs/dashboard/`, the Documents screen lists no root for
it. Moving the output, removing the tracked copies and moving the stamp to the
top is a change in that repository.

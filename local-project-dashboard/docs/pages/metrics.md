# Metrics

Metrics is at `/metrics` (sd:2119): spend against budget, the week, the
provider scorecard, skill use, the month's usage and age in status.
`GET /api/metrics` (`metrics_screen.document`) reads only sd.db and writes
nothing.

Dollars are the month to date, as `sd usage` reads them (`usage.read`), so the
page, the classic Usage screen and the CLI agree. The design asked for dollars
this week by bill and provider; a week of spend would be a second computation
of the same ledger, so the bars show the month. The window trend is the last
7-day-window meter reading of each day for a week (`reads.meter_days`). Skill
use counts four Monday-start weeks (`reads.skill_use_days`).

Each part is guarded on its own: one that fails is unknown with its reason,
and the page reports a partial read. Three panels have no reader and always
show as unknown with the reason: by model (the cost ledger has no model
column), CI health (Actions is off and no local-gate run is stored), and flaky
tests (no test-result store).

Gaps from the design: no CI table, and the age chart is the design's bar list
at every width. Configure and Cap do not write: Configure is a CLI line to
copy, and Cap opens the classic Usage screen, which keeps the cap form.

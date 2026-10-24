# Metrics

Metrics is at `/metrics` (sd:2119): spend against budget, the week, the
provider scorecard, the month's usage and age in status. Skill use moved to
the Skills page's Usage view (sd:5881); an old `/metrics#view=skills`
address opens it there.
`GET /api/metrics` (`metrics_screen.document`) reads sd.db and the provider
registry beside it, and writes nothing.

Dollars are the month to date, as `sd usage` reads them (`usage.read`), so the
page, the classic Usage screen and the CLI agree. The design asked for dollars
this week by bill and provider; a week of spend would be a second computation
of the same ledger, so the bars show the month. The window trend is the last
7-day-window meter reading of each day for a week (`reads.meter_days`).

Each part is guarded on its own: one that fails is unknown with its reason,
and the page reports a partial read. By model has no reader and always shows
as unknown with the reason: the cost ledger has no model column.

CI health and Flaky tests read the merge lane's gate runs (sd:5473): the last
checkpoint of each lane entry (`reads.lane_entries`) that ended in the last 7
days. Merged or prepared passed the gate. A failed entry is a gate failure
when its reason is sd-ship's gate refusal; a review stop or a conflict ran no
gate and counts as stopped. A flaky head failed its gate and later passed at
that same head in the same repository. Flakiness is per head, not per test:
no store names the failing test.

Configure and Cap write (sd:3387). They post the routes the classic Usage
forms post, `/api/providers/configure` and `/api/bills/<bill>/cap`, with the
registry revision the page read; the library refuses it once the registry, a
cap or the order policy changed. Cap asks for the new number and takes `none`
to clear. A bill a `start` entry is billed to takes no cap, except the clear of
a legacy row cap, which has no Undo. Configure asks for the enabled providers
and both orders; a blank field keeps its value. A 400 or 409 wrote nothing;
any other failure, or no answer, shows in Details as unknown. Both reread the
page after every answer.

Gaps from the design: the CI table has no per-day bars, and the age chart is the design's bar list
at every width.

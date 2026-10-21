# Review

Review is at `/review` (sd:3709), in two views (sd:5497).

Questions lists the decisions that wait on the operator: each open
`question` note with its `Option:` lines, the rows Today's Decisions lists.
`GET /api/decisions` (`decisions.document`) reads them, and `decisions.js`
draws them on both pages: the recommendation in words, a filled button that
takes it, then each option as written. A choice posts
`/api/decisions/answer`, which writes the ruling and resolves the question,
as `sd task note N "<ruling>" --kind decision --answers <note>` does.

Bulk scoring is the labelling queue. A batch is a task filed by
`sd-db review file`; its questions are question notes, and a label is the
Decisions ruling. `GET /api/review` (`review_screen.document`) lists the open
batches, nightly suggestions first, then by age, with each open question. A
blind or sealed batch sends only the question and its options. The rows band
by batch, one fold each. Each open row shows its recommendation (the
`Proposed label:` evidence line), one button per option and a checkbox. The
bar over the table selects all or clears, and labels every selected row with
an option they all offer, or each with its own recommendation. A label posts
`/api/review/rule` under the batch revision the page read, one per question,
so a batch that changed is refused (409) and gets no more writes that run; the
toast and each row name what did not land. Its Undo posts
`/api/review/withdraw`. `GET /api/review/<item>/export` downloads a batch's
labels, the lines `sd-db review export` writes. Every read and write is
`sd_db`'s, the code the CLI verbs run, so the page adds no store and no second
writer. The design is the repository's
`docs/work/2026-10-12-review-queue/design.md`.

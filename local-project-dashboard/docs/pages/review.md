# Review

Review is at `/review` (sd:3709): the labelling queue, one question at a
time. A batch is a task filed by `sd-db review file`; its questions are
question notes, and a label is the Decisions ruling. `GET /api/review`
(`review_screen.document`) lists the open batches, nightly suggestions first,
then by age, with each open question. A blind or sealed batch sends only the
question and its options. A label posts `/api/review/rule` under the batch
revision the page read, so a batch that changed is refused (409); its Undo
posts `/api/review/withdraw`. `GET /api/review/<item>/export` downloads a
batch's labels, the lines `sd-db review export` writes. Every read and write is
`sd_db.review`'s, the code the CLI verb runs, so the page adds no store and no
second writer. The design is the repository's
`docs/work/2026-10-12-review-queue/design.md`.

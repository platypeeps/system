# Designs

Designs is at `/designs` (sd:2126): the design's lamps, filter and ledger
over every drawn page in the ui-design checkout, grouped by product, plus one
row per product with a brief and no page. `GET /api/designs`
(`designs.ledger`) is the document the design's `data/designs-data.js` holds,
read live: each page's kind, title, size and last commit, its screenshots and
which of them are stale, and whether the working tree changed it. Details
opens the page and its 1440 px screenshot at `/designs/<path>`, where the tab
serves them sandboxed, as before. History, Retake screenshots and Read brief
are copy only: the dashboard runs no git, no `shots.mjs` and no pager.
Observed rereads `/api/designs`. The page reads through the shell's reader,
`read.js`. The old listing moved to `/classic/designs`.

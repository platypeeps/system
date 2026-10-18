# Research

Research is at `/research` (sd:2122): every checkout under `REPO_ROOT`, one
group deep, that carries a `research.conf.py`. `GET /api/research`
(`research_screen.document`) runs `collectors.collect_research` in a child
under a five-second budget. Each row shows the checkout's stage (the numbered
directories that hold Markdown), its render freshness and its last commit. A
config the collector refuses shows as unknown with the refusal.
`GET /api/research/<checkout>` (`research_screen.sources`) reads that
checkout's ledger: the Markdown tables in `SOURCES.md`,
`10-sources/registry.md` and `10-sources/references.md`, with or without
outer pipes (sd:2414), up to 60 rows with the whole count, cited rows first. Any other path is a 404.
The same document carries the claim map (sd:5064): each sentence in `20-map`, `30-brief` and `40-docs` that links a
ledger row's URL, matched without its fragment or trailing slash. A claim is supported when it cites an A, B or M row,
and unverified when it cites only C, U or ungraded rows. A document the reader skips (a link, over 2 MB, past the
200-document bound or the deadline) is named with its reason. Nothing reads review rounds yet, so they show as unknown
with that reason. Render and Review are
copy only: the dashboard does not run `sd-research-kit`. Start research is
off with its reason; the field shows the `sd task add` line for
Copy. The old view stays in the palette as Resources (classic), with Toolbox,
Vault and Queues. The board reads through the
shell's reader, `read.js` (sd:2491): only the newest read draws, a project
the read no longer lists runs no command, and a failed read clears the board
and the ledger. The child's deadline counts from the page's start (sd:2501).
The page has three views on the shell's view switch (sd:5064): Projects (the board, the default), Sources and Claim
map. The selected project carries to the other two; with none selected they link back to Projects. A tapped claim opens
Sources with its sources lit, and a source's ◆ count opens Claim map with its claims lit.

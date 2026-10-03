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
outer pipes (sd:2414), up to 60 rows with the whole count. Any other path is a 404. Nothing reads review rounds or
claims yet, so both show as unknown with that reason. Render and Review are
copy only: the dashboard does not run `sd-research-kit`. Start research is
off with its reason; the field shows the `sd task add` and `sd run` lines for
Copy. The old view stays in the palette as Resources (classic), with Toolbox,
Briefs, Vault and Queues. The board reads through the
shell's reader, `read.js` (sd:2491): only the newest read draws, a project
the read no longer lists runs no command, and a failed read clears the board
and the ledger. The child's deadline counts from the page's start (sd:2501).

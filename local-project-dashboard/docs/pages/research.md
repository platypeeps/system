# Research

Research is at `/research` (sd:2122): every checkout under `REPO_ROOT`, one
group deep, that carries a `research.conf.py`. `GET /api/research`
(`research_screen.document`) runs `collectors.collect_research` in a child
under a five-second budget. Each row shows the checkout's stage (the numbered
directories that hold Markdown), its render freshness and its last commit. A
config the collector cannot read without running it is read from the
`docs/dashboard/research.json` its last render wrote (sd:5659); a manifest of an
older config shows as caution, render again to refresh. With no manifest the
row shows as unknown with the refusal.
`GET /api/research/<checkout>` (`research_screen.sources`) reads that
checkout's ledger: the Markdown tables in `SOURCES.md`,
`10-sources/registry.md` and `10-sources/references.md`, with or without
outer pipes (sd:2414), up to 60 rows with the whole count, cited rows first. Any other path is a 404.
The same document carries the claim map (sd:5064): each sentence in `20-map`, `30-brief` and `40-docs` that cites a
ledger row. It cites a row by linking its URL (without fragment or trailing slash) or a deeper path under it on the
same host, where the longest row URL wins, or by naming the row's ID: a first-column word with a digit, such as `S12` or
`LOG-21201`, matched whole. A claim is supported when it cites an A, B or M row, and unverified when it cites only C, U
or ungraded rows; a ledger without grades leaves every claim unverified, and the page says so with the count. A document the reader skips (a link, over 2 MB, past the
200-document bound or the deadline) is named with its reason. Nothing reads review rounds yet, so they show as unknown
with that reason. Render and Review are
copy only: the dashboard does not run `sd-research-kit`. Start research is
off with its reason; the field shows the `sd task add` line for
Copy. The old view stays in the palette as Resources (classic), with Toolbox,
Vault and Queues. The board reads through the
shell's reader, `read.js` (sd:2491): only the newest read draws, a project
the read no longer lists runs no command, and a failed read clears the board
and the ledger. The child's deadline counts from the page's start (sd:2501).
The page has four views on the shell's view switch (sd:5064): Projects (the board, the default), Sources, Claim
map and Experiments. The selected project carries across them; with none selected Sources and Claim map link back to Projects. A tapped claim opens
Sources with its sources lit, and a source's ◆ count opens Claim map with its claims lit.
A project's Details hold its Experiment (sd:5296, `research_experiments.py`): the row of the team's experiments database
in the work Notion that the checkout's repo declaration of kind `experiment` names. `GET /api/research/experiments?project=<key>`
reads the link, the cached list, the runs and any setting that blocks one. Refresh experiments, the three drafts and
Apply draft post to `/api/research/experiments/act`; each outward read or write is a headless `claude -p` run with an exact allowlist, one at a time.
The drafts write nothing outward. Apply asks first, shows the draft it writes, and posts the draft's SHA-256; the server
refuses it when the draft, the link, the data source or the deck's account changed since. Attach writes the workflow
database only. Draft deck needs `RESEARCH_BRAND_KIT`. The shape and the failure table are in
`docs/work/2026-10-19-research-notion-experiments/design.md`.
Experiments (pack sd:5525) is each checkout's status brief, `30-brief/BRIEF-status.md`, read with the board
(`research_screen.experiment`): the State word (Running, Decided or Done ●, Waiting ▲, Blocked ■, any other ▨), the
Updated day, stale past 7 days, the line, the first two asks with the count, the Experiment and Deck links, and whether
`30-brief/deck.json` sits beside it. A row's Build deck (icon `file-text`) posts `/api/research/deck` with the row's
checkout, which queues one `sd-research-kit deck` there in Publish now's run shape (`research_deck.py`): one at a time
behind a lock, a 409 while one builds, bounded at 300 s, off with its reason when the checkout keeps no deck.json. The
page reads `/api/research/deck/<id>` until the run settles, then reads the board again; the row links the deck it wrote,
`docs/dashboard/deck.pptx` in the checkout, through `/research/deck/<key>`, a download that needs the page's session.
`/api/research/deck/status` is the last run. Build deck uploads nothing.

Import to Slides (#25693, option A) posts `/api/research/slides` with the row's checkout (`research_slides.py`). It sends
the built deck as base64, with its sha256, to workspace-mcp's `import_to_google_slides` on the work Google account,
`google.accounts.work.email`. `<state>/research-deck/slides.json` records each checkout's Slides file id, link and deck
sha256. An unchanged deck reuses its file and calls nothing; a changed one makes a new file, because the tool cannot
replace a Slides file's content from bytes, and the earlier file stays in Drive. `/api/research/slides/status` probes
workspace-mcp, so the action is off with its reason when it does not answer, when no account is set, or when no deck is
built. The row then links the Slides file and says whether it holds the current deck or an older one.

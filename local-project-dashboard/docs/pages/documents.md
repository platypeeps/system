# Documents

Documents is at `/documents` (sd:2114): the design's facets, search and
paged ledger over every file `/documents/<key>/<file>` serves.
`GET /api/documents` (`documents_screen.document`) gives each file its title,
h1 and stand line, its kind and its render state. A checkout with
`research.conf.py` makes research documents, and every other root makes
reports. A research document is render-stale when its source Markdown changed
after the page. The source is the one `research.conf.py` names, else the one
`.md` with the page's name. Open document opens the served page. Render is
copy only. Request a document is copy only: the page files no item. Pin, hide
and tag are off: no document store exists yet. Disable render is off: no
render switch exists.

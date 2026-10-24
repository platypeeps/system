# Documents

Documents is at `/documents` (sd:2114): the design's facets, search and
paged ledger over every file `/documents/<key>/<file>` serves.
`GET /api/documents` (`documents_screen.document`) gives each file its title,
h1 and stand line, its kind and its render state. A checkout with
`research.conf.py` makes research documents, and every other root makes
reports. A research document is render-stale when its source Markdown changed
after the page. The source is the one `research.conf.py` names, else the one
`.md` with the page's name anywhere in the checkout, outside dot folders and
build, node_modules, dist and storage (sd:2417). A `research.conf.py` that
computes its data is read from the `docs/dashboard/research.json` its last
render wrote, as on Research (sd:5666); with no manifest, or one from an older
config, Details and the state slot say so and name `sd-research-kit render`.
Open document opens the served page. Render (sd:5884) runs
the render as Research's does: it posts `/api/research/render` with the
root's checkout key, one run at a time, and reads the list again when the run
ends. Request a document is copy only: the page files no item. Pin, hide
and tag (sd:3428) are the workflow database's `view` state, keyed
`<root key>/<file>`: each row carries `pinned`, `hidden` and `tags`, and each
command posts `/api/documents/view` with the fields it changes, offers Undo
and reads the list again. Tag asks for the tag in a field filled with the one
typed under Tags, else the last one applied; Enter in the Tags field tags the
shown document without asking, and the toast names the tag (sd:3523). Pinned files sort first; the Hidden filter (or
`is:hidden`, `?hidden=1`) shows the hidden files and only them. A view read
that fails hides nothing, turns the six commands off and says why in the
state slot. A root key's own `view` row names and hides the root (sd:3986):
Rename root (empty goes back to the derived name) and Hide root post
`{root, label | hidden}` with Undo. A hidden root leaves the Repo chips for a
Hidden roots fold with Unhide root, and its files leave the list; the server
still serves them. Details names a `label|` line documents.conf still holds,
which is not read. Disable render is off: no render switch exists. The page reads through the
shell's reader, `read.js` (sd:2490): only the newest read draws, a document
the read no longer lists runs no command, and a failed read clears the rows.
`?page=` takes a whole page number only (sd:2427).

A research document's row shows one icon per destination it is configured
for (sd:5102, sd:5300): Drive, Notion, Obsidian. A copy is filled and opens in
a new tab; a queued one has a dashed edge; a designated one with no copy is an
outline; one whose read failed is unknown and says why. The hover names the
destination and its link or vault path. Details' Publish to ticks Drive, Notion or Obsidian: it adds the key to
`research.conf.py` and queues through `sd-research-kit publish`, which writes an Obsidian copy at once. Unticking asks
first, queues the copy for the trash (sd:5489), then removes the key; Undo ticks it again. An Obsidian untick moves
the vault copy into the vault's `.trash` (sd:5500). In the vault's own repository Obsidian stays the repo's channels
setting, and the pane says so. Publish now drains that row's requests;
RUNTIME.md names the run.

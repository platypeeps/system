---
title: documents.conf skips one file, not only a whole root
created: 2026-09-28
---
# PRD — documents.conf skips one file

## Problem

The v2 Documents mockup offers Skip on one document and Skip repo on its root.
Both draft a proposal in chat, written as `render-skip|<key>|<file>` and
`render-skip|<key>` for `documents.conf` (ui-design, verb check of 2026-09-28).

Neither line exists. The configuration grammar is `label|`, `skip|<key>` and
`root|` (the file's own header, read 2026-09-28), and the reader accepts
`skip|<key>` only (`source:local-project-dashboard/sd_dashboard/documents.py::Config`).
A whole root can already be dropped under the real spelling; that is a mockup
fix in ui-design. One file inside a root cannot be dropped at all, so a stale
report keeps its place on the tab until someone deletes it from the checkout.

## Requirements

1. `skip|<key>|<file>` drops one file from a found root. The file part is the
   name relative to the root, and a path with a space parses, as the header's
   reason for pipes requires.
2. The header of `documents.conf` documents the three-part line beside the
   two-part one.
3. A `skip|<key>` line keeps dropping the whole root, and a three-part line on
   a skipped root changes nothing.
4. The dashboard's document listing and its serving both honour the line, so a
   skipped file is neither listed nor served.
5. The mockup's proposals use the real grammar: `skip|<key>` for the root and
   `skip|<key>|<file>` for one file; `designs/tools/collect-counts.mjs`
   regenerates `data/commands.js` (ui-design, `documents.html`).

## Acceptance criteria

- [ ] With `skip|reports|old.html` in the conf, the Documents tab lists the
      root and not `old.html`, and `GET` on the file returns 404.
- [ ] `skip|reports` alone still drops the whole root.
- [ ] A file name with a space in a three-part line parses to one file.
- [ ] `local-project-dashboard/tests/test_documents.py` covers the three cases.
- [ ] The mockup's Skip commands name `skip|` lines, and the ui-design Commands
      page shows no `render-skip` text.

## References

- ui-design `products/system/designs/v2/documents.html` (`document.skip`,
  `document.skip-repo`, `propose`).
- `local-project-dashboard/sd_dashboard/documents.py`, `Config` and the `skip`
  branch of the conf parser.
- `~/.config/system/project-dashboard/documents.conf`, the header.

## Log

- 2026-09-28 created

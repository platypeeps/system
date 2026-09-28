---
title: The Documents tab keeps pin, hide and tag state
created: 2026-09-28
---
# PRD — Documents view state

## Problem

The v2 Documents mockup offers Pin, Unpin, Hide, Unhide and Tag on a document.
All five are marked "no CLI: view state; no document store yet" (ui-design,
`products/system/designs/v2/documents.html`, verb check of 2026-09-28).

Documents are found on disk: any checkout under `~/repos` with `docs/dashboard`
is a root, and `documents.conf` only relabels, skips or adds a root
(`source:local-project-dashboard/sd_dashboard/documents.py::Config`). No row
exists for a document, so nothing can remember that the operator pinned one
or tagged one. Every reload shows the same flat list.

These five are dashboard-only commands, like `sd chat --scope`; they need a
store and an endpoint, not an `sd` verb.

## Requirements

1. The dashboard keeps view state per document, `pinned`, `hidden` and a list
   of tags, keyed by a stable root identity and the file name. The root key
   is not that identity: it is the checkout's basename, and a machine-local
   `root|` line can point it elsewhere, so two roots that both hold
   `index.html` would share one row. The identity has two parts: the
   checkout's origin remote, and the root directory relative to that checkout,
   because a `root|` line can name several directories in one checkout. A
   root without a remote is keyed by a persistent installation id plus its
   resolved directory, because a path alone carries no machine identity, and
   it stays local to that installation. The sd database is the store, so a
   second machine that resolves the same remote and directory sees the same
   pins.
2. One write endpoint sets or clears any of the three and returns the changed
   row, per the build rule "return the changed row from each write"
   (ui-design `products/system/commands.md`, "Fixes the build needs").
3. The Documents listing honours the state: pinned documents first, hidden
   documents folded under one count with an Unhide, tags shown and filterable.
4. State for a document that no longer exists on disk is left alone and never
   shown; a rebuilt file under the same root identity and name gets its state
   back, and a different root that reuses the name does not.
5. The mockup's five declarations change their reason to "dashboard only", the
   way `sd chat --scope` is marked, and `designs/tools/collect-counts.mjs`
   regenerates `data/commands.js` (ui-design).

## Acceptance criteria

- [ ] Pin a document, reload the Documents tab: it is first in the list.
- [ ] Hide a document, reload: it is under the hidden count, and Unhide returns it.
- [ ] The write endpoint's response is the document row with the new state.
- [ ] Delete the file on disk, reload: no error, and the row is absent.
- [ ] Two roots that both hold `index.html` keep separate state, including
      when a `root|` line redirects one of the keys.
- [ ] Two `root|` directories inside one checkout keep separate state.
- [ ] Two installations with the same remote-less path keep separate state.
- [ ] `local-project-dashboard` tests cover the three fields, the fold and
      the three identity cases.

## References

- ui-design `products/system/designs/v2/documents.html` (`document.pin`,
  `document.unpin`, `document.hide`, `document.unhide`, `document.tag`).
- `local-project-dashboard/sd_dashboard/documents.py`, the root finder and
  `Config`.

## Log

- 2026-09-28 created
- 2026-09-28 review pass 1 (codex, advisory, addressed): keying by root key
  let two roots with the same file name share state; requirement 1 now names a
  stable root identity, requirement 4 and the criteria follow.
- 2026-09-28 review pass 2 (codex, two advisories, addressed): a remote names
  a repository, not a root, and a path names no machine; the identity now
  carries the root's directory relative to the checkout, and the remote-less
  fallback carries an installation id. Two criteria added.

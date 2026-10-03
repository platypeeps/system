# Writing

Writing is at `/writing` (sd:2125): the design's pipeline board, one column
per stage with four gate lamps per piece, a focused editor and the strip of
gate counts. `GET /api/writing` (`writing_screen.document`) gives every
piece, parked ones too: its stage, gate problems, next and correction stages
and publication claims from `writing.piece_state`, each file's size from
`writing.piece_files`, and the opening of its draft. The repository goes out
by its folder name only. Stage asks first and posts
`POST /api/items/<id>/stage` with the piece's revision; it has no Undo,
because going back is a correction. Correct asks for a reason and posts the
same route with `correct`. Park and Revive post `/park` and `/revive` and
undo each other. Publish and Capture are copy only. The page reads through
the shell's reader, `read.js`. The old list moved to `/classic/writing`.

Not read yet, so drawn as gaps:

- the vault's blog ideas column: the design read `sd store list sdw.blog-idea`,
  and the database holds no vault idea rows;
- the evidence summaries inside `research.md`, `fact-check.md` and
  `adversarial.md`: the editor shows each file's size, time and lamp;
- a draft save: the editor is read only, since no route writes a draft.

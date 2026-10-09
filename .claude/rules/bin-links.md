---
paths:
  - "local-bin-links/**"
  - "local-adversarial-gate/**"
---

# local-bin-links

- Convention 2 applies: a linked script walks `$0` (the `~/bin/common` symlink) to its real path; find them with `grep -l 'while \[ -L "$SELF" \]'`.
- The research kit lives in `platypeeps/sd-ai-command-pack` as `bin/sd-research-kit`; run it inside the repo.
- `local-bin-links` links none of the pack's commands: the pack's installer links them into `~/.local/bin`.
  - It removes old symlinks whose target is inside `$SD_PACK_ROOT` (default `~/repos/platypeeps/sd-ai-command-pack`) only when the installed copy in `~/.local/bin` is an executable regular file whose symlink chain avoids the bin dir, and that dir is on `PATH`, or the link dangles; a regular file is never touched.
- Keep the `local-bin-links` row for `adversarial-gate`; the pack tells readers to run `adversarial-gate render --lens research-brief`.
- When a document moves to the pack, check what it tells the reader to run; that command must be on `PATH` everywhere.

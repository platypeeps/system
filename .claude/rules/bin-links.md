---
paths:
  - "local-bin-links/**"
  - "local-adversarial-gate/**"
---

# local-bin-links

- A script that `local-bin-links` links walks `$0` to its real path first; `$0` is the `~/bin/common` symlink.
  - Find the scripts that do with `grep -l 'while \[ -L "$SELF" \]'`; do not keep a count in prose.
- The research kit lives in `platypeeps/sd-ai-command-pack` as `bin/sd-research-kit`; run it inside the repo.
- `local-bin-links` puts every pack `bin/sd*` command on `PATH` from `$SD_PACK_ROOT` (default `~/repos/platypeeps/sd-ai-command-pack`).
  - It reads the pack's `bin/` on each run; there is no list.
  - A missing pack checkout is a `SKIP`, not a failure.
- `bin-links.sh` keeps a `RETIRED` list that `install` and `remove` clear and `status` reports.
- Keep the `local-bin-links` row for `adversarial-gate`; the pack tells readers to run `adversarial-gate render --lens research-brief`.
- When a document moves to the pack, check what it tells the reader to run; that command must be on `PATH` everywhere.

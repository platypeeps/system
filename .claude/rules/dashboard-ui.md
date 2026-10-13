---
paths:
  - "local-project-dashboard/**"
---

# Dashboard UI work goes through hallmark

- Invoke the `hallmark` skill before you design or change a dashboard page or its CSS, and apply `ui-design/foundation/` and `products/system/design.md`: the design source binds every page.
- Read and append hallmark's design log at `~/repos/system/.hallmark/log.json`, the main checkout's untracked copy, not a worktree's: every PR appending to a tracked log conflicted (sd:3720).

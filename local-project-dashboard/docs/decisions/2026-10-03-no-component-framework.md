# No component framework for the dashboard

Decided 2026-10-03. Context: the operator asked whether Material UI (MUI) could improve the dashboard UI, speed development, or improve stability.

## Decision

Do not adopt MUI or any other framework-based component library.
Extract repeated patterns into shared plain-JS web components instead (sd:2582, then ui-design sd:2583).

## Reasons

| Reason | Source |
| --- | --- |
| MUI is a React library and needs a bundler; the dashboard has no build step by rule. | `README.md`, "No frontend build step, and there will not be one." |
| The UI foundation requires web components in plain JS, with no framework and no build step. | `ui-design/foundation/patterns.md` |
| A design must look made for its content; Material's default look is a template. | `ui-design/foundation/principles.md` |
| MUI's Emotion styling injects style tags at runtime, which needs `'unsafe-inline'` or a nonce in `style-src`. | the policy in `source:local-project-dashboard/sd_dashboard/server.py::CSP` |
| A move to React rewrites about 20 hand-written pages. | `sd_dashboard/v2/static/` |
| The data-grid features worth having (grouping, some pinning, export) are in MUI X's paid tiers. | MUI X licensing |

## What we take from MUI

Its keyboard and focus behavior, with the WAI-ARIA Authoring Practices, as a checklist for our own components.

## Reopen when

- The no-build rule changes, or
- One widget (for example a date picker) stalls sd:2583. Then evaluate a vendored, framework-free library such as Shoelace / Web Awesome against the security policy first.

---
paths:
  - "local-cron-jobs/**"
---

# Slack MCP and `slack-daily-digest`

Moved from the root `CLAUDE.md`. History: `docs/claude-md-history.md`.

- The Slack plugin MCP works headless; do not rebuild it.
  - `./cron-jobs.sh run slack-daily-digest` exits 0 and mails the digest.
  - A claim that it cannot load headless measured a dead OAuth token.
- Do not build a local `korotovsky/slack-mcp-server`; the plugin's OAuth token refreshes unattended.
- Keep the job in `personal.cron`; `work.cron` is comment-only, and a move there was reverted.
- Query mentions as the user's handle (`<handle>`), not `<@USERID>` or a bare first name.
  - Slack stores `<@USERID|handle>`, so the bracket-closed form matches nothing.
  - A bare first name false-positives on prose.
  - The working form is in the vault SKILL.md.

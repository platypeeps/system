---
paths:
  - "local-cron-jobs/**"
  - "local-repo-sync/**"
---

# The 02:xx cron slot needs a scheduled wake

- A job in the 02:xx slot needs a scheduled wake, not a cron edit: `sudo pmset repeat wakeorpoweron MTWRFSU 02:40:00`.
- Check `pmset -g sched` for a `Repeating power events:` block; a green night proves nothing.
- Unattended git with a passphrase key needs `SSH_AUTH_SOCK` in the launchd domain. Detail and symptoms: `docs/design/jobs.md`.

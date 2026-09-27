---
paths:
  - "local-cron-jobs/**"
  - "local-repo-sync/**"
---

# The 02:xx cron slot needs a scheduled wake


- `repo-sync-nightly` runs at 02:45; a laptop in deep idle answers it in a DarkWake window without full networking.
  - Symptom: every repo fails with `connect to host ssh.github.com port 443` or `Permission denied (publickey)`.
  - `pmset -g log` names the wake; the later slots (03:15, 03:45, 04:15, 04:30) are unaffected.
- A passphrase-protected SSH key needs `SSH_AUTH_SOCK` in the launchd domain for unattended git.
- Fix it with a scheduled wake, not a cron edit; `JOB_SCHEDULE` is fleet-wide and no night hour is reliably awake.
- The wake is a per-machine setting that no stage sets: `sudo pmset repeat wakeorpoweron MTWRFSU 02:40:00`.
- Before trusting a green night, check `pmset -g sched` for a `Repeating power events:` block.
- A green night does not prove the wake exists; an awake machine passes on its own.
- `repo-sync.sh nightly` exits 1 when half or more of the fleet fails.
- `repo-sync.sh` globs `$ROOT/*/.git` and `$ROOT/*/*/.git`, so checkouts one level deeper (`research/*`) are found.

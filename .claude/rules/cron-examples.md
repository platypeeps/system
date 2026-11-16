---
paths:
  - "local-cron-jobs/examples/**"
---

# A fix to an example job names its installed port

- A PR that changes `local-cron-jobs/examples/<job>.job` names, in its body, the commit that ports the change to the installed `<config>/cron-jobs/jobs/` copy, or says why none needs it: nothing installs examples, so the fix never ran (system sd:9112).
- The lane review blocks a PR that changes an example job and names neither.

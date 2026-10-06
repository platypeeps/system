---
title: A satellite installs the hub's sd_db build itself
created: 2026-10-05
item: sd:2802
---
# Implement — a satellite installs the hub's sd_db build itself

One pull request in this repository. Tests first in `local-sd-db/tests/test_self_install.py`.

1. Tests for the install: equal digest, different digest, off switch, lock, a finished install, verify, the source order.
   Check: they fail before `sd_db/self_install.py` exists.
2. `remote.tree_digest`, `BuildMismatch.hub_build`, the session counter, and the server's protocol refusal.
   Check: `TheHubsDigest` passes, and fails with `hub_build` removed from either refusal.
3. `sd_db/self_install.py` and the `Hub.open` hook.
   Check: `TheInstall`, `TheRefusal` and `TheHubOpen` pass; each fails with its guard removed.
4. `sd_db.satellite` plans and installs; `machine-setup.sh` sets `SD_DB_SOURCE_CHECKOUT`.
   Check: `TheNightly` and the machine-setup test pass.
5. `sd-db.sh install` writes `<venv>/sd-db-source`.
   Check: `TheInstallVerb` passes.
6. READMEs, then `make check`, `tests/test_citations.py`, `sd-docs-lint` and the secrets scan.

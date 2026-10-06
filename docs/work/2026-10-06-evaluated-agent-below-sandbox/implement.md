---
title: An evaluated agent runs below sandbox
created: 2026-10-06
item: sd:2870
---
# Implement — an evaluated agent runs below sandbox

One pull request in this repository, then an operator rollout.

1. Done 2026-10-06: the simulator session answered the three read-path questions; all three carry no truth (design).
   Check: the answers are recorded on sd:2870.
2. Test first: below `sandbox`, `HardDenies` names every action in the design's route table, and nothing allows one.
   Check: every action but `ec2:CreateSnapshot` fails on `origin/main`.
3. Extend the non-sandbox `HardDenies` list; `check` expects `explicitDeny` for each route action below `sandbox`.
   Check: the new test passes; `simulate sandbox` output is unchanged.
4. README: the Levels table row for launch reads "denied" below `sandbox`; "Agent and deployer" tells the agent account to set `LEVEL=operator`.
   Do the same in the `help` text and `accounts/example.env.example`.
   Check: `make check`, `tests/test_citations.py`, `sd-docs-lint` and the secrets scan pass.
5. Operator rollout after merge: `sandbox.env` sets `LEVEL=operator`; `DRY_RUN=1 apply sandbox`, `apply sandbox`, `check sandbox`.
   Check: `all expectations met for sandbox (operator)`, with `explicitDeny` on every route action.
   `check sandbox-deployer` still passes.
6. Post the check output on sd:2870 and sd:2851 so the owner can lift the rule in note #10729.

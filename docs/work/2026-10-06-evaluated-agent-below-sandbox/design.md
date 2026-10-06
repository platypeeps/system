---
title: An evaluated agent runs below sandbox
created: 2026-10-06
item: sd:2870
---
# Design — an evaluated agent runs below sandbox

## Choice: the agent moves to `operator`

Two shapes were weighed.

| Shape | Closes snapshot and launch | Cost |
| --- | --- | --- |
| Agent account sets `LEVEL=operator` | yes; `HardDenies` already holds `ec2:CreateSnapshot` and `ec2:TerminateInstances`, and no statement allows `ec2:RunInstances` | a config line, docs, one test |
| A new level between `operator` and `sandbox` | yes, if written to | a fourth level in render, check, validator, help and README, for no grant the agent still needs |

`operator` wins. After sd:2851 the agent needs no grant that `operator` lacks; launch, terminate, snapshot and cleanup are deployment work.
A new level is reopened only if requirement 6 finds a read path that `operator` cannot close.

## Every route to the disk is an explicit deny

Studio writes truth under `/opt/world-simulator/source/runs` on the instance disk (simulator session, 2026-10-06).
With SSM denied, the agent's routes to it are disk copies and instance access.
A render of `operator` on `origin/main` shows only `ec2:CreateSnapshot` explicitly denied; every other route is denied only by a missing Allow:

| Route | Actions |
| --- | --- |
| Launch from a copy | `ec2:RunInstances` |
| Copy the disk | `ec2:CreateSnapshot` (already denied), `ec2:CopySnapshot`, `ec2:CreateImage`, `ec2:CreateVolume`, `ec2:CreateRestoreImageTask` |
| Share a copy | `ec2:ModifySnapshotAttribute`, `ec2:ModifyImageAttribute` |
| Move the disk | `ec2:AttachVolume`, `ec2:DetachVolume`, `ec2:CreateReplaceRootVolumeTask` |
| Reach the guest | `ec2-instance-connect:SendSSHPublicKey`, `ec2-instance-connect:SendSerialConsoleSSHPublicKey`, `ec2:GetConsoleScreenshot` |

Below `sandbox`, `HardDenies` lists all of them.
That keeps requirement 1 true under a supplemental policy that grants `ec2:*`.
The `stmt_hard_denies` block in `local-aws-setup/aws-setup.sh` already varies its list by level, so the change extends that list.
`sandbox` is unchanged: the deployer launches and snapshots, and a sandbox agent is no longer the recommended setup.

## The read paths left at `operator`

The simulator session answered from code on 2026-10-06; it made no AWS calls.

| Path | What it returns | Answer |
| --- | --- | --- |
| `ec2:GetConsoleOutput` on a tagged instance | the guest's boot console | no truth: the bootstrap runs over SSH and logs to a file; nothing forwards the journal to the console |
| `ec2:DescribeInstanceAttribute` (`userData`), inside `ec2:Describe*` | the launch user data, base64 | no truth: a fixed stub that installs and starts the SSM agent; secrets arrive later over SSH |
| `s3:GetObject` on `S3_BUCKETS` | listed bucket objects | no truth from the simulator repository: nothing in it writes to S3 |

Two caveats stay open; neither blocks this change:
- An AMI that forwards the journal to the console would change the console answer.
- Account-level SSM output logging, or a vendor archive, could write to a bucket; the agent's listed bucket is not referenced by the simulator.

A path found later to carry truth gets an explicit `Deny` in a per-account supplemental policy, not a new level.
The supplemental policy stays per account; the base level stays generic.

## Live rollout

The operator edits `sandbox.env` to `LEVEL=operator`, runs `DRY_RUN=1 apply sandbox`, `apply sandbox` and `check sandbox`.
`apply` writes a new default version of the base policy; no user or key changes.
The deployer account file is untouched.

## Tests

`local-aws-setup/tests/test_aws_setup.py` gains one case: below `sandbox`, the render's `HardDenies` names every action in the route table, and no statement allows one.
Fail first: every action except `ec2:CreateSnapshot` fails on today's render.
`check` gains an `explicitDeny` expectation below `sandbox` for each route action on a tagged resource, so the live check proves requirement 1.

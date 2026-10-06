---
title: An evaluated agent runs below sandbox
created: 2026-10-06
item: sd:2870
---
# PRD — an evaluated agent runs below sandbox

## Problem

sd:2851 moved deployment to an operator-only deployer user and denied the agent every SSM command and session.
The agent's account file still sets `LEVEL=sandbox`.
At that level the agent may snapshot a volume tagged `claude-managed=true`, then launch a tagged instance from that snapshot with its own key pair.
The new instance mounts a copy of the deployment's disk, which holds `runs/<id>/truth/`.
`check sandbox` on 2026-10-06 confirms both grants: `allowed ec2:CreateSnapshot` on a tagged volume, `allowed ec2:RunInstances` with the managed request tag.

The owner's rule (sd:2851 note #10729) blocks every scored evaluation by an agent with the AWS MCP until this path is closed.

## Finding

The `operator` level already closes it.
It grants no `ec2:RunInstances`, and its `HardDenies` statement denies `ec2:CreateSnapshot`, `ec2:TerminateInstances` and every cleanup action.
Since sd:2851, every sandbox-only grant serves deployment, and the deployer holds deployment.

## Requirements

1. The agent principal of a deployment account has no route to a deployment's disk: no snapshot, image, volume copy, volume move, launch, or guest access outside SSM.
   Explicit denies enforce this, so a supplemental policy attached later cannot reopen it.
2. The agent keeps describe, metrics, start, stop, reboot and console output on tagged instances, and get and put on listed buckets.
3. The deployer keeps `LEVEL=sandbox`; deploy and destroy work as on 2026-10-06.
4. `check` for the agent account proves requirement 1 against the live policy.
5. The README names the level for an evaluated agent and says why.
6. The remaining read paths at `operator` are listed and judged.
   - console output
   - instance user data, readable through `ec2:Describe*`
   - the listed buckets

   Each one either carries no ground truth or gets an explicit deny.

## Out of scope

- A fourth level. Requirement 6 decides if one is needed; add it only for a read path `operator` cannot close.
- Changing the deployer or the simulator wrapper.
- Lifting the owner's rule. The owner lifts it after the live check passes.

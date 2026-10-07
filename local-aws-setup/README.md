# local-aws-setup

Scoped AWS access for coding agents (Claude Code) across several accounts.
Each account gets an agent IAM user (`agent`) with a rendered base policy.
Its name defaults to `agent-base`; the account may override it with `POLICY_NAME`.
Private `EXTRA_POLICIES` documents reproduce additional provisioning and service permissions during initial setup.
Dangerous actions are explicitly denied at every level, so they stay denied even if a
broader policy is attached later.

## Two layers

| Layer | What it does | Is it a security boundary? |
|---|---|---|
| IAM policy (`aws-setup.sh`) | Decides what the credentials can do at all | **Yes** |
| Claude Code permissions (`~/.claude/settings.json`) | Decides which `aws` commands run without prompting | No — pattern rules are easy to sidestep; convenience and guardrails only |

## Accounts

Each account is a file outside the checkout,
`<config>/aws-setup/accounts/<name>.env`; `<name>` is what every command
takes. `<config>` is `$SYSTEM_TOOLS_CONFIG` (default `~/.config/system`);
`AWS_SETUP_ACCOUNTS_DIR` names another folder. Start from
`accounts/example.env.example`. Shared settings go in `<config>/aws-setup/.env`
(start from `.env.example`).

```sh
./aws-setup.sh accounts     # list what is configured
```

| Variable | Meaning |
|---|---|
| `ACCOUNT_ID` | 12-digit account id |
| `LEVEL` | `readonly`, `operator` or `sandbox` |
| `ADMIN_PROFILE` | your admin login for that account; `apply` refuses if it is a different account. Needed by `apply`, `keys`, `rotate` and `check`; a file that only names an account may leave it out |
| `AGENT_USER` | agent IAM user (default `agent`) |
| `AGENT_PROFILE` | CLI profile holding the agent key (default `agent-<name>`) |
| `AGENT_REGION` | region for that profile (default `us-east-1`) |
| `S3_BUCKETS` | space-separated bucket names |
| `POLICY_NAME` | managed policy name in this account; overrides the shared `POLICY_NAME` in `<config>/aws-setup/.env` (default `agent-base`) |
| `EXTRA_POLICIES` | space-separated supplemental managed policy names; default empty |
| `PASS_ROLE_ARNS` | exact same-account instance role ARNs; sandbox only, passed to EC2 only; default empty |

A typical setup: `dev` (operator, profile `agent-dev`) and `sandbox`
(sandbox, profile `agent-sandbox`). The account files are the source of
truth; `./aws-setup.sh accounts` lists them.

## Levels

| | readonly | operator | sandbox |
|---|---|---|---|
| Describe/list EC2, list buckets, CloudWatch metrics | yes | yes | yes |
| Start/stop/reboot instances tagged `claude-managed=true`, and read their console output | — | yes | yes |
| Launch instances | denied | denied | only tagged `claude-managed=true` at launch |
| Terminate instances | denied | denied | tagged only |
| Disassociate/release Elastic IPs; delete security groups, key pairs, volumes, network interfaces, snapshots | denied | denied | tagged only, configured account/region |
| Listed buckets | get | get, put | get, put, delete |
| Start, stop, reboot, terminate, or read console output without `claude-managed=true` | denied | denied | denied |
| Change the `claude-managed` tag on existing resources | denied | denied | denied |
| IAM administration, role assumption, Organizations, account settings, CloudTrail tampering including its event selectors, changing an instance attribute, bucket deletion/policy/ACL/public access block, KMS key deletion | denied | denied | denied |
| Create, copy or share snapshots and images; create, attach or detach volumes; EBS direct reads; console screenshots; EC2 Instance Connect; spot and fleet launches; launch from an image or snapshot that `amazon` or Canonical (`099720109477`) does not own | denied | denied | denied |

Console output is part of managing an instance: an agent that may stop and
start one needs to read why it did not come up. It returns whatever the guest
printed at boot, so treat a boot log as readable by the agent.

The restricted rows use explicit `Deny` statements, not gaps in an `Allow`.
The difference only shows when a second policy is attached: a conditional
`Allow` says nothing about what another policy grants, and an explicit `Deny`
beats every `Allow` anywhere. That is why the untagged-instance denies render
at `readonly` too, where nothing is allowed for them to contradict.

The base policy implicitly denies unlisted actions. Supplemental policies can add grants. Print the base document with
`./aws-setup.sh render <name>`. Suggested mapping: `sandbox` for sandbox
accounts, `operator` for dev, `readonly` for staging, prod and anything
shared.

## Supplemental policies and teardown

List every additional attached policy in `EXTRA_POLICIES` before initial setup.
Store each identity policy document at `<accounts>/<name>.policies/<policy-name>.json`.
Agent and deployer are separate IAM users, each with its own account file for the same account.

### Agent and deployer

The template `accounts/provisioning-policy.json.example` runs commands as root on deployment instances (`AWS-RunShellScript`).
Never attach it to a user whose key an agent, an MCP server or an evaluated session holds.
A shell on a simulator deployment reads its ground truth, and in-instance checks cannot see the call (sd:2851).
Give the template to an operator-only deployer user instead:

```sh
# sandbox-deployer.env: the same ACCOUNT_ID and ADMIN_PROFILE as sandbox.env
LEVEL=sandbox
AGENT_USER=deployer
AGENT_PROFILE=deployer-sandbox
POLICY_NAME=deployer-base   # its own name: a shared one overwrites the agent's base policy
EXTRA_POLICIES="deployment-provisioning"
PASS_ROLE_ARNS="arn:aws:iam::123456789012:role/example-instance"
```

Copy the template to `sandbox-deployer.policies/deployment-provisioning.json`.
Create the `deployer` user as in step 2 of "Adding an account", then run `apply` and `keys` for `sandbox-deployer`.
Set the deployment wrapper's `AWS_PROFILE` to `deployer-sandbox`; keep that profile out of agent and MCP settings.

The agent's `sandbox.env` sets `LEVEL=operator` and `EXTRA_POLICIES="agent-ssm-deny"`.
Copy `accounts/agent-ssm-deny.json.example` to `sandbox.policies/agent-ssm-deny.json`.
It denies every SSM command and session to the agent, whatever else is attached later.
The agent runs at `operator`: below `sandbox` it cannot launch at all.
Every level denies each other route to a deployment's disk (sd:2870): snapshots, images, volume copies or moves,
EBS block reads, snapshot or image sharing, console screenshots and Instance Connect. `check` probes each one.
The deployer launches from a public Canonical image and needs none of them.
A launch source must be owned by `amazon` or Canonical, so an untagged private backup is denied too.

The template allows `AWS-RunShellScript` for commands and `AWS-StartPortForwardingSession` for tunnels, in separate statements.
Its instance statement sets `ssm:SessionDocumentAccessCheck`, so a session without a document cannot fall back to a shell.
`validate-policies.py` refuses a supplemental policy that grants the interactive `SSM-SessionManagerRunShell` document.
It catches a grant by name, by wildcard, and as the default document of an unchecked instance session.

Replace every `{{...}}` placeholder in the template before rendering or applying it:

| Placeholder | Private deployment input |
|---|---|
| `ACCOUNT_ID`, `REGION`, `AGENT_USER` | Values from the deployer's account configuration |
| `VPC_ID` | Existing VPC where the deployment creates security groups |
| `OWNER`, `PROJECT`, `PURPOSE`, `MANAGED_BY` | Exact resource-tag values sent by the deployment |
| `DEPLOYMENT_KEY_PREFIX` | Key-name prefix used by the deployment, without the trailing hyphen |
| `LEGACY_KEY_PREFIX` | Existing approved key-name prefix; remove its two statements when no legacy grant is needed |

The template covers security groups, key import, Elastic IPs, SSM commands and port-forwarding sessions.
The base policy supplies instance launch, rule tagging, teardown, and the optional exact-role grant.
Set `LEVEL=sandbox`, configure `EXTRA_POLICIES`, and audit the instance role before setting `PASS_ROLE_ARNS`.
Create the VPC, role, and instance profile with an administrator first; this tool grants no IAM provisioning permission.
Review SSM document permissions and ownership tags for the intended deployment before applying the template.
The fresh template requires `claude-managed=true` when creating security groups, key pairs, and Elastic IPs, alongside its ownership tags.
It also requires that tag for security-group rule changes, SSM instance access, and Elastic IP association.

Copy the existing default-version document exactly when adopting current permissions.
Keep account IDs, resource ARNs, and SSM scopes in private account configuration.
Do not broaden those grants when moving them into configuration.
Literal adoption preserves legacy grants; it does not apply the fresh template's stricter managed-tag conditions.
An adopted document that grants the interactive shell fails validation; remove that grant first.
Audit adopted SSM and address-association grants separately. The base policy denies untagged access only for its listed lifecycle actions.

```sh
./aws-setup.sh render sandbox-deployer deployment-provisioning
./aws-setup.sh simulate sandbox-deployer
DRY_RUN=1 ./aws-setup.sh apply sandbox-deployer
./aws-setup.sh apply sandbox-deployer
./aws-setup.sh check sandbox-deployer
```

`render <account>` prints the base policy; the optional policy name selects a configured supplemental document.
`simulate` evaluates all configured documents together; `check` evaluates the attached principal policies.
These checks test the base permission boundaries, including teardown. Add service-specific expectations when supplemental behavior changes.

Every supplemental document must contain valid identity-policy JSON within IAM's 6,144 non-whitespace character limit.
Policy names must be valid IAM names, unique, and different from `POLICY_NAME`.
Missing or invalid documents fail before AWS calls.
`apply` checks ownership of every existing configured policy before its first IAM write.
It refuses unconfigured attachments, group membership, and inline policies. It never adopts or detaches them automatically.

Before adopting an existing policy, inspect its default document and every attached user, group, and role.
Changing its default version affects all consumers, including automation.
Then deliberately add `managed-by=local-aws-setup` with the admin profile, as shown below for the base policy.
The same ownership requirement applies to supplemental policies.

Sandbox teardown permits cleanup only in the configured region; account-bearing resources use the configured account.
AWS snapshot ARNs omit the account field, so the managed tag alone constrains deleting a snapshot.
Elastic IP disassociation checks both the Elastic IP and its network interface; tag both during provisioning.
Apply the managed tag during resource creation. Existing resources require deliberate tagging by an administrator.
The creation exception covers `RunInstances`, `CreateSecurityGroup`, `ImportKeyPair`, `AllocateAddress`, `AuthorizeSecurityGroupIngress`, and `AuthorizeSecurityGroupEgress`.
It grants no standalone permission to retag existing resources.

Terminate the instance, then clean up managed resources.
No level may snapshot a managed disk first: a snapshot is a copy of the deployment the agent could read.
`ec2:ModifyInstanceAttribute` stays explicitly denied. Teardown does not require changing disk deletion settings.
IAM self-management stays denied; an admin applies policy updates before the agent performs teardown.

### Deploying with an existing instance role

Enable `PASS_ROLE_ARNS` only after auditing the instance role's current permissions, EC2 trust, and existing consumers.
Use exact role ARNs from the configured account. Wildcards, duplicate roles, and lower access levels are rejected.
This permits `iam:PassRole` only for those roles and only to `ec2.amazonaws.com`.
Other roles, other services, IAM administration, and role assumption remain explicitly denied.
The agent can run code with the passed role's permissions. Do not pass a role with broader privileges than intended.
An existing `AmazonSSMManagedInstanceCore` attachment includes parameter reads across resources; review that scope before permitting role passage.
No role is created or changed by this setting. Leave it empty to preserve the complete IAM deny.
AWS describes these controls in its [PassRole guide](https://docs.aws.amazon.com/IAM/latest/UserGuide/id_roles_use_passrole.html).

Sandbox permits managed tags during security-group rule creation in the configured account and region.
The supplemental policy must allow rule creation on both its intended security groups and new rule ARNs.
Tagged rule creation checks ownership request tags on the new rule; the example includes both resource permissions.
Key import policies must match the deployment's configured key-name prefix and ownership request tags.
Test the actual creation request with `DryRun=true`; an allowed EC2 request returns `DryRunOperation` without creating resources.

## Adding an account

Prerequisites: `aws` CLI v2, Python 3, and an admin login for the account.

1. **Log in as admin** under a profile named for the account. `aws login`
   uses your console session, so make sure the browser is signed in to the
   right account. The profile it writes carries no region, and the CLI refuses
   a call without one, so every admin command here passes `--region`:

   ```sh
   aws login --profile admin-sandbox --region us-east-1
   aws sts get-caller-identity --profile admin-sandbox --region us-east-1   # check Account
   ```

2. **Create the agent user** — no console access, no groups, no inline
   policies:

   ```sh
   aws iam create-user --profile admin-sandbox --region us-east-1 --user-name agent \
     --tags Key=purpose,Value=coding-agent
   ```

3. **Write the account file**:

   ```sh
   mkdir -p ~/.config/system/aws-setup/accounts
   cp accounts/example.env.example ~/.config/system/aws-setup/accounts/sandbox.env   # then edit
   ```

4. **Preview and simulate** before touching IAM:

   ```sh
   ./aws-setup.sh render sandbox
   ./aws-setup.sh simulate sandbox           # policy simulator, read-only
   DRY_RUN=1 ./aws-setup.sh apply sandbox    # the IAM calls apply would make
   ```

5. **Apply** — creates or versions `agent-base` and every configured supplemental policy, then attaches each to the user:

   ```sh
   ./aws-setup.sh apply sandbox
   ```

   A policy this tool creates is tagged `managed-by=local-aws-setup`, and
   `apply` refuses to version a policy at that ARN that carries no such tag:
   `agent-base` is a name, not an identity, and replacing the default version
   of somebody else's policy changes every principal holding it. An account
   whose `agent-base` predates the tag is adopted once, deliberately, or
   given another name with `POLICY_NAME` in its account file:

   ```sh
   aws iam tag-policy --profile admin-sandbox --region us-east-1 \
     --policy-arn arn:aws:iam::<account>:policy/agent-base \
     --tags Key=managed-by,Value=local-aws-setup
   ```

6. **Create the access key** — run it yourself. The secret goes straight into
   `~/.aws/credentials`, is never printed, and never reaches a command line,
   so it is not readable out of `ps` while the command runs. A key that never
   authenticates as the agent user is deleted again rather than stored, and so
   is one the profile refuses to take, so a failed run leaves no live key that
   nothing names. Every call the script makes names its profile on the command
   line, and an explicitly passed `--profile` takes the environment provider
   out of the credential chain, so `AWS_ACCESS_KEY_ID` in the calling shell
   cannot answer for it:

   ```sh
   ./aws-setup.sh keys sandbox
   ```

7. **Verify** — the profile is the right user in the right account, and the
   simulator agrees with the level's allow/deny table against the policies
   really attached. Exits 1 on any mismatch:

   ```sh
   ./aws-setup.sh check sandbox
   ```

8. **Tag instances** the agent may manage (as admin):

   ```sh
   aws ec2 create-tags --profile admin-sandbox --region us-east-1 \
     --resources i-0123456789abcdef0 \
     --tags Key=claude-managed,Value=true
   ```

## Claude Code

### Why a profile must be set

Without `AWS_PROFILE` or `--profile`, the AWS CLI falls back to the `default`
profile — here an `AWSReservedSSO_AdministratorAccess` login to dev. Any
`aws` command Claude runs unqualified would then run **as admin** for as long
as that session is valid, and none of the policies above would apply. Setting
`AWS_PROFILE` in Claude Code's settings affects only the commands Claude runs;
your own terminal keeps using `default`.

### Settings

This is what `~/.claude/settings.json` holds (merge into existing lists):

```json
"env": { "AWS_PROFILE": "agent-sandbox" },
"permissions": {
  "allow": [
    "Bash(aws sts get-caller-identity:*)",
    "Bash(aws ec2 describe-*:*)",
    "Bash(aws s3 ls:*)",
    "Bash(aws s3api list-*:*)",
    "Bash(aws cloudwatch get-metric-*:*)"
  ],
  "ask": [
    "Bash(aws ec2 start-instances:*)",
    "Bash(aws ec2 stop-instances:*)",
    "Bash(aws ec2 reboot-instances:*)",
    "Bash(aws ec2 run-instances:*)",
    "Bash(aws ec2 terminate-instances:*)",
    "Bash(aws s3 cp:*)",
    "Bash(aws s3 sync:*)",
    "Bash(aws s3 rm:*)"
  ],
  "deny": [
    "Bash(aws configure:*)",
    "Bash(aws iam:*)",
    "Bash(aws * --profile default*)",
    "Bash(aws * --profile admin-*)",
    "Bash(aws * --profile deployer-*)",
    "Bash(* AWS_PROFILE=default*)",
    "Bash(* AWS_PROFILE=admin-*)",
    "Bash(* AWS_PROFILE=deployer-*)",
    "Read(~/.aws/credentials)"
  ]
}
```

- **Default is sandbox**, so a command that forgets to name an account lands
  where mistakes are cheapest. Use another account explicitly:
  `--profile agent-dev` for dev. An explicit `--profile` beats the default.
- **The deny rules block the admin and deployer profiles** (`default`, `admin-*`, `deployer-*`) whether
  chosen by flag or by environment variable, plus `aws configure`, all
  `aws iam` calls and reading the credentials file.
- **Put `--profile` last** (`aws ec2 describe-instances --profile agent-dev`).
  The allow/ask rules match from the start of the command, so a leading
  `--profile` stops them matching.
- **Per project**: a repo's `.claude/settings.json` can set a different
  `AWS_PROFILE` (for example `agent-dev` in a dev-focused repo); it overrides
  the global one.
- **Side effect**: with `aws iam:*` denied, Claude cannot run IAM reads such as
  `get-user` directly. `./aws-setup.sh check` is unaffected — the script calls
  `aws` itself.

### These rules are guardrails, not a boundary

Pattern rules are easy to sidestep: a script, a subshell or an unusual
argument order is not matched. The real protection against admin use is not
having a valid admin session while Claude works. `aws login` sessions expire on
their own; to end one early after admin work:

```sh
aws logout --profile default
```

### After changing the settings

1. **Restart Claude Code** — env changes do not reach a running session.
2. **Check the default profile**: ask Claude to run
   `aws sts get-caller-identity`. Expect
   `arn:aws:iam::123456789012:user/agent`; anything else means
   `AWS_PROFILE` is not in effect.
3. **Check the block**: ask Claude to run
   `aws iam list-users --profile default`. It must be refused by the deny rule.
   If it runs, the rules are not matching — the likely cause is the `rtk` hook
   rewriting `aws ...` to `rtk aws ...` before the permission check, in which
   case add `rtk aws ...` versions of the rules.

## Maintenance

- **Rotate keys** — IAM user keys never expire on their own:

  ```sh
  ./aws-setup.sh rotate dev
  ```

  It creates the new key and proves it answers as the agent user from a
  credentials file of its own, and only then writes it into the profile; the
  old key stays live until that has happened, and is put back if the profile
  does not take the new one -- on that path and on a write that fails part-way,
  and only when the profile no longer holds it. Each account's key is separate, so rotate each.

  `keys` and `rotate` are the two commands `DRY_RUN=1` cannot preview: they
  have no printing path, so they refuse a dry run rather than create a real
  key under one.
- **Change a policy** by editing the account file (level, buckets) or the
  `stmt_*` functions, then `simulate` and `apply` again. `apply` prunes the
  oldest non-default version at IAM's five-version limit.
- **Add a service** as its own statement function with specific actions, and
  a matching `expect` line so `simulate` and `check` cover it.
- **Audit** with CloudTrail in each account:
  `userIdentity.userName = agent`.

## Why a user per account

A single hub user assuming a role in each account would mean one key and
short-lived credentials, but it breaks when an account's organization blocks
principals from outside it. A user per account always works; the cost is one
long-lived key per account, which is what `rotate` is for, and why every
level denies IAM administration (the agent cannot mint itself more keys).

## CLI or MCP server?

The CLI plus these policies is the primary route. An MCP server uses the same
credentials, so it adds no security; the IAM policy is the boundary either
way.

- `aws-api-mcp-server` exposes roughly one generic `call_aws` tool, so Claude
  Code permissions cannot tell `describe` from `stop-instances`. Skip it
  unless you want its server-enforced read-only mode for unattended runs.
- Per-service servers earn their place for specific jobs: the AWS
  documentation server (no credentials, no risk) and the CloudWatch server
  for log-heavy debugging. Run them with the account's agent profile.

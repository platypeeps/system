# local-aws-setup

Scoped AWS access for coding agents (Claude Code) across several accounts.
Every account gets its own agent IAM user (`agent`) with one managed
policy (`agent-base` unless the account names another), rendered from the account's **access level**. Dangerous
actions are explicitly denied at every level, so they stay denied even if a
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

A typical setup: `dev` (operator, profile `agent-dev`) and `sandbox`
(sandbox, profile `agent-sandbox`). The account files are the source of
truth; `./aws-setup.sh accounts` lists them.

## Levels

| | readonly | operator | sandbox |
|---|---|---|---|
| Describe/list EC2, list buckets, CloudWatch metrics | yes | yes | yes |
| Start/stop/reboot instances tagged `claude-managed=true`, and read their console output | — | yes | yes |
| Launch instances | — | — | only tagged `claude-managed=true` at launch |
| Terminate instances | denied | denied | tagged only |
| Listed buckets | get | get, put | get, put, delete |
| Anything at all on an instance that does not carry `claude-managed=true` | denied | denied | denied |
| Change the `claude-managed` tag on existing resources | denied | denied | denied |
| IAM, role assumption, Organizations, account settings, CloudTrail tampering including its event selectors, changing an instance attribute, bucket deletion/policy/ACL/public access block, KMS key deletion | denied | denied | denied |

Console output is part of managing an instance: an agent that may stop and
start one needs to read why it did not come up. It returns whatever the guest
printed at boot, so treat a boot log as readable by the agent.

The last three rows are explicit `Deny` statements, not gaps in an `Allow`.
The difference only shows when a second policy is attached: a conditional
`Allow` says nothing about what another policy grants, and an explicit `Deny`
beats every `Allow` anywhere. That is why the untagged-instance denies render
at `readonly` too, where nothing is allowed for them to contradict.

Anything not listed is implicitly denied. Print the exact document with
`./aws-setup.sh render <name>`. Suggested mapping: `sandbox` for sandbox
accounts, `operator` for dev, `readonly` for staging, prod and anything
shared.

## Adding an account

Prerequisites: `aws` CLI v2 and an admin login for the account.

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

5. **Apply** — creates `agent-base` in the account (or a new default version)
   and attaches it to the user:

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
    "Bash(* AWS_PROFILE=default*)",
    "Bash(* AWS_PROFILE=admin-*)",
    "Read(~/.aws/credentials)"
  ]
}
```

- **Default is sandbox**, so a command that forgets to name an account lands
  where mistakes are cheapest. Use another account explicitly:
  `--profile agent-dev` for dev. An explicit `--profile` beats the default.
- **The deny rules block the admin profiles** (`default`, `admin-*`) whether
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
level denies `iam:*` (the agent cannot mint itself more keys).

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

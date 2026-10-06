#!/bin/sh
# Scoped AWS access for coding agents, one IAM user per account. Each account
# is a file in <config>/aws-setup/accounts/ naming its id, admin profile, agent profile and access
# level (readonly, operator, sandbox); the level decides the policy rendered
# for that account's agent user. The IAM policy simulator checks every policy
# before it is applied and again against the user it was attached to.
# Usage: aws-setup.sh accounts | render|simulate|apply|keys|rotate|check <account>
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
. "$DIR/../lib/config.sh"
# Account files and shared settings are private, so they live outside the
# checkout: <config>/aws-setup/accounts/<name>.env and <config>/aws-setup/.env.
# AWS_SETUP_ACCOUNTS_DIR names another accounts folder.
ACCOUNTS_DIR="${AWS_SETUP_ACCOUNTS_DIR:-$(st_config_dir aws-setup)/accounts}"
st_source_env aws-setup

POLICY_NAME="${POLICY_NAME:-agent-base}"
MANAGED_TAG_KEY="${MANAGED_TAG_KEY:-claude-managed}"
SIMULATE_PROFILE="${SIMULATE_PROFILE:-default}"
DRY_RUN="${DRY_RUN:-0}"

# IAM keeps at most five versions of a managed policy.
MAX_POLICY_VERSIONS=5
# A new access key can take a while to become usable. Overridable so a suite
# watching a probe give up does not sleep a real minute to see it.
KEY_PROPAGATION_TRIES="${KEY_PROPAGATION_TRIES:-12}"
KEY_PROPAGATION_SLEEP="${KEY_PROPAGATION_SLEEP:-5}"
# Written on the managed policy when this tool creates it, and demanded again
# before a later run replaces that policy's default version.
POLICY_OWNER_TAG_KEY="${POLICY_OWNER_TAG_KEY:-managed-by}"
POLICY_OWNER_TAG_VALUE="${POLICY_OWNER_TAG_VALUE:-local-aws-setup}"

usage() {
  echo "usage: $(basename "$0") accounts | test | render|simulate|apply|keys|rotate|check <account>" >&2
}

die() {
  echo "aws-setup.sh: $*" >&2
  exit 1
}

show_help() {
  cat <<'HELPEOF'
usage: aws-setup.sh accounts | test
       aws-setup.sh render|simulate|apply|keys|rotate|check <account>

  accounts  list the account files in ./accounts.
  test      run the unittest suite in ./tests (extra args go to unittest).
  render    print base policy JSON, or render <account> <extra-policy-name>.
  simulate  run the IAM policy simulator against the rendered policy before
            anything is applied (read-only; runs as SIMULATE_PROFILE).
  apply     confirm ADMIN_PROFILE is really ACCOUNT_ID, then create or version
            POLICY_NAME and every EXTRA_POLICIES policy and attach them to
            AGENT_USER. Existing policies must carry this tool's ownership tag.
            The user must already exist. DRY_RUN=1 prints the mutating calls.
  keys      create an access key for AGENT_USER and store it in AGENT_PROFILE.
            Refuses when the profile already has a key. The secret is never
            printed and never reaches a command line; a key that never
            answers is deleted again. Not dry-runnable: it always creates a
            real key, so DRY_RUN=1 is refused rather than ignored.
  rotate    create a new key, prove it answers as AGENT_USER, store it, then
            delete the old one. The old key stays live until the new one has
            answered, and is put back if the profile does not take it.
            Not dry-runnable either.
  check     confirm AGENT_PROFILE is AGENT_USER in ACCOUNT_ID, then simulate the
            level's allowed and denied actions against the user's attached
            policies. Exits 1 on any mismatch.

Levels:
  readonly  describe/list/metrics; get on listed buckets.
  operator  readonly + start/stop/reboot and read the console output of
            instances tagged MANAGED_TAG_KEY=true; put on listed buckets.
  sandbox   operator + launch instances tagged at launch, terminate tagged
            instances; clean up tagged resources in the configured
            account/region; delete on listed buckets.
  All levels deny IAM administration, role assumption, CloudTrail tampering including its
  event selectors, changing an instance attribute, bucket policy/ACL and
  public access block changes, KMS key deletion, changing the managed tag,
  and acting on an instance that does not carry it. They also deny every
  route to a disk: snapshots, images, volume moves, EBS direct reads, EC2
  Instance Connect, and a launch from a managed image or snapshot.

Account file <config>/aws-setup/accounts/<name>.env, where <config> is
$SYSTEM_TOOLS_CONFIG (default ~/.config/system); AWS_SETUP_ACCOUNTS_DIR
names another folder. Start from local-aws-setup/accounts/example.env.example:
  ACCOUNT_ID     12-digit account id                         (required)
  LEVEL          readonly | operator | sandbox               (required)
  ADMIN_PROFILE  profile with IAM admin rights in the account (required by
                 apply, keys, rotate and check)
  AGENT_USER     IAM user the agent runs as                  (default: agent)
  AGENT_PROFILE  CLI profile holding its key                 (default: agent-<name>)
  AGENT_REGION   region written to AGENT_PROFILE             (default: us-east-1)
  S3_BUCKETS     space-separated bucket names                (default: none)
  POLICY_NAME    managed policy name in this account; overrides the shared
                 setting below, e.g. where another agent-base already exists
  EXTRA_POLICIES space-separated managed policy names         (default: none)
                 JSON in <accounts>/<name>.policies/<policy-name>.json
  PASS_ROLE_ARNS exact same-account role ARNs passed only to EC2 (default: none)
                 sandbox only; audit role privileges and consumers before use

Shared settings (environment, or <config>/aws-setup/.env — see .env.example):
  POLICY_NAME       managed policy name            (default: agent-base;
                    an account file may set its own)
  MANAGED_TAG_KEY   tag marking agent-managed EC2  (default: claude-managed)
  SIMULATE_PROFILE  profile used by simulate       (default: default)
HELPEOF
}

# --- account files -----------------------------------------------------------

load_account() {
  ACCOUNT=$1
  case "$ACCOUNT" in
    ""|*[!a-z0-9-]*) die "account name must be lowercase letters, digits or dashes: '$ACCOUNT'" ;;
  esac
  file="$ACCOUNTS_DIR/$ACCOUNT.env"
  [ -f "$file" ] || die "no account file $file (copy local-aws-setup/accounts/example.env.example to $file)"
  ACCOUNT_ID="" LEVEL="" ADMIN_PROFILE="" AGENT_USER="" AGENT_PROFILE=""
  AGENT_REGION="" S3_BUCKETS="" AGENT_USER_ARN="" EXTRA_POLICIES="" PASS_ROLE_ARNS=""
  # shellcheck disable=SC1090
  . "$file"
  AGENT_USER="${AGENT_USER:-agent}"
  AGENT_PROFILE="${AGENT_PROFILE:-agent-$ACCOUNT}"
  AGENT_REGION="${AGENT_REGION:-us-east-1}"
  validate_account
  validate_extra_policies
}

validate_account() {
  printf '%s' "$ACCOUNT_ID" | grep -Eq '^[0-9]{12}$' ||
    die "$ACCOUNT: ACCOUNT_ID must be 12 digits: '$ACCOUNT_ID'"
  case "$LEVEL" in
    readonly|operator|sandbox) ;;
    *) die "$ACCOUNT: LEVEL must be readonly, operator or sandbox: '$LEVEL'" ;;
  esac
  # One profile cannot be both. `keys` and `rotate` write the agent key into
  # AGENT_PROFILE, so naming the admin profile there overwrites the very
  # credentials this script creates the key with, and every later run acts as
  # the agent -- with no admin profile left to undo it.
  [ -z "$ADMIN_PROFILE" ] || [ "$AGENT_PROFILE" != "$ADMIN_PROFILE" ] ||
    die "$ACCOUNT: AGENT_PROFILE and ADMIN_PROFILE are both '$ADMIN_PROFILE'; the agent key would overwrite the admin profile"
  case "$AGENT_USER$AGENT_PROFILE" in
    *[!A-Za-z0-9_.@+=,-]*) die "$ACCOUNT: invalid AGENT_USER or AGENT_PROFILE" ;;
  esac
  case "$MANAGED_TAG_KEY" in
    ""|*[!A-Za-z0-9_.:/+@-]*) die "invalid MANAGED_TAG_KEY: '$MANAGED_TAG_KEY'" ;;
  esac
  # The names reach unquoted loops and a JMESPath literal, and the shell splits
  # on fewer characters than Python's split(): only IAM name characters and
  # space, tab or newline separators pass.
  [ -z "$(printf '%s' "$EXTRA_POLICIES" | LC_ALL=C tr -d 'A-Za-z0-9_+=,.@ \t\n-')" ] ||
    die "$ACCOUNT: EXTRA_POLICIES may hold only IAM policy names separated by spaces"
  for b in $S3_BUCKETS; do
    case "$b" in
      *[!a-z0-9.-]*) die "$ACCOUNT: invalid bucket name: '$b'" ;;
    esac
    if [ "${#b}" -lt 3 ] || [ "${#b}" -gt 63 ]; then
      die "$ACCOUNT: bucket name must be 3-63 characters: '$b'"
    fi
  done
}

# Validate every supplemental document before any AWS call or mutation.
validate_extra_policies() {
  "${PYTHON:-python3}" "$DIR/validate-policies.py" "$ACCOUNTS_DIR/$ACCOUNT.policies" "$POLICY_NAME" "$EXTRA_POLICIES" "$ACCOUNT_ID" "$LEVEL" "$PASS_ROLE_ARNS" ||
    die "$ACCOUNT: invalid supplemental policy configuration"
}

render_named_policy() {
  selected=${1:-$POLICY_NAME}
  if [ "$selected" = "$POLICY_NAME" ]; then
    render_policy
    return
  fi
  for configured in $EXTRA_POLICIES; do
    if [ "$selected" = "$configured" ]; then
      cat "$ACCOUNTS_DIR/$ACCOUNT.policies/$configured.json"
      return
    fi
  done
  die "$ACCOUNT: policy '$selected' is not configured"
}

# `accounts`, `render` and `simulate` never use the admin profile, so an
# account file kept only to name an account may leave it out (sd:2472).
require_admin_profile() {
  [ -n "$ADMIN_PROFILE" ] || die "$ACCOUNT: ADMIN_PROFILE is required"
}

cmd_accounts() {
  found=0
  for f in "$ACCOUNTS_DIR"/*.env; do
    [ -f "$f" ] || continue
    found=1
    (load_account "$(basename "$f" .env)"
     printf '%-12s %s  %-9s profile=%s user=%s\n' \
       "$ACCOUNT" "$ACCOUNT_ID" "$LEVEL" "$AGENT_PROFILE" "$AGENT_USER")
  done
  [ "$found" = 1 ] || echo "no accounts yet; copy local-aws-setup/accounts/example.env.example to $ACCOUNTS_DIR/<name>.env"
}

# --- policy rendering --------------------------------------------------------

# Print one JSON string per argument, comma-separated, each on its own line.
json_strings() {
  indent=$1
  shift
  remaining=$#
  for item in "$@"; do
    remaining=$((remaining - 1))
    if [ "$remaining" -gt 0 ]; then
      printf '%s"%s",\n' "$indent" "$item"
    else
      printf '%s"%s"\n' "$indent" "$item"
    fi
  done
}

stmt_discovery() {
  cat <<'EOF'
    {
      "Sid": "ReadOnlyDiscovery",
      "Effect": "Allow",
      "Action": [
        "ec2:Describe*",
        "s3:ListAllMyBuckets",
        "cloudwatch:GetMetricData",
        "cloudwatch:GetMetricStatistics",
        "cloudwatch:ListMetrics"
      ],
      "Resource": "*"
    },
EOF
}

# Console output belongs to "manage a tagged instance": an agent that may
# stop and start one needs to read why it did not come up. It returns whatever
# the guest printed at boot, so the help and the README name it beside
# start/stop/reboot instead of leaving a reader to find it in this list.
stmt_ec2_tagged() {
  [ "$LEVEL" != readonly ] || return 0
  terminate=""
  if [ "$LEVEL" = sandbox ]; then
    terminate=',
        "ec2:TerminateInstances"'
  fi
  cat <<EOF
    {
      "Sid": "Ec2ManageTaggedInstances",
      "Effect": "Allow",
      "Action": [
        "ec2:StartInstances",
        "ec2:StopInstances",
        "ec2:RebootInstances",
        "ec2:GetConsoleOutput"$terminate
      ],
      "Resource": "arn:aws:ec2:*:*:instance/*",
      "Condition": {
        "StringEquals": { "aws:ResourceTag/$MANAGED_TAG_KEY": "true" }
      }
    },
EOF
}

# Sandbox may launch instances, but only ones tagged as managed at launch.
stmt_ec2_launch() {
  [ "$LEVEL" = sandbox ] || return 0
  cat <<EOF
    {
      "Sid": "SandboxLaunchTaggedInstances",
      "Effect": "Allow",
      "Action": "ec2:RunInstances",
      "Resource": "arn:aws:ec2:*:*:instance/*",
      "Condition": {
        "StringEquals": { "aws:RequestTag/$MANAGED_TAG_KEY": "true" }
      }
    },
    {
      "Sid": "SandboxLaunchSupportingResources",
      "Effect": "Allow",
      "Action": "ec2:RunInstances",
      "Resource": [
        "arn:aws:ec2:*::image/*",
        "arn:aws:ec2:*::snapshot/*",
        "arn:aws:ec2:*:*:subnet/*",
        "arn:aws:ec2:*:*:network-interface/*",
        "arn:aws:ec2:*:*:security-group/*",
        "arn:aws:ec2:*:*:key-pair/*",
        "arn:aws:ec2:*:*:volume/*",
        "arn:aws:ec2:*:*:launch-template/*"
      ]
    },
    {
      "Sid": "SandboxTagOnLaunch",
      "Effect": "Allow",
      "Action": "ec2:CreateTags",
      "Resource": [
        "arn:aws:ec2:*:*:instance/*",
        "arn:aws:ec2:*:*:volume/*",
        "arn:aws:ec2:*:*:network-interface/*"
      ],
      "Condition": {
        "StringEquals": { "ec2:CreateAction": "RunInstances" }
      }
    },
EOF
}

# Teardown of tagged resources. Attribute changes stay explicitly denied, and
# so does a snapshot first (stmt_deny_disk_reads).
stmt_ec2_lifecycle() {
  [ "$LEVEL" = sandbox ] || return 0
  cat <<EOF
    {
      "Sid": "SandboxCleanupManagedResources",
      "Effect": "Allow",
      "Action": ["ec2:DisassociateAddress", "ec2:ReleaseAddress", "ec2:DeleteSecurityGroup", "ec2:DeleteKeyPair", "ec2:DeleteVolume", "ec2:DeleteNetworkInterface"],
      "Resource": [
        "arn:aws:ec2:$AGENT_REGION:$ACCOUNT_ID:elastic-ip/*",
        "arn:aws:ec2:$AGENT_REGION:$ACCOUNT_ID:network-interface/*",
        "arn:aws:ec2:$AGENT_REGION:$ACCOUNT_ID:security-group/*",
        "arn:aws:ec2:$AGENT_REGION:$ACCOUNT_ID:key-pair/*",
        "arn:aws:ec2:$AGENT_REGION:$ACCOUNT_ID:volume/*"
      ],
      "Condition": { "StringEquals": { "aws:ResourceTag/$MANAGED_TAG_KEY": "true" } }
    },
    {
      "Sid": "SandboxDeleteManagedSnapshot",
      "Effect": "Allow",
      "Action": "ec2:DeleteSnapshot",
      "Resource": "arn:aws:ec2:$AGENT_REGION::snapshot/*",
      "Condition": { "StringEquals": { "aws:ResourceTag/$MANAGED_TAG_KEY": "true" } }
    },
EOF
}

# Rule creation has its own CreateTags authorization and managed-tag exception.
stmt_ec2_rule_tags() {
  [ "$LEVEL" = sandbox ] || return 0
  cat <<EOF
    {
      "Sid": "SandboxTagManagedRulesAtCreation",
      "Effect": "Allow",
      "Action": "ec2:CreateTags",
      "Resource": "arn:aws:ec2:$AGENT_REGION:$ACCOUNT_ID:security-group-rule/*",
      "Condition": {
        "StringEquals": {
          "ec2:CreateAction": ["AuthorizeSecurityGroupIngress", "AuthorizeSecurityGroupEgress"],
          "aws:RequestTag/$MANAGED_TAG_KEY": "true"
        }
      }
    },
EOF
}

# Split the IAM deny by resource and action; only PassRole on approved roles
# reaches the service check. No other IAM action can use this exception.
stmt_pass_role() {
  [ -n "$PASS_ROLE_ARNS" ] || return 0
  # Role ARNs have been validated as exact same-account names, without globs.
  # shellcheck disable=SC2086
  role_lines=$(json_strings '        ' $PASS_ROLE_ARNS)
  cat <<EOF
    {
      "Sid": "DenyIamOutsideApprovedRoles",
      "Effect": "Deny",
      "Action": "iam:*",
      "NotResource": [
$role_lines
      ]
    },
    {
      "Sid": "DenyApprovedRoleActionsExceptPassRole",
      "Effect": "Deny",
      "NotAction": "iam:PassRole",
      "Resource": [
$role_lines
      ]
    },
    {
      "Sid": "DenyPassingRolesToOtherServices",
      "Effect": "Deny",
      "Action": "iam:PassRole",
      "Resource": "*",
      "Condition": {
        "StringNotEqualsIfExists": { "iam:PassedToService": "ec2.amazonaws.com" }
      }
    },
    {
      "Sid": "PassApprovedRolesToEc2",
      "Effect": "Allow",
      "Action": "iam:PassRole",
      "Resource": [
$role_lines
      ],
      "Condition": {
        "StringEquals": { "iam:PassedToService": "ec2.amazonaws.com" }
      }
    },
EOF
}

# These denies survive broader supplemental policies at every access level.
stmt_deny_untagged_lifecycle() {
  cat <<EOF
    {
      "Sid": "DenyCleanupWithoutManagedTag",
      "Effect": "Deny",
      "Action": ["ec2:DisassociateAddress", "ec2:ReleaseAddress", "ec2:DeleteSecurityGroup", "ec2:DeleteKeyPair", "ec2:DeleteVolume", "ec2:DeleteNetworkInterface", "ec2:DeleteSnapshot"],
      "Resource": "*",
      "Condition": { "StringNotEqualsIfExists": { "aws:ResourceTag/$MANAGED_TAG_KEY": "true" } }
    },
EOF
}

# A snapshot, an image, a moved volume or a guest key turns a deployment's
# disk into one the agent can read, so every level denies them (sd:2870).
# Neither the agent nor the deployer needs one, and an untagged copy would
# slip past a tag condition, so the first statement has none. A launch from a
# public image stays allowed; a launch from a managed image or snapshot does not.
stmt_deny_disk_reads() {
  cat <<EOF
    {
      "Sid": "DenyReadingManagedDisks",
      "Effect": "Deny",
      "Action": [
        "ec2:CreateSnapshot",
        "ec2:CreateSnapshots",
        "ec2:CreateImage",
        "ec2:RegisterImage",
        "ec2:CopySnapshot",
        "ec2:CopyImage",
        "ec2:ModifySnapshotAttribute",
        "ec2:ModifyImageAttribute",
        "ec2:CreateVolume",
        "ec2:AttachVolume",
        "ec2:DetachVolume",
        "ec2:CreateReplaceRootVolumeTask",
        "ec2:CreateStoreImageTask",
        "ec2:ExportImage",
        "ec2:CreateInstanceExportTask",
        "ec2:CreateFleet",
        "ec2:RequestSpotInstances",
        "ec2:RequestSpotFleet",
        "ebs:*",
        "ec2-instance-connect:*"
      ],
      "Resource": "*"
    },
    {
      "Sid": "DenyLaunchFromManagedDisk",
      "Effect": "Deny",
      "Action": "ec2:RunInstances",
      "Resource": ["arn:aws:ec2:*::image/*", "arn:aws:ec2:*::snapshot/*"],
      "Condition": { "StringEquals": { "aws:ResourceTag/$MANAGED_TAG_KEY": "true" } }
    },
EOF
}

# The Allows above are conditioned on the managed tag, so an untagged
# instance is outside them -- but only for as long as this is the one policy
# the user carries. A second policy granting ec2:* would reach every instance
# in the account, and a conditional Allow says nothing about that. These two
# Deny statements are what survives it, so they render at every level.
#
# A negated operator inside a Deny takes the IfExists form. The IAM condition
# operator reference is explicit that a Deny with a negated operator "is still
# denied even if the condition key is not present", which is the untagged case
# itself: an instance carrying no managed tag presents no aws:ResourceTag key
# at all, and a launch with no tags presents no aws:RequestTag key.
stmt_deny_untagged_ec2() {
  cat <<EOF
    {
      "Sid": "DenyManagingUntaggedInstances",
      "Effect": "Deny",
      "Action": [
        "ec2:StartInstances",
        "ec2:StopInstances",
        "ec2:RebootInstances",
        "ec2:GetConsoleOutput",
        "ec2:TerminateInstances"
      ],
      "Resource": "arn:aws:ec2:*:*:instance/*",
      "Condition": {
        "StringNotEqualsIfExists": { "aws:ResourceTag/$MANAGED_TAG_KEY": "true" }
      }
    },
    {
      "Sid": "DenyLaunchWithoutManagedTag",
      "Effect": "Deny",
      "Action": "ec2:RunInstances",
      "Resource": "arn:aws:ec2:*:*:instance/*",
      "Condition": {
        "StringNotEqualsIfExists": { "aws:RequestTag/$MANAGED_TAG_KEY": "true" }
      }
    },
EOF
}

stmt_s3() {
  [ -n "$S3_BUCKETS" ] || return 0
  case "$LEVEL" in
    readonly) object_actions='"s3:GetObject"' ;;
    operator) object_actions='"s3:GetObject", "s3:PutObject"' ;;
    sandbox)  object_actions='"s3:GetObject", "s3:PutObject", "s3:DeleteObject"' ;;
  esac
  buckets=""
  objects=""
  for b in $S3_BUCKETS; do
    buckets="$buckets arn:aws:s3:::$b"
    objects="$objects arn:aws:s3:::$b/*"
  done
  # Validated ARNs hold no spaces, and globbing is off while they split.
  set -f
  # shellcheck disable=SC2086
  bucket_lines=$(json_strings '        ' $buckets)
  # shellcheck disable=SC2086
  object_lines=$(json_strings '        ' $objects)
  set +f
  cat <<EOF
    {
      "Sid": "S3NamedBucketsList",
      "Effect": "Allow",
      "Action": ["s3:ListBucket", "s3:GetBucketLocation"],
      "Resource": [
$bucket_lines
      ]
    },
    {
      "Sid": "S3NamedBucketsObjects",
      "Effect": "Allow",
      "Action": [$object_actions],
      "Resource": [
$object_lines
      ]
    },
EOF
}

# Nobody changes the managed tag on an existing resource; that would widen the
# agent's reach. Sandbox allows managed tags only in named creation contexts.
stmt_deny_tag_changes() {
  launch_exception=""
  if [ "$LEVEL" = sandbox ]; then
    launch_exception=',
        "StringNotEqualsIfExists": { "ec2:CreateAction": ["RunInstances", "CreateSecurityGroup", "ImportKeyPair", "AllocateAddress", "AuthorizeSecurityGroupIngress", "AuthorizeSecurityGroupEgress"] }'
  fi
  cat <<EOF
    {
      "Sid": "DenyChangingManagedTag",
      "Effect": "Deny",
      "Action": ["ec2:CreateTags", "ec2:DeleteTags"],
      "Resource": "*",
      "Condition": {
        "ForAnyValue:StringEquals": { "aws:TagKeys": ["$MANAGED_TAG_KEY"] }$launch_exception
      }
    },
EOF
}

# The actions no convenience is worth, denied at every level so they stay
# denied under whatever else is attached later.
#
# ec2:ModifyInstanceAttribute rewrites an instance's user data, which the
# guest runs as root on its next boot, and clears disableApiTermination: it
# turns "may stop this instance" into "may run code on it".
# cloudtrail:PutEventSelectors stops a trail recording a class of events
# without stopping the trail, so the three CloudTrail entries beside it miss
# the quietest way to blind it. s3:PutBucketPublicAccessBlock is the one entry
# that holds the public access block: AWS checks that same permission for the
# DeletePublicAccessBlock API, so setting and deleting the block are both
# denied by it. There is no s3:DeleteBucketPublicAccessBlock action to name.
stmt_hard_denies() {
  iam_deny='"iam:*",'
  [ -z "$PASS_ROLE_ARNS" ] || iam_deny=""
  terminate='
        "ec2:TerminateInstances",
        "ec2:DisassociateAddress",
        "ec2:ReleaseAddress",
        "ec2:DeleteSecurityGroup",
        "ec2:DeleteKeyPair",
        "ec2:DeleteVolume",
        "ec2:DeleteNetworkInterface",
        "ec2:DeleteSnapshot",'
  [ "$LEVEL" != sandbox ] || terminate=""
  cat <<EOF
    {
      "Sid": "HardDenies",
      "Effect": "Deny",
      "Action": [$terminate
        $iam_deny
        "sts:AssumeRole",
        "organizations:*",
        "account:*",
        "cloudtrail:StopLogging",
        "cloudtrail:DeleteTrail",
        "cloudtrail:UpdateTrail",
        "cloudtrail:PutEventSelectors",
        "ec2:ModifyInstanceAttribute",
        "s3:DeleteBucket",
        "s3:PutBucketPolicy",
        "s3:DeleteBucketPolicy",
        "s3:PutBucketAcl",
        "s3:PutObjectAcl",
        "s3:PutBucketPublicAccessBlock",
        "kms:ScheduleKeyDeletion",
        "kms:DisableKey"
      ],
      "Resource": "*"
    }
EOF
}

render_policy() {
  printf '{\n  "Version": "2012-10-17",\n  "Statement": [\n'
  stmt_discovery
  stmt_ec2_tagged
  stmt_ec2_launch
  stmt_ec2_lifecycle
  stmt_ec2_rule_tags
  stmt_pass_role
  stmt_s3
  stmt_deny_untagged_ec2
  stmt_deny_untagged_lifecycle
  stmt_deny_disk_reads
  stmt_deny_tag_changes
  stmt_hard_denies
  printf '  ]\n}\n'
}

# --- AWS calls ---------------------------------------------------------------

require_aws() {
  command -v aws >/dev/null 2>&1 || die "aws CLI not found (brew install awscli)"
}

# `aws login` profiles carry no region, and the CLI refuses calls without one,
# so every admin call passes one. An explicitly passed --profile removes the
# environment provider from the credential chain, so the profile alone decides
# which principal answers.
admin() {
  aws --profile "$ADMIN_PROFILE" --region "$AGENT_REGION" "$@"
}

# Mutating admin call: printed instead of run under DRY_RUN=1.
admin_mutate() {
  if [ "$DRY_RUN" = 1 ]; then
    # stderr, so callers that silence the real call's output still show it.
    {
      printf 'DRY_RUN: aws --profile %s --region %s' "$ADMIN_PROFILE" "$AGENT_REGION"
      printf ' %s' "$@"
      printf '\n'
    } >&2
    return 0
  fi
  admin "$@"
}

# Refuse to touch an account through a profile that belongs to another one.
# It used to return early under DRY_RUN, which reads as "a dry run makes no
# calls" -- true of `apply` and of nothing else. `keys` and `rotate` create a
# real key whatever DRY_RUN says, so the skip left exactly the two commands
# that mutate an account unguarded. The caller that really prints instead of
# calling now decides for itself.
require_admin_account() {
  aws configure list-profiles | grep -qx "$ADMIN_PROFILE" ||
    die "ADMIN_PROFILE '$ADMIN_PROFILE' does not exist yet; create it with: aws login --profile $ADMIN_PROFILE"
  got=$(admin sts get-caller-identity --query Account --output text) ||
    die "ADMIN_PROFILE '$ADMIN_PROFILE' has no valid session (try: aws login --profile $ADMIN_PROFILE)"
  [ "$got" = "$ACCOUNT_ID" ] ||
    die "ADMIN_PROFILE '$ADMIN_PROFILE' is account $got, but '$ACCOUNT' is $ACCOUNT_ID"
}

# DRY_RUN prints the mutating calls instead of making them. `keys` and
# `rotate` have no printing path: they create and store a real access key
# whatever it is set to. Refusing is the honest answer; the alternative is a
# run that says DRY_RUN and rotates a live credential.
refuse_dry_run() {
  [ "$DRY_RUN" != 1 ] || die "$1 cannot be dry-run: it creates and stores a real access key. Unset DRY_RUN to run it."
}

# The agent user is meant to carry exactly what this script attaches: no
# groups, no inline policies, no unconfigured managed policy. Asking only whether it
# exists reads a user whose real permissions are its group's as though they
# were the rendered policy, and `check` then simulates a principal wider than
# anything this file describes. So enumerate the three ways an IAM user is
# widened and name whatever is there. Only the base and configured supplemental
# policy ARNs are accepted attachments.
# The agent user's real ARN, asked of IAM once and kept. An IAM user carries
# a path, and a user created under one is `user/automation/agent`, not
# `user/agent`: an ARN rebuilt from the account id and the user name is
# right only at the root path, and every identity comparison below would
# refuse a perfectly good key anywhere else -- `keys` and `rotate` would then
# delete the replacement key they had just created.
agent_user_arn() {
  if [ -z "$AGENT_USER_ARN" ]; then
    AGENT_USER_ARN=$(admin iam get-user --user-name "$AGENT_USER" \
      --query User.Arn --output text 2>/dev/null) || AGENT_USER_ARN=""
    case "$AGENT_USER_ARN" in
      arn:aws:iam::*:user/*) ;;
      *) AGENT_USER_ARN=""
         die "IAM user '$AGENT_USER' not found in $ACCOUNT_ID; create it: aws iam create-user --profile $ADMIN_PROFILE --user-name $AGENT_USER" ;;
    esac
  fi
  printf '%s' "$AGENT_USER_ARN"
}

require_agent_user() {
  attachment_filter="PolicyArn!='arn:aws:iam::$ACCOUNT_ID:policy/$POLICY_NAME'"
  for configured in $EXTRA_POLICIES; do
    attachment_filter="$attachment_filter && PolicyArn!='arn:aws:iam::$ACCOUNT_ID:policy/$configured'"
  done
  agent_user_arn >/dev/null
  extra=""
  found=$(admin iam list-groups-for-user --user-name "$AGENT_USER" \
    --query 'Groups[].GroupName' --output text) ||
    die "could not list the groups of '$AGENT_USER' in $ACCOUNT_ID"
  [ -z "$found" ] || [ "$found" = None ] || extra="$extra groups ($found)"
  found=$(admin iam list-user-policies --user-name "$AGENT_USER" \
    --query 'PolicyNames' --output text) ||
    die "could not list the inline policies of '$AGENT_USER' in $ACCOUNT_ID"
  [ -z "$found" ] || [ "$found" = None ] || extra="$extra inline policies ($found)"
  found=$(admin iam list-attached-user-policies --user-name "$AGENT_USER" \
    --query "AttachedPolicies[?$attachment_filter].PolicyName" --output text) ||
    die "could not list the attached policies of '$AGENT_USER' in $ACCOUNT_ID"
  [ -z "$found" ] || [ "$found" = None ] || extra="$extra other managed policies ($found)"
  [ -z "$extra" ] ||
    die "IAM user '$AGENT_USER' in $ACCOUNT_ID carries unconfigured permissions beyond $POLICY_NAME $EXTRA_POLICIES:$extra; detach them, or what this renders is not what the agent can do"
}

# Whose policy is at that ARN. The name is not an identity: a policy someone
# else created under it would take a new default version from `apply`, and
# every principal carrying that policy would silently change with it. So this
# tool tags what it creates and refuses to version anything else by name.
require_policy_is_ours() {
  policy_arn=$1
  tags=$(admin iam list-policy-tags --policy-arn "$policy_arn" \
    --query "Tags[?Key=='$POLICY_OWNER_TAG_KEY'].Value" --output text) ||
    die "could not read the tags of $policy_arn"
  case " $tags " in
    *" $POLICY_OWNER_TAG_VALUE "*) return 0 ;;
  esac
  die "$policy_arn exists but carries no $POLICY_OWNER_TAG_KEY=$POLICY_OWNER_TAG_VALUE tag, so this tool did not create it; adopt it deliberately with: aws iam tag-policy --profile $ADMIN_PROFILE --policy-arn $policy_arn --tags Key=$POLICY_OWNER_TAG_KEY,Value=$POLICY_OWNER_TAG_VALUE"
}

prune_policy_versions() {
  arn=$1
  # shellcheck disable=SC2016 # backticks are JMESPath literals, not shell
  versions=$(admin iam list-policy-versions --policy-arn "$arn" \
    --query 'Versions[?IsDefaultVersion==`false`].VersionId' --output text)
  # shellcheck disable=SC2086
  set -- $versions
  [ "$#" -ge $((MAX_POLICY_VERSIONS - 1)) ] || return 0
  oldest=$(printf '%s\n' "$@" | sed 's/^v//' | sort -n | head -1)
  admin_mutate iam delete-policy-version --policy-arn "$arn" --version-id "v$oldest"
}

cmd_apply() {
  load_account "$1"
  require_admin_profile
  require_aws
  arn="arn:aws:iam::$ACCOUNT_ID:policy/$POLICY_NAME"
  # A dry run prints the calls it would make and needs no session. Every
  # other path proves the admin profile really is this account first.
  if [ "$DRY_RUN" != 1 ]; then
    require_admin_account
    require_agent_user "$arn"
  fi
  policy_dir=$(mktemp -d)
  trap 'rm -rf "$policy_dir"' EXIT HUP INT TERM
  # Check every ownership guard before the first create, version, or attach.
  for policy in "$POLICY_NAME" $EXTRA_POLICIES; do
    render_named_policy "$policy" >"$policy_dir/$policy.json"
    "${PYTHON:-python3}" "$DIR/validate-policies.py" --document "$policy_dir/$policy.json" ||
      die "$ACCOUNT: invalid rendered policy $policy"
    arn="arn:aws:iam::$ACCOUNT_ID:policy/$policy"
    if admin iam get-policy --policy-arn "$arn" >/dev/null 2>&1; then
      require_policy_is_ours "$arn"
      touch "$policy_dir/$policy.exists"
    fi
  done
  for policy in "$POLICY_NAME" $EXTRA_POLICIES; do
    arn="arn:aws:iam::$ACCOUNT_ID:policy/$policy"
    if [ -f "$policy_dir/$policy.exists" ]; then
      prune_policy_versions "$arn"
      admin_mutate iam create-policy-version --policy-arn "$arn" \
        --policy-document "file://$policy_dir/$policy.json" --set-as-default >/dev/null
      echo "updated $arn (new default version, level $LEVEL)"
    else
      admin_mutate iam create-policy --policy-name "$policy" \
        --description "Scoped $LEVEL access for coding agents (local-aws-setup)" \
        --policy-document "file://$policy_dir/$policy.json" \
        --tags "Key=$POLICY_OWNER_TAG_KEY,Value=$POLICY_OWNER_TAG_VALUE" >/dev/null
      echo "created $arn (level $LEVEL)"
    fi
    admin_mutate iam attach-user-policy --user-name "$AGENT_USER" --policy-arn "$arn"
    echo "attached $policy to $AGENT_USER in $ACCOUNT"
  done
}

# The ARN a key pair really answers as, or nothing. The pair is written into a
# credentials file of its own under a private directory, so a key is proved
# before AGENT_PROFILE is touched and a key that never works never reaches the
# profile the agent reads.
probe_identity() {
  probe_dir=$(mktemp -d) || die "could not create a temporary directory"
  # The file below holds the new secret. A Ctrl-C, a SIGTERM or an `aws` that
  # is killed between here and the `rm -rf` would leave it on disk, which is
  # the one outcome this whole path exists to prevent. The trap removes the
  # directory however the shell leaves; it is cleared again after the normal
  # removal so a later exit does not run `rm -rf` on a name that is gone.
  # `cmd_apply` owns the only other EXIT trap and never reaches this function,
  # so nothing here replaces a trap something else is relying on.
  trap 'rm -rf "$probe_dir"' EXIT HUP INT TERM
  (umask 077
   printf '[probe]\naws_access_key_id = %s\naws_secret_access_key = %s\n' \
     "$1" "$2" >"$probe_dir/credentials")
  probe_arn=$(AWS_SHARED_CREDENTIALS_FILE="$probe_dir/credentials" \
    AWS_CONFIG_FILE="$probe_dir/config" \
    aws --profile probe --region "$AGENT_REGION" sts get-caller-identity \
    --query Arn --output text 2>/dev/null) || probe_arn=""
  rm -rf "$probe_dir"
  trap - EXIT HUP INT TERM
  printf '%s' "$probe_arn"
}

# Wait until a new key answers as the ARN its caller resolved. Identity, not a
# bare exit status: a call that succeeded as somebody else is the failure this
# is here to catch, and it is not propagation delay, so it does not wait.
#
# The wanted ARN is an argument and not a lookup, and neither side of the
# comparison may be empty. A lookup inside here would run with `set -e`
# suspended -- every caller asks this inside an `if !` -- so a failed lookup
# would leave an empty want, an unauthenticated key answers with an empty got,
# and empty equals empty would report the key as working.
wait_for_key() {
  want_arn=$1
  [ -n "$want_arn" ] || return 1
  tries=0
  while :; do
    got_arn=$(probe_identity "$2" "$3")
    if [ -n "$got_arn" ] && [ "$got_arn" = "$want_arn" ]; then
      return 0
    fi
    if [ -n "$got_arn" ]; then
      echo "aws-setup.sh: the new key answers as '$got_arn', not '$want_arn'" >&2
      return 1
    fi
    tries=$((tries + 1))
    [ "$tries" -lt "$KEY_PROPAGATION_TRIES" ] || return 1
    sleep "$KEY_PROPAGATION_SLEEP"
  done
}

# Write a key pair into AGENT_PROFILE with the secret on stdin. `aws configure
# set aws_secret_access_key "$secret"` puts it in a command line that every
# process on the machine can read out of `ps` for as long as the call runs;
# the wizard form reads all four answers from the pipe instead. printf is a
# shell builtin, so the secret is in no process's arguments at all. The fourth
# answer is empty, which leaves the profile's output format as it was.
#
# It returns a status and never dies. Its caller runs inside a command
# substitution, where `die` exits that subshell alone: the key it had just
# created would stay live with nothing left running to delete it.
write_profile() {
  printf '%s\n%s\n%s\n\n' "$1" "$2" "$AGENT_REGION" |
    aws configure --profile "$AGENT_PROFILE" >/dev/null
}

# Does the profile hold this key id? The write above is a pipe into a prompt,
# so a CLI that stopped reading its answers there would leave the profile
# untouched and say nothing; this turns that silence into a failure its caller
# can clean up after.
profile_holds() {
  stored=$(aws configure get aws_access_key_id --profile "$AGENT_PROFILE" 2>/dev/null) || stored=""
  [ -n "$stored" ] && [ "$stored" = "$1" ]
}

# Take the key fields back out of AGENT_PROFILE. `aws configure` writes the
# credentials file before the config file, so a call that fails between the two
# has persisted the key and still reported failure. `keys` refuses to run when
# the profile already holds a key, so leaving that one behind would refuse
# every later attempt in the name of a key that has just been deleted.
#
# It returns a status. A caller that reported the profile cleared while it
# still names a deleted key would send the operator away from the one thing
# left to repair by hand, so a failure here has to reach what the caller says.
clear_profile_key() {
  stored=$(aws configure get aws_access_key_id --profile "$AGENT_PROFILE" 2>/dev/null) || stored=""
  [ -n "$stored" ] || return 0
  if ! aws configure set aws_access_key_id "" --profile "$AGENT_PROFILE" 2>/dev/null; then
    echo "aws-setup.sh: could not clear $AGENT_PROFILE; it holds '$stored', which no longer exists" >&2
    return 1
  fi
  # The id is the field `keys` refuses on, so the secret failing on its own
  # leaves no refusal to explain -- but it leaves a secret for a key that is
  # gone, and the caller still has to say the profile was not fully cleared.
  if ! aws configure set aws_secret_access_key "" --profile "$AGENT_PROFILE" 2>/dev/null; then
    echo "aws-setup.sh: cleared the key id from $AGENT_PROFILE but not its secret" >&2
    return 1
  fi
  return 0
}

# Delete a key that must not stay live, saying so if it cannot be deleted.
discard_key() {
  admin iam delete-access-key --user-name "$AGENT_USER" --access-key-id "$1" >/dev/null 2>&1 ||
    echo "aws-setup.sh: could not delete the unusable key $1; delete it by hand" >&2
}

# Create a key for AGENT_USER, prove it answers as that user, then write it
# into AGENT_PROFILE. Prints the new key id on stdout; the secret only ever
# reaches ~/.aws/credentials and a 0600 file under a private temporary
# directory. A key that never answers is deleted again, so a failed run leaves
# neither a live orphan key nor a half-written profile.
store_new_key() {
  # Resolve the identity the new key has to answer as before creating it. A
  # lookup that fails afterwards leaves nothing to compare against, and a key
  # that cannot be proved has already been made by then.
  want_arn=$(agent_user_arn)
  [ -n "$want_arn" ] || die "could not read the ARN of '$AGENT_USER' in $ACCOUNT_ID"
  out=$(admin iam create-access-key --user-name "$AGENT_USER" \
    --query 'AccessKey.[AccessKeyId,SecretAccessKey]' --output text) ||
    die "could not create an access key for $AGENT_USER (a user holds at most two)"
  # Key id and secret are whitespace-free, so word splitting separates them.
  # shellcheck disable=SC2086
  set -- $out
  key_id=$1
  secret=$2
  { [ -n "$key_id" ] && [ -n "$secret" ]; } ||
    die "create-access-key returned no usable key for $AGENT_USER"
  if ! wait_for_key "$want_arn" "$key_id" "$secret"; then
    unset secret out
    discard_key "$key_id"
    die "new key $key_id never answered as $AGENT_USER in $ACCOUNT_ID; it was deleted and $AGENT_PROFILE was left alone"
  fi
  # Both the write and the check of what it wrote clean the key up here, where
  # the cleanup can still run. A `die` past this point leaves a live key that
  # no profile names, which is the orphan this whole path exists to avoid.
  if ! write_profile "$key_id" "$secret"; then
    unset secret out
    discard_key "$key_id"
    die "could not write the new key into profile $AGENT_PROFILE; key $key_id was deleted"
  fi
  unset secret out
  if ! profile_holds "$key_id"; then
    discard_key "$key_id"
    die "profile $AGENT_PROFILE did not take $key_id; the key was deleted, so nothing was left half-written"
  fi
  echo "$key_id"
}

# Does AGENT_PROFILE itself answer as the agent user?
profile_is_agent() {
  # Asked inside an `if !`, so `set -e` is suspended here too: both sides are
  # checked for emptiness rather than trusted to be there.
  want=$(agent_user_arn) || return 1
  [ -n "$want" ] || return 1
  got=$(aws --profile "$AGENT_PROFILE" --region "$AGENT_REGION" \
    sts get-caller-identity --query Arn --output text 2>/dev/null) || got=""
  [ -n "$got" ] && [ "$got" = "$want" ]
}

cmd_keys() {
  load_account "$1"
  require_admin_profile
  require_aws
  refuse_dry_run keys
  # An empty value is no key: a cleared profile has the field with nothing in
  # it, and refusing on the field alone would refuse every attempt after a
  # write that failed once.
  held=$(aws configure get aws_access_key_id --profile "$AGENT_PROFILE" 2>/dev/null) || held=""
  if [ -n "$held" ]; then
    die "profile '$AGENT_PROFILE' already has a key; use 'rotate $ACCOUNT' to replace it"
  fi
  require_admin_account
  # store_new_key has already said what went wrong and cleaned up after
  # itself; it created no key that outlives this exit.
  key_id=$(store_new_key) || {
    clear_profile_key ||
      echo "aws-setup.sh: $AGENT_PROFILE still holds a key that no longer exists; clear it by hand before running 'keys' again" >&2
    exit 1
  }
  # store_new_key proved the key from a file of its own, which says nothing
  # about the profile it was written into: a role_arn, a source_profile or a
  # stale session token left in AGENT_PROFILE can break the profile while the
  # raw key works. Ask the profile itself before reporting success.
  if ! profile_is_agent; then
    if clear_profile_key; then
      cleared="$AGENT_PROFILE was cleared"
    else
      cleared="$AGENT_PROFILE could not be cleared and needs repairing by hand"
    fi
    discard_key "$key_id"
    die "profile $AGENT_PROFILE did not authenticate as $AGENT_USER; $key_id was deleted and $cleared"
  fi
  echo "stored key $key_id in profile $AGENT_PROFILE"
}

# Put AGENT_PROFILE back to the key it held, and set `repair` to what really
# happened: the old secret is only restorable if it was readable in the first
# place, so this says which of the two it was rather than claiming the repair
# either way. It is a function and not a substitution, so the caller sees the
# variables it sets.
restore_old_profile() {
  # Nothing to put back if nothing was taken away: the store fails before it
  # writes more often than after, and rewriting the profile it never touched
  # would be a change made in the name of a repair.
  if profile_holds "$old_id"; then
    repair="the profile still holds it"
    unset old_secret
    return 0
  fi
  if [ -n "$old_secret" ] && write_profile "$old_id" "$old_secret"; then
    repair="the profile was put back"
  else
    repair="write $old_id back into $AGENT_PROFILE by hand"
  fi
  unset old_secret
}

cmd_rotate() {
  load_account "$1"
  require_admin_profile
  require_aws
  refuse_dry_run rotate
  old_id=$(aws configure get aws_access_key_id --profile "$AGENT_PROFILE" 2>/dev/null) ||
    die "profile '$AGENT_PROFILE' has no key; use 'keys $ACCOUNT' first"
  # Kept so the profile can be put back exactly as it was. It is a shell
  # variable in this process and is unset on both paths out.
  old_secret=$(aws configure get aws_secret_access_key --profile "$AGENT_PROFILE" 2>/dev/null) || old_secret=""
  require_admin_account
  # store_new_key proves the new key from a file of its own and deletes it
  # again if it never answers, so the old key is still the live one here and
  # the profile is only written once the new key has worked.
  # A store that failed part-way has deleted its own key, but it may have
  # written the profile first, so the rollback runs on that path too.
  if ! new_id=$(store_new_key); then
    restore_old_profile
    die "could not store a new key for $AGENT_USER; $old_id is still active and $repair"
  fi
  if ! profile_is_agent; then
    # The key worked from a private file but the profile does not answer with
    # it. Put back what was there, and delete the key nobody can use.
    restore_old_profile
    discard_key "$new_id"
    die "profile $AGENT_PROFILE did not take the new key; $old_id is still active and $repair"
  fi
  unset old_secret
  admin iam delete-access-key --user-name "$AGENT_USER" --access-key-id "$old_id"
  echo "rotated $old_id -> $new_id"
}

# --- simulator checks --------------------------------------------------------

# expect <decision> <action> <resource> [context-key=value]...
# SIM_MODE "custom" simulates the rendered policy (SIM_POLICY); "principal"
# simulates the policies attached to SIM_USER_ARN.
expect() {
  want=$1 action=$2 resource=$3
  shift 3
  label="$action $resource${*:+ [$*]}"
  entries=""
  for kv in "$@"; do
    key=${kv%%=*}
    type=string
    [ "$key" != aws:TagKeys ] || type=stringList
    entries="$entries ContextKeyName=$key,ContextKeyValues=${kv#*=},ContextKeyType=$type"
  done
  set -- --action-names "$action" --resource-arns "$resource" \
    --query 'EvaluationResults[0].EvalDecision' --output text
  set -f
  # shellcheck disable=SC2086 # entries hold no spaces or globs
  [ -z "$entries" ] || set -- "$@" --context-entries $entries
  set +f
  # --region on both: IAM is global, but the CLI refuses a call without a
  # region, and an `aws login` profile carries none -- the same reason the
  # admin wrapper passes it.
  if [ "$SIM_MODE" = custom ]; then
    set -- "$SIM_POLICY" "$@"
    for policy in $EXTRA_POLICIES; do
      set -- "$(render_named_policy "$policy")" "$@"
    done
    got=$(aws --profile "$SIM_PROFILE" --region "$AGENT_REGION" \
      iam simulate-custom-policy --policy-input-list "$@")
  else
    got=$(aws --profile "$SIM_PROFILE" --region "$AGENT_REGION" \
      iam simulate-principal-policy --policy-source-arn "$SIM_USER_ARN" "$@")
  fi
  if [ "$got" = "$want" ]; then
    echo "PASS $want $label"
  else
    echo "FAIL want $want, got $got: $label"
    FAILURES=$((FAILURES + 1))
  fi
}

# Decision for an action allowed only from level $1 upward.
from_level() {
  case "$1:$LEVEL" in
    readonly:*|operator:operator|operator:sandbox|sandbox:sandbox) echo allowed ;;
    *) echo implicitDeny ;;
  esac
}

expect_ec2() {
  account=$1
  instance="arn:aws:ec2:$AGENT_REGION:$account:instance/i-0123456789abcdef0"
  tagged="aws:ResourceTag/$MANAGED_TAG_KEY=true"
  untagged="aws:ResourceTag/$MANAGED_TAG_KEY=false"
  expect allowed ec2:DescribeInstances "*"
  expect "$(from_level operator)" ec2:StartInstances "$instance" "$tagged"
  expect "$(from_level operator)" ec2:StopInstances "$instance" "$tagged"
  # explicitDeny, not implicitDeny: an untagged instance is outside the
  # conditional Allow either way, but only the Deny survives a second policy.
  expect explicitDeny ec2:StopInstances "$instance" "$untagged"
  expect "$(from_level sandbox)" ec2:RunInstances "$instance" "aws:RequestTag/$MANAGED_TAG_KEY=true"
  expect explicitDeny ec2:RunInstances "$instance"
  if [ "$LEVEL" = sandbox ]; then
    expect allowed ec2:TerminateInstances "$instance" "$tagged"
    expect explicitDeny ec2:TerminateInstances "$instance" "$untagged"
    expect allowed ec2:CreateTags "$instance" "aws:TagKeys=$MANAGED_TAG_KEY" "ec2:CreateAction=RunInstances"
  else
    expect explicitDeny ec2:TerminateInstances "$instance" "$tagged"
  fi
  expect explicitDeny ec2:CreateTags "$instance" "aws:TagKeys=$MANAGED_TAG_KEY"
  expect explicitDeny ec2:DeleteTags "$instance" "aws:TagKeys=$MANAGED_TAG_KEY"
}

expect_lifecycle() {
  tagged="aws:ResourceTag/$MANAGED_TAG_KEY=true"
  requested="aws:RequestTag/$MANAGED_TAG_KEY=true"
  image="arn:aws:ec2:$AGENT_REGION::image/ami-0123456789abcdef0"
  lifecycle_decision=explicitDeny
  [ "$LEVEL" != sandbox ] || lifecycle_decision=allowed
  # Every route from a managed disk to a reader, at every level (sd:2870).
  # Listed here again, not read from the policy, so a dropped entry fails.
  for disk_read in ec2:CreateSnapshot ec2:CreateSnapshots ec2:CreateImage ec2:RegisterImage \
      ec2:CopySnapshot ec2:CopyImage ec2:ModifySnapshotAttribute ec2:ModifyImageAttribute \
      ec2:CreateVolume ec2:AttachVolume ec2:DetachVolume ec2:CreateReplaceRootVolumeTask \
      ec2:CreateStoreImageTask ec2:ExportImage ec2:CreateInstanceExportTask \
      ec2:CreateFleet ec2:RequestSpotInstances ec2:RequestSpotFleet \
      ebs:GetSnapshotBlock ebs:ListSnapshotBlocks ebs:ListChangedBlocks \
      ec2-instance-connect:SendSSHPublicKey ec2-instance-connect:SendSerialConsoleSSHPublicKey \
      ec2-instance-connect:OpenTunnel; do
    expect explicitDeny "$disk_read" "*" "$tagged"
  done
  expect explicitDeny ec2:RunInstances "$image" "$tagged"
  expect explicitDeny ec2:RunInstances "arn:aws:ec2:$AGENT_REGION::snapshot/snap-0123456789abcdef0" "$tagged"
  # The deployer's launch from a public image.
  expect "$(from_level sandbox)" ec2:RunInstances "$image"
  for pair in DisassociateAddress:elastic-ip DisassociateAddress:network-interface ReleaseAddress:elastic-ip DeleteSecurityGroup:security-group DeleteKeyPair:key-pair DeleteVolume:volume DeleteNetworkInterface:network-interface DeleteSnapshot:snapshot; do
    # Not `action`: expect() assigns that global, and the next probe would
    # ask for ec2:ec2:<Action>, which the simulator answers implicitDeny.
    teardown=${pair%%:*}
    kind=${pair#*:}
    owner=$ACCOUNT_ID
    [ "$kind" != snapshot ] || owner=""
    probe="arn:aws:ec2:$AGENT_REGION:$owner:$kind/probe"
    expect "$lifecycle_decision" "ec2:$teardown" "$probe" "$tagged"
    expect explicitDeny "ec2:$teardown" "$probe"
    expect explicitDeny "ec2:$teardown" "$probe" "aws:ResourceTag/$MANAGED_TAG_KEY=false"
  done
  expect explicitDeny ec2:ModifyInstanceAttribute "arn:aws:ec2:$AGENT_REGION:$ACCOUNT_ID:instance/probe" "$tagged"
  rule="arn:aws:ec2:$AGENT_REGION:$ACCOUNT_ID:security-group-rule/sgr-probe"
  for operation in AuthorizeSecurityGroupIngress AuthorizeSecurityGroupEgress; do
    expect "$lifecycle_decision" ec2:CreateTags "$rule" "$requested" "aws:TagKeys=$MANAGED_TAG_KEY" "ec2:CreateAction=$operation"
  done
  expect explicitDeny ec2:CreateTags "$rule" "aws:TagKeys=$MANAGED_TAG_KEY"
}

# Every bucket the account file names, not the first word of the list. Taking
# the first left the rest of a multi-bucket account unchecked; and a list that
# happened to start with a space produced an empty name, which then skipped
# the bucket cases altogether and reported the account as passing.
expect_s3() {
  checked=0
  for b in $S3_BUCKETS; do
    object="arn:aws:s3:::$b/probe.txt"
    expect allowed s3:GetObject "$object"
    expect "$(from_level operator)" s3:PutObject "$object"
    expect "$(from_level sandbox)" s3:DeleteObject "$object"
    expect explicitDeny s3:DeleteBucket "arn:aws:s3:::$b"
    checked=$((checked + 1))
  done
  if [ -n "$S3_BUCKETS" ] && [ "$checked" = 0 ]; then
    die "$ACCOUNT: S3_BUCKETS is set but names no bucket to check: '$S3_BUCKETS'"
  fi
  expect implicitDeny s3:GetObject "arn:aws:s3:::not-a-configured-bucket/probe.txt"
}

run_expectations() {
  FAILURES=0
  expect_ec2 "$1"
  expect_lifecycle
  expect_s3
  expect explicitDeny iam:CreateAccessKey "*"
  expect explicitDeny sts:AssumeRole "*"
  for role in $PASS_ROLE_ARNS; do
    expect allowed iam:PassRole "$role" "iam:PassedToService=ec2.amazonaws.com"
    expect explicitDeny iam:PassRole "$role" "iam:PassedToService=lambda.amazonaws.com"
    expect explicitDeny iam:PassRole "$role"
    expect explicitDeny iam:UpdateAssumeRolePolicy "$role"
  done
  if [ -n "$PASS_ROLE_ARNS" ]; then
    expect explicitDeny iam:PassRole "arn:aws:iam::000000000000:role/unapproved" "iam:PassedToService=ec2.amazonaws.com"
  fi
  if [ "$FAILURES" -gt 0 ]; then
    die "$FAILURES expectation(s) failed for $ACCOUNT ($LEVEL)"
  fi
  echo "all expectations met for $ACCOUNT ($LEVEL)"
}

cmd_simulate() {
  load_account "$1"
  require_aws
  aws --profile "$SIMULATE_PROFILE" --region "$AGENT_REGION" sts get-caller-identity >/dev/null ||
    die "SIMULATE_PROFILE '$SIMULATE_PROFILE' has no valid session (try: aws login --profile $SIMULATE_PROFILE)"
  SIM_MODE=custom
  SIM_PROFILE=$SIMULATE_PROFILE
  # Passed inline: file:// makes the CLI parse the JSON as the list itself,
  # which the API rejects as "invalid content".
  SIM_POLICY=$(render_policy)
  run_expectations "$ACCOUNT_ID"
}

cmd_check() {
  load_account "$1"
  require_admin_profile
  require_aws
  who=$(aws --profile "$AGENT_PROFILE" --region "$AGENT_REGION" \
    sts get-caller-identity --query Arn --output text) ||
    die "profile '$AGENT_PROFILE' cannot authenticate; run 'keys $ACCOUNT' first"
  require_admin_account
  want=$(agent_user_arn)
  [ -n "$want" ] || die "could not read the ARN of '$AGENT_USER' in $ACCOUNT_ID"
  [ "$who" = "$want" ] ||
    die "profile '$AGENT_PROFILE' is $who, expected $want"
  echo "PASS profile $AGENT_PROFILE is $who"
  require_agent_user "arn:aws:iam::$ACCOUNT_ID:policy/$POLICY_NAME"
  SIM_MODE=principal
  SIM_PROFILE=$ADMIN_PROFILE
  SIM_USER_ARN=$who
  run_expectations "$ACCOUNT_ID"
}

# --- entrypoint --------------------------------------------------------------

need_account() {
  [ -n "$2" ] || { echo "usage: $(basename "$0") $1 <account>" >&2; exit 1; }
}

case "${1:-}" in
  accounts) cmd_accounts ;;
  render)   need_account "$1" "${2:-}"; load_account "$2"; render_named_policy "${3:-}" ;;
  simulate) need_account "$1" "${2:-}"; cmd_simulate "$2" ;;
  apply)    need_account "$1" "${2:-}"; cmd_apply "$2" ;;
  keys)     need_account "$1" "${2:-}"; cmd_keys "$2" ;;
  rotate)   need_account "$1" "${2:-}"; cmd_rotate "$2" ;;
  check)    need_account "$1" "${2:-}"; cmd_check "$2" ;;
  test)     shift
            exec "${PYTHON:-python3}" -m unittest discover -s "$DIR/tests" -t "$DIR" "$@" ;;
  -h|--help|help) show_help ;;
  *)
    usage
    exit 1
    ;;
esac

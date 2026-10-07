"""`aws-setup.sh`, against a fake `aws` on a private PATH -- never an account.

Two halves, because the script has two jobs and they fail differently.

The rendered policy is checked as JSON, not as text: every case here parses
the document and asks about statements, so a reordering or a reformat changes
nothing and a missing Deny changes everything. The claims are the ones a
conditional Allow cannot make on its own. An Allow carrying a tag condition
already leaves an untagged instance outside it, so the untagged Deny looks
redundant -- until a second policy grants `ec2:*` and the conditional Allow
turns out to have said nothing about that. That is what these assert: the
Denies render at every level, including `readonly`, where nothing is allowed
for them to contradict.

The command paths are checked by watching the calls. `keys` and `rotate`
handle a secret and a live credential, and what matters about them is not
their output but what left the machine and in which order: whether the secret
was ever an argument, and whether the old key outlived the new one's first
successful call.

The fake never sleeps and nothing here reaches the network. KEY_PROPAGATION_*
are read from the environment so a probe that gives up does so at once.
"""

import json
import os
import pathlib
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

HERE = pathlib.Path(__file__).resolve().parent
SCRIPT = HERE.parent / "aws-setup.sh"
README = HERE.parent / "README.md"
FAKE = HERE / "fake_aws.py"

ACCOUNT_ID = "123456789012"
AGENT_ARN = "arn:aws:iam::%s:user/agent" % ACCOUNT_ID
ADMIN_ARN = "arn:aws:iam::%s:user/admin" % ACCOUNT_ID
OTHER_ARN = "arn:aws:iam::999988887777:user/someone-else"
OLD_KEY = "AKIAOLDKEY"
NEW_KEY = "AKIANEWKEY"
NEW_SECRET = "s3cret-that-must-not-be-an-argument"

# Every route to a deployment's disk (sd:2870), in the script's order. Every
# level denies them, except that sandbox keeps ec2:RunInstances for the deployer.
DISK_ROUTES = (
    "ec2:RunInstances", "ec2:CreateFleet", "ec2:RequestSpotInstances", "ec2:RequestSpotFleet",
    "ec2:CreateSnapshot", "ec2:CreateSnapshots", "ec2:CopySnapshot", "ec2:CreateImage", "ec2:RegisterImage", "ec2:CopyImage",
    "ec2:CreateVolume", "ec2:CreateRestoreImageTask", "ec2:CreateStoreImageTask", "ec2:ExportImage", "ec2:CreateInstanceExportTask",
    "ec2:ModifySnapshotAttribute", "ec2:ModifyImageAttribute",
    "ec2:AttachVolume", "ec2:DetachVolume", "ec2:CreateReplaceRootVolumeTask",
    "ebs:GetSnapshotBlock", "ebs:ListSnapshotBlocks", "ebs:ListChangedBlocks",
    "ec2-instance-connect:SendSSHPublicKey", "ec2-instance-connect:SendSerialConsoleSSHPublicKey",
    "ec2-instance-connect:OpenTunnel", "ec2:GetConsoleScreenshot",
)


def disk_routes(level):
    """The routes a level denies outright."""
    return [r for r in DISK_ROUTES if not (level == "sandbox" and r == "ec2:RunInstances")]


def render(level, buckets="", tag_key="claude-managed", pass_roles=""):
    """The policy document for a level, parsed."""
    with tempfile.TemporaryDirectory() as home:
        root = pathlib.Path(home)
        accounts = root / "config" / "aws-setup" / "accounts"
        accounts.mkdir(parents=True)
        (accounts / "x.env").write_text(
            "ACCOUNT_ID=%s\nLEVEL=%s\nADMIN_PROFILE=admin-x\n"
            "AGENT_PROFILE=agent-x\nS3_BUCKETS='%s'\nPASS_ROLE_ARNS='%s'\n" % (ACCOUNT_ID, level, buckets, pass_roles)
        )
        done = subprocess.run(
            ["/bin/sh", str(SCRIPT), "render", "x"],
            capture_output=True, text=True,
            env={**os.environ, "MANAGED_TAG_KEY": tag_key,
                 "SYSTEM_TOOLS_CONFIG": str(root / "config")},
        )
        if done.returncode != 0:
            raise AssertionError("render failed: " + done.stderr)
        return json.loads(done.stdout)


def statement(document, sid):
    for item in document["Statement"]:
        if item.get("Sid") == sid:
            return item
    return None


def actions_of(item):
    action = item["Action"]
    return [action] if isinstance(action, str) else action


class Sandbox:
    """The script with a fake `aws`, an account file in its own config
    directory, and a log."""

    def __init__(self, level="operator", buckets="", **account):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.config = self.dir / "config"
        self.accounts = self.config / "aws-setup" / "accounts"
        self.accounts.mkdir(parents=True)
        fields = {
            "ACCOUNT_ID": ACCOUNT_ID,
            "LEVEL": level,
            "ADMIN_PROFILE": "admin-x",
            "AGENT_PROFILE": "agent-x",
            "AGENT_REGION": "eu-west-1",
            "S3_BUCKETS": buckets,
        }
        fields.update(account)
        (self.accounts / "x.env").write_text(
            "".join("%s='%s'\n" % pair for pair in fields.items())
        )
        binaries = self.dir / "bin"
        binaries.mkdir()
        shim = binaries / "aws"
        shim.write_text('#!/bin/sh\nexec %s %s "$@"\n' % (sys.executable, FAKE))
        shim.chmod(0o755)
        self.log = self.dir / "calls.jsonl"
        self.state_path = self.dir / "state.json"
        self.scenario_path = self.dir / "scenario.json"
        self.state = {"profiles": {}, "created": [], "deleted": []}
        self.scenario = {"identities": {}, "rules": [], "new_key": [NEW_KEY, NEW_SECRET]}

    def profile(self, name, **values):
        self.state["profiles"][name] = values
        return self

    def user_arn(self, arn):
        self.scenario["user_arn"] = arn
        return self

    def identity(self, key_id, arn):
        self.scenario["identities"][key_id] = arn
        return self

    def partial_write(self, failing=True):
        self.scenario["configure_fails_after_credentials"] = failing
        return self

    def refuse(self, *match, **answer):
        self.scenario.setdefault("refusals", []).append({"match": list(match), **answer})
        return self

    def rule(self, *match, **answer):
        self.scenario["rules"].append({"match": list(match), **answer})
        return self

    def hang(self, *match):
        """Block the fake on this exact call until the script is killed."""
        self.scenario["hang"] = list(match)
        return self

    def start(self, *args, **environment):
        """Launch the script without waiting, so a test can signal it."""
        self.state_path.write_text(json.dumps(self.state))
        self.scenario_path.write_text(json.dumps(self.scenario))
        return subprocess.Popen(
            ["/bin/sh", str(SCRIPT), *args],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            # Its own session, so a test can signal the whole group the way a
            # terminal does. The probe runs in a command substitution, which
            # is a separate process: a signal sent to the script alone reaches
            # the shell that is waiting, never the one holding the secret.
            start_new_session=True,
            env={
                "PATH": "%s:%s" % (self.dir / "bin", os.environ["PATH"]),
                "HOME": str(self.dir),
                "SYSTEM_TOOLS_CONFIG": str(self.config),
                "FAKE_AWS_LOG": str(self.log),
                "FAKE_AWS_STATE": str(self.state_path),
                "FAKE_AWS_SCENARIO": str(self.scenario_path),
                "KEY_PROPAGATION_TRIES": "2",
                "KEY_PROPAGATION_SLEEP": "0",
                **environment,
            },
        )

    def run(self, *args, **environment):
        self.state_path.write_text(json.dumps(self.state))
        self.scenario_path.write_text(json.dumps(self.scenario))
        done = subprocess.run(
            ["/bin/sh", str(SCRIPT), *args],
            capture_output=True, text=True,
            env={
                "PATH": "%s:%s" % (self.dir / "bin", os.environ["PATH"]),
                "HOME": str(self.dir),
                "SYSTEM_TOOLS_CONFIG": str(self.config),
                "FAKE_AWS_LOG": str(self.log),
                "FAKE_AWS_STATE": str(self.state_path),
                "FAKE_AWS_SCENARIO": str(self.scenario_path),
                "KEY_PROPAGATION_TRIES": "2",
                "KEY_PROPAGATION_SLEEP": "0",
                **environment,
            },
        )
        self.state = json.loads(self.state_path.read_text())
        return done

    def calls(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines() if line]

    def cleanup(self):
        shutil.rmtree(self.dir, ignore_errors=True)


class PolicyCase(unittest.TestCase):
    """What the rendered document denies, asked of the JSON."""

    def test_hard_denies_name_modify_instance_attribute(self):
        # It rewrites an instance's user data, which the guest runs as root on
        # its next boot: "may stop this instance" becomes "may run code on it".
        for level in ("readonly", "operator", "sandbox"):
            with self.subTest(level=level):
                denied = actions_of(statement(render(level), "HardDenies"))
                self.assertIn("ec2:ModifyInstanceAttribute", denied)

    def test_hard_denies_hold_the_public_access_block_with_one_action(self):
        # s3:PutBucketPublicAccessBlock is the permission AWS checks for both
        # PutPublicAccessBlock and DeletePublicAccessBlock, so it denies
        # setting and deleting the block alike. There is no
        # s3:DeleteBucketPublicAccessBlock IAM action: naming one renders a
        # document that Access Analyzer reports as INVALID_ACTION.
        denied = actions_of(statement(render("sandbox"), "HardDenies"))
        self.assertIn("s3:PutBucketPublicAccessBlock", denied)
        self.assertNotIn("s3:DeleteBucketPublicAccessBlock", denied)

    def test_every_rendered_action_is_a_well_formed_iam_action(self):
        # An API operation name is not an IAM action name, and a document that
        # confuses the two denies nothing while looking as though it does.
        # Both shapes are checked: the grammar, and the services this script
        # is allowed to speak about at all.
        grammar = re.compile(r"[a-z0-9-]+:[A-Za-z0-9*]+")
        services = {"account", "cloudtrail", "cloudwatch", "ebs", "ec2",
                    "ec2-instance-connect", "iam", "kms", "organizations",
                    "s3", "sts"}
        for level in ("readonly", "operator", "sandbox"):
            for buckets in ("", "claude-managed-bucket"):
                document = render(level, buckets=buckets)
                for item in document["Statement"]:
                    for action in actions_of(item):
                        with self.subTest(level=level, action=action):
                            self.assertTrue(grammar.fullmatch(action), action)
                            self.assertIn(action.split(":")[0], services)

    def test_hard_denies_name_put_event_selectors(self):
        # Stops a trail recording a class of events without stopping the trail,
        # which the three CloudTrail entries beside it do not cover.
        denied = actions_of(statement(render("operator"), "HardDenies"))
        self.assertIn("cloudtrail:PutEventSelectors", denied)

    def test_managing_an_untagged_instance_is_denied_at_every_level(self):
        for level in ("readonly", "operator", "sandbox"):
            with self.subTest(level=level):
                deny = statement(render(level), "DenyManagingUntaggedInstances")
                self.assertIsNotNone(deny, "no untagged Deny at level " + level)
                self.assertEqual(deny["Effect"], "Deny")
                for action in ("ec2:StartInstances", "ec2:StopInstances",
                               "ec2:RebootInstances", "ec2:GetConsoleOutput",
                               "ec2:TerminateInstances"):
                    self.assertIn(action, actions_of(deny))
                self.assertEqual(
                    deny["Condition"],
                    {"StringNotEqualsIfExists": {"aws:ResourceTag/claude-managed": "true"}},
                )

    def test_launching_without_the_managed_tag_is_denied_at_every_level(self):
        for level in ("readonly", "operator", "sandbox"):
            with self.subTest(level=level):
                deny = statement(render(level), "DenyLaunchWithoutManagedTag")
                self.assertIsNotNone(deny, "no launch Deny at level " + level)
                self.assertEqual(deny["Effect"], "Deny")
                self.assertEqual(actions_of(deny), ["ec2:RunInstances"])
                self.assertEqual(
                    deny["Condition"],
                    {"StringNotEqualsIfExists": {"aws:RequestTag/claude-managed": "true"}},
                )

    def test_every_deny_condition_uses_the_if_exists_form(self):
        # A Deny with a negated operator has to hold when the key is missing,
        # which is the untagged case itself. Enumerated from the document so a
        # Deny added later cannot quietly use the plain form.
        for level in ("readonly", "operator", "sandbox"):
            for item in render(level)["Statement"]:
                if item["Effect"] != "Deny":
                    continue
                for operator in item.get("Condition", {}):
                    if "Not" not in operator:
                        continue
                    with self.subTest(level=level, sid=item["Sid"], operator=operator):
                        self.assertTrue(operator.endswith("IfExists"), operator)

    def test_the_tag_on_launch_exception_survives_a_missing_create_action(self):
        deny = statement(render("sandbox"), "DenyChangingManagedTag")
        self.assertIn("StringNotEqualsIfExists", deny["Condition"])
        self.assertEqual(
            deny["Condition"]["StringNotEqualsIfExists"],
            {"ec2:CreateAction": ["RunInstances", "CreateSecurityGroup", "ImportKeyPair", "AllocateAddress", "AuthorizeSecurityGroupIngress", "AuthorizeSecurityGroupEgress"]},
        )

    def test_the_managed_tag_key_reaches_every_statement_that_names_it(self):
        document = render("sandbox", tag_key="team-owned")
        text = json.dumps(document)
        self.assertNotIn("claude-managed", text)
        self.assertIn("aws:ResourceTag/team-owned", text)
        self.assertIn("aws:RequestTag/team-owned", text)

    def test_the_document_is_valid_json_at_every_level(self):
        for level in ("readonly", "operator", "sandbox"):
            with self.subTest(level=level):
                document = render(level, buckets="one two")
                self.assertEqual(document["Version"], "2012-10-17")
                self.assertTrue(document["Statement"])


class DocumentationCase(unittest.TestCase):
    """What the levels claim, against what they grant."""

    def test_console_output_is_named_where_the_level_is_described(self):
        # The operator level grants it, and both descriptions of that level
        # used to stop at start/stop/reboot.
        granted = actions_of(statement(render("operator"), "Ec2ManageTaggedInstances"))
        self.assertIn("ec2:GetConsoleOutput", granted)
        help_text = subprocess.run(
            ["/bin/sh", str(SCRIPT), "--help"], capture_output=True, text=True,
        ).stdout
        self.assertIn("console output", help_text)
        self.assertIn("console output", README.read_text())

    def test_every_admin_command_in_the_readme_names_a_region(self):
        # An `aws login` profile carries no region and the CLI refuses a call
        # without one, so a documented admin command with no --region fails
        # as written (sd:1364). Continuation lines join first.
        text = README.read_text().replace("\\\n", " ")
        commands = [line.strip() for line in text.splitlines()
                    if re.match(r"\s*aws \S+ \S+.*--profile admin-", line)]
        self.assertTrue(commands, "the README shows no admin command")
        for command in commands:
            self.assertIn("--region", command, command)


    def test_a_per_account_policy_name_is_documented(self):
        # An account file may name its own policy, for an account whose
        # agent-base belongs to someone else; the help, README and example
        # used to show POLICY_NAME as a shared setting only.
        help_text = subprocess.run(
            ["/bin/sh", str(SCRIPT), "--help"], capture_output=True, text=True,
        ).stdout
        account_block = help_text.split("Account file", 1)[1].split("Shared settings", 1)[0]
        self.assertIn("POLICY_NAME", account_block)
        self.assertIn("| `POLICY_NAME` |", README.read_text())
        example = HERE.parent / "accounts" / "example.env.example"
        self.assertIn("#POLICY_NAME=", example.read_text())

class CommandCase(unittest.TestCase):
    """What the commands do, watched call by call."""

    def sandbox(self, **kwargs):
        box = Sandbox(**kwargs)
        self.addCleanup(box.cleanup)
        return box

    def ready(self, **kwargs):
        """A box where the admin profile works and the agent user is clean."""
        box = self.sandbox(**kwargs)
        box.profile("admin-x", aws_access_key_id="AKIAADMIN")
        box.identity("AKIAADMIN", ADMIN_ARN)
        box.user_arn(AGENT_ARN)
        return box

    def test_agent_profile_may_not_be_the_admin_profile(self):
        # The agent key is written into AGENT_PROFILE; naming the admin
        # profile there would overwrite the credentials that created it.
        box = self.sandbox(AGENT_PROFILE="admin-x")
        done = box.run("render", "x")
        self.assertEqual(done.returncode, 1)
        self.assertIn("would overwrite the admin profile", done.stderr)

    def test_a_mapping_only_account_lists_and_renders(self):
        # A file kept only to name an account (sd:2472) has no admin profile;
        # listing and rendering never use one.
        box = self.sandbox(ADMIN_PROFILE="")
        done = box.run("accounts")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(ACCOUNT_ID, done.stdout)
        done = box.run("render", "x")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(box.calls(), [])

    def test_the_admin_commands_still_require_the_admin_profile(self):
        for command in ("apply", "keys", "rotate", "check"):
            with self.subTest(command=command):
                box = self.sandbox(ADMIN_PROFILE="")
                done = box.run(command, "x")
                self.assertEqual(done.returncode, 1)
                self.assertIn("x: ADMIN_PROFILE is required", done.stderr)
                self.assertEqual(box.calls(), [])

    def test_a_dry_run_prints_the_region_it_would_pass(self):
        box = self.ready()
        box.rule("iam", "get-policy", exit=255)
        done = box.run("apply", "x", DRY_RUN="1")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("--region eu-west-1", done.stderr)
        self.assertIn("DRY_RUN: aws --profile admin-x --region eu-west-1", done.stderr)

    def test_the_admin_profile_probe_reads_every_listed_profile(self):
        # grep -q stops at its first match and closes the pipe while aws is
        # still writing the list; aws then prints a BrokenPipeError on stderr
        # (sd:2878). The list must outgrow the pipe buffer to show it.
        box = self.ready()
        for n in range(60000):
            box.profile("zz-profile-%04d" % n)
        done = box.run("apply", "x")
        self.assertTrue(any(c["rest"][:2] == ["configure", "list-profiles"] for c in box.calls()))
        self.assertNotIn("Broken pipe", done.stderr)
        self.assertNotIn("BrokenPipeError", done.stderr)

    def test_an_account_policy_name_overrides_the_shared_one(self):
        box = self.ready(POLICY_NAME="account-policy")
        (box.config / "aws-setup" / ".env").write_text("POLICY_NAME=shared-policy\n")
        box.rule("iam", "get-policy", exit=255)
        done = box.run("apply", "x", DRY_RUN="1")
        self.assertEqual(done.returncode, 0, done.stderr)
        named = " ".join(" ".join(c["rest"]) for c in box.calls()) + done.stderr
        self.assertIn("policy/account-policy", named)
        self.assertIn("--policy-name account-policy", done.stderr)
        self.assertNotIn("shared-policy", named)

    def test_a_dry_run_previews_the_branch_it_would_really_take(self):
        # get-policy is read-only, so a dry run asks it. Skipping it printed a
        # create for every dry run, including accounts that hold the policy.
        box = self.ready()
        box.rule("iam", "get-policy", exit=0)
        box.rule("list-policy-tags", stdout="local-aws-setup")
        done = box.run("apply", "x", DRY_RUN="1")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("create-policy-version", done.stderr)
        self.assertNotIn("DRY_RUN: aws --profile admin-x --region eu-west-1 iam create-policy ",
                         done.stderr)

    def test_apply_refuses_a_policy_it_did_not_create(self):
        box = self.ready()
        box.identity("AKIAADMIN", ADMIN_ARN)
        box.rule("iam", "get-policy", exit=0)
        box.rule("list-policy-tags", stdout="")
        done = box.run("apply", "x")
        self.assertEqual(done.returncode, 1)
        self.assertIn("carries no managed-by=local-aws-setup tag", done.stderr)
        self.assertFalse([c for c in box.calls() if "create-policy-version" in c["rest"]])

    def test_a_policy_this_tool_creates_carries_its_tag(self):
        box = self.ready()
        box.rule("iam", "get-policy", exit=255)
        done = box.run("apply", "x")
        self.assertEqual(done.returncode, 0, done.stderr)
        created = [c for c in box.calls() if c["rest"][:2] == ["iam", "create-policy"]]
        self.assertEqual(len(created), 1)
        self.assertIn("Key=managed-by,Value=local-aws-setup", created[0]["rest"])

    def test_a_widened_agent_user_is_refused(self):
        for call, noise in (("list-groups-for-user", "admins"),
                            ("list-user-policies", "extra-inline"),
                            ("list-attached-user-policies", "AdministratorAccess")):
            with self.subTest(call=call):
                box = self.ready()
                box.rule("iam", "get-policy", exit=255)
                box.rule(call, stdout=noise)
                done = box.run("apply", "x")
                self.assertEqual(done.returncode, 1, done.stdout)
                self.assertIn(noise, done.stderr)
                self.assertIn("carries unconfigured permissions beyond agent-base", done.stderr)

    def test_keys_refuses_a_dry_run(self):
        # It has no printing path: it creates a real key whatever DRY_RUN says.
        box = self.ready()
        done = box.run("keys", "x", DRY_RUN="1")
        self.assertEqual(done.returncode, 1)
        self.assertIn("cannot be dry-run", done.stderr)
        self.assertEqual(box.state["created"], [])

    def test_keys_deletes_the_key_when_the_profile_does_not_authenticate(self):
        # The key answers from a credentials file of its own, so the store is
        # happy. The profile carries an expired session token, so the profile
        # is not usable. Reporting the key as stored would name a credential
        # nothing can use, and would leave a live key no profile references.
        box = self.ready()
        box.profile("agent-x", aws_session_token="stale")
        box.identity(NEW_KEY, AGENT_ARN)
        done = box.run("keys", "x")
        self.assertEqual(done.returncode, 1)
        self.assertIn("did not authenticate", done.stderr)
        self.assertEqual(box.state["deleted"], [NEW_KEY])
        self.assertEqual(box.state["profiles"]["agent-x"]["aws_access_key_id"], "")

    def test_rotate_refuses_a_dry_run(self):
        box = self.ready()
        box.profile("agent-x", aws_access_key_id=OLD_KEY, aws_secret_access_key="old")
        done = box.run("rotate", "x", DRY_RUN="1")
        self.assertEqual(done.returncode, 1)
        self.assertIn("cannot be dry-run", done.stderr)
        self.assertEqual(box.state["deleted"], [])

    def test_the_secret_never_reaches_a_command_line(self):
        box = self.ready()
        box.identity(NEW_KEY, AGENT_ARN)
        done = box.run("keys", "x")
        self.assertEqual(done.returncode, 0, done.stderr)
        for call in box.calls():
            self.assertNotIn(NEW_SECRET, " ".join(call["argv"]), call["rest"][:2])
        self.assertNotIn(NEW_SECRET, done.stdout + done.stderr)
        piped = [c for c in box.calls() if c["stdin"] and NEW_SECRET in c["stdin"]]
        self.assertEqual(len(piped), 1, "the secret should go down one pipe")
        self.assertEqual(piped[0]["rest"], ["configure"])
        self.assertEqual(box.state["profiles"]["agent-x"]["aws_secret_access_key"], NEW_SECRET)
        self.assertEqual(box.state["profiles"]["agent-x"]["region"], "eu-west-1")

    def test_a_key_that_answers_as_somebody_else_is_never_stored(self):
        box = self.ready()
        box.identity(NEW_KEY, OTHER_ARN)
        done = box.run("keys", "x")
        self.assertEqual(done.returncode, 1)
        self.assertIn("answers as '%s'" % OTHER_ARN, done.stderr)
        self.assertNotIn("agent-x", box.state["profiles"])
        self.assertEqual(box.state["deleted"], [NEW_KEY])

    def test_a_key_that_never_answers_is_deleted_again(self):
        box = self.ready()
        done = box.run("keys", "x")
        self.assertEqual(done.returncode, 1)
        self.assertIn("never answered", done.stderr)
        self.assertNotIn("agent-x", box.state["profiles"])
        self.assertEqual(box.state["deleted"], [NEW_KEY])

    def test_a_user_under_a_path_is_not_mistaken_for_somebody_else(self):
        # An IAM user carries a path, so a user created under one answers as
        # `user/automation/agent`. An ARN rebuilt from the account id and
        # the user name would not match it, and the key just created for that
        # user would be deleted as another identity's.
        pathed = "arn:aws:iam::%s:user/automation/agent" % ACCOUNT_ID
        box = self.ready()
        box.user_arn(pathed)
        box.identity(NEW_KEY, pathed)
        done = box.run("keys", "x")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(box.state["deleted"], [])
        self.assertEqual(box.state["profiles"]["agent-x"]["aws_access_key_id"], NEW_KEY)

    def test_a_key_is_not_created_when_the_user_cannot_be_read(self):
        # Both sides of the identity comparison would be empty if the lookup
        # failed after the key was made -- and empty equals empty, so an
        # unauthenticated key would have been stored and reported as working.
        box = self.ready()
        box.user_arn("")
        done = box.run("keys", "x")
        self.assertEqual(done.returncode, 1)
        self.assertEqual(box.state["created"], [])
        self.assertEqual(box.state["deleted"], [])
        self.assertNotIn("agent-x", box.state["profiles"])

    def test_a_key_that_cannot_be_written_into_the_profile_is_deleted(self):
        # store_new_key runs inside a command substitution, where an exit ends
        # that subshell alone. A `die` on the write left the key it had just
        # created live, with nothing running to delete it.
        box = self.ready()
        box.identity(NEW_KEY, AGENT_ARN)
        box.refuse("configure", stderr="cannot write the credentials file")
        done = box.run("keys", "x")
        self.assertEqual(done.returncode, 1)
        self.assertEqual(box.state["created"], [NEW_KEY])
        self.assertEqual(box.state["deleted"], [NEW_KEY])

    def test_a_key_the_profile_persisted_before_failing_is_taken_back_out(self):
        # `aws configure` writes the credentials before the configuration, so
        # a failure between the two persists the key and still fails. Deleting
        # the key and leaving it named in the profile refused every later
        # `keys` in the name of a key that no longer exists.
        box = self.ready()
        box.identity(NEW_KEY, AGENT_ARN)
        box.partial_write()
        done = box.run("keys", "x")
        self.assertEqual(done.returncode, 1)
        self.assertEqual(box.state["deleted"], [NEW_KEY])
        self.assertNotEqual(box.state["profiles"].get("agent-x", {}).get("aws_access_key_id"),
                            NEW_KEY)
        # And the next attempt is not refused by what the failed one left.
        box.partial_write(False)
        box.scenario["new_key"] = ["AKIASECOND", "second-secret"]
        box.identity("AKIASECOND", AGENT_ARN)
        again = box.run("keys", "x")
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(box.state["profiles"]["agent-x"]["aws_access_key_id"], "AKIASECOND")

    def test_a_profile_that_could_not_be_cleared_says_so_instead_of_claiming_it_was(self):
        # `keys` refuses while the profile names a key, so a cleanup that
        # failed silently sent the operator away from the one thing left to
        # repair by hand and left every later run refused in the name of a key
        # that no longer exists.
        box = self.ready()
        box.identity(NEW_KEY, AGENT_ARN)
        box.partial_write()
        box.refuse("configure", "set", "aws_access_key_id", "")
        done = box.run("keys", "x")
        self.assertEqual(done.returncode, 1)
        self.assertIn("clear it by hand", done.stderr)

    def test_a_cleared_profile_is_not_reported_as_needing_repair(self):
        # The other half of the pair: the same failure with a cleanup that
        # works must not tell the operator to go and repair anything.
        box = self.ready()
        box.identity(NEW_KEY, AGENT_ARN)
        box.partial_write()
        done = box.run("keys", "x")
        self.assertEqual(done.returncode, 1)
        self.assertNotIn("clear it by hand", done.stderr)

    def test_an_interrupted_probe_leaves_no_secret_on_disk(self):
        # The probe writes the new secret into a temporary file of its own.
        # Without a trap, a Ctrl-C or a SIGTERM between that write and the
        # `rm -rf` left the secret on disk, which is the single thing the
        # private file exists to avoid. The directory is read from the call
        # log rather than from TMPDIR: macOS `mktemp` resolves its own
        # per-user temporary directory and ignores that variable.
        box = self.ready()
        box.identity(NEW_KEY, AGENT_ARN)
        box.hang("sts", "get-caller-identity")

        process = box.start("keys", "x")
        self.addCleanup(process.kill)
        deadline = time.monotonic() + 30
        written = None
        while time.monotonic() < deadline:
            for call in box.calls():
                if call.get("env_credentials_file"):
                    written = pathlib.Path(call["env_credentials_file"])
                    break
            if written is not None and written.exists():
                break
            time.sleep(0.05)
        else:
            self.fail("the probe never wrote its credentials file")
        self.assertIn(NEW_SECRET, written.read_text())

        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        process.wait(timeout=30)
        # The probe runs in a command substitution, so the shell holding the
        # directory is not the one `wait` returns for. Give its trap a moment
        # to run rather than reading the filesystem a microsecond too early.
        gone = time.monotonic() + 30
        while written.parent.exists() and time.monotonic() < gone:
            time.sleep(0.05)

        self.assertFalse(written.parent.exists(),
                         "the probe directory outlived the shell that made it")

    def test_rotate_keeps_the_old_key_when_the_profile_cannot_be_written(self):
        box = self.ready()
        box.profile("agent-x", aws_access_key_id=OLD_KEY, aws_secret_access_key="old-secret")
        box.identity(OLD_KEY, AGENT_ARN)
        box.identity(NEW_KEY, AGENT_ARN)
        box.refuse("configure", stderr="cannot write the credentials file")
        done = box.run("rotate", "x")
        self.assertEqual(done.returncode, 1)
        self.assertIn(OLD_KEY, done.stderr)
        self.assertNotIn(OLD_KEY, box.state["deleted"])
        self.assertEqual(box.state["deleted"], [NEW_KEY])

    def test_rotate_keeps_the_old_key_when_the_new_one_never_answers(self):
        box = self.ready()
        box.profile("agent-x", aws_access_key_id=OLD_KEY, aws_secret_access_key="old-secret")
        box.identity(OLD_KEY, AGENT_ARN)
        done = box.run("rotate", "x")
        self.assertEqual(done.returncode, 1)
        self.assertEqual(box.state["deleted"], [NEW_KEY])
        self.assertNotIn(OLD_KEY, box.state["deleted"])
        self.assertEqual(box.state["profiles"]["agent-x"],
                         {"aws_access_key_id": OLD_KEY, "aws_secret_access_key": "old-secret"})

    def test_rotate_deletes_the_old_key_only_after_the_profile_holds_the_new_one(self):
        box = self.ready()
        box.profile("agent-x", aws_access_key_id=OLD_KEY, aws_secret_access_key="old-secret")
        box.identity(OLD_KEY, AGENT_ARN)
        box.identity(NEW_KEY, AGENT_ARN)
        done = box.run("rotate", "x")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(box.state["deleted"], [OLD_KEY])
        self.assertEqual(box.state["profiles"]["agent-x"]["aws_access_key_id"], NEW_KEY)
        order = [call["rest"] for call in box.calls()]
        wrote = order.index(["configure"])
        deleted = [i for i, rest in enumerate(order)
                   if rest[:2] == ["iam", "delete-access-key"]]
        self.assertEqual(len(deleted), 1)
        self.assertGreater(deleted[0], wrote)

    def test_simulate_passes_a_region_to_every_call_it_makes(self):
        # IAM is global, but the CLI refuses a call without a region and an
        # `aws login` profile carries none.
        box = self.sandbox()
        box.profile("default", aws_access_key_id="AKIAADMIN")
        box.identity("AKIAADMIN", ADMIN_ARN)
        box.rule("simulate-custom-policy", stdout="allowed")
        box.run("simulate", "x")
        made = [c for c in box.calls() if c["rest"][:2] != ["configure"]]
        self.assertTrue(made)
        for call in made:
            self.assertEqual(call["region"], "eu-west-1", call["rest"][:2])
        simulated = [c for c in box.calls() if "simulate-custom-policy" in c["rest"]]
        self.assertTrue(simulated)
        identity = [c for c in box.calls() if c["rest"][:2] == ["sts", "get-caller-identity"]]
        self.assertTrue(identity)

    def test_every_configured_bucket_is_simulated(self):
        # Taking the first word of S3_BUCKETS left the rest of a multi-bucket
        # account unchecked and reported it as passing.
        box = self.sandbox(buckets="alpha beta gamma")
        box.profile("default", aws_access_key_id="AKIAADMIN")
        box.identity("AKIAADMIN", ADMIN_ARN)
        box.rule("simulate-custom-policy", stdout="allowed")
        box.run("simulate", "x")
        resources = []
        for call in box.calls():
            if "--resource-arns" in call["rest"]:
                resources.append(call["rest"][call["rest"].index("--resource-arns") + 1])
        for bucket in ("alpha", "beta", "gamma"):
            self.assertIn("arn:aws:s3:::%s/probe.txt" % bucket, resources)
            self.assertIn("arn:aws:s3:::%s" % bucket, resources)

    def test_a_bucket_list_of_only_separators_is_a_failure_not_a_pass(self):
        box = self.sandbox(buckets="   ")
        box.profile("default", aws_access_key_id="AKIAADMIN")
        box.identity("AKIAADMIN", ADMIN_ARN)
        box.rule("simulate-custom-policy", stdout="allowed")
        done = box.run("simulate", "x")
        self.assertEqual(done.returncode, 1)
        self.assertIn("names no bucket to check", done.stderr)

    def test_the_check_fails_when_a_disk_read_path_reopens(self):
        # A simulator that allows everything stands for a policy that lost
        # the deny: every route to a managed disk must then fail (sd:2870).
        for level in ("readonly", "operator", "sandbox"):
            with self.subTest(level=level):
                box = self.sandbox(level=level)
                box.profile("default", aws_access_key_id="AKIAADMIN")
                box.identity("AKIAADMIN", ADMIN_ARN)
                box.rule("simulate-custom-policy", stdout="allowed")
                done = box.run("simulate", "x")
                self.assertEqual(done.returncode, 1)
                for action in disk_routes(level):
                    self.assertIn("FAIL want explicitDeny, got allowed: %s * " % action, done.stdout)
                for kind in ("snapshot", "image"):
                    for context in ("aws:ResourceTag/claude-managed=true ec2:Owner=%s" % ACCOUNT_ID, "ec2:Owner=%s" % ACCOUNT_ID):
                        self.assertRegex(done.stdout, r"FAIL want explicitDeny, got allowed: ec2:RunInstances "
                                                      r"arn:aws:ec2:eu-west-1::%s/\S+ \[%s\]\n" % (kind, context))

    def test_the_check_tells_a_private_image_from_an_approved_one(self):
        # A simulator that answers by owner, as the policy should: the private
        # untagged image is denied, and the Canonical one launches at sandbox.
        box = self.sandbox(level="sandbox")
        box.profile("default", aws_access_key_id="AKIAADMIN")
        box.identity("AKIAADMIN", ADMIN_ARN)
        box.rule("simulate-custom-policy", "ContextKeyValues=%s," % ACCOUNT_ID, stdout="explicitDeny")
        box.rule("simulate-custom-policy", stdout="allowed")
        done = box.run("simulate", "x")
        image = "ec2:RunInstances arn:aws:ec2:eu-west-1::image/ami-0123456789abcdef0"
        self.assertIn("PASS explicitDeny %s [ec2:Owner=%s]\n" % (image, ACCOUNT_ID), done.stdout)
        self.assertIn("PASS allowed %s [ec2:Owner=099720109477]\n" % image, done.stdout)



class ConfigLocation(unittest.TestCase):
    """Account files and shared settings live in <config>/aws-setup/."""

    def test_no_accounts_names_the_config_folder_and_the_example(self):
        with tempfile.TemporaryDirectory() as home:
            config = pathlib.Path(home) / "config"
            done = subprocess.run(
                ["/bin/sh", str(SCRIPT), "accounts"], capture_output=True, text=True,
                env={"PATH": os.environ["PATH"], "HOME": home,
                     "SYSTEM_TOOLS_CONFIG": str(config)})
            want = config / "aws-setup" / "accounts"
            self.assertIn("copy local-aws-setup/accounts/example.env.example to %s/<name>.env" % want,
                          done.stdout + done.stderr)

    def test_the_accounts_dir_override_and_the_shared_env_are_read(self):
        with tempfile.TemporaryDirectory() as home:
            root = pathlib.Path(home)
            config = root / "config"
            (config / "aws-setup").mkdir(parents=True)
            (config / "aws-setup" / ".env").write_text("MANAGED_TAG_KEY=from-config-env\n")
            accounts = root / "elsewhere"
            accounts.mkdir()
            (accounts / "x.env").write_text(
                "ACCOUNT_ID=%s\nLEVEL=operator\nADMIN_PROFILE=admin-x\n" % ACCOUNT_ID)
            done = subprocess.run(
                ["/bin/sh", str(SCRIPT), "render", "x"], capture_output=True, text=True,
                env={"PATH": os.environ["PATH"], "HOME": home,
                     "SYSTEM_TOOLS_CONFIG": str(config),
                     "AWS_SETUP_ACCOUNTS_DIR": str(accounts)})
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertIn("from-config-env", done.stdout)


class LifecycleCase(unittest.TestCase):
    def decision(self, document, action, resource, context, broad=False):
        # Small independent evaluator for the renderer's supported conditions.
        from fnmatch import fnmatchcase
        allowed = broad
        for item in document["Statement"]:
            resources = item.get("Resource", item.get("NotResource"))
            if isinstance(resources, str):
                resources = [resources]
            action_patterns = item.get("Action", item.get("NotAction"))
            if isinstance(action_patterns, str):
                action_patterns = [action_patterns]
            matches_action = any(fnmatchcase(action, pattern) for pattern in action_patterns)
            if "NotAction" in item:
                matches_action = not matches_action
            if not matches_action:
                continue
            matches_resource = any(fnmatchcase(resource, pattern) for pattern in resources)
            if "NotResource" in item:
                matches_resource = not matches_resource
            if not matches_resource:
                continue
            matches = True
            for operator, entries in item.get("Condition", {}).items():
                for key, expected in entries.items():
                    expected = expected if isinstance(expected, list) else [expected]
                    actual = context.get(key)
                    if operator == "StringEquals":
                        matches &= actual in expected
                    elif operator == "StringNotEqualsIfExists":
                        matches &= actual not in expected
                    elif operator == "ForAnyValue:StringEquals":
                        matches &= any(value in expected for value in (actual or []))
                    elif operator == "BoolIfExists":
                        matches &= actual is None or actual in expected
                    else:
                        self.fail("unsupported condition " + operator)
            if matches:
                if item["Effect"] == "Deny":
                    return "explicitDeny"
                allowed = True
        return "allowed" if allowed else "implicitDeny"

    def test_cleanup_requires_tags_and_sandbox_even_with_broad_extra(self):
        cases = {"DisassociateAddress": ["elastic-ip", "network-interface"],
                 "ReleaseAddress": ["elastic-ip"], "DeleteSecurityGroup": ["security-group"],
                 "DeleteKeyPair": ["key-pair"], "DeleteVolume": ["volume"],
                 "DeleteNetworkInterface": ["network-interface"], "DeleteSnapshot": ["snapshot"]}
        for level in ("readonly", "operator", "sandbox"):
            policy = render(level)
            for action, kinds in cases.items():
                for kind in kinds:
                    account = "" if kind == "snapshot" else ACCOUNT_ID
                    resource = f"arn:aws:ec2:us-east-1:{account}:{kind}/probe"
                    with self.subTest(level=level, action=action, kind=kind):
                        for tag in (None, "false"):
                            context = {} if tag is None else {"aws:ResourceTag/claude-managed": tag}
                            self.assertEqual(self.decision(policy, "ec2:" + action, resource, context, broad=True), "explicitDeny")
                        context = {"aws:ResourceTag/claude-managed": "true"}
                        expected = "allowed" if level == "sandbox" else "explicitDeny"
                        self.assertEqual(self.decision(policy, "ec2:" + action, resource, context, broad=True), expected)
                        self.assertEqual(self.decision(policy, "ec2:" + action, resource, context), expected)

    def test_cleanup_stays_in_the_configured_account(self):
        policy = render("sandbox")
        volume = f"arn:aws:ec2:us-east-1:999988887777:volume/probe"
        self.assertEqual(self.decision(policy, "ec2:DeleteVolume", volume, {"aws:ResourceTag/claude-managed": "true"}), "implicitDeny")

    def test_snapshot_tags_cannot_change(self):
        policy = render("sandbox")
        resource = "arn:aws:ec2:us-east-1::snapshot/probe"
        context = {"aws:TagKeys": ["claude-managed"], "aws:RequestTag/claude-managed": "true"}
        for action in ("ec2:CreateTags", "ec2:DeleteTags"):
            self.assertEqual(self.decision(policy, action, resource, context, broad=True), "explicitDeny")
        for operation in ("CreateSnapshot", "CreateVolume"):
            context["ec2:CreateAction"] = operation
            self.assertEqual(self.decision(policy, "ec2:CreateTags", resource, context, broad=True), "explicitDeny")

    def test_no_level_reaches_a_managed_disk(self):
        # A snapshot, image, volume or guest key turns a deployment's disk
        # into something the agent can read (sd:2870). Every level denies
        # each route in one statement, so the levels cannot drift apart, and
        # a missing Allow says nothing once a broader policy is attached.
        tagged = {"aws:ResourceTag/claude-managed": "true",
                  "aws:RequestTag/claude-managed": "true"}
        for level in ("readonly", "operator", "sandbox"):
            policy = render(level)
            self.assertEqual(actions_of(statement(policy, "DenyReadingManagedDisks")), disk_routes(level))
            self.assertEqual([a for a in DISK_ROUTES if a in actions_of(statement(policy, "HardDenies"))], [])
            for action in disk_routes(level):
                for resource in ("arn:aws:ec2:us-east-1::snapshot/probe", "arn:aws:ec2:us-east-1:%s:volume/probe" % ACCOUNT_ID):
                    with self.subTest(level=level, action=action, resource=resource):
                        self.assertEqual(self.decision(policy, action, resource, tagged, broad=True), "explicitDeny")

    def test_launch_sources_are_an_owner_allowlist(self):
        # A tag names only the disks someone remembered to tag: an untagged
        # private backup image or snapshot must not launch either (sd:2870).
        private = {"ec2:Owner": ACCOUNT_ID}
        for level in ("readonly", "operator", "sandbox"):
            policy = render(level)
            for kind in ("snapshot/snap-probe", "image/ami-probe"):
                resource = "arn:aws:ec2:us-east-1::" + kind
                for context in ({**private, "aws:ResourceTag/claude-managed": "true"}, private, {}):
                    with self.subTest(level=level, kind=kind, context=context):
                        self.assertEqual(self.decision(policy, "ec2:RunInstances", resource, context, broad=True), "explicitDeny")
                for owner in ("amazon", "099720109477"):
                    with self.subTest(level=level, kind=kind, owner=owner):
                        self.assertEqual(self.decision(policy, "ec2:RunInstances", resource, {"ec2:Owner": owner}),
                                         "allowed" if level == "sandbox" else "explicitDeny")

    def test_rendered_policies_fit_managed_policy_limit(self):
        for level in ("readonly", "operator", "sandbox"):
            self.assertLessEqual(len(json.dumps(render(level), separators=(",", ":"))), 6144)


class ExtraPolicyCase(unittest.TestCase):
    sandbox = CommandCase.sandbox
    ready = CommandCase.ready
    def extra(self, box, name="provision", document=None):
        folder = box.accounts / "x.policies"
        folder.mkdir(exist_ok=True)
        document = document or {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "ssm:DescribeInstanceInformation", "Resource": "*"}]}
        (folder / (name + ".json")).write_text(json.dumps(document))
        return document

    def test_extras_render_and_apply_with_base(self):
        box = self.ready(EXTRA_POLICIES="provision")
        document = self.extra(box)
        done = box.run("render", "x", "provision")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout), document)
        box.rule("get-policy", exit=255)
        done = box.run("apply", "x")
        self.assertEqual(done.returncode, 0, done.stderr)
        calls = box.calls()
        attached = [c for c in calls if "attach-user-policy" in c["rest"]]
        self.assertEqual({c["rest"][-1] for c in attached}, {f"arn:aws:iam::{ACCOUNT_ID}:policy/{name}" for name in ("agent-base", "provision")})
        query = next(c["argv"] for c in calls if "list-attached-user-policies" in c["rest"])
        query = query[query.index("--query") + 1]
        self.assertIn("policy/agent-base'", query)
        self.assertIn("policy/provision'", query)

    def test_fresh_provisioning_example_renders_complete_scoped_setup(self):
        values = {"ACCOUNT_ID": ACCOUNT_ID, "REGION": "eu-west-1", "VPC_ID": "vpc-example",
                  "OWNER": "example-owner", "PROJECT": "example-project", "PURPOSE": "example-purpose",
                  "MANAGED_BY": "example-deployer", "DEPLOYMENT_KEY_PREFIX": "example-deployment",
                  "LEGACY_KEY_PREFIX": "example-legacy", "AGENT_USER": "agent"}
        raw = (HERE.parent / "accounts" / "provisioning-policy.json.example").read_text()
        self.assertEqual(set(re.findall(r"{{([A-Z_]+)}}", raw)), set(values))
        for name, value in values.items():
            raw = raw.replace("{{" + name + "}}", value)
        role = f"arn:aws:iam::{ACCOUNT_ID}:role/example-instance"
        box = self.ready(level="sandbox", EXTRA_POLICIES="provision", PASS_ROLE_ARNS=role)
        extra = self.extra(box, document=json.loads(raw))
        rendered = box.run("render", "x", "provision")
        self.assertEqual(rendered.returncode, 0, rendered.stderr)
        self.assertEqual(json.loads(rendered.stdout), extra)
        base = box.run("render", "x")
        self.assertEqual(base.returncode, 0, base.stderr)
        policy = json.loads(base.stdout)
        policy["Statement"].extend(extra["Statement"])
        decision = LifecycleCase.decision
        prefix = f"arn:aws:ec2:eu-west-1:{ACCOUNT_ID}:"
        tags = {"Owner": values["OWNER"], "Project": values["PROJECT"], "Purpose": values["PURPOSE"],
                "ManagedBy": values["MANAGED_BY"], "claude-managed": "true"}
        request = {"aws:RequestTag/" + key: value for key, value in tags.items()}
        resource = {"aws:ResourceTag/" + key: value for key, value in tags.items()}
        for action, arn, context in [
            ("ec2:ImportKeyPair", prefix + "key-pair/example-deployment-123", request),
            ("ec2:ImportKeyPair", prefix + "key-pair/example-legacy-123", request),
            ("ec2:CreateSecurityGroup", prefix + "vpc/vpc-example", {}),
            ("ec2:CreateSecurityGroup", prefix + "security-group/probe", request),
            ("ec2:AuthorizeSecurityGroupIngress", prefix + "security-group/probe", resource),
            ("ec2:AuthorizeSecurityGroupIngress", prefix + "security-group-rule/probe", request),
            ("ec2:AuthorizeSecurityGroupEgress", prefix + "security-group-rule/probe", request),
            ("ec2:AllocateAddress", prefix + "elastic-ip/probe", request),
            ("ec2:AssociateAddress", prefix + "network-interface/probe", resource),
            ("ssm:SendCommand", prefix + "instance/probe", resource),
            ("ssm:SendCommand", "arn:aws:ssm:eu-west-1::document/AWS-RunShellScript", {}),
            ("ssm:StartSession", prefix + "instance/probe", {**resource, "ssm:SessionDocumentAccessCheck": "true"}),
            ("ssm:StartSession", "arn:aws:ssm:eu-west-1::document/AWS-StartPortForwardingSession", {}),
            ("iam:PassRole", role, {"iam:PassedToService": "ec2.amazonaws.com"}),
            ("ec2:DeleteSecurityGroup", prefix + "security-group/probe", resource),
        ]:
            with self.subTest(action=action, arn=arn):
                self.assertEqual(decision(self, policy, action, arn, context), "allowed")
        for action, kinds in (("ssm:SendCommand", ("instance",)),
                              ("ssm:StartSession", ("instance",)),
                              ("ec2:AuthorizeSecurityGroupIngress", ("security-group",)),
                              ("ec2:RevokeSecurityGroupIngress", ("security-group",)),
                              ("ec2:AuthorizeSecurityGroupEgress", ("security-group",)),
                              ("ec2:RevokeSecurityGroupEgress", ("security-group",)),
                              ("ec2:AssociateAddress", ("instance", "elastic-ip", "network-interface"))):
            for kind in kinds:
                for managed in (None, "false"):
                    context = dict(resource)
                    if managed is None:
                        del context["aws:ResourceTag/claude-managed"]
                    else:
                        context["aws:ResourceTag/claude-managed"] = managed
                    with self.subTest(action=action, kind=kind, managed=managed):
                        self.assertEqual(decision(self, policy, action, prefix + kind + "/probe", context), "implicitDeny")
        for operation, kind in (("AllocateAddress", "elastic-ip/probe"),
                                ("CreateSecurityGroup", "security-group/probe"),
                                ("ImportKeyPair", "key-pair/example-legacy-probe")):
            for action in ("ec2:" + operation, "ec2:CreateTags"):
                for managed in (None, "false"):
                    context = {**request, "ec2:CreateAction": operation}
                    if managed is None:
                        del context["aws:RequestTag/claude-managed"]
                    else:
                        context["aws:RequestTag/claude-managed"] = managed
                    with self.subTest(action=action, kind=kind, managed=managed):
                        self.assertEqual(decision(self, policy, action, prefix + kind, context), "implicitDeny")
        key = prefix + "key-pair/example-deployment-123"
        for tag in request:
            missing = dict(request)
            del missing[tag]
            self.assertEqual(decision(self, policy, "ec2:ImportKeyPair", key, missing), "implicitDeny")
            for action in ("ec2:AuthorizeSecurityGroupIngress", "ec2:AuthorizeSecurityGroupEgress"):
                self.assertEqual(decision(self, policy, action, prefix + "security-group-rule/probe", missing), "implicitDeny")
        self.assertEqual(decision(self, policy, "ec2:ImportKeyPair", key.replace("example-deployment", "unrelated"), request), "implicitDeny")
        self.assertEqual(decision(self, policy, "ec2:DeleteSecurityGroup", prefix + "security-group/probe", {}), "explicitDeny")
        # Commands and tunnels only: no interactive shell, and no shell document for commands (sd:2851).
        for action, document in (("ssm:StartSession", "AWS-RunShellScript"),
                                 ("ssm:StartSession", "SSM-SessionManagerRunShell"),
                                 ("ssm:SendCommand", "AWS-StartPortForwardingSession"),
                                 ("ssm:SendCommand", "SSM-SessionManagerRunShell")):
            for owner in ("", ACCOUNT_ID):
                arn = f"arn:aws:ssm:eu-west-1:{owner}:document/{document}"
                with self.subTest(action=action, arn=arn):
                    self.assertEqual(decision(self, policy, action, arn, {}), "implicitDeny")
        self.assertEqual(decision(self, policy, "ssm:StartSession", prefix + "instance/probe",
                                  {**resource, "ssm:SessionDocumentAccessCheck": "false"}), "implicitDeny")

    def test_agent_ssm_deny_example_overrides_deployment_grants(self):
        # The agent user holds this beside anything else attached; the deployer never does (sd:2851).
        deny = json.loads((HERE.parent / "accounts" / "agent-ssm-deny.json.example").read_text())
        grants = json.loads((HERE.parent / "accounts" / "provisioning-policy.json.example").read_text())
        box = self.ready(EXTRA_POLICIES="agent-ssm-deny")
        self.extra(box, name="agent-ssm-deny", document=deny)
        done = box.run("render", "x", "agent-ssm-deny")
        self.assertEqual(done.returncode, 0, done.stderr)
        policy = {"Statement": grants["Statement"] + deny["Statement"]}
        prefix = "arn:aws:ec2:{{REGION}}:{{ACCOUNT_ID}}:"
        tags = {"aws:ResourceTag/" + key: value for key, value in
                {"Owner": "{{OWNER}}", "Project": "{{PROJECT}}", "Purpose": "{{PURPOSE}}", "claude-managed": "true"}.items()}
        for action in ("ssm:SendCommand", "ssm:StartSession"):
            for arn in (prefix + "instance/i-probe", "arn:aws:ssm:{{REGION}}::document/AWS-RunShellScript",
                        "arn:aws:ssm:{{REGION}}::document/AWS-StartPortForwardingSession"):
                with self.subTest(action=action, arn=arn):
                    self.assertEqual(LifecycleCase.decision(self, policy, action, arn, tags, broad=True), "explicitDeny")

    def test_interactive_shell_grant_never_calls_aws(self):
        instance = f"arn:aws:ec2:us-east-1:{ACCOUNT_ID}:instance/*"
        checked = {"BoolIfExists": {"ssm:SessionDocumentAccessCheck": "true"}}
        for statement in (
            {"Action": "ssm:StartSession", "Resource": "arn:aws:ssm:us-east-1::document/SSM-SessionManagerRunShell"},
            {"Action": ["ssm:SendCommand", "ssm:StartSession"], "Resource": f"arn:aws:ssm:us-east-1:{ACCOUNT_ID}:document/SSM-SessionManagerRunShell"},
            {"Action": "ssm:startsession", "Resource": "arn:aws:ssm:*:*:document/SSM-*"},
            {"Action": "ssm:*", "Resource": "*"},
            {"NotAction": "ec2:*", "Resource": "*"},
            {"Action": "ssm:StartSession", "NotResource": "arn:aws:ssm:us-east-1::document/AWS-StartPortForwardingSession"},
            {"Action": "ssm:StartSession", "Resource": instance},
            {"Action": "ssm:StartSession", "Resource": f"arn:aws:ec2:us-east-1:{ACCOUNT_ID}:instance/i-0123456789abcdef0"},
            {"Action": "ssm:StartSession", "Resource": f"arn:aws:ec2:us-east-1:{ACCOUNT_ID}:instance/i-abc*"},
            {"Action": "ssm:StartSession", "Resource": f"arn:aws:ssm:us-east-1:{ACCOUNT_ID}:managed-instance/*"},
            {"Action": "ssm:StartSession", "Resource": "arn:aws:ssm:*:*:*"},
            {"Action": "ssm:StartSession", "Resource": "arn:aws:ssm:us-east-1:*", "Condition": checked},
            {"Action": "ssm:StartSession", "Resource": f"arn:aws:ssm:us-east-1:{ACCOUNT_ID}:*", "Condition": checked},
            {"Action": "ssm:StartSession", "Resource": f"arn:aws:ec2:us-east-1:{ACCOUNT_ID}:inst*/i-ab*"},
            {"Action": "ssm:StartSession", "Resource": "arn:aws:*:us-east-1:*:*/*"},
            {"Action": "ssm:StartSession", "Resource": "arn:aws:ssm:us-east-1:%s:document/${aws:PrincipalTag/Doc, 'SSM-SessionManagerRunShell'}" % ACCOUNT_ID},
            {"Action": "ssm:StartSession", "NotResource": "arn:aws:ssm:us-east-1::document/SSM-SessionManagerRunShell", "Condition": checked},
            {"Action": "ssm:StartSession", "NotResource": f"arn:aws:ssm:us-east-1:{ACCOUNT_ID}:document/SSM-SessionManagerRunShell", "Condition": checked},
            {"Action": "ssm:StartSession", "Resource": instance, "Condition": {"BoolIfExists": {"ssm:SessionDocumentAccessCheck": "false"}}},
        ):
            with self.subTest(statement=statement):
                box = self.ready(EXTRA_POLICIES="provision")
                self.extra(box, document={"Version": "2012-10-17", "Statement": [{"Effect": "Allow", **statement}]})
                done = box.run("apply", "x")
                self.assertNotEqual(done.returncode, 0)
                self.assertIn("interactive SSM shell", done.stderr)
                self.assertEqual(box.calls(), [])
        for statement in (
            {"Action": "ssm:StartSession", "Resource": instance, "Condition": checked},
            {"Action": "ssm:StartSession", "Resource": "arn:aws:ssm:us-east-1::document/AWS-StartPortForwardingSession"},
            {"Action": "ssm:SendCommand", "Resource": [instance, "arn:aws:ssm:us-east-1::document/AWS-RunShellScript"]},
            {"Action": "*", "Resource": "arn:aws:s3:::example-bucket/*"},
        ):
            with self.subTest(statement=statement):
                box = self.ready(EXTRA_POLICIES="provision")
                self.extra(box, document={"Version": "2012-10-17", "Statement": [{"Effect": "Allow", **statement}]})
                done = box.run("render", "x", "provision")
                self.assertEqual(done.returncode, 0, done.stderr)

    def test_all_ownership_checks_precede_every_write(self):
        box = self.ready(EXTRA_POLICIES="provision")
        self.extra(box)
        box.rule("list-policy-tags", "policy/agent-base", stdout="local-aws-setup")
        box.rule("list-policy-tags", "policy/provision", stdout="")
        done = box.run("apply", "x")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("carries no managed-by", done.stderr)
        self.assertFalse([c for c in box.calls() if any(verb in c["rest"] for verb in ("create-policy", "create-policy-version", "delete-policy-version", "attach-user-policy"))])

    def test_invalid_configuration_never_calls_aws(self):
        for names, raw in (("missing", None), ("provision provision", "{}"), ("agent-base", "{}"), ("../escape", "{}"), ("provision", "not json"), ("provision", '{"Version":"2012-10-17","Statement":[]}'), ("provision", "x" * 6145)):
            with self.subTest(names=names, raw=raw and raw[:30]):
                box = self.ready(EXTRA_POLICIES=names)
                self.extra(box)
                if raw is not None:
                    (box.accounts / "x.policies" / "provision.json").write_text(raw)
                done = box.run("apply", "x")
                self.assertNotEqual(done.returncode, 0)
                self.assertEqual(box.calls(), [])

    def test_names_python_splits_but_the_shell_does_not_never_call_aws(self):
        # Python's split() also splits on \v, \f and \r; the shell does not, so
        # the validator and the loops would read different names.
        for separator in ("\v", "\f", "\r"):
            with self.subTest(separator=repr(separator)):
                box = self.ready(EXTRA_POLICIES="provision%sother" % separator)
                self.extra(box)
                self.extra(box, name="other")
                done = box.run("apply", "x")
                self.assertNotEqual(done.returncode, 0)
                self.assertIn("EXTRA_POLICIES may hold only", done.stderr)
                self.assertEqual(box.calls(), [])

    def test_a_quote_or_backslash_in_a_name_never_calls_aws(self):
        # The names reach unquoted loops and a JMESPath literal (sd:2848).
        # Each value sits in single quotes in the account file: '\'' is one quote.
        for name in ("it'\\''s", "a\\b"):
            with self.subTest(name=name):
                box = self.ready(EXTRA_POLICIES="provision " + name)
                self.extra(box)
                done = box.run("apply", "x")
                self.assertEqual(done.returncode, 1, done.stderr)
                self.assertIn("EXTRA_POLICIES may hold only IAM policy names", done.stderr)
                self.assertEqual(box.calls(), [])

    def test_a_simulator_call_too_long_for_argv_fails_instead_of_passing(self):
        # Whitespace does not count toward IAM's limit, so a valid document can
        # outgrow ARG_MAX. The exec then fails as a whole; no subset reaches AWS.
        box = self.ready(EXTRA_POLICIES="provision")
        document = json.dumps({"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "ssm:DescribeInstanceInformation", "Resource": "*"}]})
        self.extra(box)
        (box.accounts / "x.policies" / "provision.json").write_text(document[:-1] + " " * (4 << 20) + "}")
        box.profile("default", aws_access_key_id="AKIAADMIN")
        box.rule("simulate-custom-policy", stdout="allowed")
        done = box.run("simulate", "x")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("Argument list too long", done.stderr)
        self.assertNotIn("PASS", done.stdout)
        calls = [c["rest"][:2] for c in box.calls()]
        # The document passed validation: the session check ran after it.
        self.assertEqual(calls, [["sts", "get-caller-identity"]])

    def test_unconfigured_attachment_rejected_even_with_configured_extra(self):
        box = self.ready(EXTRA_POLICIES="provision")
        self.extra(box)
        box.rule("list-attached-user-policies", stdout="AdministratorAccess")
        done = box.run("apply", "x")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("AdministratorAccess", done.stderr)
        self.assertFalse([c for c in box.calls() if "get-policy" in c["rest"]])

    def test_invalid_second_document_fails_before_any_aws_call(self):
        box = self.ready(EXTRA_POLICIES="provision broken")
        self.extra(box)
        self.extra(box, name="broken", document={"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": "*", "Action": "ssm:*", "Resource": "*"}]})
        done = box.run("apply", "x")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("Principal is forbidden", done.stderr)
        self.assertEqual(box.calls(), [])

    def test_custom_simulator_receives_all_documents(self):
        box = self.ready(EXTRA_POLICIES="provision")
        document = self.extra(box)
        box.profile("default", aws_access_key_id="AKIAADMIN")
        box.rule("simulate-custom-policy", stdout="allowed")
        box.run("simulate", "x")
        calls = [c for c in box.calls() if "simulate-custom-policy" in c["rest"]]
        self.assertTrue(calls)
        for call in calls:
            args = call["rest"]
            documents = args[args.index("--policy-input-list") + 1:args.index("--action-names")]
            self.assertEqual(len(documents), 2)
            self.assertIn(document, [json.loads(item) for item in documents])
        actions = [c["rest"][c["rest"].index("--action-names") + 1] for c in calls]
        self.assertIn("ec2:CreateSnapshot", actions)
        self.assertIn("ec2:DisassociateAddress", actions)


class RoleAndRuleCase(unittest.TestCase):
    decision = LifecycleCase.decision
    sandbox = CommandCase.sandbox
    ready = CommandCase.ready
    role = f"arn:aws:iam::{ACCOUNT_ID}:role/application/ssm-instance"

    def test_pass_role_is_opt_in_and_exact(self):
        policy = render("sandbox", pass_roles=self.role)
        context = {"iam:PassedToService": "ec2.amazonaws.com"}
        self.assertEqual(self.decision(policy, "iam:PassRole", self.role, context), "allowed")
        for resource in (self.role + "-other", self.role.replace(ACCOUNT_ID, "999988887777")):
            self.assertEqual(self.decision(policy, "iam:PassRole", resource, context, broad=True), "explicitDeny")
        for context in ({}, {"iam:PassedToService": "lambda.amazonaws.com"}):
            self.assertEqual(self.decision(policy, "iam:PassRole", self.role, context, broad=True), "explicitDeny")
        self.assertEqual(self.decision(render("sandbox"), "iam:PassRole", self.role,
                                      {"iam:PassedToService": "ec2.amazonaws.com"}, broad=True), "explicitDeny")

    def test_iam_administration_and_assumption_stay_explicitly_denied(self):
        policy = render("sandbox", pass_roles=self.role)
        for action in ("iam:CreateAccessKey", "iam:CreateRole", "iam:AttachRolePolicy", "iam:PutRolePolicy",
                       "iam:UpdateAssumeRolePolicy", "iam:PassRole", "iam:CreatePolicyVersion", "sts:AssumeRole"):
            self.assertEqual(self.decision(policy, action, self.role, {}, broad=True), "explicitDeny", action)
            if action != "iam:PassRole":
                for resource in (self.role, "*"):
                    self.assertEqual(self.decision(policy, action, resource,
                                                  {"iam:PassedToService": "ec2.amazonaws.com"}, broad=True), "explicitDeny", action)

    def test_invalid_role_config_fails_before_aws_calls(self):
        for level, roles in (("readonly", self.role), ("operator", self.role),
                             ("sandbox", self.role + "*"), ("sandbox", self.role + "?"),
                             ("sandbox", self.role.replace(ACCOUNT_ID, "999988887777")),
                             ("sandbox", self.role + " " + self.role)):
            with self.subTest(level=level, roles=roles):
                box = self.ready(level=level, PASS_ROLE_ARNS=roles)
                done = box.run("apply", "x")
                self.assertNotEqual(done.returncode, 0)
                self.assertIn("PASS_ROLE_ARNS", done.stderr)
                self.assertEqual(box.calls(), [])

    def test_security_rule_managed_tags_only_at_creation(self):
        resource = f"arn:aws:ec2:us-east-1:{ACCOUNT_ID}:security-group-rule/sgr-probe"
        policy = render("sandbox")
        for operation in ("AuthorizeSecurityGroupIngress", "AuthorizeSecurityGroupEgress"):
            context = {"ec2:CreateAction": operation, "aws:RequestTag/claude-managed": "true", "aws:TagKeys": ["claude-managed"]}
            self.assertEqual(self.decision(policy, "ec2:CreateTags", resource, context), "allowed")
            for value in (None, "false"):
                probe = dict(context)
                if value is None:
                    del probe["aws:RequestTag/claude-managed"]
                else:
                    probe["aws:RequestTag/claude-managed"] = value
                self.assertNotEqual(self.decision(policy, "ec2:CreateTags", resource, probe), "allowed")
            for wrong in (resource.replace("us-east-1", "eu-west-1"), resource.replace(ACCOUNT_ID, "999988887777")):
                self.assertNotEqual(self.decision(policy, "ec2:CreateTags", wrong, context), "allowed")
            for level in ("readonly", "operator"):
                self.assertEqual(self.decision(render(level), "ec2:CreateTags", resource, context, broad=True), "explicitDeny")
        self.assertEqual(self.decision(policy, "ec2:CreateTags", resource,
                                      {"aws:TagKeys": ["claude-managed"]}, broad=True), "explicitDeny")

    def test_simulator_checks_approved_role_and_denial_boundaries(self):
        box = self.ready(level="sandbox", PASS_ROLE_ARNS=self.role)
        box.profile("default", aws_access_key_id="AKIAADMIN")
        box.rule("simulate-custom-policy", stdout="allowed")
        box.run("simulate", "x")
        calls = [c for c in box.calls() if "simulate-custom-policy" in c["rest"]]
        approved = [c for c in calls if "iam:PassRole" in c["rest"] and self.role in c["rest"]]
        self.assertEqual(len(approved), 3)
        self.assertTrue(any("ContextKeyValues=ec2.amazonaws.com" in " ".join(c["rest"]) for c in approved))
        self.assertTrue(any("ContextKeyValues=lambda.amazonaws.com" in " ".join(c["rest"]) for c in approved))
        self.assertTrue(any("arn:aws:iam::000000000000:role/unapproved" in c["rest"] for c in calls))
        self.assertLessEqual(len(json.dumps(render("sandbox", pass_roles=self.role), separators=(",", ":"))), 6144)


    def test_every_simulated_action_has_one_service_prefix(self):
        # expect() assigns the global `action`, and the teardown loop used the
        # same name: its second and third probe asked for ec2:ec2:<Action>,
        # which the simulator answers implicitDeny (16 false FAILs, sd:2851).
        box = self.ready(level="sandbox", PASS_ROLE_ARNS=self.role)
        box.profile("default", aws_access_key_id="AKIAADMIN")
        box.rule("simulate-custom-policy", stdout="allowed")
        box.run("simulate", "x")
        names = [c["rest"][c["rest"].index("--action-names") + 1] for c in box.calls()
                 if "--action-names" in c["rest"]]
        self.assertTrue(names)
        malformed = sorted({n for n in names if not re.fullmatch(r"[a-z0-9-]+:[A-Za-z0-9*]+", n)})
        self.assertEqual(malformed, [])
        teardown = [n for n in names if n == "ec2:DeleteKeyPair"]
        self.assertEqual(len(teardown), 3)

    def test_check_proves_every_disk_route_denied_below_sandbox(self):
        # The live check, not only the render, has to show the routes closed:
        # it is what the owner reads before lifting the evaluation rule.
        box = self.ready(level="operator")
        box.profile("default", aws_access_key_id="AKIAADMIN")
        box.rule("simulate-custom-policy", stdout="explicitDeny")
        done = box.run("simulate", "x")
        for action in DISK_ROUTES:
            self.assertIn("PASS explicitDeny " + action + " ", done.stdout)

if __name__ == "__main__":
    unittest.main()

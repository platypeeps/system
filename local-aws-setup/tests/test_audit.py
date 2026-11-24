"""The deployment audit trail: the tamper deny, `audit` and `events`.

The threat model in the simulator's design notes wants three controls outside
the instance: a CloudTrail trail for SSM sessions and commands in a bucket the
deployer cannot clean, `AWS-RunShellScript` output kept in that bucket, and a
review of those events. These cases watch the calls against the fake `aws`.
"""

import json
import unittest

from tests.test_aws_setup import ACCOUNT_ID, ADMIN_ARN, Sandbox, statement

BUCKET = "example-audit-bucket"
TRAIL_ARN = "arn:aws:cloudtrail:eu-west-1:%s:trail/deployment-audit" % ACCOUNT_ID
TAMPER = ("s3:PutObject", "s3:DeleteObject", "s3:DeleteObjectVersion",
          "s3:PutLifecycleConfiguration", "s3:PutBucketVersioning")


def event(name, user="deployer", account=ACCOUNT_ID, kind="IAMUser",
          when="2026-11-23T10:00:00Z", **params):
    record = {
        "eventSource": "ssm.amazonaws.com",
        "eventName": name,
        "eventTime": when,
        "userIdentity": {"type": kind, "accountId": account, "userName": user,
                         "arn": "arn:aws:iam::%s:user/%s" % (account, user)},
        "requestParameters": params,
    }
    return {"EventName": name, "CloudTrailEvent": json.dumps(record)}


class Case(unittest.TestCase):
    def box(self, **account):
        account.setdefault("AUDIT_BUCKET", BUCKET)
        box = Sandbox(**account)
        self.addCleanup(box.cleanup)
        box.profile("admin-x", aws_access_key_id="AKIAADMIN")
        box.identity("AKIAADMIN", ADMIN_ARN)
        return box

    def rests(self, box):
        return [c["rest"] for c in box.calls()]


class TamperDenyCase(Case):
    def render(self, box):
        done = box.run("render", "x")
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def test_every_level_denies_cleaning_the_audit_bucket(self):
        for level in ("readonly", "operator", "sandbox"):
            with self.subTest(level=level):
                item = statement(self.render(self.box(level=level)), "DenyAuditTampering")
                self.assertIsNotNone(item)
                self.assertEqual(item["Effect"], "Deny")
                for action in TAMPER:
                    self.assertIn(action, item["Action"])
                self.assertEqual(sorted(item["Resource"]),
                                 ["arn:aws:s3:::%s" % BUCKET, "arn:aws:s3:::%s/*" % BUCKET])

    def test_no_audit_bucket_renders_no_statement(self):
        document = self.render(self.box(AUDIT_BUCKET=""))
        self.assertIsNone(statement(document, "DenyAuditTampering"))

    def test_the_audit_bucket_may_not_be_a_working_bucket(self):
        box = self.box(buckets="alpha %s" % BUCKET)
        done = box.run("render", "x")
        self.assertEqual(done.returncode, 1)
        self.assertIn("AUDIT_BUCKET may not be listed in S3_BUCKETS", done.stderr)

    def test_a_working_bucket_that_only_contains_the_name_is_fine(self):
        box = self.box(buckets="%s-copy" % BUCKET)
        self.assertEqual(box.run("render", "x").returncode, 0)

    def test_an_invalid_audit_bucket_name_is_refused(self):
        for name in ("Upper", "a b", "x", "bad\nname", "é-bucket"):
            with self.subTest(name=name):
                done = self.box(AUDIT_BUCKET=name).run("render", "x")
                self.assertEqual(done.returncode, 1)
                self.assertIn("AUDIT_BUCKET", done.stderr)

    def test_simulate_expects_the_tamper_deny(self):
        box = self.box()
        box.profile("default", aws_access_key_id="AKIAADMIN")
        box.rule("simulate-custom-policy", stdout="allowed")
        done = box.run("simulate", "x")
        self.assertEqual(done.returncode, 1)
        self.assertIn("FAIL want explicitDeny, got allowed: s3:DeleteObjectVersion "
                      "arn:aws:s3:::%s/probe.txt" % BUCKET, done.stdout)
        self.assertIn("FAIL want explicitDeny, got allowed: s3:PutBucketVersioning "
                      "arn:aws:s3:::%s\n" % BUCKET, done.stdout)


class AuditCase(Case):
    def fresh(self, **account):
        box = self.box(**account)
        box.rule("head-bucket", exit=254,
                 stderr="An error occurred (404) when calling the HeadBucket operation: Not Found")
        box.rule("get-trail", exit=254,
                 stderr="An error occurred (TrailNotFoundException) when calling the GetTrail operation")
        return box

    def test_a_fresh_account_gets_a_bucket_and_a_running_trail(self):
        box = self.fresh()
        done = box.run("audit", "x")
        self.assertEqual(done.returncode, 0, done.stderr)
        rests = self.rests(box)
        names = [r[1] for r in rests if r[:1] in (["s3api"], ["cloudtrail"])]
        self.assertEqual(names, ["head-bucket", "create-bucket", "put-public-access-block",
                                 "put-bucket-versioning", "put-bucket-policy",
                                 "get-trail", "create-trail", "start-logging"])
        create = next(r for r in rests if r[1:2] == ["create-bucket"])
        self.assertIn("LocationConstraint=eu-west-1", create)
        policy = next(r for r in rests if r[1:2] == ["put-bucket-policy"])
        policy = json.loads(policy[policy.index("--policy") + 1])
        write = statement(policy, "CloudTrailWrite")
        self.assertEqual(write["Resource"],
                         "arn:aws:s3:::%s/AWSLogs/%s/*" % (BUCKET, ACCOUNT_ID))
        self.assertEqual(write["Condition"]["StringEquals"]["aws:SourceArn"], TRAIL_ARN)
        trail = next(r for r in rests if r[1:2] == ["create-trail"])
        for flag in ("--enable-log-file-validation", "--is-multi-region-trail"):
            self.assertIn(flag, trail)
        self.assertEqual(trail[trail.index("--s3-bucket-name") + 1], BUCKET)

    def test_us_east_1_creates_the_bucket_without_a_location(self):
        box = self.fresh(AGENT_REGION="us-east-1")
        done = box.run("audit", "x")
        self.assertEqual(done.returncode, 0, done.stderr)
        create = next(r for r in self.rests(box) if r[1:2] == ["create-bucket"])
        self.assertFalse([a for a in create if "LocationConstraint" in a])

    def test_a_second_run_creates_nothing_and_keeps_the_trail_running(self):
        box = self.box()
        box.rule("get-trail", stdout=BUCKET)
        done = box.run("audit", "x")
        self.assertEqual(done.returncode, 0, done.stderr)
        names = [r[1] for r in self.rests(box) if r[:1] in (["s3api"], ["cloudtrail"])]
        self.assertNotIn("create-bucket", names)
        self.assertNotIn("create-trail", names)
        self.assertEqual(names[-1], "start-logging")

    def test_a_trail_that_writes_elsewhere_is_refused(self):
        box = self.box()
        box.rule("get-trail", stdout="somebody-elses-bucket")
        done = box.run("audit", "x")
        self.assertEqual(done.returncode, 1)
        self.assertIn("writes to somebody-elses-bucket", done.stderr)
        names = [r[1] for r in self.rests(box) if r[:1] == ["cloudtrail"]]
        self.assertEqual(names, ["get-trail"])

    def test_a_bucket_in_another_account_is_never_written(self):
        box = self.box()
        box.rule("head-bucket", exit=254,
                 stderr="An error occurred (403) when calling the HeadBucket operation: Forbidden")
        done = box.run("audit", "x")
        self.assertEqual(done.returncode, 1)
        self.assertIn("(403)", done.stderr)
        writes = [r for r in self.rests(box) if r[:1] in (["s3api"], ["cloudtrail"])]
        self.assertEqual([r[1] for r in writes], ["head-bucket"])

    def test_a_trail_read_that_fails_otherwise_is_not_a_missing_trail(self):
        box = self.box()
        box.rule("get-trail", exit=254, stderr="An error occurred (AccessDenied)")
        done = box.run("audit", "x")
        self.assertEqual(done.returncode, 1)
        self.assertNotIn("create-trail", [r[1] for r in self.rests(box) if len(r) > 1])

    def test_a_failed_bucket_create_stops_before_the_trail(self):
        box = self.fresh()
        box.refuse("s3api", "create-bucket", "--bucket", BUCKET,
                   "--create-bucket-configuration", "LocationConstraint=eu-west-1")
        done = box.run("audit", "x")
        self.assertEqual(done.returncode, 1)
        self.assertNotIn("get-trail", [r[1] for r in self.rests(box) if len(r) > 1])

    def test_no_audit_bucket_makes_no_call(self):
        box = self.box(AUDIT_BUCKET="")
        done = box.run("audit", "x")
        self.assertEqual(done.returncode, 1)
        self.assertIn("AUDIT_BUCKET is not set", done.stderr)
        self.assertEqual(box.calls(), [])

    def test_a_dry_run_prints_every_write(self):
        box = self.fresh()
        done = box.run("audit", "x", DRY_RUN="1")
        self.assertEqual(done.returncode, 0, done.stderr)
        for name in ("create-bucket", "put-bucket-policy", "create-trail", "start-logging"):
            self.assertIn(" %s " % name, done.stderr)
        made = {r[1] for r in self.rests(box) if len(r) > 1}
        self.assertFalse(made & {"create-bucket", "put-bucket-policy", "create-trail", "start-logging"})


class EventsCase(Case):
    def review(self, events, *args, **account):
        box = self.box(AGENT_USER="deployer", **account)
        box.rule("lookup-events", stdout=json.dumps({"Events": events}))
        return box, box.run("events", "x", *args, TZ="America/Denver")

    def test_logged_commands_and_own_sessions_pass(self):
        box, done = self.review([
            event("SendCommand", documentName="AWS-RunShellScript",
                  instanceIds=["i-0123456789abcdef0"], outputS3BucketName=BUCKET),
            event("StartSession", target="i-0123456789abcdef0", documentName="AWS-StartPortForwardingSession"),
            event("TerminateSession", sessionId="deployer-0123"),
        ])
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("TUNNEL", done.stdout)
        self.assertIn("2026-11-23 03:00 MST", done.stdout)
        self.assertIn("3 events, 0 flagged", done.stdout)

    def test_each_reaching_call_is_looked_up_with_a_start_time(self):
        box, done = self.review([], "6")
        looked = [r for r in self.rests(box) if r[1:2] == ["lookup-events"]]
        names = sorted(r[r.index("--lookup-attributes") + 1] for r in looked)
        self.assertEqual(names, ["AttributeKey=EventName,AttributeValue=%s" % n for n in
                                 ("ResumeSession", "SendCommand", "StartSession", "TerminateSession")])
        for rest in looked:
            self.assertRegex(rest[rest.index("--start-time") + 1], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_a_session_somebody_else_started_is_flagged(self):
        _, done = self.review([event("StartSession", user="intruder", target="i-0123456789abcdef0")])
        self.assertEqual(done.returncode, 1)
        self.assertIn("UNEXPECTED", done.stdout)

    def test_the_same_name_in_another_account_is_flagged(self):
        _, done = self.review([event("StartSession", account="999988887777")])
        self.assertEqual(done.returncode, 1)
        self.assertIn("UNEXPECTED", done.stdout)

    def test_a_command_without_logged_output_is_flagged(self):
        for params in ({}, {"outputS3BucketName": "other-bucket"}, {"outputS3BucketName": None}):
            with self.subTest(params=params):
                _, done = self.review([event("SendCommand", instanceIds=["i-1"], **params)])
                self.assertEqual(done.returncode, 1)
                self.assertIn("UNLOGGED", done.stdout)

    def test_without_an_audit_bucket_every_command_is_unlogged(self):
        _, done = self.review([event("SendCommand", outputS3BucketName="")], AUDIT_BUCKET="")
        self.assertEqual(done.returncode, 1)
        self.assertIn("UNLOGGED", done.stdout)

    def test_newlines_and_markup_in_a_field_stay_on_one_line(self):
        _, done = self.review([event("StartSession", user="x\nPASS fake\x1b[0m",
                                     target="<b>i-1</b>\r\n")])
        self.assertEqual(done.returncode, 1)
        rows = [line for line in done.stdout.splitlines() if "StartSession" in line]
        self.assertEqual(len(rows), 1)
        self.assertNotIn("\x1b", done.stdout)
        self.assertNotIn("\r", done.stdout)

    def test_malformed_events_fail_instead_of_passing(self):
        for bad in ({"Events": "nope"}, {"Events": [{"CloudTrailEvent": "{"}]},
                    {"Events": [{"CloudTrailEvent": json.dumps({"eventName": "StartSession"})}]},
                    {"Events": [{"CloudTrailEvent": json.dumps(["list"])}]}, ["top"]):
            with self.subTest(bad=bad):
                box = self.box(AGENT_USER="deployer")
                box.rule("lookup-events", stdout=json.dumps(bad))
                done = box.run("events", "x")
                self.assertEqual(done.returncode, 1)
                self.assertIn("malformed", done.stderr)

    def test_a_failed_lookup_is_an_error_not_a_clean_review(self):
        box = self.box(AGENT_USER="deployer")
        box.rule("lookup-events", exit=254, stderr="AccessDenied")
        done = box.run("events", "x")
        self.assertEqual(done.returncode, 1)
        self.assertNotIn("0 flagged", done.stdout)

    def test_bad_hours_make_no_call(self):
        for hours in ("0", "-1", "2161", "abc", "1.5", "99999999999999999999", ""):
            with self.subTest(hours=hours):
                box = self.box()
                done = box.run("events", "x", hours)
                self.assertEqual(done.returncode, 1)
                self.assertIn("hours", done.stderr)
                self.assertEqual(box.calls(), [])


if __name__ == "__main__":
    unittest.main()

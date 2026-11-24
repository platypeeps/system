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
GOOD_SELECTORS = json.dumps({"EventSelectors": [
    {"ReadWriteType": "All", "IncludeManagementEvents": True, "DataResources": [],
     "ExcludeManagementEventSources": []}]})


def trail(**changed):
    """get-trail's Trail object for a trail as `audit` creates it."""
    made = {"Name": "deployment-audit", "S3BucketName": BUCKET, "TrailARN": TRAIL_ARN,
            "HomeRegion": "eu-west-1", "IsMultiRegionTrail": True,
            "LogFileValidationEnabled": True, "IsOrganizationTrail": False,
            "IncludeGlobalServiceEvents": True}
    made.update(changed)
    return json.dumps({k: v for k, v in made.items() if v is not ...})


COMMAND_ID = "0b6f1c2e-3d4a-4b5c-8d9e-0f1a2b3c4d5e"
SAVED = json.dumps({"Contents": [{"Key": "ssm-output/x/i-1/awsrunShellScript/0/stdout"}]})


HEALTHY = json.dumps({"IsLogging": True,
                      "LatestDeliveryTime": "2026-11-23T10:00:00Z"})


TAMPER = ("s3:PutObject", "s3:DeleteObject", "s3:DeleteObjectVersion",
          "s3:PutLifecycleConfiguration", "s3:PutBucketVersioning")


def event(name, user="deployer", account=ACCOUNT_ID, kind="IAMUser",
          when="2026-11-23T10:00:00Z", response=..., **params):
    record = {
        "eventSource": "ssm.amazonaws.com",
        "eventName": name,
        "eventTime": when,
        "userIdentity": {"type": kind, "accountId": account, "userName": user,
                         "arn": "arn:aws:iam::%s:user/%s" % (account, user)},
        "requestParameters": params,
    }
    if response is ... and name == "SendCommand":
        response = {"command": {"commandId": COMMAND_ID}}
    if response is not ...:
        record["responseElements"] = response
    return {"EventName": name, "CloudTrailEvent": json.dumps(record)}


class Case(unittest.TestCase):
    def box(self, **account):
        account.setdefault("AUDIT_BUCKET", BUCKET)
        box = Sandbox(**account)
        self.addCleanup(box.cleanup)
        box.profile("admin-x", aws_access_key_id="AKIAADMIN")
        box.identity("AKIAADMIN", ADMIN_ARN)
        # An existing bucket has no lifecycle rules unless a case says so.
        box.rule("get-bucket-lifecycle-configuration", exit=254,
                 stderr="An error occurred (NoSuchLifecycleConfiguration) when calling "
                        "the GetBucketLifecycleConfiguration operation")
        # A started trail delivers until a case says otherwise.
        box.rule("get-trail-status", stdout=HEALTHY)
        return box

    def status(self, box, **answer):
        """Answer the trail status read ahead of the default above."""
        box.scenario["rules"].insert(0, {"match": ["get-trail-status"], **answer})

    def lifecycle(self, box, **answer):
        """Answer the lifecycle read ahead of the default above."""
        box.scenario["rules"].insert(
            0, {"match": ["get-bucket-lifecycle-configuration"], **answer})

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
        self.assertEqual(names, ["head-bucket", "get-trail", "create-bucket",
                                 "put-public-access-block", "put-bucket-versioning",
                                 "put-bucket-policy", "create-trail", "start-logging",
                                 "get-trail-status"])
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
        box.rule("get-trail", stdout=trail())
        box.rule("get-event-selectors", stdout=GOOD_SELECTORS)
        done = box.run("audit", "x")
        self.assertEqual(done.returncode, 0, done.stderr)
        names = [r[1] for r in self.rests(box) if r[:1] in (["s3api"], ["cloudtrail"])]
        self.assertNotIn("create-bucket", names)
        self.assertNotIn("create-trail", names)
        self.assertEqual(names[-2:], ["start-logging", "get-trail-status"])

    def test_an_existing_bucket_is_read_for_lifecycle_rules_first(self):
        box = self.box()
        box.rule("get-trail", stdout=trail())
        box.rule("get-event-selectors", stdout=GOOD_SELECTORS)
        done = box.run("audit", "x")
        self.assertEqual(done.returncode, 0, done.stderr)
        rests = self.rests(box)
        names = [r[1] for r in rests if r[:1] in (["s3api"], ["cloudtrail"])]
        self.assertEqual(names[:3], ["head-bucket", "get-bucket-lifecycle-configuration",
                                     "get-trail"])
        read = rests[[r[1:2] for r in rests].index(["get-bucket-lifecycle-configuration"])]
        self.assertEqual(read[read.index("--expected-bucket-owner") + 1], ACCOUNT_ID)

    def test_a_missing_bucket_has_no_lifecycle_to_read(self):
        box = self.fresh()
        self.assertEqual(box.run("audit", "x").returncode, 0)
        names = [r[1] for r in self.rests(box) if len(r) > 1]
        self.assertNotIn("get-bucket-lifecycle-configuration", names)

    def test_a_lifecycle_that_expires_objects_is_refused_before_any_write(self):
        def rules(*items):
            return json.dumps({"Rules": list(items)})
        bad = {
            "expires current objects": rules(
                {"ID": "tidy", "Status": "Enabled", "Filter": {}, "Expiration": {"Days": 30}}),
            "expires on a date": rules(
                {"ID": "tidy", "Status": "Enabled", "Expiration": {"Date": "2027-01-01T00:00:00Z"}}),
            "expires noncurrent versions": rules(
                {"ID": "old", "Status": "Enabled",
                 "NoncurrentVersionExpiration": {"NoncurrentDays": 1}}),
            "expires under one prefix only": rules(
                {"ID": "logs", "Status": "Enabled", "Filter": {"Prefix": "AWSLogs/"},
                 "Expiration": {"Days": 90}}),
            "status not Disabled": rules(
                {"ID": "odd", "Status": "enabled", "Expiration": {"Days": 1}}),
            "status missing": rules({"ID": "odd", "Expiration": {"Days": 1}}),
            "second rule expires": rules(
                {"ID": "move", "Status": "Enabled",
                 "Transitions": [{"Days": 30, "StorageClass": "GLACIER"}]},
                {"ID": "x\n\x1b[0m\u00e9", "Status": "Enabled", "Expiration": {"Days": 9}}),
            "rules not a list": json.dumps({"Rules": "Expiration"}),
            "rule not an object": rules("Expiration"),
            "no rules key": json.dumps({}),
            "not an object": json.dumps(["Rules"]),
            "not json": "Rules",
            "empty": "",
        }
        for why, answer in bad.items():
            with self.subTest(why=why):
                box = self.box()
                self.lifecycle(box, stdout=answer)
                box.rule("get-trail", stdout=trail())
                box.rule("get-event-selectors", stdout=GOOD_SELECTORS)
                done = box.run("audit", "x")
                self.assertEqual(done.returncode, 1, done.stdout)
                self.assertIn("bucket %s" % BUCKET, done.stderr)
                self.assertNotIn("\x1b", done.stderr)
                self.assertEqual(done.stderr.count("\n"), 1, done.stderr)
                names = [r[1] for r in self.rests(box) if r[:1] in (["s3api"], ["cloudtrail"])]
                self.assertEqual(names, ["head-bucket", "get-bucket-lifecycle-configuration"])

    def test_a_lifecycle_that_keeps_every_object_is_kept(self):
        keeps = {
            "transition only": [{"ID": "cold", "Status": "Enabled", "Filter": {},
                                 "Transitions": [{"Days": 30, "StorageClass": "GLACIER"}]}],
            "multipart cleanup": [{"ID": "mpu", "Status": "Enabled", "Filter": {},
                                   "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7}}],
            "disabled expiry": [{"ID": "off", "Status": "Disabled", "Expiration": {"Days": 1}}],
        }
        for why, items in keeps.items():
            with self.subTest(why=why):
                box = self.box()
                self.lifecycle(box, stdout=json.dumps({"Rules": items}))
                box.rule("get-trail", stdout=trail())
                box.rule("get-event-selectors", stdout=GOOD_SELECTORS)
                done = box.run("audit", "x")
                self.assertEqual(done.returncode, 0, done.stderr)

    def test_a_lifecycle_read_that_fails_otherwise_is_refused(self):
        box = self.box()
        self.lifecycle(box, exit=254, stderr="An error occurred (AccessDenied)")
        done = box.run("audit", "x")
        self.assertEqual(done.returncode, 1)
        self.assertIn("AccessDenied", done.stderr)
        names = [r[1] for r in self.rests(box) if r[:1] in (["s3api"], ["cloudtrail"])]
        self.assertEqual(names, ["head-bucket", "get-bucket-lifecycle-configuration"])

    def test_a_trail_that_writes_elsewhere_is_refused(self):
        box = self.box()
        box.rule("get-trail", stdout=trail(S3BucketName="somebody-elses-bucket"))
        done = box.run("audit", "x")
        self.assertEqual(done.returncode, 1)
        self.assertIn("writes to somebody-elses-bucket", done.stderr)
        names = [r[1] for r in self.rests(box) if r[:1] in (["s3api"], ["cloudtrail"])]
        # No bucket write either: a refused run leaves the bucket as it was.
        self.assertEqual(names, ["head-bucket", "get-bucket-lifecycle-configuration",
                                 "get-trail"])

    def test_a_trail_that_drops_management_writes_is_refused_before_any_write(self):
        bad = (
            {"EventSelectors": [{"ReadWriteType": "All", "IncludeManagementEvents": False}]},
            {"EventSelectors": [{"ReadWriteType": "ReadOnly", "IncludeManagementEvents": True}]},
            {"EventSelectors": [{"ReadWriteType": "All", "IncludeManagementEvents": True,
                                 "ExcludeManagementEventSources": ["ssm.amazonaws.com"]}]},
            {"EventSelectors": []},
            {"AdvancedEventSelectors": [{"Name": "data only", "FieldSelectors": []}]},
            {"EventSelectors": "All"},
            "not json",
        )
        for selectors in bad:
            with self.subTest(selectors=selectors):
                box = self.box()
                box.rule("get-trail", stdout=trail())
                box.rule("get-event-selectors",
                         stdout=selectors if isinstance(selectors, str) else json.dumps(selectors))
                done = box.run("audit", "x")
                self.assertEqual(done.returncode, 1)
                self.assertIn("management", done.stderr)
                names = [r[1] for r in self.rests(box) if r[:1] in (["s3api"], ["cloudtrail"])]
                self.assertEqual(names, ["head-bucket", "get-bucket-lifecycle-configuration",
                                         "get-trail", "get-event-selectors"])

    def test_a_trail_unlike_the_one_audit_creates_is_refused_before_any_write(self):
        # Every property a new trail gets is demanded of an existing one.
        other_home = TRAIL_ARN.replace("eu-west-1", "us-west-2")
        bad = {
            "single region": trail(IsMultiRegionTrail=False),
            "region flag missing": trail(IsMultiRegionTrail=...),
            "no log file validation": trail(LogFileValidationEnabled=False),
            "validation flag missing": trail(LogFileValidationEnabled=...),
            "validation flag a string": trail(LogFileValidationEnabled="true"),
            "key prefix the bucket policy does not allow": trail(S3KeyPrefix="logs"),
            "homed in another region": trail(TrailARN=other_home, HomeRegion="us-west-2"),
            "another account's trail": trail(TrailARN=TRAIL_ARN.replace(ACCOUNT_ID, "999988887777")),
            "organization trail": trail(IsOrganizationTrail=True),
            "bucket name missing": trail(S3BucketName=...),
            "not an object": json.dumps([BUCKET]),
            "not json": BUCKET,
            "empty": "",
        }
        for why, answer in bad.items():
            with self.subTest(why=why):
                box = self.box()
                box.rule("get-trail", stdout=answer)
                box.rule("get-event-selectors", stdout=GOOD_SELECTORS)
                done = box.run("audit", "x")
                self.assertEqual(done.returncode, 1, done.stdout)
                self.assertIn("trail deployment-audit", done.stderr)
                names = [r[1] for r in self.rests(box) if r[:1] in (["s3api"], ["cloudtrail"])]
                self.assertEqual(names, ["head-bucket", "get-bucket-lifecycle-configuration",
                                         "get-trail"])

    def test_a_trail_recording_management_writes_is_kept(self):
        for kind in ("All", "WriteOnly"):
            with self.subTest(kind=kind):
                box = self.box()
                box.rule("get-trail", stdout=trail())
                box.rule("get-event-selectors", stdout=json.dumps({"EventSelectors": [
                    {"ReadWriteType": "ReadOnly", "IncludeManagementEvents": True},
                    {"ReadWriteType": kind, "IncludeManagementEvents": True,
                     "ExcludeManagementEventSources": ["kms.amazonaws.com"]}]}))
                done = box.run("audit", "x")
                self.assertEqual(done.returncode, 0, done.stderr)

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
        made = [r[1] for r in self.rests(box) if len(r) > 1]
        self.assertNotIn("put-bucket-policy", made)
        self.assertNotIn("create-trail", made)

    def test_no_audit_bucket_makes_no_call(self):
        box = self.box(AUDIT_BUCKET="")
        done = box.run("audit", "x")
        self.assertEqual(done.returncode, 1)
        self.assertIn("AUDIT_BUCKET is not set", done.stderr)
        self.assertEqual(box.calls(), [])

    def test_the_trail_status_is_read_after_logging_starts(self):
        box = self.fresh()
        done = box.run("audit", "x")
        self.assertEqual(done.returncode, 0, done.stderr)
        names = [r[1] for r in self.rests(box) if r[:1] == ["cloudtrail"]]
        self.assertEqual(names[-2:], ["start-logging", "get-trail-status"])

    def test_a_trail_that_cannot_deliver_is_refused(self):
        bad = {
            "log delivery": {"IsLogging": True,
                             "LatestDeliveryError": "AccessDenied"},
            "digest delivery": {"IsLogging": True,
                                "LatestDigestDeliveryError": "NoSuchBucket"},
            "error with markup": {"IsLogging": True,
                                  "LatestDeliveryError": "<b>x</b>\nfake ok"},
            "not logging": {"IsLogging": False},
            "logging as a string": {"IsLogging": "true"},
            "error not a string": {"IsLogging": True, "LatestDeliveryError": 5},
            "not an object": [],
        }
        for why, answer in bad.items():
            with self.subTest(why=why):
                box = self.fresh()
                self.status(box, stdout=json.dumps(answer))
                done = box.run("audit", "x")
                self.assertEqual(done.returncode, 1, done.stdout)
                self.assertIn("trail deployment-audit", done.stderr)
                self.assertNotIn("is logging to", done.stdout)
                self.assertNotIn("\nfake ok", done.stderr)
        for why, answer in (("empty", ""), ("not json", "{")):
            with self.subTest(why=why):
                box = self.fresh()
                self.status(box, stdout=answer)
                self.assertEqual(box.run("audit", "x").returncode, 1)

    def test_a_failed_status_read_is_refused(self):
        box = self.fresh()
        self.status(box, exit=254, stderr="An error occurred (AccessDenied)")
        done = box.run("audit", "x")
        self.assertEqual(done.returncode, 1)
        self.assertIn("could not read the status of trail deployment-audit",
                      done.stderr)

    def test_an_empty_error_is_no_error(self):
        box = self.fresh()
        self.status(box, stdout=json.dumps({"IsLogging": True,
                                            "LatestDeliveryError": ""}))
        self.assertEqual(box.run("audit", "x").returncode, 0)

    def test_a_dry_run_prints_every_write(self):
        box = self.fresh()
        done = box.run("audit", "x", DRY_RUN="1")
        self.assertEqual(done.returncode, 0, done.stderr)
        for name in ("create-bucket", "put-bucket-policy", "create-trail", "start-logging"):
            self.assertIn(" %s " % name, done.stderr)
        made = {r[1] for r in self.rests(box) if len(r) > 1}
        self.assertFalse(made & {"create-bucket", "put-bucket-policy", "create-trail", "start-logging"})


class EventsCase(Case):
    def review(self, events, *args, regions=("eu-west-1",), first=(), **account):
        box = self.box(AGENT_USER="deployer", **account)
        for rule in first:
            box.rule(*rule.pop("match"), **rule)
        box.rule("describe-regions", stdout=json.dumps(list(regions)))
        box.rule("lookup-events", region="eu-west-1", stdout=json.dumps({"Events": events}))
        box.rule("lookup-events", stdout=json.dumps({"Events": []}))
        box.rule("list-objects-v2", stdout=SAVED)
        return box, box.run("events", "x", *args, TZ="America/Denver")

    def listed(self, box):
        return [r for r in self.rests(box) if r[1:2] == ["list-objects-v2"]]

    def test_every_enabled_region_is_reviewed(self):
        intruder = event("StartSession", user="intruder", target="i-0123456789abcdef0")
        box, done = self.review([], regions=("eu-west-1", "us-west-2"), first=[
            {"match": ["lookup-events", "StartSession"], "region": "us-west-2",
             "stdout": json.dumps({"Events": [intruder]})}])
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        rows = [line for line in done.stdout.splitlines() if "StartSession" in line]
        self.assertEqual(len(rows), 1)
        self.assertIn("UNEXPECTED", rows[0])
        self.assertIn("us-west-2", rows[0])
        looked = {c["region"] for c in box.calls() if c["rest"][1:2] == ["lookup-events"]}
        self.assertEqual(looked, {"eu-west-1", "us-west-2"})

    def test_a_failed_region_list_is_an_error_not_a_clean_review(self):
        _, done = self.review([], first=[
            {"match": ["describe-regions"], "exit": 254, "stderr": "AccessDenied"}])
        self.assertEqual(done.returncode, 1)
        self.assertNotIn("flagged", done.stdout)
        self.assertIn("regions", done.stderr)

    def test_a_malformed_region_list_fails(self):
        for bad in ([], "eu-west-1", [1], ["../etc"], ["us-west-2"], {"Regions": []}):
            with self.subTest(bad=bad):
                box, done = self.review([], first=[
                    {"match": ["describe-regions"], "stdout": json.dumps(bad)}])
                self.assertEqual(done.returncode, 1)
                self.assertIn("malformed", done.stderr)
                self.assertFalse([r for r in self.rests(box) if r[1:2] == ["lookup-events"]])

    def test_a_command_whose_output_never_arrived_is_unlogged(self):
        for answer in ({}, {"Contents": []}, {"KeyCount": 0}):
            with self.subTest(answer=answer):
                _, done = self.review([event("SendCommand", instanceIds=["i-1"],
                                             outputS3BucketName=BUCKET,
                                             outputS3KeyPrefix="ssm-output")],
                                      first=[{"match": ["list-objects-v2"],
                                              "stdout": json.dumps(answer)}])
                self.assertEqual(done.returncode, 1, done.stdout)
                self.assertIn("UNLOGGED", done.stdout)

    def test_each_instance_needs_its_own_saved_output(self):
        for prefix in ("ssm-output", "ssm-output/"):
            with self.subTest(prefix=prefix):
                missing = "ssm-output/%s/i-2/" % COMMAND_ID
                box, done = self.review([event("SendCommand", instanceIds=["i-1", "i-2"],
                                               outputS3BucketName=BUCKET,
                                               outputS3KeyPrefix=prefix)],
                                        first=[{"match": ["list-objects-v2", missing],
                                                "stdout": json.dumps({})}])
                self.assertEqual(done.returncode, 1, done.stdout)
                self.assertIn("UNLOGGED", done.stdout)
                listed = self.listed(box)
                self.assertEqual(sorted(r[r.index("--prefix") + 1] for r in listed),
                                 ["ssm-output/%s/i-1/" % COMMAND_ID, missing])
                for rest in listed:
                    self.assertEqual(rest[rest.index("--bucket") + 1], BUCKET)
                    self.assertEqual(rest[rest.index("--max-items") + 1], "1")

    def test_a_command_whose_targets_cannot_be_named_is_unlogged(self):
        # Only explicit instance ids say whose output to look for; one object
        # under the command id proves nothing about the other targets.
        tag = [{"Key": "tag:role", "Values": ["web"]}]
        cases = {
            "tag targets": {"targets": tag},
            "ids and tag targets": {"instanceIds": ["i-1"], "targets": tag},
            "targets not a list": {"instanceIds": ["i-1"], "targets": "all"},
            "no ids": {},
            "empty ids": {"instanceIds": []},
            "ids a string": {"instanceIds": "i-1"},
            "one id malformed": {"instanceIds": ["i-1", "i-1/../i-2"]},
            "one id not a string": {"instanceIds": ["i-1", None]},
        }
        for why, params in cases.items():
            with self.subTest(why=why):
                box, done = self.review([event("SendCommand", outputS3BucketName=BUCKET,
                                               outputS3KeyPrefix="ssm-output", **params)])
                self.assertEqual(done.returncode, 1, done.stdout)
                self.assertIn("UNLOGGED", done.stdout)
                self.assertEqual(self.listed(box), [])

    def test_output_is_listed_only_in_this_accounts_bucket(self):
        box, done = self.review([event("SendCommand", instanceIds=["i-1"],
                                       outputS3BucketName=BUCKET,
                                       outputS3KeyPrefix="ssm-output")])
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        (rest,) = self.listed(box)
        self.assertEqual(rest[rest.index("--expected-bucket-owner") + 1], ACCOUNT_ID)

    def test_a_cut_short_history_is_an_error_not_a_clean_review(self):
        for token in ("abc", "", None):
            with self.subTest(token=token):
                _, done = self.review([], first=[
                    {"match": ["lookup-events", "StartSession"],
                     "stdout": json.dumps({"Events": [], "NextToken": token})}])
                self.assertEqual(done.returncode, 1, done.stdout)
                self.assertNotIn("flagged", done.stdout)
                self.assertIn("incomplete", done.stderr)

    def test_a_command_without_a_usable_id_is_unlogged_unlisted(self):
        for response in (None, {}, {"command": {}}, {"command": {"commandId": "../x"}},
                         {"command": "x"}):
            with self.subTest(response=response):
                box, done = self.review([event("SendCommand", instanceIds=["i-1"],
                                               response=response,
                                               outputS3BucketName=BUCKET,
                                               outputS3KeyPrefix="ssm-output")])
                self.assertEqual(done.returncode, 1, done.stdout)
                self.assertIn("UNLOGGED", done.stdout)
                self.assertEqual(self.listed(box), [])

    def test_a_failed_output_listing_is_an_error_not_a_clean_review(self):
        for answer in ({"exit": 254, "stderr": "AccessDenied"}, {"stdout": "not json"}):
            with self.subTest(answer=answer):
                _, done = self.review([event("SendCommand", instanceIds=["i-1"],
                                             outputS3BucketName=BUCKET,
                                             outputS3KeyPrefix="ssm-output")],
                                      first=[{"match": ["list-objects-v2"], **answer}])
                self.assertEqual(done.returncode, 1)
                self.assertNotIn("0 flagged", done.stdout)

    def test_logged_commands_and_own_sessions_pass(self):
        box, done = self.review([
            event("SendCommand", documentName="AWS-RunShellScript",
                  instanceIds=["i-0123456789abcdef0"], outputS3BucketName=BUCKET,
                  outputS3KeyPrefix="ssm-output"),
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

    def test_output_outside_the_instance_role_prefix_is_unlogged(self):
        # The instance role may write only ssm-output/*; any other key is lost.
        for prefix in (None, "", "other", "ssm-output-x", "x/ssm-output/", "ssm-outputs/"):
            with self.subTest(prefix=prefix):
                _, done = self.review([event("SendCommand", instanceIds=["i-1"],
                                             outputS3BucketName=BUCKET, outputS3KeyPrefix=prefix)])
                self.assertEqual(done.returncode, 1)
                self.assertIn("UNLOGGED", done.stdout)

    def test_output_under_the_instance_role_prefix_is_logged(self):
        for prefix in ("ssm-output", "ssm-output/", "ssm-output/deploy-1"):
            with self.subTest(prefix=prefix):
                _, done = self.review([event("SendCommand", instanceIds=["i-1"],
                                             outputS3BucketName=BUCKET, outputS3KeyPrefix=prefix)])
                self.assertEqual(done.returncode, 0, done.stdout)

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
                box.rule("describe-regions", stdout=json.dumps(["eu-west-1"]))
                box.rule("lookup-events", stdout=json.dumps(bad))
                done = box.run("events", "x")
                self.assertEqual(done.returncode, 1)
                self.assertIn("malformed", done.stderr)

    def test_a_failed_lookup_is_an_error_not_a_clean_review(self):
        box = self.box(AGENT_USER="deployer")
        box.rule("describe-regions", stdout=json.dumps(["eu-west-1"]))
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

"""shared-upload.sh against a fake `aws` first on PATH (sd:5346).

Every test runs the real entrypoint with a scratch config folder and a fake
`aws` that records its argv and prints keys, or fails. No test reaches AWS.
The signer was checked once against botocore's `S3SigV4PostAuth` at a fixed
time, policy and signature equal byte for byte; here the signing key is
pinned to the AWS documentation's Signature Version 4 example instead.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import json
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOL = HERE.parent
SCRIPT = TOOL / "shared-upload.sh"
sys.path.insert(0, str(TOOL))

import shared_upload  # noqa: E402

SECRET = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"
AWS = """#!/bin/sh
printf '%s\\n' "$*" >> "$AWS_CALLS"
[ -z "$AWS_FAILS" ] || { echo "Unable to locate credentials" >&2; exit 255; }
printf '{"Version": 1, "AccessKeyId": "AKIDEXAMPLE", "SecretAccessKey": "%s"%s}\\n' \\
  "$AWS_SECRET" "${AWS_TOKEN:+, \\"SessionToken\\": \\"$AWS_TOKEN\\", \\"Expiration\\": \\"2026-10-20T00:00:00+00:00\\"}"
"""


class SharedUpload(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        (bin_dir / "aws").write_text(AWS)
        (bin_dir / "aws").chmod(0o755)
        self.config = self.root / "config"
        (self.config / "shared-upload").mkdir(parents=True)
        self.env_file("SHARED_UPLOAD_BUCKET=example-bucket\nSHARED_UPLOAD_REGION=us-east-1\n"
                      "SHARED_UPLOAD_PROFILE=example-uploader\n")
        self.calls = self.root / "aws-calls"
        self.env = {"HOME": str(self.root), "PATH": f"{bin_dir}:/usr/bin:/bin", "SYSTEM_TOOLS_CONFIG": str(self.config),
                    "AWS_CALLS": str(self.calls), "AWS_SECRET": SECRET}

    def env_file(self, text):
        (self.config / "shared-upload" / ".env").write_text(text)

    def run_tool(self, *args, **env):
        return subprocess.run(["sh", str(SCRIPT), *args], env={**self.env, **env}, capture_output=True, text=True,
                              timeout=60, stdin=subprocess.DEVNULL)

    def aws_calls(self):
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def fields(self, out):
        return {line.split(": ", 1)[0][len("field "):]: line.split(": ", 1)[1]
                for line in out.splitlines() if line.startswith("field ")}

    def test_signing_key_is_the_aws_documentation_example(self):
        key = shared_upload.signing_key(SECRET, "20120215", "us-east-1", "iam")
        self.assertEqual(key.hex(), "f4780e2d9f65fa895f9c67b32ce1baf0b0d8a43505a000a1a9e090d414db404d")

    def test_link_prints_a_signed_post_for_the_prefix_from_the_config_folder(self):
        before = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
        done = self.run_tool("link", "/example-repo/reports/")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stderr, "")
        self.assertEqual(self.aws_calls(), ["configure export-credentials --profile example-uploader --format process"])
        self.assertIn("url: https://example-bucket.s3.us-east-1.amazonaws.com/\n", done.stdout)
        self.assertIn("public: https://example-bucket.s3.us-east-1.amazonaws.com/example-repo/reports/<file name>\n",
                      done.stdout)
        fields = self.fields(done.stdout)
        self.assertEqual(list(fields), ["key", "x-amz-algorithm", "x-amz-credential", "x-amz-date", "policy",
                                        "x-amz-signature"])
        self.assertEqual(fields["key"], "example-repo/reports/${filename}")
        day = fields["x-amz-date"][:8]
        self.assertEqual(fields["x-amz-credential"], f"AKIDEXAMPLE/{day}/us-east-1/s3/aws4_request")
        policy = json.loads(base64.b64decode(fields["policy"]))
        self.assertIn({"bucket": "example-bucket"}, policy["conditions"])
        self.assertIn(["starts-with", "$key", "example-repo/reports/"], policy["conditions"])
        self.assertIn(["content-length-range", 1, 500 * 1024 * 1024], policy["conditions"])
        expiry = datetime.datetime.strptime(policy["expiration"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc)
        self.assertLessEqual(abs(expiry - before - datetime.timedelta(hours=168)), datetime.timedelta(minutes=2))
        self.assertIn(f"expires: {policy['expiration']}\n", done.stdout)
        signature = hmac.new(shared_upload.signing_key(SECRET, day, "us-east-1"), fields["policy"].encode(),
                             hashlib.sha256).hexdigest()
        self.assertEqual(fields["x-amz-signature"], signature)
        self.assertNotIn(SECRET, done.stdout)
        # The curl line sends every field as written, the file last.
        [curl] = [line[len("curl: "):] for line in done.stdout.splitlines() if line.startswith("curl: ")]
        words = shlex.split(curl)
        self.assertEqual(words[-3:], ["-F", "file=@/path/to/file", "https://example-bucket.s3.us-east-1.amazonaws.com/"])
        self.assertEqual(dict(word.split("=", 1) for word in words[2:-3:2]), fields)
        self.assertEqual(set(words[1:-3:2]), {"--form-string"})

    def test_options_and_the_config_set_lifetime_and_size(self):
        done = self.run_tool("link", "example-repo", "--expires", "30m", "--max-mb", "20")
        self.assertEqual(done.returncode, 0, done.stderr)
        policy = json.loads(base64.b64decode(self.fields(done.stdout)["policy"]))
        self.assertIn(["content-length-range", 1, 20 * 1024 * 1024], policy["conditions"])
        self.assertEqual(self.fields(done.stdout)["key"], "example-repo/${filename}")
        stamp = datetime.datetime.strptime(self.fields(done.stdout)["x-amz-date"], "%Y%m%dT%H%M%SZ")
        expiry = datetime.datetime.strptime(policy["expiration"], "%Y-%m-%dT%H:%M:%SZ")
        self.assertEqual(expiry - stamp, datetime.timedelta(minutes=30))
        self.assertEqual(self.run_tool("link", "example-repo", SHARED_UPLOAD_EXPIRES="7d").returncode, 0)

    def test_a_bad_option_fails_before_aws_is_asked(self):
        for args, said in ((("link", "example-repo", "--expires", "169h"), "past 168h"),
                           (("link", "example-repo", "--expires", "8d"), "past 168h"),
                           (("link", "example-repo", "--expires", "10x"), "a count and a unit"),
                           (("link", "example-repo", "--max-mb", "0"), "--max-mb takes"),
                           (("link", "example-repo", "--max-mb", "5121"), "--max-mb takes"),
                           (("link", "example-repo", "--size", "1"), "not '--size'"),
                           (("link", "/"), "one key prefix"),
                           (("link",), "one key prefix"),
                           (("link", "a", "b"), "one key prefix")):
            with self.subTest(args=args):
                done = self.run_tool(*args)
                self.assertEqual((done.returncode, done.stdout), (1, ""), done.stderr)
                self.assertIn(said, done.stderr)
        self.assertEqual(self.aws_calls(), [])

    def test_keys_that_do_not_read_fail_naming_the_cause_and_print_no_link(self):
        done = self.run_tool("link", "example-repo", AWS_FAILS="1")
        self.assertEqual((done.returncode, done.stdout), (1, ""))
        self.assertIn("aws configure export-credentials --profile example-uploader exited 255: Unable to locate "
                      "credentials", done.stderr)
        done = self.run_tool("link", "example-repo", AWS_SECRET="")
        self.assertEqual((done.returncode, done.stdout), (1, ""))
        self.assertIn("printed no keys", done.stderr)

    def test_temporary_keys_sign_with_their_token_and_say_when_the_link_dies(self):
        done = self.run_tool("link", "example-repo", AWS_TOKEN="FwoGZXIvYXdzEXAMPLE/+token==")
        self.assertEqual(done.returncode, 0, done.stderr)
        fields = self.fields(done.stdout)
        self.assertEqual(fields["x-amz-security-token"], "FwoGZXIvYXdzEXAMPLE/+token==")
        self.assertIn({"x-amz-security-token": "FwoGZXIvYXdzEXAMPLE/+token=="},
                      json.loads(base64.b64decode(fields["policy"]))["conditions"])
        self.assertIn("example-uploader gives temporary keys; the link stops working when they expire "
                      "(2026-10-20T00:00:00+00:00)", done.stderr)

    def test_a_missing_value_names_it_and_both_remedies_and_an_export_wins(self):
        self.env_file("SHARED_UPLOAD_REGION=us-east-1\n")
        done = self.run_tool("url", "a.txt")
        self.assertEqual((done.returncode, done.stdout), (1, ""))
        self.assertIn("SHARED_UPLOAD_BUCKET is not set", done.stderr)
        self.assertIn(f"copy local-shared-upload/.env.example to {self.config}/shared-upload/.env", done.stderr)
        # url needs no profile; link does.
        done = self.run_tool("url", "a.txt", SHARED_UPLOAD_BUCKET="exported-bucket")
        self.assertEqual(done.stdout, "https://exported-bucket.s3.us-east-1.amazonaws.com/a.txt\n")
        done = self.run_tool("link", "example-repo", SHARED_UPLOAD_BUCKET="exported-bucket")
        self.assertEqual(done.returncode, 1)
        self.assertIn("SHARED_UPLOAD_PROFILE is not set", done.stderr)
        self.assertEqual(self.aws_calls(), [])

    def test_url_is_the_public_get_url_of_the_key(self):
        done = self.run_tool("url", "/example-repo/a report #1.pdf")
        self.assertEqual((done.returncode, done.stderr), (0, ""))
        self.assertEqual(done.stdout, "https://example-bucket.s3.us-east-1.amazonaws.com/example-repo/a%20report%20%231.pdf\n")
        for args in (("url",), ("url", "/"), ("url", "a", "b")):
            with self.subTest(args=args):
                self.assertEqual(self.run_tool(*args).returncode, 1)
        self.assertEqual(self.aws_calls(), [])

    def test_help_exits_0_and_a_bare_call_exits_1_with_usage_on_stderr(self):
        for verb in ("help", "-h", "--help"):
            done = self.run_tool(verb)
            self.assertEqual(done.returncode, 0)
            self.assertIn("usage: shared-upload.sh link", done.stdout)
        done = self.run_tool()
        self.assertEqual((done.returncode, done.stdout), (1, ""))
        self.assertIn("usage: shared-upload.sh link", done.stderr)


if __name__ == "__main__":
    unittest.main()

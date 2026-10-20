"""The shared bucket's links (sd:5346), signed with the standard library; `shared-upload.sh` runs this.

    shared_upload.py link <key-prefix> [--expires 168h] [--max-mb N]
    shared_upload.py url <key>

The environment names the bucket, its region, the signing profile and the defaults (see `shared-upload.sh help`).
`aws configure export-credentials` reads the profile's keys, so any profile kind the CLI knows can sign; nothing here
calls S3, and the secret is never printed.

A link is an S3 presigned POST (Signature Version 4): a policy naming the bucket, keys under the prefix, a size range
and an expiry, signed with the profile's secret. The form's key is `<prefix>${filename}`, which S3 replaces with the
uploaded file's name, so one link takes many files under its prefix.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import json
import os
import re
import shlex
import subprocess
import sys
import urllib.parse

#: Signature Version 4 signs for 7 days at most.
MAX_SECONDS = 7 * 24 * 3600
UNITS = {"m": 60, "h": 3600, "d": 86400}
#: S3 takes one POST of 5 GiB at most.
MAX_MB = 5 * 1024
AWS_SECONDS = 30


def seconds(text: str) -> int:
    found = re.fullmatch(r"([1-9][0-9]*)([mhd])", text)
    if not found:
        raise ValueError(f"--expires takes a count and a unit, m, h or d, such as 168h, not {text!r}")
    total = int(found[1]) * UNITS[found[2]]
    if total > MAX_SECONDS:
        raise ValueError(f"--expires {text} is past 168h, the Signature Version 4 limit")
    return total


def megabytes(text: str) -> int:
    if not re.fullmatch(r"[1-9][0-9]*", text) or int(text) > MAX_MB:
        raise ValueError(f"--max-mb takes a whole number of MiB from 1 to {MAX_MB}, not {text!r}")
    return int(text)


def signing_key(secret: str, day: str, region: str, service: str = "s3") -> bytes:
    key = ("AWS4" + secret).encode()
    for part in (day, region, service, "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    return key


def endpoint(bucket: str, region: str) -> str:
    return f"https://{bucket}.s3.{region}.amazonaws.com/"


def public_url(bucket: str, region: str, key: str) -> str:
    return endpoint(bucket, region) + urllib.parse.quote(key, safe="/~")


def credentials(profile: str) -> dict:
    """The profile's keys from `aws configure export-credentials`; `RuntimeError` naming why there are none."""
    argv = ["aws", "configure", "export-credentials", "--profile", profile, "--format", "process"]
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=AWS_SECONDS, stdin=subprocess.DEVNULL,
                              check=False)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{' '.join(argv[:3])} gave no answer in {AWS_SECONDS} s") from None
    except OSError as error:
        raise RuntimeError(f"aws did not run: {error}") from None
    if done.returncode != 0:
        said = (done.stderr.strip().splitlines() or ["no message"])[-1]
        raise RuntimeError(f"{' '.join(argv[:5])} exited {done.returncode}: {said}")
    try:
        found = json.loads(done.stdout)
        if not (found["AccessKeyId"] and found["SecretAccessKey"]):
            raise ValueError
    except (ValueError, KeyError, TypeError):
        raise RuntimeError(f"{' '.join(argv[:5])} printed no keys") from None
    return found


def presigned_post(bucket: str, region: str, prefix: str, keys: dict, lifetime: int, max_bytes: int,
                   now: datetime.datetime) -> tuple[str, dict, str]:
    """`(url, fields, expiry)`: the form's fields in the order a POST sends them, the file field last."""
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    fields = {"key": prefix + "${filename}", "x-amz-algorithm": "AWS4-HMAC-SHA256",
              "x-amz-credential": f"{keys['AccessKeyId']}/{stamp[:8]}/{region}/s3/aws4_request", "x-amz-date": stamp}
    if keys.get("SessionToken"):
        fields["x-amz-security-token"] = keys["SessionToken"]
    expiry = (now + datetime.timedelta(seconds=lifetime)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conditions = [{"bucket": bucket}, ["starts-with", "$key", prefix], ["content-length-range", 1, max_bytes]]
    conditions += [{name: value} for name, value in fields.items() if name != "key"]
    policy = base64.b64encode(json.dumps({"expiration": expiry, "conditions": conditions}).encode()).decode()
    fields["policy"] = policy
    fields["x-amz-signature"] = hmac.new(signing_key(keys["SecretAccessKey"], stamp[:8], region), policy.encode(),
                                         hashlib.sha256).hexdigest()
    return endpoint(bucket, region), fields, expiry


def link(args: list[str], out, err) -> int:
    options = {"--expires": os.environ.get("SHARED_UPLOAD_EXPIRES") or "168h",
               "--max-mb": os.environ.get("SHARED_UPLOAD_MAX_MB") or "500"}
    rest = []
    while args:
        word = args.pop(0)
        if word in options and args:
            options[word] = args.pop(0)
        elif word in options or word.startswith("-"):
            raise ValueError(f"link takes --expires <count><m|h|d> and --max-mb <N>, not {word!r}")
        else:
            rest.append(word)
    if len(rest) != 1 or not rest[0].strip("/"):
        raise ValueError("link takes one key prefix, such as example-repo/reports")
    prefix = rest[0].strip("/") + "/"
    lifetime, max_bytes = seconds(options["--expires"]), megabytes(options["--max-mb"]) * 1024 * 1024
    bucket, region, profile = (os.environ[name] for name in
                               ("SHARED_UPLOAD_BUCKET", "SHARED_UPLOAD_REGION", "SHARED_UPLOAD_PROFILE"))
    keys = credentials(profile)
    url, fields, expiry = presigned_post(bucket, region, prefix, keys, lifetime, max_bytes,
                                         datetime.datetime.now(datetime.timezone.utc))
    # --form-string sends a value as written: curl reads no @, < or ;type= in it.
    curl = " ".join(["curl"] + [f"--form-string {shlex.quote(f'{name}={value}')}" for name, value in fields.items()]
                    + ["-F file=@/path/to/file", url])
    print(f"url: {url}", file=out)
    for name, value in fields.items():
        print(f"field {name}: {value}", file=out)
    print(f"expires: {expiry}", file=out)
    print(f"curl: {curl}", file=out)
    print(f"public: {public_url(bucket, region, prefix)}<file name>", file=out)
    if keys.get("SessionToken"):
        print(f"shared-upload: {profile} gives temporary keys; the link stops working when they expire "
              f"({keys.get('Expiration') or 'at a time the CLI did not give'}), even before {expiry}", file=err)
    return 0


def main(argv: list[str], out=None, err=None) -> int:
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    try:
        if argv[:1] == ["link"]:
            return link(argv[1:], out, err)
        if argv[:1] == ["url"] and len(argv) == 2 and argv[1].strip("/"):
            print(public_url(os.environ["SHARED_UPLOAD_BUCKET"], os.environ["SHARED_UPLOAD_REGION"],
                             argv[1].lstrip("/")), file=out)
            return 0
        raise ValueError("usage: shared-upload.sh link <key-prefix> [--expires 168h] [--max-mb N] | url <key>")
    except (ValueError, RuntimeError) as error:
        print(f"shared-upload: {error}", file=err)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

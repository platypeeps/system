"""Review the SSM calls that reach a deployment, from CloudTrail's event history.

aws-setup.sh `events` runs this with the admin profile. Each call that reaches
an instance prints one row, oldest first, in local time:

  UNEXPECTED  made by anyone but the deployer user of this account
  UNLOGGED    a SendCommand whose output does not go to the audit bucket
  TUNNEL      a port-forwarding session: CloudTrail has who and when, no log
              has what ran inside it

Exit 0 when nothing is UNEXPECTED or UNLOGGED, 1 otherwise, and 1 when the
history cannot be read or is not the shape CloudTrail documents.
"""
import argparse
import datetime
import json
import subprocess
import sys

NAMES = ("SendCommand", "StartSession", "ResumeSession", "TerminateSession")
REACHING = {"SendCommand", "StartSession", "ResumeSession"}
TUNNEL = "AWS-StartPortForwardingSession"


class Malformed(Exception):
    pass


def clean(value):
    """One printable line: a field may carry newlines or escapes."""
    text = value if isinstance(value, str) else json.dumps(value)
    return "".join(c if c.isprintable() else "?" for c in text)[:120] or "-"


def lookup(args, name, start):
    done = subprocess.run(
        ["aws", "--profile", args.profile, "--region", args.region,
         "cloudtrail", "lookup-events",
         "--lookup-attributes", "AttributeKey=EventName,AttributeValue=" + name,
         "--start-time", start, "--output", "json"],
        capture_output=True, text=True)
    if done.returncode != 0:
        sys.stderr.write(done.stderr)
        raise SystemExit("review-events: could not read %s events" % name)
    try:
        page = json.loads(done.stdout)
    except ValueError:
        raise Malformed("lookup-events output is not JSON")
    if not isinstance(page, dict) or not isinstance(page.get("Events"), list):
        raise Malformed("lookup-events output has no Events list")
    return page["Events"]


def record_of(item):
    if not isinstance(item, dict) or not isinstance(item.get("CloudTrailEvent"), str):
        raise Malformed("an event carries no CloudTrailEvent text")
    try:
        record = json.loads(item["CloudTrailEvent"])
    except ValueError:
        raise Malformed("a CloudTrailEvent is not JSON")
    if not isinstance(record, dict):
        raise Malformed("a CloudTrailEvent is not an object")
    who = record.get("userIdentity")
    when = record.get("eventTime")
    if not isinstance(who, dict) or not isinstance(when, str):
        raise Malformed("a CloudTrailEvent has no userIdentity or eventTime")
    try:
        moment = datetime.datetime.strptime(when, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        raise Malformed("a CloudTrailEvent eventTime is not UTC ISO 8601")
    params = record.get("requestParameters")
    return record, who, moment.replace(tzinfo=datetime.timezone.utc), \
        params if isinstance(params, dict) else {}


def review(args, items):
    rows = []
    for name, item in items:
        record, who, moment, params = record_of(item)
        # Each lookup asks for one name; anything else in its answer is not
        # what was asked for, and would count an event twice.
        if record.get("eventSource") != "ssm.amazonaws.com" or record.get("eventName") != name:
            continue
        flags = []
        own = (who.get("type") == "IAMUser" and who.get("accountId") == args.account
               and who.get("userName") == args.user)
        if name in REACHING and not own:
            flags.append("UNEXPECTED")
        output = params.get("outputS3BucketName")
        if name == "SendCommand" and not (args.bucket and output == args.bucket):
            flags.append("UNLOGGED")
        if params.get("documentName") == TUNNEL:
            flags.append("TUNNEL")
        target = params.get("target") or params.get("instanceIds") or params.get("targets") \
            or params.get("sessionId")
        if record.get("errorCode"):
            flags.append("error=" + clean(record["errorCode"]))
        rows.append((moment, name, who.get("arn") or who.get("userName") or who.get("type"),
                     target, flags))
    rows.sort(key=lambda row: row[0])
    flagged = 0
    for moment, name, actor, target, flags in rows:
        if {"UNEXPECTED", "UNLOGGED"} & set(flags):
            flagged += 1
        print("%s  %-16s %-34s %s %s" % (
            moment.astimezone().strftime("%Y-%m-%d %H:%M %Z"), clean(name),
            clean(actor), clean(target), " ".join(flags)))
    print("%d events, %d flagged" % (len(rows), flagged))
    return 1 if flagged else 0


def main():
    parser = argparse.ArgumentParser()
    for option in ("profile", "region", "account", "user", "bucket"):
        parser.add_argument("--" + option, required=True)
    parser.add_argument("--hours", type=int, required=True)
    args = parser.parse_args()
    start = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=args.hours)
    start = start.strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        items = [(name, item) for name in NAMES for item in lookup(args, name, start)]
        return review(args, items)
    except Malformed as error:
        sys.stderr.write("review-events: malformed CloudTrail history: %s\n" % error)
        return 1


if __name__ == "__main__":
    sys.exit(main())

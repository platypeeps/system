"""Review the SSM calls that reach a deployment, from CloudTrail's event history.

aws-setup.sh `events` runs this with the admin profile. Event history is kept
per region, so it reads every region the account has enabled. Each call that
reaches an instance prints one row, oldest first, in local time:

  UNEXPECTED  made by anyone but the deployer user of this account
  UNLOGGED    a SendCommand whose output does not go to ssm-output/ in the
              audit bucket, the one prefix the instance role may write, or
              whose output is not there: no object under
              <prefix>/<command id>/<instance id>/ for each instance named;
              a command sent to tag targets names none, so it is UNLOGGED;
              every association, automation, change request,
              maintenance window and document version call read (ROUTES
              below) is UNLOGGED: the
              deployer may not make one, and no saved output proves what
              it ran
  TUNNEL      a port-forwarding session: CloudTrail has who and when, no log
              has what ran inside it

A row prints identifiers only (an instance, a target tag, an association, a
window, a document name), never a document's parameters.

Exit 0 when nothing is UNEXPECTED or UNLOGGED, 1 otherwise, and 1 when the
history cannot be read or is not the shape CloudTrail documents.
"""
import argparse
import datetime
import json
import re
import subprocess
import sys

# The other calls this review reads: each makes a document run on an
# instance, now or later, or changes what an existing association, automation
# or window task runs or reaches. Per kind: an association (create, one or a
# batch; update; run once), an automation (start, resume signal, change
# request), a maintenance window (its schedule or enabled switch, a target,
# a task) and the document itself (a new version, the default version). The
# deployer's policy grants none, and their output never reaches the audit
# bucket's proof, so each call is UNLOGGED whoever made it, a refused one too.
# This is a list of names, not every SSM write: README.md says what it leaves.
ROUTES = ("CreateAssociation", "CreateAssociationBatch", "UpdateAssociation",
          "StartAssociationsOnce", "StartAutomationExecution", "SendAutomationSignal",
          "StartChangeRequestExecution", "UpdateMaintenanceWindow",
          "RegisterTargetWithMaintenanceWindow", "UpdateMaintenanceWindowTarget",
          "RegisterTaskWithMaintenanceWindow", "UpdateMaintenanceWindowTask",
          "UpdateDocument", "UpdateDocumentDefaultVersion")
NAMES = ("SendCommand", "StartSession", "ResumeSession", "TerminateSession") + ROUTES
REACHING = {"SendCommand", "StartSession", "ResumeSession"} | set(ROUTES)
TUNNEL = "AWS-StartPortForwardingSession"
# The instance role may write only <bucket>/ssm-output/*; output under any
# other key is never written, so it counts as unlogged.
OUTPUT_PREFIX = "ssm-output"
REGION = re.compile(r"[a-z]{2}(-[a-z]+)+-[0-9]{1,2}")
COMMAND_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
INSTANCE_ID = re.compile(r"(i|mi)-[0-9a-f]{1,17}")


class Malformed(Exception):
    pass


def clean(value):
    """One printable line: a field may carry newlines or escapes."""
    text = value if isinstance(value, str) else json.dumps(value)
    return "".join(c if c.isprintable() else "?" for c in text)[:120] or "-"


def text(value):
    return value if isinstance(value, str) else None


def strings(value):
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def picked(value, shapes):
    """The named fields of each object in a list, each read by its shape."""
    found = []
    for one in value if isinstance(value, list) else []:
        if isinstance(one, dict):
            kept = {key: shape(one.get(key)) for key, shape in shapes}
            kept = {key: got for key, got in kept.items() if got}
            if kept:
                found.append(kept)
    return found


def tags(value):
    return picked(value, (("key", text), ("values", strings),
                          ("Key", text), ("Values", strings)))


# What a row may print of a request: these fields, each only in the shape
# named, first one found. A request also carries document parameters (the
# commands, a token in them); a field not listed, or of another shape, is
# never printed.
SHOWN = (
    ("target", text), ("instanceId", text), ("instanceIds", strings), ("targets", tags),
    ("entries", lambda v: picked(v, (("instanceId", text), ("targets", tags),
                                     ("name", text)))),
    ("associationId", text), ("associationIds", strings),
    ("automationExecutionId", text), ("windowId", text),
    ("runbooks", lambda v: picked(v, (("documentName", text),))),
    ("sessionId", text), ("name", text), ("documentName", text),
)


def shown(params):
    return next((got for got in (shape(params.get(key)) for key, shape in SHOWN) if got), "")


def aws(args, region, what, *argv):
    """One admin call's JSON answer; a failed call ends the review."""
    done = subprocess.run(
        ["aws", "--profile", args.profile, "--region", region, *argv, "--output", "json"],
        capture_output=True, text=True)
    if done.returncode != 0:
        sys.stderr.write(done.stderr)
        raise SystemExit("review-events: could not read %s" % what)
    try:
        return json.loads(done.stdout)
    except ValueError:
        raise Malformed("%s output is not JSON" % argv[1])


def regions(args):
    names = aws(args, args.region, "the enabled regions", "ec2", "describe-regions",
                "--query", "Regions[].RegionName")
    if (not isinstance(names, list) or not all(
            isinstance(n, str) and REGION.fullmatch(n) for n in names)
            or args.region not in names):
        raise Malformed("describe-regions gave no region list with %s in it" % args.region)
    return sorted(set(names))


def lookup(args, region, name, start):
    page = aws(args, region, "%s events in %s" % (name, region), "cloudtrail", "lookup-events",
               "--lookup-attributes", "AttributeKey=EventName,AttributeValue=" + name,
               "--start-time", start)
    if not isinstance(page, dict) or not isinstance(page.get("Events"), list):
        raise Malformed("lookup-events output has no Events list")
    # The CLI follows every page itself; a token left over means it did not,
    # and the events after it were never read.
    if "NextToken" in page:
        raise Malformed("lookup-events output is incomplete: it stops at a NextToken")
    return page["Events"]


def saved(args, record, params, prefix):
    """True when the bucket holds output for every instance the command named.

    The requested destination proves nothing: the upload can fail or never
    run. The deployer may not write the bucket, so an object there was put
    by an instance role. Only a list of instance ids says whose output to
    look for: a command sent to tag targets, to nobody named, or to both ids
    and targets counts as unsaved, since one object under the command id says
    nothing of the other targets. A command id or instance id that is not
    the shape SSM gives is never put into a listing and counts as unsaved.
    """
    response = record.get("responseElements")
    command = response.get("command") if isinstance(response, dict) else None
    command_id = command.get("commandId") if isinstance(command, dict) else None
    if not isinstance(command_id, str) or not COMMAND_ID.fullmatch(command_id):
        return False
    base = "%s/%s/" % (prefix.rstrip("/"), command_id)
    instances = params.get("instanceIds")
    if (params.get("targets") or not isinstance(instances, list) or not instances
            or not all(isinstance(i, str) and INSTANCE_ID.fullmatch(i) for i in instances)):
        return False
    for key in sorted({base + i + "/" for i in instances}):
        # --expected-bucket-owner: a bucket of that name in another account
        # is no evidence; its refusal ends the review.
        page = aws(args, args.region, "the output under %s" % key, "s3api", "list-objects-v2",
                   "--bucket", args.bucket, "--expected-bucket-owner", args.account,
                   "--prefix", key, "--max-items", "100")
        if not isinstance(page, dict):
            raise Malformed("list-objects-v2 output is not an object")
        # A key ending in "/" is a folder marker, not output; it sorts
        # first, so the listing reads past it. 100 keys fail closed. A key
        # the answer holds outside the prefix asked for is another
        # command's or instance's, and no evidence for this one.
        contents = page.get("Contents")
        if not (isinstance(contents, list) and any(
                isinstance(o, dict) and isinstance(o.get("Key"), str)
                and o["Key"].startswith(key) and not o["Key"].endswith("/")
                for o in contents)):
            return False
    return True


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
    for region, name, item in items:
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
        prefix = params.get("outputS3KeyPrefix")
        under = isinstance(prefix, str) and (
            prefix == OUTPUT_PREFIX or prefix.startswith(OUTPUT_PREFIX + "/"))
        if name == "SendCommand" and not (args.bucket and output == args.bucket and under
                                          and saved(args, record, params, prefix)):
            flags.append("UNLOGGED")
        if name in ROUTES:
            flags.append("UNLOGGED")
        if params.get("documentName") == TUNNEL:
            flags.append("TUNNEL")
        target = shown(params)
        if record.get("errorCode"):
            flags.append("error=" + clean(record["errorCode"]))
        rows.append((moment, region, name,
                     who.get("arn") or who.get("userName") or who.get("type"), target, flags))
    rows.sort(key=lambda row: row[0])
    flagged = 0
    for moment, region, name, actor, target, flags in rows:
        if {"UNEXPECTED", "UNLOGGED"} & set(flags):
            flagged += 1
        print("%s  %-14s %-16s %-34s %s %s" % (
            moment.astimezone().strftime("%Y-%m-%d %H:%M %Z"), region, clean(name),
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
        items = [(region, name, item) for region in regions(args) for name in NAMES
                 for item in lookup(args, region, name, start)]
        return review(args, items)
    except Malformed as error:
        sys.stderr.write("review-events: malformed CloudTrail history: %s\n" % error)
        return 1


if __name__ == "__main__":
    sys.exit(main())

"""Validate configured IAM identity documents before aws-setup makes AWS calls."""
import fnmatch
import json
import pathlib
import re
import sys


def strings(value):
    return (isinstance(value, str) and bool(value)) or (
        isinstance(value, list) and bool(value)
        and all(isinstance(item, str) and item for item in value)
    )


def unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


SHELL_DOCUMENT = "SSM-SessionManagerRunShell"


def covers(item, key, value):
    """Whether the statement's Action or Resource, or its Not form, covers value."""
    patterns = item.get(key, item.get("Not" + key))
    patterns = [patterns] if isinstance(patterns, str) else patterns
    if key == "Action":
        # IAM action names are case-insensitive.
        value, patterns = value.lower(), [pattern.lower() for pattern in patterns]
    else:
        # Match any region and account: arn:partition:service:region:account:resource.
        patterns = [":".join(fields[:3] + ["*", "*"] + fields[5:]) if len(fields) == 6 else pattern
                    for pattern in patterns for fields in [pattern.split(":", 5)]]
    return any(fnmatch.fnmatchcase(value, pattern) for pattern in patterns) != ("Not" + key in item)


def grants_shell(item):
    """An Allow that opens the interactive Session Manager shell (sd:2851).

    StartSession without a document runs SSM-SessionManagerRunShell, and IAM
    checks that document only when ssm:SessionDocumentAccessCheck is true.
    """
    if item["Effect"] != "Allow" or not covers(item, "Action", "ssm:StartSession"):
        return False
    if covers(item, "Resource", "arn:aws:ssm:region:account:document/" + SHELL_DOCUMENT):
        return True
    for operator, entries in item.get("Condition", {}).items():
        check = entries.get("ssm:SessionDocumentAccessCheck") if isinstance(entries, dict) else None
        if operator in ("Bool", "BoolIfExists") and str(check).lower() in ("true", "['true']"):
            return False
    return covers(item, "Resource", "arn:aws:ec2:region:account:instance/i-0")


def validate(path):
    raw = path.read_text()
    if len(re.sub(r"\s", "", raw)) > 6144:
        raise ValueError(f"{path}: exceeds the 6144-character managed policy limit")
    document = json.loads(raw, object_pairs_hook=unique_keys)
    if not isinstance(document, dict) or set(document) - {"Version", "Id", "Statement"}:
        raise ValueError(f"{path}: invalid policy document fields")
    if document.get("Version") != "2012-10-17":
        raise ValueError(f"{path}: Version must be 2012-10-17")
    statements = document.get("Statement")
    if isinstance(statements, dict):
        statements = [statements]
    if not isinstance(statements, list) or not statements:
        raise ValueError(f"{path}: Statement must contain identity policy statements")
    for item in statements:
        if not isinstance(item, dict) or set(item) - {
            "Sid", "Effect", "Action", "NotAction", "Resource", "NotResource", "Condition"
        }:
            raise ValueError(f"{path}: invalid identity statement fields (Principal is forbidden)")
        if item.get("Effect") not in ("Allow", "Deny"):
            raise ValueError(f"{path}: Effect must be Allow or Deny")
        for first, second in (("Action", "NotAction"), ("Resource", "NotResource")):
            if (first in item) == (second in item) or not strings(item.get(first, item.get(second))):
                raise ValueError(f"{path}: exactly one valid {first}/{second} is required")
        if "Condition" in item and (not isinstance(item["Condition"], dict) or not item["Condition"]):
            raise ValueError(f"{path}: Condition must be a nonempty object")
        if grants_shell(item):
            raise ValueError(f"{path}: grants the interactive SSM shell ({SHELL_DOCUMENT}); allow named "
                             "documents only, with ssm:SessionDocumentAccessCheck true on instance sessions")
    return document


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--document":
        validate(pathlib.Path(sys.argv[2]))
        return
    folder, base, configured, account, level, pass_roles = sys.argv[1:]
    roles = pass_roles.split()
    if roles and level != "sandbox":
        raise ValueError("PASS_ROLE_ARNS requires LEVEL=sandbox")
    if len(set(roles)) != len(roles):
        raise ValueError("duplicate role in PASS_ROLE_ARNS")
    for role in roles:
        if not re.fullmatch(r"arn:aws:iam::" + re.escape(account) + r":role/[A-Za-z0-9_+=,.@/-]+", role):
            raise ValueError("PASS_ROLE_ARNS requires exact same-account role ARNs without wildcards")

    names = [base, *configured.split()]
    if len(set(names)) != len(names):
        raise ValueError("duplicate or base policy name in EXTRA_POLICIES")
    for name in names:
        if not re.fullmatch(r"[\w+=,.@-]{1,128}", name, flags=re.ASCII):
            raise ValueError(f"invalid IAM policy name: {name!r}")
    for name in names[1:]:
        validate(pathlib.Path(folder) / f"{name}.json")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError) as error:
        sys.exit(str(error))

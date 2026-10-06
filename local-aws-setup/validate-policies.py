"""Validate configured IAM identity documents before aws-setup makes AWS calls."""
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
    return document


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--document":
        validate(pathlib.Path(sys.argv[2]))
        return
    folder, base, configured = sys.argv[1:]
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

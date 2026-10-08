"""The Claude Code settings baseline every machine carries (the tooling stage).

`claude_settings.py missing SETTINGS` prints one line per baseline entry the
file lacks; `claude_settings.py merge SETTINGS` adds those entries in place.
Only missing entries are added: an entry the operator set, to any value, is
left alone, and nothing is removed. The stage backs the file up first.
"""

import json
import os
import shutil
import sys

#: Claude Code's file tools may not read these (was the pack's fleet stamp, sd:3010).
SECRET_READ_DENY = [
    "Read(//**/.env)",
    "Read(//**/.env.local)",
    "Read(//**/.env.*.local)",
    "Read(//**/.env.development)",
    "Read(//**/.env.staging)",
    "Read(//**/.env.production)",
    "Read(//**/secrets/**)",
    "Read(//**/*.key)",
    "Read(//**/*-key.pem)",
    "Read(//**/*.p12)",
    "Read(//**/*.pfx)",
    "Read(//**/id_rsa)",
    "Read(//**/id_ecdsa)",
    "Read(//**/id_ed25519)",
    "Read(//**/.netrc)",
    "Read(//**/.pypirc)",
    "Read(~/.ssh/**)",
    "Read(~/.aws/credentials)",
    "Read(~/.config/gh/hosts.yml)",
]
SESSION_START = "sd today 2>/dev/null | head -40"


def session_start_commands(settings):
    groups = settings.get("hooks", {}).get("SessionStart", [])
    return [hook.get("command") for group in groups for hook in group.get("hooks", [])]


def missing(settings):
    deny = settings.get("permissions", {}).get("deny", [])
    gaps = [f"permissions.deny {rule}" for rule in SECRET_READ_DENY if rule not in deny]
    gaps += [f"attribution.{key}" for key in ("commit", "pr") if key not in settings.get("attribution", {})]
    if "includeCoAuthoredBy" not in settings:
        gaps.append("includeCoAuthoredBy")
    if SESSION_START not in session_start_commands(settings):
        gaps.append(f"hooks.SessionStart {SESSION_START}")
    return gaps


def merge(settings):
    deny = settings.setdefault("permissions", {}).setdefault("deny", [])
    deny += [rule for rule in SECRET_READ_DENY if rule not in deny]
    attribution = settings.setdefault("attribution", {})
    attribution.setdefault("commit", "")
    attribution.setdefault("pr", "")
    settings.setdefault("includeCoAuthoredBy", False)
    if SESSION_START not in session_start_commands(settings):
        hook = {"hooks": [{"type": "command", "command": SESSION_START}]}
        settings.setdefault("hooks", {}).setdefault("SessionStart", []).append(hook)
    return settings


def main(argv):
    verb, path = argv
    with open(path, encoding="utf-8") as fh:
        settings = json.load(fh)
    if verb == "missing":
        for gap in missing(settings):
            print(gap)
    elif verb == "merge" and missing(settings):
        temporary = path + ".tmp"
        with open(temporary, "w", encoding="utf-8") as fh:
            json.dump(merge(settings), fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        shutil.copymode(path, temporary)
        os.replace(temporary, path)
    elif verb != "merge":
        sys.exit(f"usage: claude_settings.py missing|merge SETTINGS (got {verb})")


if __name__ == "__main__":
    main(sys.argv[1:])

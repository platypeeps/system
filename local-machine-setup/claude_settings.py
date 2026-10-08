"""The Claude Code and opencode settings baseline every machine carries (the tooling stage).

`claude_settings.py missing SETTINGS` prints one line per baseline entry the
file lacks; `claude_settings.py merge SETTINGS` adds those entries in place.
`opencode-missing` and `opencode-merge` do the same for opencode.json.
Only missing entries are added: an entry the operator set, to any value, is
left alone, and nothing is removed. The stage backs the file up first.
"""

import json
import os
import re
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
#: Work is tracked in sd, not GitHub issues or claude-mem work state.
TOOL_DENY = [
    "Bash(gh issue create:*)",
    "Bash(gh issue transfer:*)",
    "Bash(gh api *repos/*/issues --method POST*)",
    "Bash(gh api *repos/*/issues -X POST*)",
    "mcp__github__issue_write",
    "mcp__github__sub_issue_write",
    "mcp__plugin_claude-mem_mcp-search__work_state_write",
    "mcp__plugin_claude-mem_mcp-search__work_state_read",
]
SESSION_START = "sd today 2>/dev/null | head -40"
#: Claude Code updates in the weekly upgrade only (sd:3033); this stops its background updater.
ENV = {"DISABLE_AUTOUPDATER": "1"}
#: The same paths for opencode's read permission, whose `*` also matches `/`.
OPENCODE_READ_DENY = ["*" + rule[5:-1].removeprefix("//**/").removeprefix("~/").replace("**", "*").lstrip("*")
                      for rule in SECRET_READ_DENY]
STRING = r'("(?:\\.|[^"\\])*")'
COMMENT = re.compile(STRING + r"|//[^\n]*|/\*.*?\*/", re.S)
TRAILING_COMMA = re.compile(STRING + r"|,(\s*[}\]])")
#: Each alternative matches one way only, so a run of `//` with no brace fails at once
#: (CodeQL alerts 38 and 39). Not possessive: machine-setup runs this under /usr/bin's 3.9.
#: A `{` inside a leading `//` comment is no longer taken as the opening brace.
OPENING_BRACE = re.compile(r"\A((?:\s|//[^\n]*(?=\n|\Z)|/\*(?:[^*]|\*(?!/))*\*/)*)\{", re.S)


def session_start_commands(settings):
    groups = settings.get("hooks", {}).get("SessionStart", [])
    return [hook.get("command") for group in groups for hook in group.get("hooks", [])]


def missing(settings):
    deny = settings.get("permissions", {}).get("deny", [])
    gaps = [f"permissions.deny {rule}" for rule in SECRET_READ_DENY + TOOL_DENY if rule not in deny]
    gaps += [f"attribution.{key}" for key in ("commit", "pr") if key not in settings.get("attribution", {})]
    if "includeCoAuthoredBy" not in settings:
        gaps.append("includeCoAuthoredBy")
    gaps += [f"env.{key}" for key in ENV if key not in settings.get("env", {})]
    if SESSION_START not in session_start_commands(settings):
        gaps.append(f"hooks.SessionStart {SESSION_START}")
    return gaps


def merge(settings):
    deny = settings.setdefault("permissions", {}).setdefault("deny", [])
    deny += [rule for rule in SECRET_READ_DENY + TOOL_DENY if rule not in deny]
    attribution = settings.setdefault("attribution", {})
    attribution.setdefault("commit", "")
    attribution.setdefault("pr", "")
    settings.setdefault("includeCoAuthoredBy", False)
    env = settings.setdefault("env", {})
    for key, value in ENV.items():
        env.setdefault(key, value)
    if SESSION_START not in session_start_commands(settings):
        hook = {"hooks": [{"type": "command", "command": SESSION_START}]}
        settings.setdefault("hooks", {}).setdefault("SessionStart", []).append(hook)
    return settings


def load_jsonc(text):
    """opencode.json allows comments and trailing commas; strings keep theirs."""
    text = COMMENT.sub(lambda m: m.group(1) or "", text)
    return json.loads(TRAILING_COMMA.sub(lambda m: m.group(1) or m.group(2), text))


def opencode_missing(config):
    if "permission" in config:
        read = config["permission"].get("read") if isinstance(config["permission"], dict) else None
        if isinstance(read, dict) and all(read.get(p) == "deny" for p in OPENCODE_READ_DENY):
            return []
        return ["has its own permission block; add the read denies by hand"]
    return [f"permission.read {pattern}" for pattern in OPENCODE_READ_DENY]


def opencode_merge(text):
    """Insert the block as text after the opening brace, so comments stay."""
    block = json.dumps({"read": dict.fromkeys(OPENCODE_READ_DENY, "deny")}, indent=2).replace("\n", "\n  ")
    merged = OPENING_BRACE.sub(lambda m: m.group(1) + '{\n  "permission": ' + block + ",", text, count=1)
    if opencode_missing(load_jsonc(merged)):
        sys.exit("claude_settings.py: the merged opencode.json does not carry the read denies")
    return merged


def main(argv):
    verb, path = argv
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    if verb.startswith("opencode-"):
        gaps = opencode_missing(load_jsonc(text))
        if verb == "opencode-missing":
            for gap in gaps:
                print(gap)
        elif gaps and gaps[0].startswith("permission.read"):
            write(path, opencode_merge(text))
        return
    settings = json.loads(text)
    if verb == "missing":
        for gap in missing(settings):
            print(gap)
    elif verb == "merge" and missing(settings):
        write(path, json.dumps(merge(settings), indent=2, ensure_ascii=False) + "\n")
    elif verb != "merge":
        sys.exit(f"usage: claude_settings.py missing|merge|opencode-missing|opencode-merge FILE (got {verb})")


def write(path, text):
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as fh:
        fh.write(text)
    shutil.copymode(path, temporary)
    os.replace(temporary, path)


if __name__ == "__main__":
    main(sys.argv[1:])

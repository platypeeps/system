"""The Claude Code and opencode settings baseline every machine carries (the tooling stage).

`claude_settings.py missing SETTINGS` prints one line per baseline entry the
file lacks; `claude_settings.py merge SETTINGS` adds those entries in place.
`opencode-missing` and `opencode-merge` do the same for opencode.json.
Only missing entries are added: an entry the operator set, to any value, is
left alone, and nothing is removed. The stage backs the file up first.

`pref-missing FILE KEY VALUE` and `pref-merge FILE KEY VALUE` hold one
top-level KEY of an app's JSON settings at a JSON VALUE (the macos stage
turns self-update off with them, sd:3062). Unlike the baseline, a KEY set to
another value is changed. A file with comments keeps them: the KEY goes in
as text after the opening brace, and one already there is left for a hand.
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


def pref_gap(text, key, value):
    """'' when KEY holds VALUE; else what the file has instead."""
    config = load_jsonc(text) if text.strip() else {}
    if key not in config:
        return "unset"
    if config[key] != value:
        return f"is {json.dumps(config[key])}"
    return ""


def pref_merge(text, key, value):
    try:
        config = json.loads(text) if text.strip() else {}
    except ValueError:
        config = None
    if config is not None:
        config[key] = value
        return json.dumps(config, indent=2, ensure_ascii=False) + "\n"
    if key in load_jsonc(text):
        sys.exit(f"claude_settings.py: the file has comments and its own {key}; set it by hand")
    entry = "\n  " + json.dumps(key) + ": " + json.dumps(value) + ","
    return OPENING_BRACE.sub(lambda m: m.group(1) + "{" + entry, text, count=1)


def main(argv):
    if argv[:1] in (["pref-missing"], ["pref-merge"]):
        verb, path, key, value = argv
        value = json.loads(value)
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        except FileNotFoundError:
            text = ""
        if verb == "pref-missing":
            print(pref_gap(text, key, value))
            return
        if pref_gap(text, key, value):
            merged = pref_merge(text, key, value)
            if pref_gap(merged, key, value):
                sys.exit(f"claude_settings.py: the merged {path} does not hold {key}")
            if text:
                write(path, merged)
            else:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(merged)
        return
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
        sys.exit(f"usage: claude_settings.py missing|merge|opencode-missing|opencode-merge FILE"
                 f" | pref-missing|pref-merge FILE KEY VALUE (got {verb})")


def write(path, text):
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as fh:
        fh.write(text)
    shutil.copymode(path, temporary)
    os.replace(temporary, path)


if __name__ == "__main__":
    main(sys.argv[1:])

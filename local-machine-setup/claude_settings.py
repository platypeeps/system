"""The Claude Code and opencode settings baseline every machine carries (the tooling stage).

`claude_settings.py missing SETTINGS` prints one line per baseline entry the
file lacks; `claude_settings.py merge SETTINGS` adds those entries in place.
`opencode-missing` and `opencode-merge` do the same for opencode.json.
Only missing entries are added: an entry the operator set, to any value, is
left alone. The removals are what the baseline itself retired (RETIRED_DENY,
RETIRED_ENV at the value it wrote), reported as STALE. The stage backs the file up first.

`local-missing FILE` and `local-merge FILE` do the same for the shared keys of
a repo's CLAUDE.local.md block (the repos stage, sd:3030). Here the source
wins: a shared key with another value is rewritten. The repo's other keys, its
comments and every line outside the block stay as they are.

`codex-missing FILE ENDPOINT PROTOCOL` and `codex-merge` hold the [otel] table of
codex's config.toml at the hub's exporter (sd:6944); the source wins, the rest stays.

`pref-missing FILE KEY VALUE` and `pref-merge FILE KEY VALUE` hold one
top-level KEY of an app's JSON settings at a JSON VALUE (the macos stage
turns self-update off with them, sd:3062). Unlike the baseline, a KEY set to
another value is changed. A file with comments keeps them: the KEY goes in
as text after the opening brace, and one already there is left for a hand.
"""

import json
import os
import re
import shlex
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
#: mcp__github__issue_write is not here: a user deny beats any project allow, so the pack's
#: sd-issue-guard gates it per repo instead (sd:4810: a github_issues-on repo closes its issues).
TOOL_DENY = [
    "Bash(gh issue create:*)",
    "Bash(gh issue transfer:*)",
    "Bash(gh api *repos/*/issues --method POST*)",
    "Bash(gh api *repos/*/issues -X POST*)",
    "mcp__github__sub_issue_write",
    "mcp__plugin_claude-mem_mcp-search__work_state_write",
    "mcp__plugin_claude-mem_mcp-search__work_state_read",
]
#: Rules an older baseline added and this one takes back out.
RETIRED_DENY = ["mcp__github__issue_write"]
SESSION_START = "sd today 2>/dev/null | head -40"
#: Agents do not write in the system and pack checkouts (sd:6001). The test keeps a
#: moved or deleted checkout from blocking every call: python3 on a missing file exits 2,
#: which Claude Code reads as a deny.
GUARD_PATH = shlex.quote(os.path.join(os.path.dirname(os.path.abspath(__file__)), "checkout_guard.py"))
GUARD = f"test ! -f {GUARD_PATH} || python3 {GUARD_PATH}"
GUARD_MATCHER = "Bash|Write|Edit|MultiEdit|NotebookEdit"
#: Claude Code updates in the weekly upgrade only (sd:3033); this stops its background updater.
ENV = {"DISABLE_AUTOUPDATER": "1"}
#: Env values an older baseline wrote and this one takes back out, where unchanged. The
#: tool shell was Apple's bash for its file grant (sd:6427), but macOS charges the process
#: launchd or the terminal started, not the shell (sd:6466); it cost bash 5.
RETIRED_ENV = {"CLAUDE_CODE_SHELL": "/bin/bash"}
#: The hub's sessions export metrics and logs to genai-traces (sd:6944); the stage passes
#: `--otel ENDPOINT PROTOCOL HOST` on the hub only. The source wins here: a key with another
#: value is rewritten, so the endpoint follows genai-traces and prompts stay out of the logs.
#: cron-jobs.sh adds sd.job to OTEL_RESOURCE_ATTRIBUTES through --settings: this env beats
#: the process environment.
def otel_env(endpoint, protocol, host):
    return {
        "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
        "OTEL_METRICS_EXPORTER": "otlp",
        "OTEL_LOGS_EXPORTER": "otlp",
        "OTEL_EXPORTER_OTLP_ENDPOINT": endpoint,
        "OTEL_EXPORTER_OTLP_PROTOCOL": protocol,
        "OTEL_RESOURCE_ATTRIBUTES": f"service.namespace=sd-workflow,host.name={host}",
        "OTEL_LOG_USER_PROMPTS": "0",
        "OTEL_LOG_TOOL_DETAILS": "0",
    }


#: The same paths for opencode's read permission, whose `*` also matches `/`.
OPENCODE_READ_DENY = ["*" + rule[5:-1].removeprefix("//**/").removeprefix("~/").replace("**", "*").lstrip("*")
                      for rule in SECRET_READ_DENY]
#: The CLAUDE.local.md block keys every auto repo shares (sd:3030); other keys stay per repo.
#: A value holds no quote or backslash, so it is written double-quoted as is.
SHARED_LOCAL_KEYS = {
    "threat-model": "One operator on their own machines. Out of scope: a hostile local user, "
                    "a hostile program on PATH, and two concurrent runs of one manual command. "
                    "Flag defects that hurt that operator.",
}
LOCAL_START = "<!-- SD-AI-COMMAND-PACK:LOCAL:START -->"
LOCAL_END = "<!-- SD-AI-COMMAND-PACK:LOCAL:END -->"
LOCAL_KEY = re.compile(r"^([A-Za-z0-9_.-]+):[ \t]*(.*?)[ \t]*$")
STRING = r'("(?:\\.|[^"\\])*")'
COMMENT = re.compile(STRING + r"|//[^\n]*|/\*.*?\*/", re.S)
TRAILING_COMMA = re.compile(STRING + r"|,(\s*[}\]])")
#: Each alternative matches one way only, so a run of `//` with no brace fails at once
#: (CodeQL alerts 38 and 39). Not possessive: machine-setup runs this under /usr/bin's 3.9.
#: A `{` inside a leading `//` comment is no longer taken as the opening brace.
OPENING_BRACE = re.compile(r"\A((?:\s|//[^\n]*(?=\n|\Z)|/\*(?:[^*]|\*(?!/))*\*/)*)\{", re.S)


def hook_commands(settings, event):
    groups = settings.get("hooks", {}).get(event, [])
    return [hook.get("command") for group in groups for hook in group.get("hooks", [])]


def missing(settings, otel=None):
    deny = settings.get("permissions", {}).get("deny", [])
    gaps = [f"permissions.deny {rule}" for rule in SECRET_READ_DENY + TOOL_DENY if rule not in deny]
    gaps += [f"STALE permissions.deny {rule} (retired)" for rule in RETIRED_DENY if rule in deny]
    gaps += [f"attribution.{key}" for key in ("commit", "pr") if key not in settings.get("attribution", {})]
    if "includeCoAuthoredBy" not in settings:
        gaps.append("includeCoAuthoredBy")
    gaps += [f"env.{key}" for key in ENV if key not in settings.get("env", {})]
    gaps += [f"STALE env.{key} (retired)" for key, value in RETIRED_ENV.items()
             if settings.get("env", {}).get(key) == value]
    gaps += [("env." if key not in settings.get("env", {}) else "STALE env.") + f"{key}={value}"
             for key, value in (otel or {}).items() if settings.get("env", {}).get(key) != value]
    if SESSION_START not in hook_commands(settings, "SessionStart"):
        gaps.append(f"hooks.SessionStart {SESSION_START}")
    if GUARD not in hook_commands(settings, "PreToolUse"):
        gaps.append(f"hooks.PreToolUse {GUARD}")
    return gaps


def merge(settings, otel=None):
    deny = settings.setdefault("permissions", {}).setdefault("deny", [])
    deny[:] = [rule for rule in deny if rule not in RETIRED_DENY]
    deny += [rule for rule in SECRET_READ_DENY + TOOL_DENY if rule not in deny]
    attribution = settings.setdefault("attribution", {})
    attribution.setdefault("commit", "")
    attribution.setdefault("pr", "")
    settings.setdefault("includeCoAuthoredBy", False)
    env = settings.setdefault("env", {})
    for key, value in ENV.items():
        env.setdefault(key, value)
    for key, value in RETIRED_ENV.items():
        if env.get(key) == value:
            del env[key]
    env.update(otel or {})
    if SESSION_START not in hook_commands(settings, "SessionStart"):
        hook = {"hooks": [{"type": "command", "command": SESSION_START}]}
        settings.setdefault("hooks", {}).setdefault("SessionStart", []).append(hook)
    if GUARD not in hook_commands(settings, "PreToolUse"):
        hook = {"matcher": GUARD_MATCHER, "hooks": [{"type": "command", "command": GUARD}]}
        settings.setdefault("hooks", {}).setdefault("PreToolUse", []).append(hook)
    return settings


def local_lines(text):
    """The block's lines as (index of START, index of END, lines); no block is (None, None, lines)."""
    lines = text.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if line.strip() == LOCAL_START]
    ends = [i for i, line in enumerate(lines) if line.strip() == LOCAL_END]
    if not starts and not ends:
        return None, None, lines
    if len(starts) != 1 or len(ends) != 1 or ends[0] < starts[0]:
        raise ValueError("the SD-AI-COMMAND-PACK:LOCAL markers are not one ordered pair")
    return starts[0], ends[0], lines


def local_value(raw):
    if len(raw) > 1 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw[1:-1]
    return raw


def local_gaps(text):
    """`MISSING key` or `STALE key` for each shared key the block lacks or holds another value for."""
    start, end, lines = local_lines(text)
    have = {}
    for line in lines[start + 1:end] if start is not None else []:
        match = LOCAL_KEY.match(line.rstrip("\r\n"))
        if match:
            have[match.group(1)] = local_value(match.group(2))
    return [("MISSING " if key not in have else "STALE ") + key
            for key, value in sorted(SHARED_LOCAL_KEYS.items()) if have.get(key) != value]


def local_merge(text):
    start, end, lines = local_lines(text)
    shared = {key: f'{key}: "{value}"\n' for key, value in sorted(SHARED_LOCAL_KEYS.items())}
    if start is None:
        block = [LOCAL_START + "\n", *shared.values(), LOCAL_END + "\n"]
        separator = [] if not text or text.endswith("\n\n") else ["\n"] if text.endswith("\n") else ["\n\n"]
        return text + "".join(separator + block)
    body, written = [], set()
    for line in lines[start + 1:end]:
        match = LOCAL_KEY.match(line.rstrip("\r\n"))
        key = match.group(1) if match else None
        if key in written:
            continue
        if key in shared:
            written.add(key)
            line = shared.pop(key)
        body.append(line)
    return "".join(lines[:start + 1] + body + list(shared.values()) + lines[end:])


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


def codex_otel(endpoint, protocol):
    """The [otel] table codex exports its logs with (sd:6944): grpc, or OTLP/HTTP's binary."""
    if protocol == "grpc":
        exporter = f'{{ otlp-grpc = {{ endpoint = "{endpoint}" }} }}'
    else:
        exporter = f'{{ otlp-http = {{ endpoint = "{endpoint}/v1/logs", protocol = "binary" }} }}'
    return f'[otel]\nenvironment = "sd-workflow"\nexporter = {exporter}\nlog_user_prompt = false\n'


#: A table header line, and one in the otel hierarchy: [otel], [otel.exporter.otlp-grpc],
#: [[otel.x]], with the key bare, "quoted" or 'quoted' and spaces around the dots.
#: A comma or `=` makes it an array line, not a header.
OTEL_KEY = r"""(?:otel|"otel"|'otel')"""
TOML_HEADER = re.compile(r"\s*\[\[?[^\[\]=,]*\]\]?\s*(?:#.*)?$")
OTEL_HEADER = re.compile(r"\s*\[\[?\s*" + OTEL_KEY + r"\s*[.\]]")
#: otel set at the root by a dotted key or an inline table: no table to replace.
OTEL_ROOT_KEY = re.compile(r"\s*" + OTEL_KEY + r"\s*[.=]")


def codex_split(text):
    """(rest, otel, at): the lines outside and inside every otel table, and where the first began.

    The child tables go with [otel]: one left beside the inline exporter made the file
    invalid.
    """
    rest, otel, inside, at = [], [], False, None
    for line in text.splitlines(keepends=True):
        if TOML_HEADER.match(line):
            inside = bool(OTEL_HEADER.match(line))
            if inside and at is None:
                at = len(rest)
        (otel if inside else rest).append(line)
    return rest, otel, at


def codex_unsupported(text):
    """Why the line scan cannot rewrite this file safely, or ''.

    ponytail: a line scan, not a TOML parser (machine-setup runs /usr/bin's 3.9, which
    has no tomllib); what it cannot read it reports for a hand edit instead.
    """
    lines = text.splitlines()
    root = next((i for i, line in enumerate(lines) if TOML_HEADER.match(line)), len(lines))
    if any(OTEL_ROOT_KEY.match(line) for line in lines[:root]):
        return "sets otel by a root key"
    if '"""' in text or "'''" in text:
        return "has a multi-line string"
    return ""


def codex_gap(text, block):
    """'' when config.toml's otel tables are BLOCK alone; else MISSING, STALE or DIFFERS."""
    why = codex_unsupported(text)
    if why:
        return f"DIFFERS codex config.toml {why}; set [otel] by hand"
    _, otel, at = codex_split(text)
    if at is None:
        return "MISSING [otel]"
    return "" if "".join(otel).rstrip("\n") + "\n" == block else "STALE [otel]"


def codex_merge(text, block):
    """The source wins: every otel table goes, and BLOCK stands where the first was."""
    rest, _, at = codex_split(text)
    at = len(rest) if at is None else at
    before, after = "".join(rest[:at]), "".join(rest[at:])
    separator = "" if not before or before.endswith("\n\n") else "\n" if before.endswith("\n") else "\n\n"
    return before + separator + block + ("\n" + after if after else "")


def toml_holds(text, block):
    """TEXT parses and its otel is BLOCK's, where tomllib exists (3.11+); True without it."""
    try:
        import tomllib
    except ImportError:
        return True
    try:
        return tomllib.loads(text).get("otel") == tomllib.loads(block)["otel"]
    except tomllib.TOMLDecodeError:
        return False


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
    if argv[:1] in (["codex-missing"], ["codex-merge"]):
        verb, path, endpoint, protocol = argv
        text = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
        block = codex_otel(endpoint, protocol)
        gap = codex_gap(text, block)
        if verb == "codex-missing":
            print(gap)
        elif gap.startswith("DIFFERS"):
            sys.exit(f"claude_settings.py: {gap}")
        elif gap:
            merged = codex_merge(text, block)
            if codex_gap(merged, block) or not toml_holds(merged, block):
                sys.exit("claude_settings.py: the merged config.toml does not hold [otel]; set it by hand")
            write(path, merged)
        return
    verb, path, *extra = argv
    if verb.startswith("local-"):
        try:
            text = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
            gaps = local_gaps(text)
        except (OSError, ValueError) as error:
            sys.exit(f"claude_settings.py: {path}: {error}")
        if verb == "local-missing":
            for gap in gaps:
                print(gap)
        elif gaps:
            write(path, local_merge(text))
        return
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
    otel = otel_env(*extra[1:]) if extra[:1] == ["--otel"] and len(extra) == 4 else None
    if extra and otel is None:
        sys.exit("usage: claude_settings.py missing|merge FILE [--otel ENDPOINT PROTOCOL HOST]")
    if verb == "missing":
        for gap in missing(settings, otel):
            print(gap)
    elif verb == "merge" and missing(settings, otel):
        write(path, json.dumps(merge(settings, otel), indent=2, ensure_ascii=False) + "\n")
    elif verb != "merge":
        sys.exit("usage: claude_settings.py missing|merge|opencode-missing|opencode-merge|local-missing|local-merge FILE"
                 f" | pref-missing|pref-merge FILE KEY VALUE (got {verb})")


def write(path, text):
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as fh:
        fh.write(text)
    if os.path.exists(path):
        shutil.copymode(path, temporary)
    os.replace(temporary, path)


if __name__ == "__main__":
    main(sys.argv[1:])

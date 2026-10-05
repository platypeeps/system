#!/bin/sh
# ai-apps — one place to inventory, compare, and align the AI coding apps
# (claude-code, claude-desktop, codex, copilot, opencode, antigravity):
# which skills, MCP servers, agents, and plugins each app has, captured into
# committable per-machine-profile manifests (names only — never secrets).
# Usage: ai-apps.sh status|capture|compare|setup|adopt|update
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
. "$DIR/../lib/config.sh"
. "$DIR/../lib/bounded.sh"
# The .inv manifests are a machine's own inventory, so they live in the shared
# config directory outside the checkout. AI_APPS_PROFILES_DIR points
# elsewhere; a private fork that tracks them sets it to this folder's
# profiles/. profiles/example.inv stays in the repository as the format.
PROFILES="${AI_APPS_PROFILES_DIR:-$(st_config_dir ai-apps)/profiles}"

# The work checkout root. AI_APPS_WORK_ROOT and local-repo-sync's
# REPO_SYNC_WORK_ROOT mean the same thing; SYSTEM_TOOLS_WORK_ROOT sets both.
WORK_ROOT="${AI_APPS_WORK_ROOT:-${SYSTEM_TOOLS_WORK_ROOT:-}}"

APPS="claude-code claude-desktop codex copilot opencode antigravity"

# machine-setup records what this machine is; that answer beats guessing, and
# this is the last tool here still guessing. The old chain was: explicit
# override, else a work checkout root, else fall through to "personal" — so any
# third machine silently claimed to be the personal one. On terra that meant a
# capture rewrote profiles/personal.inv with terra's contents: 244 entries down
# to 1, header restamped to this host. Unlike the same bug in repo-sync,
# maintenance and cron-jobs, this tool WRITES, so it did not merely misreport.
#
# The heuristic stays as the fallback for a machine provisioned before the
# state file existed. An unrecognised profile is an error rather than a default,
# because defaulting is exactly what caused the clobber.
MACHINE_SETUP_PROFILE_FILE="${MACHINE_SETUP_STATE:-$HOME/.config/machine-setup}/profile"
if [ -n "${AI_APPS_PROFILE:-}" ]; then
  PROFILE="$AI_APPS_PROFILE"
elif [ -r "$MACHINE_SETUP_PROFILE_FILE" ]; then
  PROFILE="$(head -1 "$MACHINE_SETUP_PROFILE_FILE" | tr -d '\n\r')"
elif [ -n "$WORK_ROOT" ] && [ -d "$WORK_ROOT" ]; then
  PROFILE="work"
else
  PROFILE="personal"
fi

case "$PROFILE" in
  work|personal|terra) ;;
  *)
    echo "ai-apps.sh: unknown profile '$PROFILE' (want: work, personal, terra)" >&2
    exit 1
    ;;
esac

usage() {
  echo "usage: $(basename "$0") status|capture [profile]|compare [p1 p2]|setup [profile] [--apply]|adopt <kind> <name> <from> <to> [--apply]|update|nightly|test" >&2
  exit 1
}

# Where each app keeps copyable things. Skills follow the SKILL.md dir
# convention everywhere; agents are markdown for claude-code/opencode but
# TOML for codex (not cross-copyable).
skill_dir() {
  case "$1" in
    claude-code) echo "$HOME/.claude/skills" ;;
    codex)       echo "$HOME/.codex/skills" ;;
    copilot)     echo "$HOME/.copilot/skills" ;;
    opencode)    echo "$HOME/.config/opencode/skills" ;;
    antigravity) echo "$HOME/.gemini/skills" ;;
    *)           echo "" ;;
  esac
}

agent_dir() {
  case "$1" in
    claude-code) echo "$HOME/.claude/agents" ;;
    codex)       echo "$HOME/.codex/agents" ;;
    opencode)    echo "$HOME/.config/opencode/agents" ;;
    *)           echo "" ;;
  esac
}

# Emits "app|kind|name" for everything found on this machine, or
# "app|kind|name|key" where the name is derived and so not unique: the key is
# the row's identity and the name is only its label. Names only: manifests
# built from this are safe to commit. All parsing is tolerant — a missing app
# just contributes nothing.
inventory() {
  python3 - <<'PY'
import hashlib, json, os, sys, glob
from urllib.parse import urlsplit
try:
    import tomllib
except ImportError:
    tomllib = None

H = os.environ["HOME"]
out = []

def add(app, kind, name, key=None):
    # The key is kept beside the name, not instead of it. The rows go into a
    # set, so whatever the row carries is what tells two entries apart; with
    # the name alone, two plugins that derive one name were one row, and when
    # either was removed the survivor kept the row and the capture said none.
    # A | in a label would add a field, so it is percent-encoded (% first,
    # so the encoding reads back); the label is display only, the key decides.
    if name:
        name = name.replace("%", "%25").replace("|", "%7C")
        out.append(f"{app}|{kind}|{name}" + (f"|{key}" if key else ""))

def spec_key(spec):
    # A digest, not the spec. The manifest is kept and mailed,
    # and a spec can carry a URL password or a download token. Any escaping
    # of the spec text is also lossy (| and %7C read alike), where a hash of
    # the raw spec is not. A leading home becomes ~ first, so two machines
    # agree; only the prefix, since home can also sit inside another path.
    for lead in ("", "file://"):
        if spec.startswith(lead + H + "/"):
            spec = lead + "~" + spec[len(lead + H):]
            break
    return hashlib.sha256(spec.encode()).hexdigest()[:12]

def strip_jsonc(text):
    """JSON with the comments and trailing commas JSONC allows, removed.

    Whole-line `//` was all this removed, and every app here accepts more than
    that: an inline `// note` after a value, a `/* */` block, a comma before a
    closing brace. Since a parse error is now fatal for the whole capture, a
    partial reader turns one tolerated config into a nightly that writes
    nothing. Comment bytes become spaces rather than disappearing, so the
    column a parse error reports is still the column in the file.
    """
    out, i, n = [], 0, len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            out.append(ch)
            i += 1
            while i < n:
                out.append(text[i])
                if text[i] == "\\" and i + 1 < n:
                    out.append(text[i + 1])
                    i += 2
                    continue
                if text[i] == '"':
                    i += 1
                    break
                i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                out.append(" ")
                i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            while i < n and not (text[i] == "*" and i + 1 < n and text[i + 1] == "/"):
                out.append("\n" if text[i] == "\n" else " ")
                i += 1
            out.append("  ")
            i += 2
            continue
        out.append(ch)
        i += 1
    body = "".join(out)
    # A trailing comma, outside strings, before the bracket that closes its
    # container. The comment pass above already blanked every comment, so a
    # comma and its bracket are separated by whitespace and nothing else.
    cleaned, j, m = [], 0, len(body)
    while j < m:
        ch = body[j]
        if ch == '"':
            cleaned.append(ch)
            j += 1
            while j < m:
                cleaned.append(body[j])
                if body[j] == "\\" and j + 1 < m:
                    cleaned.append(body[j + 1])
                    j += 2
                    continue
                if body[j] == '"':
                    j += 1
                    break
                j += 1
            continue
        if ch == ",":
            k = j + 1
            while k < m and body[k] in " \t\r\n":
                k += 1
            if k < m and body[k] in "}]":
                cleaned.append(" ")
                j += 1
                continue
        cleaned.append(ch)
        j += 1
    return "".join(cleaned)


def jload(path):
    # Every one of these configs is JSON that its app also accepts with `//`
    # line comments, so the comments are stripped for all of them rather than
    # for the one file somebody remembered. `jsonc=True` used to be opt-in and
    # opencode.json did not have it: a `//` note added beside a Bedrock region
    # made the file unparseable to this reader, and the 2026-09-21 capture
    # recorded opencode as having no MCP servers and no plugins at all.
    #
    # Absent and unparseable are different answers and no longer share one.
    # A file that is not there contributes nothing, which is the ordinary
    # state of an app this machine does not run. A file that IS there and does
    # not parse means every row it would have contributed is missing, and a
    # capture that writes that absence looks exactly like an uninstall. Exit
    # instead: cmd_capture is what refuses to overwrite the inventory, and the
    # nightly turns this into a failed run rather than a wrong file.
    try:
        with open(path) as f:
            text = f.read()
    except OSError:
        return {}
    try:
        return json.loads(strip_jsonc(text))
    except ValueError as exc:
        sys.exit(f"ai-apps: {path} is present but does not parse: {exc}")

def dirs_in(path):
    try:
        return sorted(d for d in os.listdir(path)
                      if os.path.isdir(os.path.join(path, d))
                      and not d.startswith("."))
    except Exception:
        return []

def files_in(path, ext):
    try:
        return sorted(os.path.splitext(f)[0] for f in os.listdir(path)
                      if f.endswith(ext))
    except Exception:
        return []

# claude-code
for n in jload(f"{H}/.claude.json").get("mcpServers", {}):
    add("claude-code", "mcp", n)
for n in dirs_in(f"{H}/.claude/skills"):
    add("claude-code", "skill", n)
for n in files_in(f"{H}/.claude/agents", ".md"):
    add("claude-code", "agent", n)
p = jload(f"{H}/.claude/plugins/installed_plugins.json").get("plugins", {})
names = p.keys() if isinstance(p, dict) else \
    [x.get("name", "") for x in p if isinstance(x, dict)]
for n in names:
    add("claude-code", "plugin", n)

# claude-desktop
dsk = f"{H}/Library/Application Support/Claude"
for n in jload(f"{dsk}/claude_desktop_config.json").get("mcpServers", {}):
    add("claude-desktop", "mcp", n)
for n in dirs_in(f"{dsk}/Claude Extensions"):
    add("claude-desktop", "plugin", n.removeprefix("ant.dir."))

# codex
if tomllib:
    try:
        with open(f"{H}/.codex/config.toml", "rb") as f:
            for n in tomllib.load(f).get("mcp_servers", {}):
                add("codex", "mcp", n)
    except Exception:
        pass
for n in dirs_in(f"{H}/.codex/skills"):
    add("codex", "skill", n)
for n in files_in(f"{H}/.codex/agents", ".toml"):
    add("codex", "agent", n)

# copilot (config.json is JSONC)
for n in jload(f"{H}/.copilot/mcp-config.json").get("mcpServers", {}):
    add("copilot", "mcp", n)
for n in dirs_in(f"{H}/.copilot/skills"):
    add("copilot", "skill", n)
for pl in jload(f"{H}/.copilot/config.json").get("installedPlugins", []):
    if isinstance(pl, dict):
        add("copilot", "plugin", pl.get("name", ""))

# opencode
oc = jload(f"{H}/.config/opencode/opencode.json")
for n in oc.get("mcp", {}):
    add("opencode", "mcp", n)
for n in dirs_in(f"{H}/.config/opencode/skills"):
    add("opencode", "skill", n)
for n in files_in(f"{H}/.config/opencode/agents", ".md"):
    add("opencode", "agent", n)
for pl in oc.get("plugin", []):
    # The name is a label derived from the path, and any path-to-one-token
    # rule collides: alpha/dist/main.js and beta/dist/main.js are both
    # `main`. A digest of the spec is the key, so the name can stay a label.
    # The name reads the URL path only: the userinfo, query and fragment of
    # a URL spec are where credentials and tokens sit. Every spec is split,
    # not only one with a scheme: ./x.js?token=.. and //host/x.js carry them
    # too, and a plain path comes back as it went in.
    if not isinstance(pl, str):
        continue
    path = urlsplit(pl).path
    stem = os.path.splitext(os.path.basename(path))[0]
    if stem in ("plugin", "index"):
        stem = os.path.basename(os.path.dirname(path))
    add("opencode", "plugin", stem, spec_key(pl))

# antigravity (user-level; builtin skills excluded on purpose)
for n in jload(f"{H}/.gemini/antigravity/mcp_config.json").get("mcpServers", {}):
    add("antigravity", "mcp", n)
for n in dirs_in(f"{H}/.gemini/skills"):
    add("antigravity", "skill", n)
for n in dirs_in(f"{H}/.gemini/extensions"):
    add("antigravity", "plugin", n)

print("\n".join(sorted(set(out))))
PY
}

app_present() {
  case "$1" in
    claude-code)    command -v claude >/dev/null 2>&1 ;;
    claude-desktop) [ -d "$HOME/Library/Application Support/Claude" ] ;;
    codex)          command -v codex >/dev/null 2>&1 ;;
    copilot)        command -v copilot >/dev/null 2>&1 ;;
    opencode)       command -v opencode >/dev/null 2>&1 ;;
    antigravity)    [ -d "$HOME/.gemini/antigravity" ] ;;
  esac
}

cmd_status() {
  INV=$(inventory)
  echo "profile: $PROFILE"
  echo
  printf '%-15s %-10s %6s %6s %6s %7s\n' app installed mcp skill agent plugin
  for app in $APPS; do
    if app_present "$app"; then inst=yes; else inst=NO; fi
    set -- 0 0 0 0
    m=$(echo "$INV" | grep -c "^$app|mcp|" || true)
    s=$(echo "$INV" | grep -c "^$app|skill|" || true)
    a=$(echo "$INV" | grep -c "^$app|agent|" || true)
    p=$(echo "$INV" | grep -c "^$app|plugin|" || true)
    printf '%-15s %-10s %6s %6s %6s %7s\n' "$app" "$inst" "$m" "$s" "$a" "$p"
  done
}

cmd_capture() {
  prof="${1:-$PROFILE}"
  target="$PROFILES/$prof.inv"
  mkdir -p "$PROFILES"
  tmp=$(mktemp)
  # Checked, not left to `set -e`. The caller that writes `|| rc=$?` around
  # this function suppresses `set -e` inside it, and the run would then carry
  # on past a failed inventory and `mv` a two-line header over the real file.
  # An inventory this tool could not read whole is not an inventory.
  if ! {
    echo "# ai-apps inventory, profile: $prof — captured $(date '+%Y-%m-%d') on $(hostname -s)"
    echo "# format: app|kind|name[|key]  (names only — safe to commit)"
    inventory
  } > "$tmp"; then
    rm -f "$tmp"
    echo "ai-apps: the inventory is incomplete; $target left as it was." >&2
    return 1
  fi
  if [ -f "$target" ]; then
    echo "changes vs previous capture:"
    # Every header line is dropped, not only the dated one: the format line
    # changed once (sd:1273), and a comment is not an inventory change.
    # The last grep is also the pipeline's status, and so what says "none".
    # The old rows are read as manifest_rows reads them, so a file with
    # literal labels reports no change for its labels' new encoding.
    old_rows=$(mktemp)
    manifest_rows "$target" > "$old_rows"
    if diff "$old_rows" "$tmp" | grep '^[<>]' | sed 's/^</  gone: /; s/^>/  new:  /' | show_rows | grep -v '^  [a-z]*: *#'; then :; else
      echo "  none"
    fi
    rm -f "$old_rows"
    # The header carries a capture date, so writing unconditionally made the
    # file differ every single day even when nothing on the machine had
    # changed — and the nightly cron then left the repo permanently dirty.
    # Compare the inventory itself, ignoring comments: unchanged means the
    # existing file is already correct, and the date it carries goes on
    # meaning "when this last actually changed" instead of "when it last ran".
    old_body=$(mktemp); new_body=$(mktemp)
    grep -v '^#' "$target" > "$old_body" || :
    grep -v '^#' "$tmp"    > "$new_body" || :
    if cmp -s "$old_body" "$new_body"; then
      rm -f "$tmp" "$old_body" "$new_body"
      echo "unchanged: $target — left as captured $(sed -n '1s/.*captured //p' "$target")"
      return 0
    fi
    rm -f "$old_body" "$new_body"
  fi
  mv "$tmp" "$target"
  echo "captured: $target ($(grep -cv '^#' "$target" | tr -d ' ') entries)"
}

# A manifest label as it was: add() writes | as %7C and % as %25, so %7C
# is undone first and a literal "%7C" (stored %257C) survives.
decode() { sed 's/%7C/|/g; s/%25/%/g'; }

# A manifest's rows, labels in the encoded form inventory() writes. Labels
# were first encoded with the key (sd:1273), whose capture writes the format
# line "app|kind|name[|key]". A manifest without that line holds literal
# labels, so its % is encoded here, or decode would read a literal "100%25"
# as "100%" (sd:1826). A literal label holds no |: it would split the row.
manifest_rows() { # file
  if grep '^# format:' "$1" | grep -qF 'name[|key]'; then
    grep -v '^#' "$1" || :
  else
    grep -v '^#' "$1" | awk -F'|' -v OFS='|' 'NF >= 3 { gsub(/%/, "%25", $3) } { print }'
  fi
}

# Manifest rows as a report prints them: the label decoded, and a fourth
# field that is not a digest withheld. A profile captured before the key was
# a digest holds the raw spec there, which can carry a password or a token,
# and the capture report is mailed. The row prefix, if any, sits in field 1.
show_rows() {
  awk -F'|' '{
    if (NF < 3) { print; next }
    n = $3; gsub(/%7C/, "|", n); gsub(/%25/, "%", n)
    row = $1 "|" $2 "|" n
    if (NF == 4 && length($4) == 12 && $4 ~ /^[0-9a-f]+$/) row = row "|" $4
    else if (NF >= 4) row = row "|(old key withheld)"
    print row
  }'
}

# Cross-app matrix on the live machine: one row per name, one column per
# app that supports the kind, so gaps are visible at a glance.
matrix() { # kind app...
  kind="$1"; shift
  echo "== $kind"
  printf '  %-38s' ""
  for a in "$@"; do printf ' %-13s' "$a"; done
  echo
  echo "$INV" | awk -F'|' -v k="$kind" '$2 == k { print $3 }' | sort -u \
    | while IFS= read -r n; do
    printf '  %-38s' "$(printf '%s\n' "$n" | decode)"
    for a in "$@"; do
      # The first three fields, not the whole line: a keyed row carries a
      # fourth, and the matrix is by label.
      if echo "$INV" | awk -F'|' -v a="$a" -v k="$kind" -v n="$n" \
          '$1 == a && $2 == k && $3 == n { f = 1 } END { exit !f }'; then
        printf ' %-13s' "x"
      else
        printf ' %-13s' "-"
      fi
    done
    echo
  done
  echo
}

cmd_compare() {
  if [ $# -eq 2 ]; then
    f1="$PROFILES/$1.inv"; f2="$PROFILES/$2.inv"
    for f in "$f1" "$f2"; do
      [ -f "$f" ] || { echo "missing $f — run capture on that machine first" >&2; exit 1; }
    done
    t1=$(mktemp); t2=$(mktemp)
    manifest_rows "$f1" | sort > "$t1"
    manifest_rows "$f2" | sort > "$t2"
    only1=$(comm -23 "$t1" "$t2")
    only2=$(comm -13 "$t1" "$t2")
    rm -f "$t1" "$t2"
    if [ -z "$only1" ] && [ -z "$only2" ]; then
      echo "no differences between $1 and $2"
      return 0
    fi
    [ -n "$only1" ] && { echo "only in $1:"; echo "$only1" | show_rows | sed 's/^/  /'; }
    [ -n "$only2" ] && { echo "only in $2:"; echo "$only2" | show_rows | sed 's/^/  /'; }
    echo
    echo "adjust: edit $PROFILES/<p>.inv, or use 'adopt' / 'setup --apply' on the machine that should change"
    return 0
  fi
  INV=$(inventory)
  matrix mcp    claude-code claude-desktop codex copilot opencode antigravity
  matrix skill  claude-code codex copilot opencode antigravity
  matrix agent  claude-code codex opencode
  matrix plugin claude-code claude-desktop copilot opencode antigravity
  echo "adopt something: $(basename "$0") adopt <kind> <name> <from-app> <to-app>"
}

cmd_setup() {
  prof="$PROFILE"; apply=0
  for a in "$@"; do
    case "$a" in --apply) apply=1 ;; *) prof="$a" ;; esac
  done
  manifest="$PROFILES/$prof.inv"
  [ -f "$manifest" ] || { echo "missing $manifest — run capture first" >&2; exit 1; }
  tl=$(mktemp); tm=$(mktemp)
  inventory > "$tl"
  manifest_rows "$manifest" | sort > "$tm"
  missing=$(comm -13 "$tl" "$tm")
  extra=$(comm -23 "$tl" "$tm")
  rm -f "$tl" "$tm"

  if [ -z "$missing" ] && [ -z "$extra" ]; then
    echo "machine matches profile '$prof'"
    return 0
  fi
  if [ -n "$missing" ]; then
    echo "in profile '$prof' but missing on this machine:"
    echo "$missing" | while IFS='|' read -r app kind name _key; do
      name=$(printf '%s\n' "$name" | decode)
      # A skill some other app already has locally is directly copyable.
      src=""
      if [ "$kind" = "skill" ]; then
        for other in $APPS; do
          [ "$other" = "$app" ] && continue
          d=$(skill_dir "$other")
          [ -n "$d" ] && [ -d "$d/$name" ] && { src="$other"; break; }
        done
      fi
      if [ -n "$src" ]; then
        if [ "$apply" -eq 1 ]; then
          cp -R "$(skill_dir "$src")/$name" "$(skill_dir "$app")/$name"
          echo "  copied skill '$name' from $src to $app"
        else
          echo "  $app $kind $name  (copyable from $src — rerun with --apply)"
        fi
      else
        echo "  $app $kind $name  (install by hand; 'adopt' prints the recipe if another app has it)"
      fi
    done
  fi
  if [ -n "$extra" ]; then
    echo "on this machine but not in profile '$prof' (capture to keep, remove by hand to drop):"
    echo "$extra" | show_rows | sed 's/^/  /'
  fi
}

# Prints (or, for skills/agents with --apply, performs) the transfer of one
# item between two apps. MCP definitions are never written automatically:
# their env/header values are secrets, so the recipe is printed with values
# redacted and the source config named for manual copy-over.
cmd_adopt() {
  [ $# -ge 4 ] || usage
  kind="$1"; name="$2"; from="$3"; to="$4"; apply=0
  [ "${5:-}" = "--apply" ] && apply=1

  case "$kind" in
    skill)
      sd="$(skill_dir "$from")"; td="$(skill_dir "$to")"
      [ -n "$sd" ] && [ -n "$td" ] || { echo "no skill dir for $from or $to" >&2; exit 1; }
      [ -d "$sd/$name" ] || { echo "skill '$name' not found in $from ($sd)" >&2; exit 1; }
      if [ -d "$td/$name" ]; then
        echo "$to already has skill '$name' ($td/$name)"
        return 0
      fi
      if [ "$apply" -eq 1 ]; then
        mkdir -p "$td"
        cp -R "$sd/$name" "$td/$name"
        echo "copied: $sd/$name -> $td/$name"
      else
        echo "would copy: $sd/$name -> $td/$name  (rerun with --apply)"
      fi
      ;;
    agent)
      case "$from-$to" in
        claude-code-opencode|opencode-claude-code) ;;
        *) echo "agents are only markdown-compatible between claude-code and opencode; $from -> $to needs a manual rewrite" >&2; exit 1 ;;
      esac
      sf="$(agent_dir "$from")/$name.md"; tf="$(agent_dir "$to")/$name.md"
      [ -f "$sf" ] || { echo "agent '$name' not found in $from" >&2; exit 1; }
      if [ "$apply" -eq 1 ]; then
        mkdir -p "$(agent_dir "$to")"
        cp "$sf" "$tf"
        echo "copied: $sf -> $tf (review the frontmatter — tool fields differ slightly)"
      else
        echo "would copy: $sf -> $tf  (rerun with --apply)"
      fi
      ;;
    mcp)
      FROM_APP="$from" TO_APP="$to" MCP_NAME="$name" python3 - <<'PY'
import json, os, sys
try:
    import tomllib
except ImportError:
    tomllib = None

H = os.environ["HOME"]
frm, to, name = os.environ["FROM_APP"], os.environ["TO_APP"], os.environ["MCP_NAME"]
R = "<REDACTED - copy value from the source config>"

def jload(path, jsonc=False):
    with open(path) as f:
        text = f.read()
    if jsonc:
        text = "\n".join(l for l in text.splitlines()
                         if not l.lstrip().startswith("//"))
    return json.loads(text)

SRC = {
    "claude-code": f"{H}/.claude.json",
    "claude-desktop": f"{H}/Library/Application Support/Claude/claude_desktop_config.json",
    "copilot": f"{H}/.copilot/mcp-config.json",
    "antigravity": f"{H}/.gemini/antigravity/mcp_config.json",
    "opencode": f"{H}/.config/opencode/opencode.json",
    "codex": f"{H}/.codex/config.toml",
}
path = SRC.get(frm)
if not path or not os.path.exists(path):
    sys.exit(f"no MCP config for source app '{frm}'")

if frm == "codex":
    with open(path, "rb") as f:
        raw = tomllib.load(f).get("mcp_servers", {}).get(name)
elif frm == "opencode":
    raw = jload(path).get("mcp", {}).get(name)
else:
    raw = jload(path, jsonc=(frm == "copilot")).get("mcpServers", {}).get(name)
if raw is None:
    sys.exit(f"MCP server '{name}' not found in {frm} ({path})")

# Normalize to one internal shape, redacting every env/header value.
d = {"command": None, "args": [], "env": {}, "url": None, "headers": {}}
if frm == "opencode":
    cmd = raw.get("command") or []
    d["command"], d["args"] = (cmd[0], cmd[1:]) if cmd else (None, [])
    d["env"] = {k: R for k in raw.get("environment", {})}
    d["url"] = raw.get("url")
    d["headers"] = {k: R for k in raw.get("headers", {})}
else:
    d["command"] = raw.get("command")
    d["args"] = raw.get("args", [])
    d["env"] = {k: R for k in raw.get("env", {})}
    d["url"] = raw.get("url")
    d["headers"] = {k: R for k in raw.get("headers", {})}

stdio = d["command"] is not None
print(f"# {name}: {frm} -> {to}")
print(f"# source of truth (and of the secret values): {path}")

if to == "claude-code":
    if stdio:
        envs = " ".join(f"-e {k}={R!r}" for k in d["env"])
        args = " ".join(d["args"])
        print(f"claude mcp add {name} -s user {envs} -- {d['command']} {args}".replace("  ", " "))
    else:
        hdrs = " ".join(f'--header "{k}: {R}"' for k in d["headers"])
        print(f"claude mcp add {name} -s user --transport http {d['url']} {hdrs}".strip())
elif to == "codex":
    print(f"# append to ~/.codex/config.toml")
    print(f"[mcp_servers.{name}]")
    if stdio:
        print(f'command = "{d["command"]}"')
        print(f"args = {json.dumps(d['args'])}")
        if d["env"]:
            print(f"[mcp_servers.{name}.env]")
            for k in d["env"]:
                print(f'{k} = "{R}"')
    else:
        print(f'url = "{d["url"]}"')
elif to == "opencode":
    entry = {"type": "local" if stdio else "remote"}
    if stdio:
        entry["command"] = [d["command"], *d["args"]]
        if d["env"]:
            entry["environment"] = d["env"]
    else:
        entry["url"] = d["url"]
        if d["headers"]:
            entry["headers"] = d["headers"]
    print(f'# merge into the "mcp" object of ~/.config/opencode/opencode.json')
    print(json.dumps({name: entry}, indent=2))
else:  # claude-desktop, copilot, antigravity — mcpServers JSON shape
    entry = {}
    if stdio:
        entry["command"] = d["command"]
        if d["args"]:
            entry["args"] = d["args"]
        if d["env"]:
            entry["env"] = d["env"]
    else:
        entry["type"] = "http"
        entry["url"] = d["url"]
        if d["headers"]:
            entry["headers"] = d["headers"]
    target = SRC.get(to, "the app's MCP config")
    print(f'# merge into the "mcpServers" object of {target}')
    print(json.dumps({name: entry}, indent=2))
PY
      ;;
    plugin)
      echo "plugins install through each app's own manager; from $from to $to try:"
      case "$to" in
        claude-code) echo "  claude plugin install -s user $name" ;;
        opencode)    echo "  add '$name' to the plugin list in ~/.config/opencode/opencode.json" ;;
        copilot)     echo "  copilot: /plugin install $name (inside the CLI)" ;;
        *)           echo "  $to: install '$name' through its UI/marketplace" ;;
      esac
      ;;
    *) usage ;;
  esac
}

# The apps themselves all come from homebrew; upgrade just these six. Each
# brew call is a bounded step (lib/bounded.sh): the query gets 600 s and the
# upgrade 1800 s, or AI_APPS_STEP_TIMEOUT seconds each when that is set. A
# query that did not finish checked nothing, so it is not "all current".
cmd_update() {
  PKGS="codex opencode claude claude-code antigravity copilot-cli"
  rc=0
  out=$(st_step "${AI_APPS_STEP_TIMEOUT:-600}" brew outdated --quiet) || rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "brew outdated failed (exit $rc); no AI app was checked or upgraded"
    return 1
  fi
  todo=""
  for p in $PKGS; do
    echo "$out" | grep -qx "$p" && todo="$todo $p"
  done
  if [ -z "$todo" ]; then
    echo "all AI apps current (checked: $PKGS)"
    return 0
  fi
  echo "upgrading:$todo"
  # shellcheck disable=SC2086  # word splitting of the package list is the point
  st_step "${AI_APPS_STEP_TIMEOUT:-1800}" brew upgrade $todo
}

# Cron flavor: upgrade the six apps, re-capture the inventory, and email
# only when something actually moved (an upgrade or an inventory change).
# Exits 1 when the email could not be delivered or the update failed, and
# with capture's code when capture failed.
cmd_nightly() {
  NOTIFY="$DIR/../local-notify/notify.sh"
  TMPD=$(mktemp -d)
  # EXIT removes the folder; a signal exits, and so runs EXIT. A TERM trap
  # that only removed it returned into the run, which then wrote the capture
  # into the folder it had just removed (sd:2660).
  trap 'rm -rf "$TMPD"' EXIT
  trap 'exit 129' HUP
  trap 'exit 130' INT
  trap 'exit 143' TERM
  # fd 3 is the job log. The update's output goes into the report, and each
  # brew step names itself here as it starts, so a hang names its step.
  exec 3>&2
  # shellcheck disable=SC2034 # read by st_step in lib/bounded.sh
  ST_STEP_FD3=1

  # The update's code is kept too. A brew step that failed or timed out on a
  # quiet night used to exit 0 with no mail, its step line in the job log the
  # only trace (sd:2662). The report still goes when something moved; the exit
  # then fails the night, so cron-jobs raises its failure banner and push.
  up_rc=0
  cmd_update > "$TMPD/up" 2>&1 || up_rc=$?

  # The rc is kept, not discarded. `capture` refuses to write when a config it
  # reads is present and unparseable, and the old `|| true` turned that refusal
  # into a night that emailed and exited 0 -- the inventory silently a day
  # stale with nothing saying so.
  cap_rc=0
  cmd_capture > "$TMPD/cap" 2>&1 || cap_rc=$?
  cat "$TMPD/up" "$TMPD/cap"
  if [ "$cap_rc" -ne 0 ]; then
    echo "ai-apps: capture failed; the inventory was not rewritten." >&2
    exit "$cap_rc"
  fi

  upgraded=0
  grep -q '^upgrading:' "$TMPD/up" && upgraded=1
  changed=1
  grep -q '^  none$' "$TMPD/cap" && changed=0

  if [ "$upgraded" -eq 1 ] || [ "$changed" -eq 1 ]; then
    send_report
  fi
  # 1 and not the update's own code: a step's timeout is 124, which cron-jobs
  # uses for the job's own limit.
  if [ "$up_rc" -ne 0 ]; then
    echo "ai-apps: the app update failed (exit $up_rc); the step lines above name the brew call" >&2
    exit 1
  fi
  return 0
}

# The nightly's report mail. Exits 1 when it could not be delivered.
send_report() {

  subject="ai-apps: changes on $(hostname -s)"
  body=$(printf 'ai-apps nightly — %s on %s\n\nApp upgrades:\n%s\n\nInventory (profile %s):\n%s\n' \
    "$(date '+%Y-%m-%d %H:%M')" "$(hostname -s)" \
    "$(cat "$TMPD/up")" "$PROFILE" "$(cat "$TMPD/cap")")
  if ! sh "$NOTIFY" -t "$subject" -k status -c ntfy,email -b "$body"; then
    echo "email FAILED — exiting 1 so the cron failure push fires" >&2
    exit 1
  fi
}

case "${1:-}" in
  # The suite lives beside the tool and CI reaches it through this verb, so a
  # suite with no verb is a suite CI never runs.
  test)    shift; exec "${PYTHON:-python3}" -m unittest discover -s "$DIR/tests" -t "$DIR" "$@" ;;
  status)  cmd_status ;;
  capture) shift; cmd_capture "$@" ;;
  compare) shift; cmd_compare "$@" ;;
  setup)   shift; cmd_setup "$@" ;;
  adopt)   shift; cmd_adopt "$@" ;;
  update)  cmd_update ;;
  nightly) cmd_nightly ;;
  -h|--help|help)
    cat <<'HELPEOF'
usage: ai-apps.sh status|capture [profile]|compare [p1 p2]|setup [profile] [--apply]|adopt ...|update|nightly

  status                     which apps are installed and how many MCP
                             servers / skills / agents / plugins each has
  capture [profile]          snapshot the live inventory (names only, no
                             secrets) into <config>/ai-apps/profiles/
                             <profile>.inv; shows the
                             diff against the previous capture
  compare                    cross-APP matrix on this machine: every skill,
                             MCP server, agent and plugin vs the apps that
                             could carry it
  compare <p1> <p2>          cross-PROFILE diff of two captured manifests
                             (e.g. personal vs work)
  setup [profile] [--apply]  diff this machine against a profile manifest;
                             --apply copies skills over from an app that
                             already has them, everything else prints its
                             install recipe
  adopt <kind> <name> <from> <to> [--apply]
                             move one item between apps. skills/agents are
                             copied (--apply); MCP servers print the target
                             app's config snippet with secret values REDACTED
                             (never written automatically); plugins print the
                             install command
  update                     brew upgrade for just the six AI apps; each
                             brew call is logged as it starts and stopped
                             after 600 s (query) or 1800 s (upgrade), or
                             AI_APPS_STEP_TIMEOUT seconds when that is set
  nightly                    update + capture into the config folder, emailing
                             the report when an app was upgraded or the
                             inventory changed (what the ai-apps-nightly cron
                             job runs). Exits 1 when the email could not be
                             delivered or a brew step of the update failed or
                             timed out, and with capture's code when capture
                             failed and the inventory was not rewritten.

apps: claude-code, claude-desktop, codex, copilot, opencode, antigravity
manifests: <config>/ai-apps/profiles/<profile>.inv, where <config> is
$SYSTEM_TOOLS_CONFIG (default ~/.config/system); AI_APPS_PROFILES_DIR
names another folder. profiles/example.inv in this folder shows the format.
profile detection: the profile machine-setup recorded in
~/.config/machine-setup/profile; failing that, work if AI_APPS_WORK_ROOT
(else SYSTEM_TOOLS_WORK_ROOT, which local-repo-sync reads too) names an
existing directory, else personal. Override with AI_APPS_PROFILE. An unrecognised profile
is refused rather than defaulted — capture writes the profile's .inv, so a
wrong answer overwrites another machine's inventory.
HELPEOF
    exit 0
    ;;
  *) usage ;;
esac

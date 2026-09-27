#!/bin/sh
# claude.sh — version the MCP server list of Claude Code and Claude Desktop
# without versioning the credentials it holds.
#
# The live configs (~/.claude.json, Claude Desktop's Application Support file)
# carry API tokens inline, so they can only ever be tracked as symlinks — git
# stores the target path, never the content. That leaves the actual server set
# recorded nowhere: adding, removing or repointing an MCP server is a decision
# made once, on one machine, and lost on the next.
#
# capture writes a sanitized copy of just the mcpServers block: every
# credential replaced by ${VARNAME} naming the ~/.config/shell/env.sh export
# holds it, every path under $HOME rewritten to ${HOME}. That file is the
# record, and it lives in <config>/claude/mcp/, outside the checkout: the
# server set is one machine owner's choice, not part of the tool. restore puts
# it back, expanding the placeholders from the environment.
#
# The record is per-profile (mcp/common/ + mcp/<profile>/) because a flat one
# belongs to whichever machine ran capture last: the first snapshot was taken
# on the work machine, and capturing on the personal one would have quietly
# deleted the two servers only work has.
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
. "$DIR/../lib/config.sh"
# <config> is $SYSTEM_TOOLS_CONFIG, default ~/.config/system.
SNAP_DIR="$(st_config_dir claude)/mcp"

# The snapshot is layered the way machine-setup layers its manifests:
# mcp/common/ holds the servers every machine runs, mcp/<profile>/ the ones
# only that machine does. Flat, it was a cross-machine hazard — the file was
# captured on the work machine, so running capture on the personal one would
# have silently dropped the two servers only work has. capture writes the
# profile file alone and never touches common, exactly as machine-setup's
# capture subtracts the common manifest instead of dumping the whole machine.
#
# The profile comes from the state file machine-setup records, so there is one
# answer per machine and nothing new to keep in sync.
STATE_DIR="${MACHINE_SETUP_STATE:-$HOME/.config/machine-setup}"
PROFILE="${MACHINE_SETUP_PROFILE:-}"
if [ -z "$PROFILE" ] && [ -f "$STATE_DIR/profile" ]; then
  PROFILE=$(head -1 "$STATE_DIR/profile")
fi

APPLY=0

# name|live config|snapshot|who expands ${VAR}
#
# Claude Code expands ${VAR} out of its own process environment, in env blocks
# and in headers alike — three servers here already rely on it. So restore
# writes the placeholders through untouched and the live config keeps no
# credential at all. Claude Desktop is a GUI app with no login shell behind it:
# its config holds literals, so restore has to expand before writing.
# name|live config|common snapshot|profile snapshot|who expands ${VAR}
TARGETS="claude-code|$HOME/.claude.json|$SNAP_DIR/common/claude-code.json|$SNAP_DIR/$PROFILE/claude-code.json|app
claude-desktop|$HOME/Library/Application Support/Claude/claude_desktop_config.json|$SNAP_DIR/common/claude-desktop.json|$SNAP_DIR/$PROFILE/claude-desktop.json|restore"

# Header values say "Bearer <token>" and the JSON key is just "Authorization",
# so unlike an env block the key name does not say which credential it holds.
# One entry per server that authenticates by header: <server>=<variable>.
# Only needed when the live config holds the credential as a literal: a value
# that is already ${VAR} names its own variable and is kept as-is.
HEADER_VARS="github=GITHUB_PERSONAL_ACCESS_TOKEN
openwhispr=OPENWHISPR_API_KEY
ydc-server=YDC_API_KEY"

usage() {
  cat <<'EOF'
usage: claude.sh capture|status|restore|prune-plugin-cache|prune-mem-logs|mem-pro-watchdog|test [--apply] [-h|--help]

Version the MCP server list of Claude Code and Claude Desktop with the
credentials stripped. Dry run by default; --apply writes.

Snapshots live in <config>/claude/mcp/, outside the checkout; <config> is
$SYSTEM_TOOLS_CONFIG, default ~/.config/system. They are layered like
machine-setup's manifests: mcp/common/ holds the servers every machine runs,
mcp/<profile>/ the rest. The profile is the one machine-setup recorded;
MACHINE_SETUP_PROFILE overrides it for one run.

  capture   read the live configs, replace every credential with a
            ${VARNAME} placeholder and every $HOME path with ${HOME},
            and write the result to mcp/<profile>/<config>.json. Only
            servers mcp/common/ does not already define identically are
            written, and common is never modified — so capturing on one
            machine cannot drop the servers only another machine runs.
            Refuses to write anything that still looks like a live key.
  status    compare the live configs against common + profile: which
            servers were added, removed or changed, and which layer each
            came from. Reports names only, never values.
  restore   merge common + profile's mcpServers back into the live config.
            Claude Code expands ${VARNAME} itself, so its file keeps the
            placeholders and holds no credential; Claude Desktop has no
            shell behind it, so those are expanded before writing. ${HOME}
            is always expanded. The live file is backed up next to itself
            (mode 600) and nothing outside mcpServers is touched. Quit the
            app first — both rewrite their config on exit and will clobber
            the write.

  prune-plugin-cache
            delete every plugin version directory under
            ~/.claude/plugins/cache that installed_plugins.json does not
            list as installed, plus the temp_git_* dirs an install leaves
            behind. Claude Code clones each `plugin update` into a new
            directory and never evicts the old one; `claude plugin prune`
            removes auto-installed dependencies, not these. Refuses to run
            when the manifest lists nothing, because an empty keep-set
            would condemn the whole cache.

  prune-mem-logs
            delete claude-mem's daily logs/claude-mem-YYYY-MM-DD.log files
            older than CLAUDE_MEM_LOG_KEEP_DAYS (default 14) days, and trim
            logs/pro-watchdog.log to its last 5000 lines. Today's and
            yesterday's files always stay, and no other name is touched;
            claude-mem has no retention setting of its own. Prints the
            bytes reclaimed. Run daily by the claude-mem-log-prune cron job.
            In claude-mem.db it deletes tool_uses rows older than
            CLAUDE_MEM_TOOL_USES_KEEP_DAYS (default 7, at least 1) days and
            no other table, then runs PRAGMA incremental_vacuum when
            auto_vacuum is INCREMENTAL; otherwise the freed pages are reused
            inside the file. A dry run opens the file read-only and prints
            the rows and approximate bytes; --apply prints the rows deleted
            and the file size before and after. It runs beside the live
            worker, in short transactions: a lock held past
            CLAUDE_MEM_DB_BUSY_TIMEOUT (default 30) seconds is a logged skip
            with exit 0, and the next run catches up. A missing database, a
            database that is a symlink, or a missing table is skipped the
            same way.

  mem-pro-watchdog
            move claude-mem's observer along an ordered provider chain:
            down when the current provider runs out, back up once a
            higher one answers again. Run every 15 minutes by the
            claude-mem-pro-watchdog cron job. The chain is
            CLAUDE_MEM_PROVIDER_CHAIN in ~/.claude-mem/settings.json,
            highest priority first, from openrouter (the cmem Pro
            gateway), gemini and claude (the subscription); absent, it is
            "openrouter,claude". A provider counts when the chain lists it
            and it holds what the worker needs: a cm_pro_ token, the
            gateway URL and a matching OPENROUTER key for openrouter; a
            key in CLAUDE_MEM_GEMINI_API_KEY or GEMINI_API_KEY in
            ~/.claude-mem/.env for gemini (the worker reads no other
            place). This flips exactly one key, CLAUDE_MEM_PROVIDER, and
            writes no key anywhere; the worker re-reads settings at every
            generator start, so no restart. It moves down when the
            worker reports quota_exhausted AND a 1-token probe confirms it
            (openrouter, gemini) or when the worker still enforces
            claude's quota cooldown (30 minutes from arming), to the next
            provider below that answers, or else at once to one above
            that answers. A provider in that 30-minute cooldown is never
            a target. While the current provider works, it moves up once
            a higher provider has answered for
            CLAUDE_MEM_PRO_RETURN_DELAY_MIN (default 60) minutes running.
            Stuck with nothing to move to, it notifies once. A CLAUDE_MEM_PROVIDER the chain does not
            list is left alone (exit 0). State and a log live in
            ~/.claude-mem/pro-watchdog.json and logs/pro-watchdog.log.

  test      run the unittest suite in tests/. It drives mem-pro-watchdog
            against a scratch CLAUDE_MEM_DATA_DIR, so it reads and writes
            nothing under ~/.claude-mem and asks no remote anything.

options:
  --apply   perform the write (capture, restore, prune-plugin-cache,
            prune-mem-logs, mem-pro-watchdog). Without
            it, print the plan.
EOF
}

py() {
  MODE="$1" TARGETS="$TARGETS" HEADER_VARS="$HEADER_VARS" APPLY="$APPLY" \
    PROFILE="$PROFILE" \
    python3 - <<'PYEOF'
import json, os, re, shutil, sys

MODE = os.environ["MODE"]
APPLY = os.environ["APPLY"] == "1"
PROFILE = os.environ.get("PROFILE", "")
HOME = os.path.expanduser("~")

# Anchored on word boundaries, not a bare substring: PAT inside PATH matched,
# and Claude Desktop stores a literal PATH in one server's env block.
SECRET_KEY = re.compile(
    r"(?:^|_)(KEY|TOKEN|SECRET|PASS|PASSWORD|PAT|CRED|CREDENTIAL|AUTH)(?:$|_)", re.I)
# Header names are spelled as headers, not as shell variables — Authorization
# does not survive the underscore anchoring above.
SECRET_HEADER = re.compile(r"(authorization|api[-_]?key|token|secret)", re.I)
PLACEHOLDER = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*\}$")
# Anything matching these must never reach a tracked file. Deliberately broad:
# a false positive costs one map entry, a false negative commits a live key.
LIVE_KEY = [
    re.compile(r"\b(sk|rk|pk)-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{20,}"),
    re.compile(r"\bey[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\."),
    # Bare high-entropy blob: 32+ chars mixing case and digits, no spaces.
    re.compile(r"(?=[A-Za-z0-9_+/=-]{32,})(?=[^ ]*[a-z])(?=[^ ]*[A-Z])(?=[^ ]*[0-9])[A-Za-z0-9_+/=-]{32,}"),
]

header_vars = {}
for line in os.environ["HEADER_VARS"].splitlines():
    if "=" in line:
        k, v = line.split("=", 1)
        header_vars[k.strip()] = v.strip()

targets = []
for line in os.environ["TARGETS"].splitlines():
    if line.strip():
        targets.append(line.split("|"))


def unhome(value):
    if isinstance(value, str) and value.startswith(HOME + "/"):
        return "${HOME}" + value[len(HOME):]
    return value


def sanitize_server(name, server):
    out = {}
    for field, value in server.items():
        if field == "env" and isinstance(value, dict):
            out[field] = {
                k: ("${%s}" % k if SECRET_KEY.search(k) else unhome(v))
                for k, v in value.items()
            }
        elif field == "headers" and isinstance(value, dict):
            hdrs = {}
            for k, v in value.items():
                if not SECRET_HEADER.search(k):
                    hdrs[k] = unhome(v)
                    continue
                # Already a ${VAR}? Then it names the variable that holds
                # it and there is nothing to strip. Rewriting it would
                # need a map entry to say what it already says, and
                # without one it became ${UNMAPPED} - losing the name and
                # breaking restore for that server. HEADER_VARS is only
                # for literals.
                bare = v[7:] if isinstance(v, str) and v.startswith("Bearer ") else v
                if isinstance(bare, str) and PLACEHOLDER.match(bare):
                    hdrs[k] = v
                    continue
                var = header_vars.get(name)
                if var is None:
                    hdrs[k] = "${UNMAPPED}"
                    print("  WARN    %s: header %s has no HEADER_VARS entry — "
                          "add <server>=<variable> in claude.sh" % (name, k))
                elif isinstance(v, str) and v.startswith("Bearer "):
                    hdrs[k] = "Bearer ${%s}" % var
                else:
                    hdrs[k] = "${%s}" % var
            out[field] = hdrs
        elif isinstance(value, list):
            out[field] = [unhome(x) for x in value]
        else:
            out[field] = unhome(value)
    return out


def sanitize(config):
    servers = config.get("mcpServers") or {}
    return {name: sanitize_server(name, s) for name, s in sorted(servers.items())}


def find_live_keys(obj, path=""):
    hits = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            hits += find_live_keys(v, "%s.%s" % (path, k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            hits += find_live_keys(v, "%s[%d]" % (path, i))
    elif isinstance(obj, str) and not PLACEHOLDER.match(obj):
        for pat in LIVE_KEY:
            if pat.search(obj):
                hits.append(path.lstrip("."))
                break
    return hits


def short(path):
    """Repo-relative when that is shorter, absolute otherwise — relpath on a
    snapshot dir outside the cwd produces a wall of ../.."""
    rel = os.path.relpath(path)
    return path if rel.startswith("..") else rel


def load(path):
    with open(path) as fh:
        return json.load(fh)


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")


def expand_home(value):
    if isinstance(value, dict):
        return {k: expand_home(v) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_home(v) for v in value]
    if isinstance(value, str):
        return value.replace("${HOME}", HOME)
    return value


def placeholder_vars(server):
    """(variable, field, key) for every ${VAR} still in an env/headers block."""
    for field in ("env", "headers"):
        block = server.get(field)
        if not isinstance(block, dict):
            continue
        for key, value in block.items():
            if isinstance(value, str):
                for var in re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", value):
                    yield var, field, key


def expand(value, missing):
    if isinstance(value, dict):
        return {k: expand(v, missing) for k, v in value.items()}
    if isinstance(value, list):
        return [expand(v, missing) for v in value]
    if not isinstance(value, str):
        return value

    def sub(m):
        var = m.group(1)
        if var == "HOME":
            return HOME
        val = os.environ.get(var)
        if val is None:
            missing.add(var)
            return m.group(0)
        return val

    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", sub, value)


rc = 0
if not PROFILE:
    print("FAIL    no profile recorded and MACHINE_SETUP_PROFILE unset —")
    print("        run: machine-setup.sh setup <profile>")
    sys.exit(1)

for name, live_path, common_path, prof_path, expands in targets:
    print("== %s" % name)
    # common applies everywhere, the profile file adds to it and wins on a
    # server both define — the same precedence machine-setup gives its
    # manifests.
    common = load(common_path) if os.path.exists(common_path) else {}
    profile_snap = load(prof_path) if os.path.exists(prof_path) else {}
    merged = dict(common)
    merged.update(profile_snap)

    if MODE in ("capture", "status"):
        if not os.path.exists(live_path):
            print("  SKIP    live config not on this machine: %s" % live_path)
            continue
        clean = sanitize(load(live_path))
        leaks = find_live_keys(clean)
        if leaks:
            print("  FAIL    value still looks like a live credential at: %s"
                  % ", ".join(leaks))
            print("          not written — add the field to HEADER_VARS or "
                  "rename the env key so it says what it holds")
            rc = 1
            continue

    if MODE == "capture":
        # Only what common does not already provide, so capture can never
        # rewrite another machine's servers: a server common defines
        # identically is left to common, one this machine defines differently
        # is recorded here and overrides it.
        out = {n: srv for n, srv in clean.items() if common.get(n) != srv}
        if os.path.exists(prof_path) and profile_snap == out:
            print("  ok      %s current (%d server(s), %d from common)"
                  % (short(prof_path), len(out), len(clean) - len(out)))
        elif APPLY:
            write_json(prof_path, out)
            print("  wrote   %s (%d server(s), %d left to common)"
                  % (short(prof_path), len(out), len(clean) - len(out)))
        else:
            print("  [dry-run] would write %s (%d server(s), %d left to common)"
                  % (short(prof_path), len(out), len(clean) - len(out)))
        # Never deleted from common here: common is the other machines' record
        # too, and this one not running a server is not evidence they don't.
        for n in sorted(set(common) - set(clean)):
            print("  WARN    %s is in common but not on this machine — left "
                  "alone; move it to a profile if it is not shared" % n)

    elif MODE == "status":
        # "no servers recorded" is not the same as "never captured". A machine
        # that legitimately runs none of this target's servers has empty
        # snapshots, and telling it to run the capture it has already run is a
        # warning nothing can clear. Judge by whether a snapshot file exists.
        if not merged:
            if os.path.exists(prof_path) or os.path.exists(common_path):
                if clean:
                    print("  WARN    snapshot records no servers, but %d live "
                          "— run: claude.sh capture --apply" % len(clean))
                    rc = 1
                else:
                    print("  ok      no servers recorded, none live")
            else:
                print("  WARN    no snapshot yet — run: claude.sh capture --apply")
                rc = 1
            continue
        for server in sorted(set(merged) | set(clean)):
            layer = "profile" if server in profile_snap else "common"
            if server not in merged:
                print("  added   %s (live only — capture to record it)" % server)
                rc = 1
            elif server not in clean:
                print("  removed %s (in %s, not on this machine — capture, or "
                      "restore it)" % (server, layer))
                rc = 1
            elif merged[server] != clean[server]:
                fields = sorted(
                    k for k in set(merged[server]) | set(clean[server])
                    if merged[server].get(k) != clean[server].get(k)
                )
                print("  changed %s (%s) [%s]"
                      % (server, ", ".join(fields), layer))
                rc = 1
        if not rc:
            print("  ok      %d server(s) match the snapshot (%d common + %d %s)"
                  % (len(clean), len(clean) - len(profile_snap),
                     len(profile_snap), PROFILE))

    elif MODE == "restore":
        if not merged:
            print("  SKIP    no snapshot: %s" % short(prof_path))
            continue
        if not os.path.exists(live_path):
            print("  FAIL    live config missing: %s (start the app once)" % live_path)
            rc = 1
            continue
        missing = set()
        snapshot = merged
        live = load(live_path)
        if expands == "app":
            # Only ${HOME} — the app resolves the rest at launch, so the file
            # it reads never holds a credential. A variable the shell does not
            # export would resolve to nothing and break that server, so keep
            # whatever the live config has for it and say so.
            servers = expand_home(snapshot)
            for name_, srv in servers.items():
                for var, field, key in placeholder_vars(srv):
                    if var in os.environ:
                        continue
                    missing.add(var)
                    old = (live.get("mcpServers", {}).get(name_, {})
                           .get(field, {}).get(key))
                    if old is not None:
                        srv[field][key] = old
        else:
            servers = expand(snapshot, missing)
        same = live.get("mcpServers") == servers
        if same:
            print("  ok      live mcpServers already matches the snapshot")
        elif APPLY:
            backup = live_path + ".bak"
            shutil.copy2(live_path, backup)
            os.chmod(backup, 0o600)
            live["mcpServers"] = servers
            with open(live_path, "w") as fh:
                json.dump(live, fh, indent=2)
                fh.write("\n")
            print("  wrote   %d server(s) into %s (backup: %s)"
                  % (len(servers), live_path, os.path.basename(backup)))
        else:
            print("  [dry-run] would write %d server(s) into %s"
                  % (len(servers), live_path))
        for var in sorted(missing):
            print("  WARN    ${%s} not exported — kept whatever the live config "
                  "held; export it in ~/.config/shell/env.sh and re-run to "
                  "drop the "
                  "last plaintext copy" % var)

sys.exit(rc)
PYEOF
}

# Claude Code never evicts a superseded plugin version: `claude plugin update`
# clones each release into its own directory under plugins/cache and leaves the
# old one, node_modules and all. `claude plugin prune` does not cover them --
# it removes auto-installed dependency plugins, a different thing. Measured
# 2026-09-03: 17 claude-mem versions at 480M each, one of them live, 7.5G dead.
#
# installed_plugins.json is the authority on what is live, so this keeps
# exactly the installPath of every installed entry and drops every other
# version directory, plus the temp_git_* clone dirs an install leaves behind.
# Deriving the keep-set from the manifest rather than from a version sort is
# the point: "newest" is not "installed", and settings.json puts one live
# plugin's bin/ on PATH.
prune_plugin_cache() {
  APPLY="$APPLY" python3 - <<'PYEOF'
import json, os, pathlib, shutil, sys, time

APPLY = os.environ.get("APPLY") == "1"
home = pathlib.Path.home()
root = home / ".claude/plugins/cache"
manifest = home / ".claude/plugins/installed_plugins.json"

if not root.is_dir():
    print("no plugin cache at %s -- nothing to do" % root)
    sys.exit(0)
try:
    data = json.loads(manifest.read_text())
except (OSError, ValueError) as exc:
    sys.exit("claude.sh: cannot read %s: %s" % (manifest, exc))

live = {pathlib.Path(e["installPath"]).resolve()
        for entries in data.get("plugins", {}).values() for e in entries}
# An empty keep-set would mark every directory stale. That is the shape of a
# moved or truncated manifest, not of a machine with no plugins, and the cost
# of guessing wrong is the whole cache.
if not live:
    sys.exit("claude.sh: %s lists no installed plugins -- refusing to prune" % manifest)

stale = []
for entry in sorted(root.iterdir()):
    if not entry.is_dir():
        continue
    if entry.name.startswith("temp_git_"):
        stale.append(entry)
        continue
    for plugin in sorted(entry.iterdir()):
        if not plugin.is_dir():
            continue
        for version in sorted(plugin.iterdir()):
            if version.is_dir() and version.resolve() not in live:
                stale.append(version)

def size(path):
    total = 0
    for base, _, files in os.walk(path, onerror=lambda e: None):
        for name in files:
            try:
                total += os.lstat(os.path.join(base, name)).st_size
            except OSError:
                pass
    return total

def human(n):
    for unit in ("B", "K", "M", "G"):
        if n < 1024 or unit == "G":
            return "%.0f%s" % (n, unit) if unit != "G" else "%.1fG" % n
        n /= 1024.0

print("keeping %d installed version(s):" % len(live))
for path in sorted(live):
    print("  %s" % path.relative_to(root))
if not stale:
    print("no stale versions -- nothing to prune")
    sys.exit(0)

# Claude Code watches this cache and drops a `.orphaned_at` stamp into a
# directory it finds emptied. Measured 2026-09-03: that write landed between
# rmtree clearing 13.18.1 and its closing rmdir, which then failed with
# "Directory not empty" and aborted the run with half the cache still there.
# So each removal retries, and one that will not go reports itself instead of
# stopping the pass -- a racing writer must not cost the other nineteen.
def remove(path):
    for attempt in range(4):
        try:
            shutil.rmtree(path)
            return None
        except FileNotFoundError:
            return None
        except OSError as exc:
            if attempt == 3:
                return exc
            time.sleep(0.4)

freed = 0
failed = []
for path in stale:
    n = size(path)
    if APPLY:
        # resolve() before the containment check: a symlinked version dir must
        # not let a delete escape the cache root.
        if root.resolve() not in path.resolve().parents:
            sys.exit("claude.sh: %s is outside %s -- refusing" % (path, root))
        err = remove(path)
        if err is not None:
            failed.append((path, err))
            print("  FAILED  %-52s %s" % (path.relative_to(root), err))
            continue
    freed += n
    print("  %s %-52s %s" % ("removed" if APPLY else "would remove",
                             path.relative_to(root), human(n)))
print("%s %s across %d entr%s"
      % ("reclaimed" if APPLY else "would reclaim", human(freed),
         len(stale), "y" if len(stale) == 1 else "ies"))
if not APPLY:
    print("re-run with --apply to remove them")
elif failed:
    sys.exit("claude.sh: %d entr%s could not be removed"
             % (len(failed), "y" if len(failed) == 1 else "ies"))
PYEOF
}

mem_pro_watchdog() {
  APPLY="$APPLY" python3 - <<'PYEOF'
import json, os, pathlib, re, subprocess, sys, tempfile, time, urllib.request, urllib.error

APPLY = os.environ.get("APPLY") == "1"
DATA = pathlib.Path(os.environ.get("CLAUDE_MEM_DATA_DIR") or pathlib.Path.home() / ".claude-mem")
SETTINGS = DATA / "settings.json"
STATE = DATA / "pro-watchdog.json"
LOG = DATA / "logs" / "pro-watchdog.log"
HEALTH = DATA / "observer-health.json"
COOLDOWN = DATA / "quota-cooldown.json"
# The worker reads GEMINI_API_KEY from this file and never from its own
# environment, so neither does the watchdog.
ENV_FILE = DATA / ".env"
# Both overridable so the test harness can point them at a local stub server.
GATEWAY = (os.environ.get("CLAUDE_MEM_PRO_GATEWAY") or "https://cmem.ai/api/inference/v1").rstrip("/")
GEMINI_API = (os.environ.get("CLAUDE_MEM_GEMINI_ENDPOINT")
              or "https://generativelanguage.googleapis.com/v1beta/models").rstrip("/")
# The models claude-mem 13.25.3 accepts; it replaces any other with the first.
GEMINI_MODELS = ("gemini-flash-latest", "gemini-flash-lite-latest", "gemini-3.5-flash",
                 "gemini-3.1-flash-lite", "gemini-3-flash-preview")
# The gateway's observer alias, probed when CLAUDE_MEM_OPENROUTER_MODEL names none.
OPENROUTER_MODEL = "cmem-observer"
# How many of the worker's configured models one gateway probe tries.
OPENROUTER_PROBE_MODELS = 3
PROVIDERS = ("openrouter", "gemini", "claude")
# Absent CLAUDE_MEM_PROVIDER_CHAIN means the two-provider toggle this watchdog
# was before the chain existed.
DEFAULT_CHAIN = "openrouter,claude"
LABEL = {"openrouter": "the cmem Pro gateway", "gemini": "Gemini",
         "claude": "the Claude subscription"}
RETURN_DELAY = 60 * int(os.environ.get("CLAUDE_MEM_PRO_RETURN_DELAY_MIN") or "60")
NOTIFY = os.environ.get("CLAUDE_MEM_PRO_WATCHDOG_NOTIFY", "1") == "1"
now = time.time()
stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now))

def log(msg):
    line = "[pro-watchdog] %s %s" % (stamp, msg)
    print(line)
    if APPLY:
        try:
            LOG.parent.mkdir(parents=True, exist_ok=True)
            import fcntl
            # prune-mem-logs trims this file under the same lock and renames
            # the trimmed copy over it. A writer that waited on the lock then
            # holds the old inode, so it reopens the path before appending.
            for _ in range(5):
                with LOG.open("a") as fh:
                    fcntl.flock(fh, fcntl.LOCK_EX)
                    if os.fstat(fh.fileno()).st_ino == os.stat(LOG).st_ino:
                        fh.write(line + "\n")
                        break
        except OSError as exc:
            print("cannot append %s: %s" % (LOG, exc), file=sys.stderr)

def notify(text):
    log("notify: " + text)
    if not (APPLY and NOTIFY):
        return
    try:
        subprocess.run(["notify", "-b", "-t", "claude-mem", text], check=False, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log("notify failed: %s" % exc)

def load(path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default

def write_json(path, data):
    # Same directory, temp + rename, and mode 600 before the rename: the
    # settings file holds API keys, and a reader that sees a half-written
    # file would treat every key as unset.
    if not APPLY:
        return
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".")
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)

def env_file_value(name):
    """One variable from ~/.claude-mem/.env, read as dotenv: `KEY=value`,
    optional `export `, optional quotes, `#` comments."""
    try:
        text = ENV_FILE.read_text()
    except OSError:
        return ""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[7:].lstrip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        if k.strip() != name:
            continue
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        elif " #" in v:
            v = v.split(" #", 1)[0].rstrip()
        return v.strip()
    return ""

def http_post(url, payload, headers):
    """(status, body text); status None when no HTTP answer came back."""
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers=dict(headers, **{"Content-Type": "application/json",
                                                          "User-Agent": "claude-mem-pro-watchdog"}))
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
            return resp.status, ""
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as exc:
        return None, str(exc)

def error_field(text, field):
    try:
        value = json.loads(text).get("error", {}).get(field, "")
        return value if isinstance(value, str) else ""
    except (ValueError, AttributeError):
        return ""

# Each probe returns (verdict, detail) with verdict in ok|exhausted|inactive|
# unknown. Unknown never moves state, so a Wi-Fi blip cannot flip the
# provider. No detail ever carries a key.
def openrouter_models():
    """The models the worker tries, in order: CLAUDE_MEM_OPENROUTER_MODEL read
    as a comma or space separated list, whose first entry is the model and
    the rest its fallbacks. Nothing usable there means the gateway's observer
    alias, the value cmem's Pro setup writes. At most OPENROUTER_PROBE_MODELS,
    so one run makes a bounded number of 30-second requests."""
    raw = settings.get("CLAUDE_MEM_OPENROUTER_MODEL")
    names = re.split(r"[\s,]+", raw.strip()) if isinstance(raw, str) else []
    names = [n for n in dict.fromkeys(names) if n]
    return names[:OPENROUTER_PROBE_MODELS] or [OPENROUTER_MODEL]

def probe_openrouter():
    # One max_tokens=1 completion per model: a 402 costs nothing, a 200 costs
    # ~$0.001 of allowance. The worker falls back to its next model, so a
    # refusal of one model moves on to the next; only an answer about the key
    # or the allowance speaks for the whole provider.
    tried = []
    for model in openrouter_models():
        status, text = http_post(GATEWAY + "/chat/completions",
                                 {"model": model, "max_tokens": 1,
                                  "messages": [{"role": "user", "content": "ping"}]},
                                 {"Authorization": "Bearer " + pro_key})
        if status is None:
            # No answer at all is not about the model; another would fare the same.
            return "unknown", "; ".join(tried + [text])
        if 200 <= status < 300:
            return "ok", "HTTP %d %s" % (status, model)
        # The gateway's error code names the cause, so it decides before the
        # status. A 403 without one can be a model or endpoint the key may
        # not use, which says nothing about the subscription.
        code = error_field(text, "code")
        if code == "allowance_exhausted":
            return "exhausted", "HTTP %d %s" % (status, code)
        if code in ("key_invalid", "subscription_inactive"):
            return "inactive", "HTTP %d %s" % (status, code)
        if status == 402:
            return "exhausted", "HTTP %d %s" % (status, code)
        if status == 401:
            return "inactive", "HTTP %d %s" % (status, code)
        tried.append("HTTP %d %s %s" % (status, model, code or text[:120]))
    return "unknown", "; ".join(tried)

def probe_gemini():
    # One maxOutputTokens=1 generateContent with the model the worker would
    # use. The key goes in a header, not in the URL the worker builds, so no
    # proxy or error message can echo it. The worker files a body saying
    # RESOURCE_EXHAUSTED or "quota exceeded" as quota_exhausted.
    model = settings.get("CLAUDE_MEM_GEMINI_MODEL") or GEMINI_MODELS[0]
    if model not in GEMINI_MODELS:
        model = GEMINI_MODELS[0]
    status, text = http_post("%s/%s:generateContent" % (GEMINI_API, model),
                             {"contents": [{"role": "user", "parts": [{"text": "ping"}]}],
                              "generationConfig": {"maxOutputTokens": 1}},
                             {"x-goog-api-key": gemini_key})
    if status is None:
        return "unknown", text
    if 200 <= status < 300:
        return "ok", "HTTP %d %s" % (status, model)
    low = text.lower()
    reason = error_field(text, "status")
    if status == 429 or "resource_exhausted" in low or "quota exceeded" in low:
        return "exhausted", "HTTP %d %s" % (status, reason or "quota")
    if status in (401, 403) or (status == 400 and any(
            s in low for s in ("api key not valid", "api_key_invalid", "api key expired"))):
        return "inactive", "HTTP %d %s" % (status, reason or "key rejected")
    return "unknown", "HTTP %d %s" % (status, reason or text[:120])

def unmanaged(why):
    """Say why there is nothing to do, and exit 0 rather than 1.

    Every condition that calls this says one thing: the claude-mem on this
    machine is not a configuration this watchdog manages. That is a state
    somebody chose, not a fault. cron-jobs.sh reads any non-zero exit as
    FAILED and calls `notify_failure "$job" "$rc"`, in `cmd_exec` of
    `local-cron-jobs/cron-jobs.sh`, and the run report it records then opens
    a report item through `sd reports ingest`. On a */15 schedule one
    deliberate setting became ninety-six report items a day, each with an
    unresolved followup note, which `sd reports acknowledge` refuses to close
    (sd:880).

    Two conditions keep a non-zero exit, because nobody sets them on purpose:
    settings.json present but unreadable, and a cm_pro_ token beside an
    OPENROUTER key that is a different string. They are worth a page.
    """
    log("not managing claude-mem here: " + why)
    sys.exit(0)

settings = load(SETTINGS, None)
if settings is None and not SETTINGS.exists() and not SETTINGS.is_symlink():
    # `load` answers a missing file and an unparsable one the same way, so the
    # existence test is what separates "claude-mem is not installed" from
    # "claude-mem's settings are broken". Only the second is a fault.
    #
    # `exists()` follows the link, so a settings.json that is a symlink to
    # nothing answers False and would be filed as absent. It is not absent, it
    # is broken, and pointing it somewhere wrong is a way to break it that
    # config snapshots kept as symlinks invite. So the
    # no-follow test decides, and a dangling link falls through to the fault
    # below.
    unmanaged("%s does not exist" % SETTINGS)
if not isinstance(settings, dict):
    sys.exit("claude.sh: cannot read %s" % SETTINGS)

# The chain, highest priority first. A name claude-mem does not know is
# dropped with a log line rather than failing the job every 15 minutes.
raw_chain = settings.get("CLAUDE_MEM_PROVIDER_CHAIN")
if not (isinstance(raw_chain, str) and raw_chain.strip()):
    raw_chain = DEFAULT_CHAIN
chain = []
for name in raw_chain.split(","):
    name = name.strip().lower()
    if name and name not in PROVIDERS:
        log("CLAUDE_MEM_PROVIDER_CHAIN names %r, which is not one of %s; ignoring it"
            % (name, "|".join(PROVIDERS)))
    elif name and name not in chain:
        chain.append(name)

# A provider is enabled when the chain lists it and it holds the credentials
# the worker would use. Every reason one is not goes into `off`.
off = {}
pro_key = (settings.get("CLAUDE_MEM_CLOUD_SYNC_TOKEN") or "").strip()
gemini_key = ""
if "openrouter" in chain:
    if not pro_key.startswith("cm_pro_"):
        off["openrouter"] = "CLAUDE_MEM_CLOUD_SYNC_TOKEN is not a cm_pro_ key; nothing to watch"
    elif (settings.get("CLAUDE_MEM_OPENROUTER_BASE_URL") or "").rstrip("/") != GATEWAY:
        off["openrouter"] = ("CLAUDE_MEM_OPENROUTER_BASE_URL is not the cmem gateway (%s); "
                             "the watchdog only toggles CLAUDE_MEM_PROVIDER and expects the Pro "
                             "gateway config to stay in settings.json" % GATEWAY)
    elif (settings.get("CLAUDE_MEM_OPENROUTER_API_KEY") or "").strip() != pro_key:
        sys.exit("claude.sh: CLAUDE_MEM_OPENROUTER_API_KEY differs from the Pro token; refusing to guess")
if "gemini" in chain:
    gemini_key = ((settings.get("CLAUDE_MEM_GEMINI_API_KEY") or "").strip()
                  or env_file_value("GEMINI_API_KEY"))
    if not gemini_key:
        off["gemini"] = ("no Gemini key in CLAUDE_MEM_GEMINI_API_KEY or GEMINI_API_KEY in %s; "
                         "the worker reads no other place" % ENV_FILE)
enabled = [p for p in chain if p not in off]

current = settings.get("CLAUDE_MEM_PROVIDER")
if current not in chain:
    unmanaged("CLAUDE_MEM_PROVIDER is %r, which the chain %s does not list, so it is the "
              "operator's to manage" % (current, ",".join(chain)))
if len(enabled) < 2:
    unmanaged("the chain %s leaves %s to switch between: %s"
              % (",".join(chain), "only " + enabled[0] if enabled else "nothing",
                 "; ".join("%s: %s" % kv for kv in off.items())))

state = load(STATE, {})
if not isinstance(state, dict):
    state = {}
if "mode" in state:
    # The first shape: mode pro|fallback and one scalar okSince, which
    # counted the gateway's streak while on the Claude subscription.
    old = state.pop("mode")
    prev = {"pro": "openrouter", "fallback": "claude"}.get(old)
    streak = state.get("okSince")
    state["okSince"] = ({"openrouter": streak}
                        if prev == "claude" and isinstance(streak, (int, float)) else {})
    if "lastProbe" in state:
        state["probes"] = {"openrouter": {"at": state.pop("lastProbe"),
                                          "verdict": state.pop("lastProbeVerdict", None),
                                          "detail": state.pop("lastProbeDetail", None)}}
    state["provider"] = prev
    log("state: migrated %s from mode=%s to provider=%s" % (STATE.name, old, prev))
for key in ("okSince", "probes"):
    if not isinstance(state.get(key), dict):
        state[key] = {}
state.setdefault("provider", current)
if state["provider"] != current:
    # settings.json is the truth; a hand edit resets the watchdog's memory.
    log("settings say %s but state said %s; following settings" % (current, state["provider"]))
    state.update({"provider": current, "since": stamp, "okSince": {}, "stuck": None})
state.setdefault("since", stamp)
state.setdefault("stuck", None)
ok_since = state["okSince"]

# The worker refuses a provider for 30 minutes after it arms a cooldown and
# then admits one probe (claude-mem 13.25.3). The entry itself stays in the
# file until a success with that provider clears it, which never happens
# while another provider runs. So an entry older than that window is history,
# not a refusal.
WORKER_COOLDOWN_MS = 30 * 60 * 1000

def cooldown_entries(provider):
    cd = load(COOLDOWN, [])
    if not isinstance(cd, list):
        return []
    return [e for e in cd if isinstance(e, dict) and e.get("provider") == provider]

def cooldown_armed(provider):
    return bool(cooldown_entries(provider))

def cooldown_fresh(provider):
    for e in cooldown_entries(provider):
        armed = e.get("armedAtMs")
        if not isinstance(armed, (int, float)) or now * 1000 - armed < WORKER_COOLDOWN_MS:
            return True
    return False

def worker_reports_exhausted(provider):
    if cooldown_armed(provider):
        return "quota-cooldown.json armed for %s" % provider
    h = load(HEALTH, {})
    if (isinstance(h, dict) and h.get("lastErrorKind") == "quota_exhausted"
            and h.get("lastErrorProvider") == provider
            and (h.get("lastErrorAt") or 0) > (h.get("lastSuccessAt") or 0)):
        return "observer-health.json lastErrorKind=quota_exhausted for %s after last success" % provider
    return None

def check(provider, candidate=True):
    """Can `provider` take the observer now? A candidate the worker still
    refuses cannot, however its probe would answer, so a cooldown under 30
    minutes old decides without a probe. The Claude subscription has no cheap
    probe, so that cooldown is its only answer. The current provider's own
    check (candidate=False) probes: there the cooldown is the signal the
    probe must confirm.

    Any answer but ok ends the provider's success streak, whichever step
    asked: a failure seen while looking down the chain counts as much as one
    seen while looking up it."""
    if provider == "claude":
        if cooldown_fresh("claude"):
            verdict, detail = "exhausted", "quota-cooldown.json armed for claude"
        else:
            verdict, detail = "ok", "no quota cooldown the worker still enforces for claude"
    elif candidate and cooldown_fresh(provider):
        verdict, detail = "exhausted", ("quota-cooldown.json armed for %s under 30 min ago; "
                                        "the worker still refuses it" % provider)
    else:
        verdict, detail = probe_openrouter() if provider == "openrouter" else probe_gemini()
        state["probes"][provider] = {"at": stamp, "verdict": verdict, "detail": detail}
    if verdict != "ok" and ok_since.get(provider):
        log("%s: %s streak broken by %s (%s); restarting the delay"
            % (current, provider, verdict, detail))
        ok_since[provider] = None
    return verdict, detail

def flip(to, why):
    settings["CLAUDE_MEM_PROVIDER"] = to
    if to == "openrouter":
        # An unparsable non-empty stamp makes the plugin's own fallback
        # permanent (mbe() returns truthy on NaN); clear it on every return.
        settings["CLAUDE_MEM_PRO_FALLBACK_AT"] = ""
    write_json(SETTINGS, settings)
    ok_since.pop(to, None)
    ok_since[current] = None
    state.update({"provider": to, "since": stamp, "stuck": None,
                  "lastFlip": stamp, "lastFlipWhy": why})
    log("%s -> %s: %s" % (current, to, why) + ("" if APPLY else " (dry run, nothing written)"))

def down_message(verdict, detail, to):
    target = LABEL[to]
    if verdict == "unusable":
        # No probe ran: the settings themselves disable the provider.
        return ("%s is not usable with this configuration (%s); observer switched to %s. "
                "Fix the setting or drop it from CLAUDE_MEM_PROVIDER_CHAIN."
                % (LABEL[current], detail, target))
    if current == "openrouter" and verdict == "inactive":
        return ("cmem Pro key rejected (%s); observer switched to %s. This is a lapsed "
                "subscription, not a monthly reset -- check cmem.ai/pro." % (detail, target))
    if current == "openrouter":
        return ("cmem Pro allowance exhausted; observer switched to %s. It returns to Pro "
                "on its own once the allowance resets." % target)
    if current == "gemini" and verdict == "inactive":
        return ("Gemini key rejected (%s); observer switched to %s. Check GEMINI_API_KEY in "
                "~/.claude-mem/.env." % (detail, target))
    if current == "gemini":
        return ("Gemini quota exhausted (%s); observer switched to %s. It returns to Gemini "
                "once probes succeed for %d min." % (detail, target, RETURN_DELAY // 60))
    return "%s unavailable (%s); observer switched to %s." % (LABEL[current], detail, target)

# Fail down: the current provider is out when the worker says so and a probe
# agrees, or when it lacks the credentials the worker needs (the worker then
# runs the Claude subscription without saying so).
down = None
if current not in enabled:
    down = ("unusable", off[current])
    log("%s: not usable here (%s)" % (current, off[current]))
else:
    signal = worker_reports_exhausted(current)
    if not signal:
        log("%s: worker healthy, no probe" % current)
    else:
        verdict, detail = check(current, candidate=False)
        if verdict in ("exhausted", "inactive"):
            down = (verdict, "%s; %s" % (signal, detail) if detail != signal else signal)
        else:
            log("%s: worker reports %s but the check says %s (%s); leaving it to the worker's "
                "own cooldown" % (current, signal, verdict, detail))

# The chain's order below comes first. When nothing below can take over, a
# provider above that answers now takes the observer at once: the return
# delay guards a preference while the current provider works, not the way
# out of one that cannot.
moved = False
if down:
    tried = []
    below = [p for p in enabled if chain.index(p) > chain.index(current)]
    above = [p for p in enabled if chain.index(p) < chain.index(current)]
    for candidate in below + above:
        verdict, detail = check(candidate)
        tried.append("%s %s (%s)" % (candidate, verdict, detail))
        if verdict == "ok":
            flip(candidate, "%s; %s ok (%s)" % (down[1], candidate, detail))
            if candidate in below:
                notify(down_message(down[0], down[1], candidate))
            else:
                notify("%s is out (%s) and nothing below it can take over; observer moved up "
                       "to %s, which answers now." % (LABEL[current], down[1], LABEL[candidate]))
            moved = True
            break
    if not moved:
        text = ("%s is out (%s) and no other provider in the chain can take over: %s"
                % (current, down[1], "; ".join(tried) or "no other enabled provider"))
        stuck = state.get("stuck")
        if isinstance(stuck, dict) and stuck.get("provider") == current:
            log("still stuck since %s: %s" % (stuck.get("since"), text))
        else:
            state["stuck"] = {"provider": current, "since": stamp}
            notify("claude-mem observer stuck on %s: %s. It moves on its own once a "
                   "provider in the chain answers again." % (LABEL[current], text))
elif state.get("stuck"):
    log("%s: no longer reported out; clearing the stuck flag" % current)
    state["stuck"] = None

# Fail up: while the current provider works, every enabled provider above it
# is checked each run, and the observer returns to the highest one that has
# answered ok continuously for RETURN_DELAY. Neither cmem.ai nor Google
# publishes a reset time, so the streak is the only evidence a reset
# happened. A current provider that is out already had every provider above
# it checked in the step before.
if not down:
    for higher in [p for p in enabled if chain.index(p) < chain.index(current)]:
        verdict, detail = check(higher)
        if verdict == "ok":
            if not ok_since.get(higher):
                ok_since[higher] = now
                log("%s: %s answering again (%s); returning after %d min of that"
                    % (current, higher, detail, RETURN_DELAY // 60))
            elif now - float(ok_since[higher]) >= RETURN_DELAY:
                flip(higher, "%s ok for %d min (%s)"
                     % (higher, (now - float(ok_since[higher])) // 60, detail))
                notify("%s is back; observer returned to it." % LABEL[higher])
                break
            else:
                log("%s: %s ok for %d of %d min" % (current, higher,
                    (now - float(ok_since[higher])) // 60, RETURN_DELAY // 60))
        else:
            ok_since[higher] = None
            log("%s: %s still %s (%s)" % (current, higher, verdict, detail))

state["lastRun"] = stamp
write_json(STATE, state)
PYEOF
}

# claude-mem writes one logs/claude-mem-YYYY-MM-DD.log a day and never deletes
# one; 13.25.3 has CLAUDE_MEM_LOG_LEVEL and no retention setting. Measured
# 2026-09-25: 41 files, 997M, the daily files 16-56 MB each. This deletes the
# dated files older than CLAUDE_MEM_LOG_KEEP_DAYS and trims the watchdog's own
# log.
#
# The date in the name is the worker's, which is UTC: in the evening west of
# Greenwich the worker already writes tomorrow's file. So age is counted from
# the local date, a file dated today or later is never old, and yesterday's is
# kept whatever the setting says.
#
# In the database it touches one table, tool_uses: the worker inserts a row per
# tool call, raw input and response, and deletes none. In 13.25.3 only two HTTP
# lookups read it (getToolUsesByIds, queryToolUses); cloud sync covers
# observations, session_summaries and user_prompts; no trigger or foreign key
# names it; the worker updates only recent rows' observation_id. So rows older
# than CLAUDE_MEM_TOOL_USES_KEEP_DAYS go, beside the live worker: WAL, a busy
# timeout, short batched transactions, and never a full VACUUM or a restart.
prune_mem_logs() {
  APPLY="$APPLY" python3 - <<'PYEOF'
import datetime, fcntl, math, os, pathlib, re, sqlite3, stat, sys, tempfile, time, urllib.parse

APPLY = os.environ.get("APPLY") == "1"
DATA = pathlib.Path(os.environ.get("CLAUDE_MEM_DATA_DIR") or pathlib.Path.home() / ".claude-mem")
LOGS = DATA / "logs"
DB = DATA / "claude-mem.db"
WATCHDOG_LOG = LOGS / "pro-watchdog.log"
WATCHDOG_KEEP_LINES = 5000
DAILY = re.compile(r"^claude-mem-(\d{4})-(\d{2})-(\d{2})\.log$")
# Rows per delete transaction and pages per incremental_vacuum step: each
# holds the write lock for milliseconds, so the worker's inserts queue briefly.
DELETE_BATCH = 2000
VACUUM_STEP = 2048


def whole_days(name, default):
    raw = os.environ.get(name) or default
    try:
        days = int(raw)
    except ValueError:
        days = -1
    if days < 0:
        sys.exit("claude.sh: %s=%r is not a whole number of days" % (name, raw))
    return days


# Every setting is checked before anything is deleted.
# Today's file is being written and yesterday's may be, so neither goes.
keep_days = max(whole_days("CLAUDE_MEM_LOG_KEEP_DAYS", "14"), 1)
# The worker updates a recent row's observation_id; never prune from under it.
tool_uses_keep_days = max(whole_days("CLAUDE_MEM_TOOL_USES_KEEP_DAYS", "7"), 1)
raw = os.environ.get("CLAUDE_MEM_DB_BUSY_TIMEOUT") or "30"
try:
    busy_timeout = float(raw)
except ValueError:
    busy_timeout = -1.0
if not (math.isfinite(busy_timeout) and busy_timeout > 0):
    sys.exit("claude.sh: CLAUDE_MEM_DB_BUSY_TIMEOUT=%r is not a positive number of seconds" % raw)

logs_present = LOGS.is_dir()
if not logs_present:
    print("no claude-mem log directory at %s -- nothing to do" % LOGS)

def human(n):
    for unit in ("B", "K", "M", "G"):
        if n < 1024 or unit == "G":
            return "%.0f%s" % (n, unit) if unit != "G" else "%.1fG" % n
        n /= 1024.0

today = datetime.date.today()
old = []
for entry in sorted(LOGS.iterdir()) if logs_present else ():
    m = DAILY.match(entry.name)
    if not m:
        continue
    try:
        day = datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        st = entry.lstat()
    except (ValueError, OSError):
        continue
    # A regular file only: a symlink or a directory wearing the name is not
    # a log the worker wrote.
    if not stat.S_ISREG(st.st_mode):
        continue
    if (today - day).days > keep_days:
        old.append((entry, st.st_size))

freed = 0
failed = 0
for path, size in old:
    if APPLY:
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        except OSError as exc:
            failed += 1
            print("  FAILED  %s %s" % (path.name, exc))
            continue
    freed += size
    print("  %s %-32s %s" % ("removed" if APPLY else "would remove", path.name, human(size)))
if logs_present:
    print("%d dated log(s) older than %d day(s)" % (len(old), keep_days))


def locked_watchdog_log():
    """Open the watchdog log and hold its exclusive flock, or return None.

    The watchdog's log() appends under the same lock, so holding it from the
    read to the rename means no line lands in the copy being replaced. Anyone
    who queued on the lock meanwhile, a writer or a second trim, holds an
    inode no longer at the path, so this reopens until the two agree.
    """
    for _ in range(5):
        try:
            src = WATCHDOG_LOG.open("rb")
        except FileNotFoundError:
            return None
        fcntl.flock(src, fcntl.LOCK_EX)
        try:
            if os.fstat(src.fileno()).st_ino == os.stat(WATCHDOG_LOG).st_ino:
                return src
        except FileNotFoundError:
            pass
        src.close()
    print("  %s kept being replaced; not trimmed" % WATCHDOG_LOG.name)
    return None


# The watchdog appends a few lines every 15 minutes and never trims. Keep the
# tail, written to a temp file beside it and renamed over it, so a reader
# never sees half a file; the mode is carried over.
try:
    st = WATCHDOG_LOG.lstat()
except OSError:
    st = None
src = locked_watchdog_log() if st is not None and stat.S_ISREG(st.st_mode) else None
if src is not None:
    with src:
        held = os.fstat(src.fileno())
        lines = src.readlines()
        trim = len(lines) > WATCHDOG_KEEP_LINES
        if trim:
            tail = b"".join(lines[-WATCHDOG_KEEP_LINES:])
            saved = held.st_size - len(tail)
            if APPLY:
                fd, tmp = tempfile.mkstemp(dir=str(LOGS), prefix=WATCHDOG_LOG.name + ".")
                with os.fdopen(fd, "wb") as fh:
                    fh.write(tail)
                os.chmod(tmp, stat.S_IMODE(held.st_mode))
                os.replace(tmp, WATCHDOG_LOG)
    if trim:
        freed += saved
        print("  %s %s from %d to %d lines, %s" % (
            "trimmed" if APPLY else "would trim", WATCHDOG_LOG.name, len(lines),
            WATCHDOG_KEEP_LINES, human(saved)))

if logs_present:
    print("%s %d bytes (%s)" % ("reclaimed" if APPLY else "would reclaim", freed, human(freed)))


def is_busy(exc):
    code = getattr(exc, "sqlite_errorcode", None)
    if code is not None:
        return code & 0xFF in (5, 6)  # SQLITE_BUSY, SQLITE_LOCKED
    return "locked" in str(exc) or "busy" in str(exc)


def ident(name):
    return '"%s"' % name.replace('"', '""')


def scalar(conn, sql, args=()):
    return conn.execute(sql, args).fetchone()[0]


def prune_tool_uses():
    """Delete tool_uses rows older than the keep window; report what went.

    A dry run opens the file read-only. --apply opens it read-write, never
    creating it, and deletes in short transactions. A lock held past the busy
    timeout is a logged skip: one night's miss is caught up the next.
    """
    try:
        st = DB.lstat()
    except FileNotFoundError:
        print("no claude-mem database at %s -- tool_uses skipped" % DB)
        return
    # A regular file only, as for the logs: deleting through a symlink would
    # reach a database outside the data directory.
    if not stat.S_ISREG(st.st_mode):
        print("%s is not a regular file -- tool_uses skipped" % DB)
        return
    cutoff = int(time.time() * 1000) - tool_uses_keep_days * 86400 * 1000
    which = "tool_uses row(s) older than %d day(s)" % tool_uses_keep_days
    uri = "file:%s?mode=%s" % (urllib.parse.quote(str(DB)), "rw" if APPLY else "ro")
    conn = None
    deleted = 0
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=busy_timeout, isolation_level=None)
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table'"
                            " AND name = 'tool_uses'").fetchone():
            print("no tool_uses table in %s -- skipped" % DB)
            return
        auto_vacuum = scalar(conn, "PRAGMA auto_vacuum")
        if not APPLY:
            # Row data only, the way SQLite stores it; index entries are extra.
            columns = [row[1] for row in conn.execute("PRAGMA table_info(tool_uses)")]
            size = " + ".join("COALESCE(length(CAST(%s AS BLOB)), 0)" % ident(c) for c in columns)
            rows, approx = conn.execute(
                "SELECT count(*), COALESCE(sum(%s), 0) FROM tool_uses"
                " WHERE created_at_epoch < ?" % size, (cutoff,)).fetchone()
            print("would delete %d %s, about %d bytes (%s) of row data"
                  % (rows, which, approx, human(approx)))
        else:
            before = DB.stat().st_size
            while True:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    batch = conn.execute(
                        "DELETE FROM tool_uses WHERE rowid IN (SELECT rowid FROM tool_uses"
                        " WHERE created_at_epoch < ? LIMIT ?)", (cutoff, DELETE_BATCH)).rowcount
                    conn.execute("COMMIT")
                except BaseException:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                    raise
                deleted += batch
                if batch < DELETE_BATCH:
                    break
            print("deleted %d %s" % (deleted, which))
            if auto_vacuum == 2:
                # One incremental_vacuum statement frees one page per step, so
                # each is read to the end. Stop if a step frees nothing.
                free = scalar(conn, "PRAGMA freelist_count")
                while free:
                    conn.execute("PRAGMA incremental_vacuum(%d)" % VACUUM_STEP).fetchall()
                    now_free = scalar(conn, "PRAGMA freelist_count")
                    if now_free >= free:
                        break
                    free = now_free
                # In WAL mode the file shrinks when a checkpoint copies the
                # truncation back; PASSIVE waits for no reader or writer.
                conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchall()
            after = DB.stat().st_size
            print("%s: %d bytes (%s) before, %d bytes (%s) after"
                  % (DB.name, before, human(before), after, human(after)))
        if auto_vacuum != 2:
            print("auto_vacuum is %d, not 2 (INCREMENTAL): the freed pages are reused"
                  " inside the file, not returned to disk" % auto_vacuum)
    except sqlite3.Error as exc:
        if not is_busy(exc):
            sys.exit("claude.sh: pruning tool_uses in %s failed: %s" % (DB, exc))
        print("tool_uses: database locked past %gs -- skipped after deleting %d row(s);"
              " the next run continues" % (busy_timeout, deleted))
    finally:
        if conn is not None:
            conn.close()


prune_tool_uses()

if not APPLY:
    print("re-run with --apply to prune")
elif failed:
    sys.exit("claude.sh: %d log(s) could not be removed" % failed)
PYEOF
}

# `test` is taken before the option loop, not inside it: the loop rejects
# every argument it does not name, so unittest's own flags (-v, a dotted test
# name) could not reach it otherwise. This is the shape the sibling tools use.
if [ "${1:-}" = "test" ]; then
  shift
  exec "${PYTHON:-python3}" -m unittest discover -s "$DIR/tests" -t "$DIR" "$@"
fi

cmd=""
for arg in "$@"; do
  case "$arg" in
    --apply) APPLY=1 ;;
    -h|--help|help) usage; exit 0 ;;
    capture|status|restore|prune-plugin-cache|prune-mem-logs|mem-pro-watchdog) cmd="$arg" ;;
    *) echo "claude.sh: unknown argument: $arg" >&2; usage >&2; exit 1 ;;
  esac
done

if [ -z "$cmd" ]; then
  usage >&2
  exit 1
fi

if [ "$cmd" = "prune-plugin-cache" ]; then
  prune_plugin_cache
elif [ "$cmd" = "prune-mem-logs" ]; then
  prune_mem_logs
elif [ "$cmd" = "mem-pro-watchdog" ]; then
  mem_pro_watchdog
else
  py "$cmd"
fi

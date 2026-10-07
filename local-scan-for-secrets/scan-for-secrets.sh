#!/bin/sh
# scan-for-secrets.sh — scan the CURRENT directory (recursively) for leaked
# tokens/keys. Two passes:
#   1. pattern pass: well-known credential formats (OpenAI, GitHub, AWS, ...).
#      Findings whose matched text is one of YOUR keys (a value exported in
#      ~/.config/shell/env.sh or ~/.bash_profile) are highlighted with the
#      variable name.
#   2. env pass: the literal values of key-like variables exported in
#      the shell env files (KEY/TOKEN/SECRET/PASS/PAT/CRED in the name), so a
#      personal key pasted into some config is found even if its format is
#      not in the pattern list. Values are read at runtime and never printed —
#      matches are reported by variable name, findings masked.
# Uses ripgrep (fast, skips binaries by default) with grep -rEI fallback.
set -eu

usage() {
  cat <<EOF
usage: $(basename "$0") [critical|mask|prune|refresh] [-a|--all] [--apply]
       [--no-prune]
       [--here] [-h|--help]

Scan for leaked tokens/keys. Default: the current directory, recursively.
Findings that are YOUR keys (values exported in ~/.config/shell/env.sh,
or ~/.bash_profile on a machine not split yet) are
highlighted with the owning variable name; known prefixes are labeled
with the provider they identify.

  critical     scan only: dotfiles at the top level of \$HOME, ~/bin and
               ~/repos recursively, and AI-assistant session logs
               (~/.claude/projects, ~/.codex, OpenCode, ~/.gemini,
               Claude Desktop session stores) and agent scratchpad dirs
               (/private/tmp/claude-*/*/scratchpad and the \$TMPDIR
               equivalent — session-local, but a session's mid-task copy
               of a credential file lands there; scratchpad hits are
               reported as TRANSIENT and exit 0, since they self-clear —
               only durable findings exit 2). ~/.codex/shell_snapshots is
               TRANSIENT for the same reason: codex rewrites it on every
               session start, so it is a cache of the exported shell env
               and not a record. Reported, never paged on. ~/.bash_profile itself is
               excluded in this mode (it is the key source, it would
               match itself), as are third-party checkouts and test
               fixtures (see CRITICAL_EXCLUDE_PATHS in this script, plus
               S4S_EXTRA_CRITICAL_EXCLUDE_PATHS in
               <config>/scan-for-secrets/scan-for-secrets.conf;
               rg only — the grep fallback cannot path-exclude).
               File-size cap raised to 20M for the logs.
  mask         replace YOUR key values (shell env exports) with
               <masked:\$VARNAME>, and any string matching the known
               credential patterns with <masked:pattern>, in SAFE files
               only: ~/.viminfo, shell history (~/.bash_history,
               ~/.zsh_history), the AI-assistant session logs listed
               above, and the agent scratchpad dirs. Masking rewrites in
               place on the same inode, so a scratchpad a live session is
               still using keeps working. A file modified in the last
               S4S_MASK_SETTLE_MIN minutes (10), or one that grows while
               it is rewritten, is skipped and listed as busy; the next
               run masks it. Also prunes AI session log
               files past their directory's retention (30 days, 7 for the
               codex shell-snapshot cache)
               and removes derived build output dirs under ~/repos
               (.build/.next/.nuxt/.output — rebuilt on demand, and a
               classic place for baked-in secrets). Live auth stores
               inside those dirs (auth.json, settings.json, config.toml,
               oauth_creds.json, ...) are never touched. Dry run by
               default — prints per-file occurrence counts and the prune
               tally; add --apply to rewrite/delete for real.
               --no-prune does the masking only, leaving the session logs
               and build dirs alone. Masking is reversible in principle
               (the values are still in the shell env file); the deletions
               are not, so they are worth being able to decline.
  prune        the half of mask that is safe to run unattended: delete
               AI session log files past their directory's retention and
               nothing else. ~/.claude/projects is not among them: Claude
               Code deletes its own transcripts on its own schedule.
               No masking (it rewrites files a live session may still be
               appending to) and no build dirs (removing a .next
               mid-workday is not maintenance). Dry run by default; add
               --apply to delete. Run nightly by local-maintenance.
  refresh      the inverse of mask: push the CURRENT shell env
               values into the files that are ALLOWED to hold keys
               (.env, .env.*, *.env, .envrc) under ~/repos, plus the
               named JSON configs of launched services (codexbar, Zed,
               opencode, Claude Desktop — REFRESH_JSON_TARGETS in this
               script), so a rotated key propagates in one step. In JSON,
               a key named after the variable is matched automatically
               and anything else needs a REFRESH_JSON_MAP path; the swap
               is byte-level, so formatting and JSONC comments survive.
               A name exported twice is reported with the last 4 chars of
               each value and the last one wins; a name commented out in
               ~/.bash_profile is disabled on purpose and never written.
               Only existing assignments of
               a matching variable name are rewritten — refresh never
               adds a variable and never creates a file. Skipped:
               git-tracked files (writing a live key into something that
               gets committed is the leak this tool hunts for),
               templates (.env.example/.sample/.template/.dist), values
               that are \${...} references or placeholders, and
               commented-out lines. A few file-side spellings map to a
               profile variable (GOOGLE_API_KEY -> GEMINI_API_KEY, ...);
               see REFRESH_ALIASES in this script. Names listed in
               REFRESH_GH_SECRETS are also pushed as GitHub Actions repo
               secrets (gh secret set) to every repo under ~/repos whose
               workflows reference secrets.<NAME>. GitHub never lets a
               secret be read back, so a local salted hash of the last
               push (.gh-secrets-state, gitignored) decides current vs
               changed. Dry run by default —
               reports per file which variables are stale, by name only,
               never showing a value; add --apply to write.
  --apply      (mask and refresh modes) actually rewrite files; without
               it both are a dry run
  --here       (refresh mode) work on the current directory instead of
               ~/repos
  -a, --all    also scan local secrets files (.env, *.env, .envrc,
               .netrc, .npmrc, .pypirc, credentials.json,
               service-account*.json, id_rsa, *.pem, *.key), which are
               skipped by default because they are the sanctioned place
               for local secrets
  -h, --help   this help

exit 0 = clean (or, in critical mode, transient scratchpad findings only)
exit 2 = durable findings (matches are masked; open the file to inspect)
EOF
}

SCAN_SECRETS_FILES=0
MODE=cwd
APPLY=0
HERE=0
NO_PRUNE=0
for arg in "$@"; do
  case "$arg" in
    critical) MODE=critical ;;
    mask) MODE=mask ;;
    prune) MODE=prune ;;
    refresh) MODE=refresh ;;
    --apply) APPLY=1 ;;
    -n|--dry-run) APPLY=0 ;;
    --here) HERE=1 ;;
    --no-prune) NO_PRUNE=1 ;;
    -a|--all) SCAN_SECRETS_FILES=1 ;;
    -h|--help|help) usage; exit 0 ;;
    *) usage >&2; exit 1 ;;
  esac
done
# Resolve the script dir BEFORE the cd below: critical/mask/prune move to
# $HOME, after which a relative $0 no longer resolves (GH_STATE and ../lib
# live next to us). Walk a symlink (e.g. from ~/bin) to the real script
# first, or the siblings are looked for next to the link.
S4S_SELF="$0"
while [ -L "$S4S_SELF" ]; do
  s4s_link=$(readlink "$S4S_SELF")
  case "$s4s_link" in
    /*) S4S_SELF="$s4s_link" ;;
    *)  S4S_SELF="$(dirname "$S4S_SELF")/$s4s_link" ;;
  esac
done
DIR="$(cd "$(dirname "$S4S_SELF")" && pwd)"
case "$MODE" in critical|mask|prune) cd "$HOME" ;; esac

# scan targets: empty in cwd mode (rg/grep default to .), explicit list in
# critical mode — top-level dotfiles (regular files only), bin/ and repos/
# recursively, plus AI-assistant session logs (Claude Code/Desktop, Codex,
# OpenCode, Gemini) which accumulate pasted keys and tool output.
# Tool config DIRECTORIES under $HOME that hold real credentials. Both halves
# of this script used to miss them: critical mode's loop below takes regular
# files only (`[ -f "$f" ]`), so a dot-directory was skipped outright, and
# refresh globs under ~/repos, which these are not in. ~/.prism/.env and
# ~/.gito/.env each held a live 164-character key that neither mode could see.
# Listed as directories, not filenames, so any .env/.envrc that appears in one
# later is picked up without editing this list again.
# .google_workspace_mcp holds one OAuth token file per Google account, and is
# listed here TOGETHER WITH a SECRETS_GLOBS entry for
# **/.google_workspace_mcp/credentials/*.json. The pairing is the point: the
# live per-account files are the sanctioned place for those tokens and would
# otherwise be reported every Monday with nothing to action, while anything
# else in that directory is not sanctioned and is exactly what wants finding —
# a .bak or a hand-renamed leftover is a live refresh token nothing rotates.
# Note the glob is a PATH, so it only takes effect under rg; the grep fallback
# matches basenames only. Same limitation as **/.auth/user.json above it.
HOME_CONFIG_DIRS='.prism
.gito
.google_workspace_mcp'

AI_LOG_DIRS='.claude/projects
.codex
.local/share/opencode
.gemini
Library/Application Support/Claude/local-agent-mode-sessions
Library/Application Support/Claude/claude-code-sessions'

# Agent scratchpad dirs. Not session logs, and not under $HOME: these hold
# whatever a session copied aside mid-task, which in practice means backups of
# ~/.bash_profile and ~/.config/shell/env.sh. Eight such files sat here with
# live keys in them — one with 46 provider-prefixed values — while every other
# store this script reads reported clean, because nothing scanned /private/tmp.
# Emitted absolute, so they stay out of the $HOME-relative lists above.
# Trailing `:` for the same set -e reason documented at the MASK_TARGETS
# assembly: a glob that matches nothing leaves the loop exiting non-zero.
# find, not a fixed-depth glob: the layout is
# <root>/claude-<uid>/<project>/<session>/scratchpad, and a glob written one
# level short matches nothing and reports clean, which is the exact failure
# this function exists to end.
# S4S_SCRATCH_ROOTS (one root per line) replaces the two roots, so a test
# reaches only its own fixture and never a live session's scratchpad.
scratch_roots() {
  if [ -n "${S4S_SCRATCH_ROOTS:-}" ]; then
    printf '%s\n' "$S4S_SCRATCH_ROOTS"
  else
    printf '%s\n' "${TMPDIR:-/tmp}" /private/tmp
  fi
}
agent_scratch_dirs() {
  scratch_roots | while IFS= read -r root; do
    [ -d "$root" ] || continue
    # `|| :` is load-bearing: find exits non-zero when it cannot read a
    # subdirectory (routine under $TMPDIR), the loop body runs inside the
    # pipeline's subshell, and set -e then kills that subshell before
    # /private/tmp is ever reached — silently returning no dirs at all.
    find "$root" -maxdepth 4 -type d -name scratchpad -path '*/claude-*' 2>/dev/null || :
  done | sort -u
  :
}

targets() {
  [ "$MODE" = critical ] || return 0
  for f in .*; do
    [ -f "$f" ] || continue
    [ "$f" = ".bash_profile" ] && continue
    printf '%s\n' "$f"
  done
  [ -d bin ]   && printf 'bin\n'
  [ -d repos ] && printf 'repos\n'
  printf '%s\n' "$HOME_CONFIG_DIRS" | while IFS= read -r d; do
    [ -d "$d" ] && printf '%s\n' "$d"
  done
  printf '%s\n' "$AI_LOG_DIRS" | while IFS= read -r d; do
    [ -d "$d" ] && printf '%s\n' "$d"
  done
  agent_scratch_dirs
  return 0
}

MAX_SIZE="1M"
# session-log transcripts routinely exceed 1M; raise the cap in critical mode
[ "$MODE" = critical ] && MAX_SIZE="20M"
EXCLUDE_DIRS=".git node_modules .venv venv __pycache__ .cache .npm .cargo target dist build .build .next .nuxt .output coverage storage volumes falkordb_data mcp_logs logs OLD .Trash"
# Sanctioned local secrets files, skipped unless -a/--all. Explicit variants
# instead of a `.env.*` wildcard so additions stay deliberate.
# .codex-global-state.json.bak is listed next to the file it backs up for the
# same reason it is in MASK_EXCLUDE_PATHS: codex rewrites it on every state
# save, the glob for the original does not match it, and without the entry the
# weekly critical scan reports the same JWT every Monday on a file that is by
# definition a copy of one already sanctioned. Deleting it does not help — it
# is back within the hour. An alert nothing can action is the failure mode this
# repo already killed once, in doctor.
SECRETS_GLOBS=".env .env.local .env.development .env.staging .env.production .env.example .env.production.example *.env .envrc .netrc .npmrc .pypirc credentials.json service-account*.json id_rsa id_ed25519 *_ed25519 *.pem *.key auth.json mcp-auth.json oauth_creds.json **/.google_workspace_mcp/credentials/*.json .codex-global-state.json .codex-global-state.json.bak **/.auth/user.json"
# Third-party checkouts and test fixtures under ~/repos, skipped in critical
# mode only — they are full of sample keys and other people's material.
# Rule for adding: not your remote AND no local files of yours inside.
CRITICAL_EXCLUDE_PATHS="
.gemini/extensions
.codex/.tmp
.codex/tool-venvs
.codex/plugins
.codex/vendor_imports
.codex/process_manager
repos/platypeeps/sd-ai-command-pack/tests
"
# Per-user additions live in <config>/scan-for-secrets/scan-for-secrets.conf
# (<config> is $SYSTEM_TOOLS_CONFIG, default ~/.config/system; copy
# scan-for-secrets.conf.example there), or the file S4S_CONF names.
# It is sourced as shell and may set:
#   S4S_EXTRA_SECRETS_GLOBS         more sanctioned secrets files (-a scans them)
#   S4S_EXTRA_CRITICAL_EXCLUDE_PATHS more third-party trees, one per line,
#                                   relative to $HOME, skipped in critical mode
# Missing is fine: the generic defaults above apply.
. "$DIR/../lib/config.sh"
S4S_CONF="${S4S_CONF:-$(st_config_dir scan-for-secrets)/scan-for-secrets.conf}"
S4S_EXTRA_SECRETS_GLOBS=""
S4S_EXTRA_CRITICAL_EXCLUDE_PATHS=""
if [ -f "$S4S_CONF" ]; then
  # shellcheck disable=SC1090
  . "$S4S_CONF"
fi
[ -z "$S4S_EXTRA_SECRETS_GLOBS" ] || SECRETS_GLOBS="$SECRETS_GLOBS $S4S_EXTRA_SECRETS_GLOBS"
[ -z "$S4S_EXTRA_CRITICAL_EXCLUDE_PATHS" ] || CRITICAL_EXCLUDE_PATHS="$CRITICAL_EXCLUDE_PATHS
$S4S_EXTRA_CRITICAL_EXCLUDE_PATHS
"
# Skip obviously non-config payloads even when small.
EXCLUDE_GLOBS="*.png *.jpg *.jpeg *.gif *.webp *.ico *.pdf *.zip *.tar *.gz *.tgz *.bz2 *.xz *.7z *.dmg *.iso *.img *.sqlite *.db *.bin *.exe *.so *.dylib *.a *.o *.pyc *.class *.jar *.woff *.woff2 *.ttf *.mp3 *.mp4 *.mov *.parquet *.pack *.idx *.lock package-lock.json yarn.lock Cargo.lock uv.lock poetry.lock"

# Well-known credential formats. Case-sensitive on purpose — prefixes are exact.
#
# The last entry is the exception, and it exists because everything above it is
# a *known vendor prefix*: a bare base64 secret under a header name matches none
# of them. A live vendor pipeline key once sat in a tracked config.yaml for
# exactly that reason. It requires base64
# evidence — padding, or a '+' inside the value — because length alone matches
# CamelCase identifiers like `OfflineContinuationAuthorization;` and prose
# containing a slash. Measured across ~/repos: 0 false positives with the
# evidence requirement, 2 without it. Deliberately narrow to `Authorization`:
# widening it to api_key/token/secret matched 11 vendored test fixtures, and
# PATTERNS also drives `mask`, which rewrites files in place.
#
# The URL entry leaves `[` and `]` out of the user and password: RFC 3986
# allows neither in userinfo, and a grammar line such as
# `scheme://[user[:password]@]host` matched as a credential (sd:2591).
PATTERNS='sk-proj-[A-Za-z0-9_-]{20,}
sk-ant-[A-Za-z0-9_-]{20,}
sk-[A-Za-z0-9]{40,}
github_pat_[A-Za-z0-9_]{20,}
gh[pousr]_[A-Za-z0-9]{36,}
glpat-[A-Za-z0-9_-]{20,}
AKIA[0-9A-Z]{16}
ASIA[0-9A-Z]{16}
AIza[0-9A-Za-z_-]{35}
ya29\.[0-9A-Za-z_-]{30,}
xox[baprs]-[0-9A-Za-z-]{10,}
xapp-[0-9]-[0-9A-Za-z-]{10,}
hf_[A-Za-z0-9]{30,}
npm_[A-Za-z0-9]{36}
sktsec_[A-Za-z0-9_-]{20,}
sts_[0-9a-f]{20,}
r8_[A-Za-z0-9]{30,}
pk_live_[0-9A-Za-z]{20,}
sk_live_[0-9A-Za-z]{20,}
sq0atp-[0-9A-Za-z_-]{20,}
SG\.[0-9A-Za-z_-]{20,}\.[0-9A-Za-z_-]{20,}
eyJ[A-Za-z0-9_-]{20,}\.eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}
-----BEGIN (RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY( BLOCK)?-----
[a-z][a-z0-9+.-]*://[^]/\s:@"\\\[]{3,}:[^]/\s:@"\\\[]{8,}@
[Aa]uthorization[^A-Za-z0-9]{1,4}([Bb]earer[^A-Za-z0-9]{1,3})?[A-Za-z0-9+/]{24,}(=|[+][A-Za-z0-9+/]{2,}={0,2})'

command -v rg >/dev/null 2>&1 && HAVE_RG=1 || HAVE_RG=0

# PATTERNS for `grep -E`, the fallback without ripgrep. POSIX ERE has no `\s`:
# inside a bracket it is a backslash and an `s`, so the URL entry missed every
# password holding an `s`. Each `\s` in PATTERNS sits in a bracket, where
# `[:space:]` is the POSIX spelling (sd:2591).
ere_patterns() {
  printf '%s\n' "$PATTERNS" | sed 's/\\s/[:space:]/g'
}
[ -t 1 ] && S4S_TTY=1 || S4S_TTY=0
export S4S_TTY

# --- your keys: name=value pairs of key-like exports in the shell env ------
# Values stay in process memory only (they are already in your shell env);
# they are never written to disk or printed.
#
# Two sources, both read: the exports moved out of ~/.bash_profile into
# ~/.config/shell/env.sh so LaunchAgents could load them too, and a machine
# that has not been split yet still keeps them in the profile. Reading only
# the profile silently found nothing after the move — every "YOURS" label,
# every mask and the whole of refresh quietly became a no-op — so this reads
# both and a missing file is simply skipped.
PROFILE="$HOME/.bash_profile"
SHELL_ENV="$HOME/.config/shell/env.sh"
S4S_KEY_SOURCES=""
for _src in "$SHELL_ENV" "$PROFILE"; do
  [ -f "$_src" ] && S4S_KEY_SOURCES="$S4S_KEY_SOURCES $_src"
done
S4S_PAIRS=""
if [ -n "$S4S_KEY_SOURCES" ]; then
  # shellcheck disable=SC2086 # deliberate word-splitting of the source list
  S4S_PAIRS=$(
    sed -n 's/^[[:space:]]*export \([A-Za-z_][A-Za-z0-9_]*\)=\(.*\)$/\1=\2/p' $S4S_KEY_SOURCES \
    | grep -E '^[A-Za-z0-9_]*(KEY|TOKEN|SECRET|PASS|PAT|CRED)[A-Za-z0-9_]*=' \
    | sort -u \
    | while IFS= read -r line; do
        name=${line%%=*}
        val=${line#*=}
        case $val in
          (\"*\") val=${val#\"}; val=${val%\"} ;;
          (\'*\') val=${val#\'}; val=${val%\'} ;;
          (*) val=${val%% *} ;;
        esac
        [ ${#val} -lt 12 ] && continue
        case $val in (*\$*) continue ;; (change-me*|xxx*|TODO*) continue ;; esac
        printf '%s=%s\n' "$name" "$val"
      done
  )
fi
export S4S_PAIRS

# --- mask mode: rewrite YOUR key values in safe files ----------------------
# Only files that are pure history/logs are touched: ~/.viminfo and the AI
# session log dirs. Live credential stores inside those dirs are excluded —
# masking them would break the tools that read them.
MASK_EXCLUDE_GLOBS="auth.json mcp-auth.json oauth_creds.json google_accounts.json settings.json config.toml config.json credentials.json .credentials.json"
# Vendored and derived trees that can appear anywhere under a mask target —
# most often a scratchpad a session ran `npm install` or `python -m venv` in.
# rg_args applies EXCLUDE_DIRS to the critical scan, but mask walks its own
# target list and applied nothing, so mask would rewrite a file inside
# node_modules in place. Deliberately NOT the whole of EXCLUDE_DIRS: that list
# also carries logs, storage, mcp_logs, OLD and .Trash, and a credential
# sitting in one of those is precisely what mask exists to neutralise. Only
# third-party code, build output, and .git — where a rewrite corrupts objects —
# belong here.
MASK_EXCLUDE_DIRS=".git node_modules .venv venv __pycache__ .npm .cargo target dist build .build .next .nuxt .output coverage"
# Installed code, plugin docs, and live tool state (not session data)
# living under the AI dirs — never mask.
# The .bak is listed alongside the live file on purpose: codex rewrites it on
# every state save, and the rg glob for the original does not match it — so
# mask would have corrupted the backup while carefully sparing the file it
# backs up. Deleting it is not a fix either; it comes straight back.
MASK_EXCLUDE_PATHS=".gemini/extensions .codex/plugins .codex/.tmp .codex/tool-venvs .codex/vendor_imports .codex/process_manager .codex/.codex-global-state.json .codex/.codex-global-state.json.bak"
if [ "$MODE" = mask ] || [ "$MODE" = prune ]; then
  # No exports is not a failure: the known credential patterns need none, so
  # mask still masks them, and a machine with no key-like exports is not left
  # holding every pattern hit (sd:1254).
  if [ "$MODE" = mask ] && [ -z "$S4S_PAIRS" ]; then
    echo "no key-like exports found in ~/.config/shell/env.sh or ~/.bash_profile; masking known patterns only" >&2
  fi
  MASK_TARGETS=""
  # if, not `[ -f "$f" ] && MASK_TARGETS=...`: the AND-list is the last
  # statement in the loop body, so on a machine with none of these three files
  # the loop exits 1 and set -e kills the script silently. It never showed
  # because this machine has all three — a scratch $HOME with only session
  # logs in it does not.
  for f in .viminfo .bash_history .zsh_history; do
    if [ -f "$f" ]; then
      MASK_TARGETS="$MASK_TARGETS
$f"
    fi
  done
  # Same shape as the loop above, and here it bites harder: set -e is
  # inherited by the command substitution's subshell, so a final iteration
  # whose directory is absent kills the subshell before the trailing `:` can
  # set the status back to 0, and the assignment takes the whole script down.
  MASK_TARGETS="$MASK_TARGETS$(printf '\n%s' "$AI_LOG_DIRS" | while IFS= read -r d; do
    if [ -d "$d" ]; then printf '\n%s' "$d"; fi; done; :)"
  MASK_TARGETS="$MASK_TARGETS$(agent_scratch_dirs | while IFS= read -r d; do
    printf '\n%s' "$d"; done; :)"
  if [ "$MODE" = mask ]; then
    echo "mask targets: ~/.viminfo + shell history + AI session logs + agent scratchpads (live auth stores excluded)"
  fi

  # Prune AI session logs past their retention. `<dir>|<days>`, one line per
  # directory that holds nothing but session data — never the tool roots
  # (.codex, .gemini, ...), which also contain auth and config.
  #
  # The window is per directory because two of these are not the same kind of
  # thing. A session transcript is a record and 30 days of them is a
  # reasonable archive; .codex/shell_snapshots is a cache of the exported
  # shell environment that codex rewrites on every session start, so a
  # 30-day window never reaches a single file (measured 2026-09-21: 6 files,
  # newest minutes old, oldest two days) and the directory was effectively
  # unpruned while appearing in the list.
  #
  # .claude/projects is deliberately absent. Claude Code deletes its own
  # transcripts on its own schedule, `cleanupPeriodDays` in
  # ~/.claude/settings.json, and two retention owners on one directory means
  # the shorter one silently wins and the setting reads as if it did nothing.
  # Deleting Claude Code's transcripts out from under it is the wrong
  # mechanism even when the window agrees (sd:1254).
  PRUNE_DIRS='.codex/sessions|30
.codex/archived_sessions|30
.codex/shell_snapshots|7
.local/share/opencode/storage|30
.gemini/tmp|30
Library/Application Support/Claude/local-agent-mode-sessions|30
Library/Application Support/Claude/claude-code-sessions|30'
  # Set before the --no-prune guard, not inside its else: the mask exit-code
  # paths below read PRUNE_FOUND unconditionally, and under set -u an unset
  # one aborted `mask --no-prune` the moment there was nothing left to mask —
  # exactly the run that confirms a sweep is clean.
  PRUNE_FOUND=0
  if [ "$NO_PRUNE" = 1 ]; then
    echo "== prune: skipped (--no-prune)"
  else
  echo "== prune: AI session log files past their directory's retention"
  # Two lists, built in one pass: OLD is the files to delete, and the
  # positional parameters are the directories, kept for the empty-directory
  # sweep after the deletion. The find is per directory because the window
  # is, and it is `-type f` with no -L on purpose: find does not descend a
  # symlinked directory unless asked, so a link planted under a session dir
  # cannot walk the deletion out of the tree it was pointed at.
  OLD=""
  set --
  while IFS= read -r spec; do
    [ -n "$spec" ] || continue
    d=${spec%|*}
    days=${spec##*|}
    [ -d "$d" ] || continue
    set -- "$@" "$d"
    HIT=$(find "$d" -type f -mtime "+$days" 2>/dev/null || true)
    # if, not an AND-list: the last statement in a while body is subject to
    # set -e, and a directory with nothing past retention would end the body
    # false and take the script down — the same trap documented at the
    # MASK_TARGETS assembly above.
    if [ -n "$HIT" ]; then
      OLD="$OLD$HIT
"
    fi
  done <<PRUNE_EOF
$PRUNE_DIRS
PRUNE_EOF
  OLD=$(printf '%s' "$OLD" | sed '/^$/d')
  if [ $# -gt 0 ]; then
    if [ -n "$OLD" ]; then
      PRUNE_FOUND=1
      N=$(printf '%s\n' "$OLD" | wc -l | tr -d ' ')
      SZ=$(printf '%s\n' "$OLD" | tr '\n' '\0' | xargs -0 du -ch 2>/dev/null | tail -1 | awk '{print $1}')
      if [ "$APPLY" = 1 ]; then
        printf '%s\n' "$OLD" | tr '\n' '\0' | xargs -0 rm -f
        find "$@" -type d -mindepth 1 -empty -delete 2>/dev/null || true
        echo "  pruned $N file(s), $SZ"
      else
        echo "  would prune $N file(s), $SZ (dry run; add --apply)"
      fi
    else
      echo "  nothing past retention"
    fi
  else
    echo "  no prune dirs present"
  fi
  fi

  # prune stops here: it is the half of mask that is safe to run unattended.
  # No masking (it rewrites files a session may still be appending to) and no
  # build dirs (removing a .next mid-workday is not maintenance).
  if [ "$MODE" = prune ]; then
    exit 0
  fi

  # Derived build output under ~/repos — regenerated by the next build, and
  # a classic place for secrets to get baked into bundles and run logs.
  # Only unambiguous build-output names; deliberately NOT plain "out".
  if [ "$NO_PRUNE" = 1 ]; then
    echo "== derived build dirs: skipped (--no-prune)"
  else
  echo "== derived build dirs under ~/repos (.build/.next/.nuxt/.output)"
  BUILD_DIRS=""
  [ -d repos ] && BUILD_DIRS=$(find repos \( -name node_modules -o -name .git \) -prune \
    -o -type d \( -name .build -o -name .next -o -name .nuxt -o -name .output \) -prune -print 2>/dev/null || true)
  if [ -n "$BUILD_DIRS" ]; then
    PRUNE_FOUND=1
    if [ "$APPLY" = 1 ]; then
      printf '%s\n' "$BUILD_DIRS" | while IFS= read -r d; do
        [ -n "$d" ] && rm -rf "$d" && echo "  removed $d"
      done
    else
      printf '%s\n' "$BUILD_DIRS" | while IFS= read -r d; do
        [ -n "$d" ] && echo "  would remove $d ($(du -sh "$d" 2>/dev/null | awk '{print $1}'))"
      done
      echo "  (dry run; add --apply)"
    fi
  else
    echo "  none found"
  fi
  fi
  echo "== mask: your key values + known credential patterns"
  VALS=$(printf '%s\n' "$S4S_PAIRS" | sed 's/^[^=]*=//')
  mask_file_list() { # $1 = fixed|regex; pattern list on stdin (fd 0)
    # Keep the caller's argument before the first set --: that call replaces
    # the positional parameters, so a later "$1" is the first rg flag, not
    # fixed|regex. A past bug — -F never reached rg, and literal key values
    # containing regex metacharacters (+ is common in base64) went unmasked.
    MATCH_MODE=$1
    if [ "$HAVE_RG" = 1 ]; then
      set -- --no-config --no-ignore --hidden -l -f -
      [ "$MATCH_MODE" = fixed ] && set -- "$@" -F
      for d in $MASK_EXCLUDE_DIRS;  do set -- "$@" -g "!$d/"; done
      for g in $EXCLUDE_GLOBS;      do set -- "$@" -g "!$g"; done
      for g in $MASK_EXCLUDE_GLOBS; do set -- "$@" -g "!$g"; done
      for p in $MASK_EXCLUDE_PATHS; do set -- "$@" -g "!$p" -g "!$p/**"; done
    else
      if [ "$MATCH_MODE" = fixed ]; then set -- -rIlF -f -; else set -- -rIlE -f -; fi
      for d in $MASK_EXCLUDE_DIRS;  do set -- "$@" "--exclude-dir=$d"; done
      for g in $EXCLUDE_GLOBS;      do set -- "$@" "--exclude=$g"; done
      for g in $MASK_EXCLUDE_GLOBS; do set -- "$@" "--exclude=$g"; done
    fi
    # targets read on fd 3 — stdin stays reserved for the pattern list
    MASK_TARGET_COUNT=0
    while IFS= read -r t <&3; do
      if [ -n "$t" ]; then set -- "$@" "$t"; MASK_TARGET_COUNT=$((MASK_TARGET_COUNT + 1)); fi
    done 3<<TARGETS_EOF
$MASK_TARGETS
TARGETS_EOF
    # No target, no search: rg and grep -r given no path search the current
    # directory, which is $HOME here, and mask would rewrite files far outside
    # its safe list, ~/repos among them (review, sd:1254).
    [ "$MASK_TARGET_COUNT" -gt 0 ] || return 0
    if [ "$HAVE_RG" = 1 ]; then rg "$@" 2>/dev/null || true
    else grep "$@" 2>/dev/null || true; fi
  }
  # No values, no literal pass: an empty line is an empty pattern, and an
  # empty pattern matches every file.
  FILES=""
  if [ -n "$VALS" ]; then FILES=$(printf '%s\n' "$VALS" | mask_file_list fixed); fi
  if [ "$HAVE_RG" = 1 ]; then PFILES=$(printf '%s\n' "$PATTERNS" | mask_file_list regex)
  else PFILES=$(ere_patterns | mask_file_list regex); fi
  ALLFILES=$(printf '%s\n%s\n' "$FILES" "$PFILES" | awk 'NF' | sort -u)
  if [ -z "$ALLFILES" ]; then
    echo "nothing to mask — no target file contains your keys or known patterns"
    [ "$APPLY" = 0 ] && [ "$PRUNE_FOUND" = 1 ] && exit 2
    exit 0
  fi
  # The rewrite lives in mask_files.py, which says how it masks and why it
  # rewrites in place. Values reach python only via the environment, never
  # argv or disk.
  printf '%s\n' "$ALLFILES" | S4S_APPLY="$APPLY" S4S_PATTERNS="$PATTERNS" \
    python3 "$DIR/mask_files.py" || MASK_RC=$?
  MASK_RC=${MASK_RC:-0}
  [ "$MASK_RC" != 0 ] && [ "$MASK_RC" != 2 ] && exit "$MASK_RC"
  if [ "$APPLY" = 0 ] && { [ "$MASK_RC" = 2 ] || [ "$PRUNE_FOUND" = 1 ]; }; then
    exit 2
  fi
  exit 0
fi

# --- refresh mode: push ~/.bash_profile values into sanctioned env files ---
# The inverse of mask: instead of scrubbing keys out of files that should not
# have them, this rewrites the keys in files that are ALLOWED to hold them
# (.env and friends) so a rotated key in ~/.bash_profile propagates in one
# step. Only existing assignments are updated — refresh never adds a variable
# and never creates a file.
REFRESH_NAME_GLOBS=".env .env.* *.env .envrc"
# Templates and committed samples: never written, they are documentation.
REFRESH_SKIP_RE='\.(example|sample|template|dist|defaults|bak.*|old.*|orig)$|\.env\.example$'
# file-side name -> ~/.bash_profile name, for projects that spell a key
# differently. Add sparingly, and only when it is the same credential.
# ANTHROPIC_API_KEY is deliberately NOT aliased here: ~/.bash_profile keeps it
# commented out and unsets it so Claude falls back to its own auth. Any name
# commented out in the profile is treated as disabled, see REFRESH_DISABLED.
REFRESH_ALIASES='GOOGLE_API_KEY=GEMINI_API_KEY
GOOGLE_GENAI_API_KEY=GEMINI_API_KEY'
# JSON configs of launched services that hold the same keys hard-coded. Not
# found by globbing — a rotation has to reach these too, so they are named
# explicitly. Missing files are skipped silently, ~ is expanded.
# A symlink to one of these files needs no entry of its own.
REFRESH_JSON_TARGETS='~/.config/codexbar/config.json
~/.config/zed/settings.json
~/.config/opencode/opencode.json
~/Library/Application Support/Claude/claude_desktop_config.json
~/.claude.json'
# Slots whose JSON key name does NOT say which credential it holds: dotted
# path = ~/.bash_profile variable. A path is tried against every target file,
# so one entry covers whichever file happens to have that shape. List
# elements are selected by a sibling field, [id=groq], never by index —
# indices shift when the app rewrites its config.
# Where the stored value wraps the key ("Bearer sk-..."), only the last
# whitespace-separated token is replaced.
# Keys whose JSON key name IS the variable name (an "env"/"environment" block)
# need no entry — those are matched automatically.
REFRESH_JSON_MAP='providers[id=minimax].apiKey=MINIMAX_API_KEY
providers[id=openrouter].tokenAccounts.accounts[0].token=OPENROUTER_API_KEY
providers[id=deepinfra].tokenAccounts.accounts[0].token=DEEPINFRA_API_KEY
providers[id=groq].tokenAccounts.accounts[0].token=GROQ_API_KEY
context_servers.mcp-server-github.settings.github_personal_access_token=GITHUB_PERSONAL_ACCESS_TOKEN
mcpServers.ydc-server.env.YDC_AUTH_HEADER=YDC_API_KEY
mcp.ydc-server.headers.Authorization=YDC_API_KEY
mcpServers.github.headers.Authorization=GITHUB_PERSONAL_ACCESS_TOKEN
mcpServers.ydc-server.headers.Authorization=YDC_API_KEY'
# GitHub Actions repo secrets that hold the same credential. GitHub's API is
# write-only for secret values (they can never be read back), so a local
# salted hash of what was last pushed (in .gh-secrets-state next to this
# script, mode 600, gitignored) stands in for the comparison: hash matches ->
# current, else each name is (re-)pushed with `gh secret set` to every repo
# under the scope whose workflows reference secrets.<NAME>.
REFRESH_GH_SECRETS='SOCKET_SECURITY_API_KEY'
GH_STATE="$DIR/.gh-secrets-state"

if [ "$MODE" = refresh ]; then
  # Re-read ~/.bash_profile in FILE ORDER. S4S_PAIRS is sort -u'd, which is
  # fine for searching but wrong for writing: with a name exported twice, the
  # last line of the file wins in the shell, and "last after sorting" is a
  # different line. Order matters here, so this pass keeps it.
  # Both key sources, env.sh first: that is the order a shell sees them, since
  # ~/.bash_profile sources env.sh before its own remaining lines, and "last
  # export of a name wins" below depends on that order being right.
  profile_seq() {
    # shellcheck disable=SC2086 # deliberate word-splitting of the source list
    sed -n 's/^[[:space:]]*export \([A-Za-z_][A-Za-z0-9_]*\)=\(.*\)$/\1=\2/p' $S4S_KEY_SOURCES \
    | grep -E '^[A-Za-z0-9_]*(KEY|TOKEN|SECRET|PASS|PAT|CRED)[A-Za-z0-9_]*=' \
    | grep -vE '^[A-Za-z0-9_]*PATH=' \
    | while IFS= read -r line; do
        name=${line%%=*}
        val=${line#*=}
        case $val in
          (\"*\") val=${val#\"}; val=${val%%\"*} ;;
          (\'*\') val=${val#\'}; val=${val%%\'*} ;;
          (*) val=${val%% *} ;;
        esac
        [ ${#val} -lt 12 ] && continue
        case $val in (*\$*) continue ;; (change-me*|xxx*|TODO*) continue ;; esac
        printf '%s=%s\n' "$name" "$val"
      done
  }
  # A key commented out in ~/.bash_profile is disabled on purpose ("should not
  # be used" — e.g. ANTHROPIC_API_KEY, which the profile also unsets so the
  # tools fall back to their own auth). Refresh must not write such a name
  # anywhere, and must not alias onto it either.
  SEQ=$(profile_seq)
  REFRESH_DISABLED=$(
    sed -n 's/^[[:space:]]*#[[:space:]]*export[[:space:]][[:space:]]*\([A-Za-z_][A-Za-z0-9_]*\)=.*/\1/p' \
      $S4S_KEY_SOURCES 2>/dev/null \
    | grep -E '^[A-Za-z0-9_]*(KEY|TOKEN|SECRET|PASS|PAT|CRED)[A-Za-z0-9_]*$' \
    | grep -vE '^[A-Za-z0-9_]*PATH$' \
    | sort -u \
    | while IFS= read -r n; do
        # Commented out somewhere AND live somewhere else is not disabled —
        # only a name with no active export at all counts.
        printf '%s\n' "$SEQ" | grep -q "^$n=" || printf '%s\n' "$n"
      done
  )
  # Last export of a name wins, matching what the shell ends up with.
  S4S_REFRESH_PAIRS=$(
    printf '%s\n' "$SEQ" | awk -F= 'NF { v=$0; sub(/^[^=]*=/, "", v)
        if (!(($1) in order)) order[$1]=NR
        last[$1]=v }
      END { for (n in last) print order[n] "\t" n "=" last[n] }' | sort -n | cut -f2-
  )
  if [ -n "$REFRESH_DISABLED" ]; then
    S4S_REFRESH_PAIRS=$(printf '%s\n' "$S4S_REFRESH_PAIRS" | grep -vE "^($(printf '%s' "$REFRESH_DISABLED" | tr '\n' '|' | sed 's/|$//'))=" || true)
  fi
  if [ -z "$S4S_REFRESH_PAIRS" ]; then
    echo "no key-like exports found in ~/.config/shell/env.sh or ~/.bash_profile, nothing to refresh" >&2
    exit 1
  fi
  export S4S_REFRESH_PAIRS
  export S4S_REFRESH_DISABLED="$REFRESH_DISABLED"
  export S4S_REFRESH_ALIASES="$REFRESH_ALIASES"
  export S4S_REFRESH_JSON_MAP="$REFRESH_JSON_MAP"

  if [ "$HERE" = 1 ]; then SCOPE="$PWD"; else SCOPE="$HOME/repos"; fi
  [ -d "$SCOPE" ] || { echo "refresh scope $SCOPE does not exist" >&2; exit 1; }
  if [ "$HERE" = 1 ]; then
    echo "refresh scope: $SCOPE (env-style files only; the named JSON configs are out of scope with --here)"
  else
    echo "refresh scope: $SCOPE + the named service JSON configs (templates and git-tracked files skipped)"
  fi
  # Names exported more than once — typically an if/else branch on $USER, so
  # "the last line wins" is not obviously the one you meant. Show the last 4
  # characters of every value in file order, and of the one being used, so the
  # right branch can be told apart at a glance without opening the profile.
  DUPES=$(printf '%s\n' "$SEQ" | awk -F= 'NF { c[$1]++ } END { for (n in c) if (c[n] > 1) print n }' | sort)
  for d in $DUPES; do
    SEEN=$(printf '%s\n' "$SEQ" | awk -F= -v n="$d" 'NF && $1 == n {
             v = $0; sub(/^[^=]*=/, "", v); printf "%s…%s", (c++ ? ", " : ""), substr(v, length(v) - 3) }')
    USING=$(printf '%s\n' "$S4S_REFRESH_PAIRS" | awk -F= -v n="$d" '$1 == n {
             v = $0; sub(/^[^=]*=/, "", v); print substr(v, length(v) - 3) }')
    if [ -n "$USING" ]; then
      echo "  note: \$$d is exported more than once in ~/.bash_profile ($SEEN) — using the last, …$USING"
    else
      echo "  note: \$$d is exported more than once in ~/.bash_profile ($SEEN) — but it is commented out, so it is not used"
    fi
  done
  # Deliberately disabled keys, so it is obvious why a slot was left untouched.
  for d in $REFRESH_DISABLED; do
    echo "  note: \$$d is commented out in ~/.bash_profile — refresh will not write it anywhere"
  done
  # The live shell can disagree with the file when a conditional branch picked
  # a different value, or the profile was edited without re-sourcing. The file
  # is the source of truth; say so rather than silently preferring either.
  printf '%s\n' "$S4S_REFRESH_PAIRS" | while IFS= read -r pair; do
    [ -n "$pair" ] || continue
    n=${pair%%=*}; fv=${pair#*=}
    eval "lv=\${$n:-}"
    if [ -n "$lv" ] && [ "$lv" != "$fv" ]; then
      echo "  note: live \$$n in this shell ends …$(printf '%s' "$lv" | tail -c 4) but ~/.bash_profile ends …$(printf '%s' "$fv" | tail -c 4) — the file wins"
    fi
  done
  :

  # Candidate files. find, not rg: this is a file-list problem, and it keeps
  # refresh working without ripgrep.
  set -- "$SCOPE"
  for d in $EXCLUDE_DIRS; do set -- "$@" -name "$d" -o; done
  set -- "$@" -name .git
  CANDIDATES=$(find "$@" -prune -o -type f \
    \( -name '.env' -o -name '.env.*' -o -name '*.env' -o -name '.envrc' \) -print 2>/dev/null \
    | grep -Ev "$REFRESH_SKIP_RE" || true)
  # The dotdir configs, which are outside $SCOPE by definition. Out of scope
  # with --here, like the named JSON targets, since they are not in $PWD.
  if [ "$HERE" = 0 ]; then
    for d in $HOME_CONFIG_DIRS; do
      [ -d "$HOME/$d" ] || continue
      EXTRA=$(find "$HOME/$d" -type f \
        \( -name '.env' -o -name '.env.*' -o -name '*.env' -o -name '.envrc' \) -print 2>/dev/null \
        | grep -Ev "$REFRESH_SKIP_RE" || true)
      [ -n "$EXTRA" ] && CANDIDATES="$CANDIDATES
$EXTRA"
    done
  fi
  # Drop anything git tracks: a tracked .env is a file that gets committed,
  # and writing a live key into it would create the leak this tool hunts for.
  # Each surviving line is tagged with its kind, so one rewriter handles both.
  TARGETS=""
  SKIPPED_TRACKED=0
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    if git -C "$(dirname "$f")" ls-files --error-unmatch "$(basename "$f")" >/dev/null 2>&1; then
      SKIPPED_TRACKED=$((SKIPPED_TRACKED + 1))
      echo "  skip (git-tracked, never written): $f"
      continue
    fi
    TARGETS="${TARGETS}env	$f
"
  done <<CAND_EOF
$CANDIDATES
CAND_EOF

  # Named JSON configs of launched services. Absolute paths, so they are only
  # in play for the default ~/repos scope, not for --here.
  if [ "$HERE" = 0 ]; then
    while IFS= read -r j; do
      [ -n "$j" ] || continue
      case "$j" in "~/"*) j="$HOME/${j#\~/}" ;; esac
      [ -f "$j" ] || continue
      if git -C "$(dirname "$j")" ls-files --error-unmatch "$(basename "$j")" >/dev/null 2>&1; then
        SKIPPED_TRACKED=$((SKIPPED_TRACKED + 1))
        echo "  skip (git-tracked, never written): $j"
        continue
      fi
      TARGETS="${TARGETS}json	$j
"
    done <<JSON_EOF
$REFRESH_JSON_TARGETS
JSON_EOF
  fi
  if [ -z "$TARGETS" ]; then
    echo "nothing writable to refresh under $SCOPE"
    exit 0
  fi

  echo "== refresh: ~/.bash_profile values -> matching assignments"
  # Values reach python through the environment only, never argv or disk, and
  # nothing about a value is printed — findings are reported by variable name.
  printf '%s' "$TARGETS" | S4S_APPLY="$APPLY" python3 -c '
import json, os, re, sys

apply_mode = os.environ.get("S4S_APPLY") == "1"
profile = {}
for line in os.environ["S4S_REFRESH_PAIRS"].split("\n"):
    if "=" in line:
        name, val = line.split("=", 1)
        profile[name] = val.encode()
for line in os.environ.get("S4S_REFRESH_ALIASES", "").split("\n"):
    line = line.strip()
    if "=" in line:
        file_name, prof_name = line.split("=", 1)
        if prof_name in profile and file_name not in profile:
            profile[file_name] = profile[prof_name]
# Names the profile comments out are disabled on purpose: never written, and
# called out when a file has one so the silence is not mistaken for a miss.
disabled = {n for n in os.environ.get("S4S_REFRESH_DISABLED", "").split("\n") if n}
for name in disabled:
    profile.pop(name, None)
json_map = []
for line in os.environ.get("S4S_REFRESH_JSON_MAP", "").split("\n"):
    line = line.strip()
    if "=" in line:
        path, var = line.rsplit("=", 1)
        if var in profile:
            json_map.append((path, var))

ASSIGN = re.compile(rb"^(\s*(?:export\s+)?)([A-Za-z_][A-Za-z0-9_]*)(\s*=\s*)(.*)$")
# Values that are placeholders or indirections, not real credentials.
PLACEHOLDER = re.compile(rb"^(change-me|changeme|xxx+|todo|your[-_].*|<.*>|\.\.\.)$", re.I)

stale = current = skipped = files_changed = 0
stale_keys = {}                           # name -> how many files it is stale in


def classify(old, new):
    """-> (verdict, note). Shared by both file kinds so the rules cannot drift."""
    if b"$" in old:
        return "skip", "left alone (value is a ${...} reference)"
    if old == b"" or PLACEHOLDER.match(old):
        return "skip", "left alone (placeholder, fill it in by hand)"
    if len(old) < 12:
        return "skip", "left alone (value too short to be a credential)"
    if old == new:
        return "current", ""
    return "stale", ("updated from ~/.bash_profile" if apply_mode
                     else "stale -> would take ~/.bash_profile value")


def record(key, verdict, note, notes):
    global stale, current, skipped
    if verdict == "current":
        current += 1
        return False
    if verdict == "skip":
        skipped += 1
        notes.append("      %s: %s" % (key, note))
        return False
    stale += 1
    stale_keys[key] = stale_keys.get(key, 0) + 1
    notes.append("      %s: %s" % (key, note))
    return True


# --- .env / .envrc --------------------------------------------------------
def split_value(rest):
    """-> (value, suffix, quote). suffix is trailing comment/whitespace."""
    if rest[:1] in (b"\x22", b"\x27"):
        q = rest[:1]
        i, n = 1, len(rest)
        while i < n:
            if rest[i:i+1] == b"\\":
                i += 2
                continue
            if rest[i:i+1] == q:
                return rest[1:i], rest[i+1:], q
            i += 1
        return rest[1:], b"", q          # unterminated quote
    m = re.match(rb"^([^\s#]*)(.*)$", rest)
    return m.group(1), m.group(2), b""


def render(val, quote):
    if quote:
        return quote + val.replace(quote, b"\\" + quote) + quote
    if val == b"" or re.search(rb"[\s#\x22\x27$]", val):
        return b"\x22" + val.replace(b"\x22", b"\\\x22") + b"\x22"
    return val


def handle_env(path, data, notes):
    lines = data.split(b"\n")
    changed = False
    for i, line in enumerate(lines):
        m = ASSIGN.match(line)
        if not m:
            continue                      # comments and blanks fall out here
        pre, name, eq, rest = m.groups()
        key = name.decode()
        if key in disabled:
            record(key, "skip",
                   "left alone (commented out in ~/.bash_profile, disabled on purpose)",
                   notes)
            continue
        if key not in profile:
            continue
        old, suffix, quote = split_value(rest)
        verdict, note = classify(old, profile[key])
        if record(key, verdict, note, notes):
            lines[i] = pre + name + eq + render(profile[key], quote) + suffix
            changed = True
    return (b"\n".join(lines) if changed else None)


# --- JSON service configs -------------------------------------------------
def loads_tolerant(text):
    """Real JSON first; then JSONC (// comments, trailing commas) as Zed writes."""
    try:
        return json.loads(text)
    except ValueError:
        pass
    stripped = re.sub(r"(?m)^\s*//[^\n]*$", "", text)
    stripped = re.sub(r"(?<![:\w])//[^\n]*", "", stripped)
    stripped = re.sub(r",(\s*[}\]])", r"\1", stripped)
    try:
        return json.loads(stripped)
    except ValueError:
        return None


STEP = re.compile(r"^([^\[\.]+)(?:\[([^\]]+)\])?$")


def resolve(obj, dotted):
    """Walk a dotted path. [id=groq] selects a list element by a sibling field,
    [0] by index. Returns None when any step is missing."""
    cur = obj
    for step in dotted.split("."):
        m = STEP.match(step)
        if not m:
            return None
        name, sel = m.group(1), m.group(2)
        if not isinstance(cur, dict) or name not in cur:
            return None
        cur = cur[name]
        if sel is None:
            continue
        if not isinstance(cur, list):
            return None
        if sel.isdigit():
            idx = int(sel)
            if idx >= len(cur):
                return None
            cur = cur[idx]
            continue
        if "=" not in sel:
            return None
        field, want = sel.split("=", 1)
        cur = next((e for e in cur
                    if isinstance(e, dict) and str(e.get(field)) == want), None)
        if cur is None:
            return None
    return cur


def find_named(obj, out, off):
    """Slots where the JSON key name IS the variable name: an env block."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in disabled and isinstance(v, str):
                off.append(k)
            elif k in profile and isinstance(v, str):
                out.append((k, v))
            else:
                find_named(v, out, off)
    elif isinstance(obj, list):
        for v in obj:
            find_named(v, out, off)
    return out


def handle_json(path, data, notes):
    text = data.decode("utf-8", "replace")
    obj = loads_tolerant(text)
    if obj is None:
        notes.append("      (unparseable JSON, left alone)")
        return None
    off = []
    slots = find_named(obj, [], off)
    for key in sorted(set(off)):
        record(key, "skip",
               "left alone (commented out in ~/.bash_profile, disabled on purpose)",
               notes)
    for dotted, var in json_map:
        got = resolve(obj, dotted)
        if isinstance(got, str):
            slots.append((var, got))
    changed = data
    seen = set()
    for key, stored in slots:
        # "Bearer sk-..." and friends: only the last token is the credential.
        parts = stored.split()
        cred = parts[-1] if parts else ""
        if not cred or (key, cred) in seen:
            continue                      # one credential can fill two slots
        seen.add((key, cred))
        new_val = profile[key]
        # Byte-level swap of the credential itself, so formatting, key order
        # and comments survive untouched. json.dumps catches any escaping.
        old_lit = json.dumps(cred)[1:-1].encode()
        new_lit = json.dumps(new_val.decode("utf-8", "replace"))[1:-1].encode()
        if old_lit in changed:
            target, repl = old_lit, new_lit
        elif cred.encode() in changed:
            target, repl = cred.encode(), new_val
        else:
            target = repl = None
        verdict, note = classify(cred.encode(), new_val)
        if verdict == "stale" and target is None:
            record(key, "skip",
                   "could not locate the stored value in the raw file, left alone",
                   notes)
            continue
        if record(key, verdict, note, notes):
            changed = changed.replace(target, repl)
    return changed if changed != data else None


for entry in sys.stdin.read().splitlines():
    if not entry or "\t" not in entry:
        continue
    kind, path = entry.split("\t", 1)
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        print("  skip %s: %s" % (path, e), file=sys.stderr)
        continue
    notes = []
    new = handle_env(path, data, notes) if kind == "env" else handle_json(path, data, notes)
    if notes:
        print("  %s" % path)
        for n in notes:
            print(n)
    if new is not None and apply_mode:
        files_changed += 1
        # Same-inode rewrite: a symlinked config keeps pointing where it did,
        # and permissions/ownership are preserved.
        with open(path, "r+b") as f:
            f.write(new)
            f.truncate()

verb = "updated" if apply_mode else "would update"
print("== %s %d value(s); %d already current, %d left alone" % (verb, stale, current, skipped))
if stale_keys:
    # Roll the per-file detail up into one list of variable names, so the
    # answer to "which keys does this touch?" does not have to be assembled
    # by eye from the file listing above.
    label = "== keys changed:" if apply_mode else "== keys that would change:"
    print(label)
    for key in sorted(stale_keys):
        n = stale_keys[key]
        print("     %s%s" % (key, "" if n == 1 else "  (in %d files)" % n))
if apply_mode:
    print("== wrote %d file(s)" % files_changed)
    sys.exit(0)
if stale:
    print("   dry run — add --apply to write")
sys.exit(2 if stale else 0)
' || REFRESH_RC=$?
  REFRESH_RC=${REFRESH_RC:-0}
  [ "$REFRESH_RC" != 0 ] && [ "$REFRESH_RC" != 2 ] && exit "$REFRESH_RC"

  # --- GitHub Actions repo secrets (REFRESH_GH_SECRETS) ---------------------
  # After the file pass so a hard failure there skips the network. Values
  # reach gh through a pipe, never argv. Push results do not change the exit
  # code: with no way to read a secret back there is no "stale" to signal.
  if [ "$HERE" = 0 ] && [ -n "$REFRESH_GH_SECRETS" ]; then
    if command -v gh >/dev/null 2>&1; then
      echo "== refresh: GitHub Actions repo secrets (compared against the local hash of the last push)"
      for name in $REFRESH_GH_SECRETS; do
        val=$(printf '%s\n' "$S4S_REFRESH_PAIRS" | sed -n "s/^$name=//p" | head -1)
        if [ -z "$val" ]; then
          echo "  $name: not exported in ~/.bash_profile, skipped"
          continue
        fi
        SLUGS=$(find "$SCOPE" -maxdepth 4 -type d -path '*/.github/workflows' 2>/dev/null \
          | while IFS= read -r wf; do
              grep -rq "secrets\.$name" "$wf" 2>/dev/null || continue
              root=${wf%/.github/workflows}
              git -C "$root" remote get-url origin 2>/dev/null \
                | sed -E 's#^(git@github\.com:|https://github\.com/)##; s#\.git$##'
            done | sort -u)
        if [ -z "$SLUGS" ]; then
          echo "  $name: no workflow under $SCOPE references secrets.$name, nothing to push"
          continue
        fi
        for slug in $SLUGS; do
          # Salted-hash record of the last successful push: "name slug salt
          # hash", one line each. The hash never leaves this machine and the
          # salt is random per push, so the file is useless for confirming a
          # guessed key against known leaks. A failed push writes nothing, so
          # the next run retries.
          rec=$(awk -v n="$name" -v s="$slug" '$1==n && $2==s' "$GH_STATE" 2>/dev/null | tail -1)
          why="no record of a previous push"
          if [ -n "$rec" ]; then
            salt=${rec#* * }; salt=${salt%% *}
            want=${rec##* }
            have=$(printf '%s%s' "$salt" "$val" | shasum -a 256 | cut -d' ' -f1)
            if [ "$have" = "$want" ]; then
              echo "  $name: current on $slug (matches the hash of the last push)"
              continue
            fi
            why="value changed since the last push"
          fi
          if [ "$APPLY" = 1 ]; then
            if printf '%s' "$val" | gh secret set "$name" -R "$slug" >/dev/null 2>&1; then
              salt=$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')
              hash=$(printf '%s%s' "$salt" "$val" | shasum -a 256 | cut -d' ' -f1)
              awk -v n="$name" -v s="$slug" '!($1==n && $2==s)' "$GH_STATE" 2>/dev/null \
                > "$GH_STATE.tmp" || :
              printf '%s %s %s %s\n' "$name" "$slug" "$salt" "$hash" >> "$GH_STATE.tmp"
              mv "$GH_STATE.tmp" "$GH_STATE"
              chmod 600 "$GH_STATE"
              echo "  $name: pushed to $slug ($why)"
            else
              echo "  $name: PUSH FAILED for $slug — check gh auth and repo access" >&2
            fi
          else
            echo "  $name: would push to $slug ($why)"
          fi
        done
      done
    else
      echo "  note: REFRESH_GH_SECRETS is set but gh is not installed — GitHub secrets not refreshed" >&2
    fi
  fi
  exit "$REFRESH_RC"
fi

# mask + provider + ownership tag: keep file:line and the first 8 chars of
# the match, name the provider the prefix identifies, and — if the match is
# one of your ~/.bash_profile values — say which variable.
mask_tag() {
  awk -F: '
    function pre(s)  { return index(m, s) == 1 }
    function has(s)  { return index(m, s) > 0 }
    function kind() {
      if (pre("sk-proj-"))     return "OpenAI project API key"
      if (pre("sk-ant-"))      return "Anthropic API key"
      if (pre("sk_live_"))     return "Stripe live secret key"
      if (pre("pk_live_"))     return "Stripe live publishable key"
      if (pre("sk-"))          return "OpenAI API key (legacy)"
      if (pre("github_pat_"))  return "GitHub fine-grained PAT"
      if (pre("ghp_"))         return "GitHub classic PAT"
      if (pre("gho_"))         return "GitHub OAuth token"
      if (pre("ghu_") || pre("ghs_")) return "GitHub app token"
      if (pre("ghr_"))         return "GitHub refresh token"
      if (pre("glpat-"))       return "GitLab PAT"
      if (pre("AKIA"))         return "AWS access key ID"
      if (pre("ASIA"))         return "AWS temporary access key ID"
      if (pre("AIza"))         return "Google API key"
      if (pre("ya29."))        return "Google OAuth access token"
      if (pre("xoxb-"))        return "Slack bot token"
      if (pre("xoxp-"))        return "Slack user token"
      if (pre("xoxa-") || pre("xoxr-") || pre("xoxs-")) return "Slack token"
      if (pre("xapp-"))        return "Slack app token"
      if (pre("hf_"))          return "Hugging Face token"
      if (pre("npm_"))         return "npm access token"
      if (pre("sts_"))         return "sts_ service/ingestion key"
      if (pre("r8_"))          return "Replicate API token"
      if (pre("sq0atp-"))      return "Square access token"
      if (pre("SG."))          return "SendGrid API key"
      if (pre("eyJ") && has(".eyJ")) return "JWT"
      if (has("PRIVATE KEY"))  return "private key material"
      if (has("://"))          return "URL with embedded credentials"
      return ""
    }
    BEGIN {
      n = split(ENVIRON["S4S_PAIRS"], P, "\n")
      if (ENVIRON["S4S_TTY"] == "1") { hi = "\033[1;31m"; off = "\033[0m" }
      else                            { hi = "";           off = "" }
    }
    {
      m = ""
      for (i = 3; i <= NF; i++) m = m (i > 3 ? ":" : "") $i
      k = kind()
      ktag = (k != "") ? " [" k "]" : ""
      tag = ""
      for (j = 1; j <= n; j++) {
        if (P[j] == "") continue
        eq = index(P[j], "="); name = substr(P[j], 1, eq - 1); val = substr(P[j], eq + 1)
        if (index(m, val) > 0 || index(val, m) > 0) {
          tag = "  " hi "<<< YOURS: $" name " (~/.bash_profile)" off
          break
        }
      }
      printf "  %s:%s: %.8s…%s%s\n", $1, $2, m, ktag, tag
    }'
}

rg_args() { # shared ripgrep flags: unignored + hidden (secrets live in
            # gitignored dotfiles), binaries auto-skipped, size-capped.
  set -- --no-config --no-ignore --hidden --max-filesize "$MAX_SIZE" -n -o
  for d in $EXCLUDE_DIRS;  do set -- "$@" -g "!$d/"; done
  for g in $EXCLUDE_GLOBS; do set -- "$@" -g "!$g";  done
  if [ "$SCAN_SECRETS_FILES" = 0 ]; then
    for g in $SECRETS_GLOBS; do set -- "$@" -g "!$g"; done
  fi
  if [ "$MODE" = critical ]; then
    set -- "$@" -g "!.bash_profile"
    for p in $CRITICAL_EXCLUDE_PATHS; do set -- "$@" -g "!$p" -g "!$p/**"; done
  fi
  printf '%s\n' "$@"
}

if [ "$MODE" = critical ]; then
  # Built from HOME_CONFIG_DIRS rather than spelled out. The literal list this
  # replaces still read "~/.prism + ~/.gito" after .google_workspace_mcp was
  # added, so the banner told you a directory was not being scanned while it
  # was. A recited inventory drifts; an enumerated one cannot.
  WHAT="~ dotfiles + ~/bin + ~/repos"
  for d in $HOME_CONFIG_DIRS; do WHAT="$WHAT + ~/$d"; done
  WHAT="$WHAT (.bash_profile excluded)"
else
  WHAT="$(pwd)"
fi
if [ "$SCAN_SECRETS_FILES" = 1 ]; then
  echo "scanning $WHAT (including local secrets files)"
else
  echo "scanning $WHAT (local secrets files skipped; -a to include)"
fi
FOUND=0
TRANSIENT_FOUND=0

# Agent scratchpad findings are TRANSIENT in critical mode: the dirs are
# session-local tmp that self-clears (the 2026-08-25 rc=2 was a JWT in a live
# session's scratchpad, gone by the next run's sweep), so they are reported —
# the scan still looks there on purpose, see agent_scratch_dirs — but do not
# alone trip exit 2, which the weekly cron escalates to a phone alarm. A
# durable finding anywhere else still exits 2. Only critical mode splits:
# scanning a scratchpad deliberately (default cwd mode) still alarms.
# The layout matched is <root>/claude-<uid>/<project>/<session>/scratchpad.
#
# ~/.codex/shell_snapshots is the second member of the class (sd:1254). Codex
# writes a fresh snapshot of the whole exported shell environment on every
# session start, so the directory is a cache of ~/.config/shell/env.sh and
# not a record of anything: measured 2026-09-21, 6 files, newest written
# minutes before the scan, oldest two days, on a machine that has run codex
# for months. Deleting them does not help — the next session writes them
# back, which is the failure mode this repo already killed once, in doctor —
# and the directory is mode 700 via HARDEN_DIRS in local-machine-setup, which
# is the control that actually covers it.
#
# Transient and not an entry in CRITICAL_EXCLUDE_PATHS, which is the stronger
# thing and the wrong one: an exclusion drops the directory from both scan
# passes, including under `critical -a`, so a value that has since been
# removed from env.sh but is still sitting in an old snapshot would stop
# being reported at all. Classifying it keeps every match visible and only
# declines to page on it.
#
# Anchored to the start of the path, which in critical mode is relative to
# $HOME (the mode cds there), so the branch matches the one directory codex
# owns and rewrites. Unanchored it would also match a committed
# repos/<project>/.codex/shell_snapshots/ under ~/repos, and a credential
# checked into a repository has no regeneration and no retention behind it —
# it is exactly the durable finding this scan exists to page on.
TRANSIENT_PATH_RE='/claude-[^/]*/[^/]*/[^/]*/scratchpad/|^\.codex/shell_snapshots/'

# --- pass 1: known credential formats -------------------------------------
echo "== pattern scan"
if [ "$HAVE_RG" = 1 ]; then
  PATTERN_ARGS=$(printf '%s\n' "$PATTERNS" | sed 's/^/-e\n/')
  OUT=$( { rg_args; printf '%s\n' "$PATTERN_ARGS"; targets; } | tr '\n' '\0' \
        | xargs -0 rg 2>/dev/null || true )
else
  EX_D=""; for d in $EXCLUDE_DIRS; do EX_D="$EX_D --exclude-dir=$d"; done
  EX_G=""; for g in $EXCLUDE_GLOBS; do EX_G="$EX_G --exclude=$g"; done
  if [ "$SCAN_SECRETS_FILES" = 0 ]; then
    for g in $SECRETS_GLOBS; do EX_G="$EX_G --exclude=$g"; done
  fi
  [ "$MODE" = critical ] && EX_G="$EX_G --exclude=.bash_profile"
  set --
  while IFS= read -r t; do [ -n "$t" ] && set -- "$@" "$t"; done <<TARGETS_EOF
$(targets)
TARGETS_EOF
  [ $# -gt 0 ] || set -- .
  RE=$(ere_patterns | paste -sd '|' -)
  # shellcheck disable=SC2086
  OUT=$(grep -rEIno $EX_D $EX_G -e "$RE" "$@" 2>/dev/null || true)
fi
# Known-fake fixtures: AWS docs example keys, and URL matches whose password
# is a placeholder (${VAR}, <name>) or an obvious dev value. Matches end at
# the "@" because rg/grep run with -o, hence the "@$" anchors.
FAKE_FIXTURE_RE='AKIA[0-9A-Z]*EXAMPLE|://[^@]*[<{$][^@]*@$|:[^@:]*pass(word)?[0-9]*@$|:[^@:]*[_-]test@$|:(postgres|p%40ssword|local-secret|super-secret|changeme)@$'
OUT=$(printf '%s\n' "$OUT" | grep -Ev "$FAKE_FIXTURE_RE" || true)
TRANSIENT_OUT=""
if [ "$MODE" = critical ] && [ -n "$OUT" ]; then
  TRANSIENT_OUT=$(printf '%s\n' "$OUT" | grep -E "$TRANSIENT_PATH_RE" || true)
  OUT=$(printf '%s\n' "$OUT" | grep -Ev "$TRANSIENT_PATH_RE" || true)
fi
if [ -n "$OUT" ]; then
  printf '%s\n' "$OUT" | mask_tag
  FOUND=$((FOUND + $(printf '%s\n' "$OUT" | wc -l)))
else
  echo "  clean"
fi
if [ -n "$TRANSIENT_OUT" ]; then
  echo "== transient (agent scratchpad / codex shell-snapshot cache — regenerated, not a record; not fatal)"
  printf '%s\n' "$TRANSIENT_OUT" | mask_tag
  TRANSIENT_FOUND=$((TRANSIENT_FOUND + $(printf '%s\n' "$TRANSIENT_OUT" | wc -l)))
fi

# --- the local judgment of each hit (sd:2761) -------------------------------
# Each durable pattern hit is asked of the local Kev, and of nothing else:
# is it a real credential? The scanner's verdict, yes, is the shadow answer,
# so the ledger gets a pair and nothing here changes: the answer is thrown
# away, and no line of output or exit code depends on it. A hit is a
# candidate credential, so it goes to Kev on loopback or nowhere
# (`--local-only`, and JEV_SECRET_SCAN is a local-only stage in jev.py), and
# it reaches jev on stdin, never in argv, where `ps` would show it.
# Bounded: S4S_JEV_MAX_HITS hits per run (default 10), S4S_JEV_TIMEOUT
# seconds per call (default 5), no retry. JEV_SECRET_SCAN=0 is the off-switch.
judge_hits() {
  JEV=$(command -v jev 2>/dev/null) || return 0
  "$JEV" enabled JEV_SECRET_SCAN --local-only --record \
    --caller local-scan-for-secrets >/dev/null 2>&1 || return 0
  max=${S4S_JEV_MAX_HITS:-10}
  case $max in ''|*[!0-9]*) max=10 ;; esac
  wait_s=${S4S_JEV_TIMEOUT:-5}
  case $wait_s in ''|*[!0-9]*) wait_s=5 ;; esac
  printf '%s\n' "$1" | head -n "$max" | while IFS= read -r hit; do
    [ -n "$hit" ] || continue
    printf '%s\n' "$hit" | JEV_TIMEOUT=$wait_s JEV_RETRIES=0 \
      "$JEV" noul 'Is this a real credential, not a test value or placeholder?' \
        --local-only --stage JEV_SECRET_SCAN --caller local-scan-for-secrets \
        --gate 0.5 --shadow yes --state-format text >/dev/null 2>&1 || :
  done
}
[ -n "$OUT" ] && { judge_hits "$OUT" </dev/null || :; }

# --- pass 2: your keys anywhere, regardless of format ---------------------
echo "== your keys (shell env exports)"
if [ -n "$S4S_PAIRS" ]; then
  printf '%s\n' "$S4S_PAIRS" \
  | while IFS= read -r pair; do
      [ -n "$pair" ] || continue
      name=${pair%%=*}
      val=${pair#*=}
      if [ "$HAVE_RG" = 1 ]; then
        HITS=$( { rg_args; printf -- '-F\n%s\n' "$val"; targets; } | tr '\n' '\0' \
               | xargs -0 rg -l 2>/dev/null || true )
      else
        EX_D=""; for d in $EXCLUDE_DIRS; do EX_D="$EX_D --exclude-dir=$d"; done
        EX_G=""; for g in $EXCLUDE_GLOBS; do EX_G="$EX_G --exclude=$g"; done
        if [ "$SCAN_SECRETS_FILES" = 0 ]; then
          for g in $SECRETS_GLOBS; do EX_G="$EX_G --exclude=$g"; done
        fi
        [ "$MODE" = critical ] && EX_G="$EX_G --exclude=.bash_profile"
        set --
        while IFS= read -r t; do [ -n "$t" ] && set -- "$@" "$t"; done <<TARGETS_EOF
$(targets)
TARGETS_EOF
        [ $# -gt 0 ] || set -- .
        # shellcheck disable=SC2086
        HITS=$(grep -rIlF $EX_D $EX_G -e "$val" "$@" 2>/dev/null || true)
      fi
      T_HITS=""
      if [ "$MODE" = critical ] && [ -n "$HITS" ]; then
        T_HITS=$(printf '%s\n' "$HITS" | grep -E "$TRANSIENT_PATH_RE" || true)
        HITS=$(printf '%s\n' "$HITS" | grep -Ev "$TRANSIENT_PATH_RE" || true)
      fi
      if [ -n "$HITS" ]; then
        if [ "$S4S_TTY" = 1 ]; then
          printf '%s\n' "$HITS" | sed "s/^/  $(printf '\033[1;31m')YOURS: \$$name$(printf '\033[0m') found in: /"
        else
          printf '%s\n' "$HITS" | sed "s/^/  YOURS: \$$name found in: /"
        fi
        echo "FOUND" >> "${TMPDIR:-/tmp}/.scan4s.$$"
      fi
      if [ -n "$T_HITS" ]; then
        printf '%s\n' "$T_HITS" | sed "s/^/  transient: \$$name found in: /"
        echo "FOUND" >> "${TMPDIR:-/tmp}/.scan4s.t.$$"
      fi
    done
  [ -f "${TMPDIR:-/tmp}/.scan4s.$$" ] \
    && { FOUND=$((FOUND+1)); rm -f "${TMPDIR:-/tmp}/.scan4s.$$"; } \
    || echo "  clean"
  [ -f "${TMPDIR:-/tmp}/.scan4s.t.$$" ] \
    && { TRANSIENT_FOUND=$((TRANSIENT_FOUND+1)); rm -f "${TMPDIR:-/tmp}/.scan4s.t.$$"; } \
    || :
else
  echo "  no key-like exports found in ~/.bash_profile, skipped"
fi

echo "== done"
if [ "$FOUND" -gt 0 ]; then
  echo "findings above — matches truncated, open the files to inspect"
  exit 2
fi
if [ "$TRANSIENT_FOUND" -gt 0 ]; then
  echo "only transient findings — reported above, in dirs their tool rewrites, not treated as a leak (exit 0)"
  exit 0
fi
echo "no secrets found"

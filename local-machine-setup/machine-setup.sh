#!/bin/sh
# Provision and maintain machines from a named profile.
# Usage: machine-setup.sh <verb> [args] [--apply]; `help` lists the verbs (VERBS).
set -e

# Byte order for every sort and comm below. glibc's en_US.UTF-8 collation
# skips punctuation, so `~/x` and `/x` sort apart from macOS's order, and a
# capture then rewrites a committed manifest it did not change.
LC_COLLATE=C
export LC_COLLATE

SELF="$0"
while [ -L "$SELF" ]; do
  link=$(readlink "$SELF")
  case "$link" in
    /*) SELF="$link" ;;
    *)  SELF="$(dirname "$SELF")/$link" ;;
  esac
done
DIR="$(cd "$(dirname "$SELF")" && pwd)"
ROOT="$(cd "$DIR/.." && pwd)"

# The profiles, dotfiles, .env templates and LaunchAgent plists are this
# machine's owner's data, not the tool's, so they live outside the checkout:
# one folder per tool under $SYSTEM_TOOLS_CONFIG (see lib/config.sh), each
# directory overridable on its own. examples/ shows every shape; copy it there
# to start. The folder may be a git checkout of its own, and capture treats it
# as one when it is.
SYSTEM_TOOLS_CONFIG="${SYSTEM_TOOLS_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/system}"
CONFIG_DIR="$SYSTEM_TOOLS_CONFIG/machine-setup"
PROFILE_DIR="${MACHINE_SETUP_PROFILE_DIR:-$CONFIG_DIR/profiles}"
DOTFILE_DIR="${MACHINE_SETUP_DOTFILE_DIR:-$CONFIG_DIR/dotfiles}"
ENV_DIR="${MACHINE_SETUP_ENV_DIR:-$CONFIG_DIR/envs}"
AGENT_DIR="${MACHINE_SETUP_AGENT_DIR:-$CONFIG_DIR/launchagents}"

# shellcheck source=../lib/config.sh
. "$ROOT/lib/config.sh"

# Per-owner values the script needs and the repository cannot hold: the
# vault path, the ssh key names, extra LaunchAgent globs. Every one has a
# default that checks nothing, so a missing file is fine. An exported value
# wins over the file, an empty one too (sd:3964).
# See machine-setup.env.example for the names.
st_source_env machine-setup machine-setup.env

# launchd labels this repository's tools install: <prefix>.<name>, and
# <prefix>.cron.<job> for local-cron-jobs.
LABEL_PREFIX="$(st_label_prefix)"

# fd 4 is this run's stderr, the job log under cron: bound_step names each step
# there, also when its caller sends the call's own stderr elsewhere.
exec 4>&2
# A run started from a terminal has someone to answer a sudo prompt and to
# stop a hang, and a bounded step runs in a process group of its own, where
# that prompt would stop on SIGTTIN. So bound_step bounds an unattended run,
# such as cron's, whose stdin is not a terminal.
if [ -t 0 ]; then ATTENDED=1; else ATTENDED=0; fi

# The Directory Service node that holds local user records (dscl paths, not
# file paths: the record is there whatever the home directory is).
DS_USERS=/Users

STATE_DIR="${MACHINE_SETUP_STATE:-$HOME/.config/machine-setup}"
STATE_FILE="$STATE_DIR/profile"

# The .release kind's tools: their bins go here, and the tag each entry has
# installed goes in RELEASE_STATE/<name>.tag (sd:4853), with the repo and
# asset it came from (sd:4916). The base URL's
# override exists for the tests, which stub curl on PATH.
RELEASE_BIN="${MACHINE_SETUP_RELEASE_BIN:-$HOME/.local/bin}"
RELEASE_STATE="$STATE_DIR/release"
RELEASE_BASE="${MACHINE_SETUP_RELEASE_BASE:-https://github.com}"

# `release` runs right after `brew`: later stages may run what it installs,
# such as the fnm the tooling stage asks for node.
# `sd` runs before `agents`: the dashboard opens the
# database at startup, so an agent bootstrapped first restarts against a
# file that does not exist yet. `satellite` sits beside it: a machine runs
# one of the two, and neither loads an agent the other needs.
STAGES="brew release shell appstore bin dotfiles envs prompts repos cron sd satellite agents services macos tooling system obsidian iterm2"
# The verbs dispatch accepts, in help's order. Help and the no-arg usage print
# this list; three hand copies of it disagreed (sd:3548).
VERBS="setup update capture status doctor test upgrade upgrade-report checklist decommission compare report profiles triage candidates apps adopt promote"
KINDS="tap brew cask mas service cron agent macos app obsidian iterm2 spotlight satellite node npm mailto release"

# Directories that hold credentials in the clear and are created world-readable
# by the tools that own them. Codex snapshots the whole shell environment —
# every exported API key — into ~/.codex/shell_snapshots; the agent CLIs keep
# transcripts of whatever scrolled past; gh/aws/gcloud keep long-lived tokens in
# plain files. A weekly secret-scan run on this machine found ~20 live keys
# sitting in 644 files under ~/.codex.
#
# The fix belongs on the directory, not the files: the tools recreate those
# files constantly, so `chmod 600` on the contents is undone by the next
# session, while 700 on the parent keeps the whole subtree unreachable to other
# local accounts no matter what mode the tool picks. Paths are relative to
# $HOME and skipped when absent, so the list can cover both machines.
HARDEN_DIRS=".codex .claude .gemini .aws .config/gh .config/gcloud .ssh .prism .gito .copilot"

# Spotlight's privacy list (System Settings > Spotlight > Search Privacy) is
# the `Exclusions` array in the data volume's VolumeConfiguration.plist. Apple
# ships no CLI for it. The file is root-owned, and root alone still cannot
# open it: the calling terminal also needs Full Disk Access. The override
# exists for the tests; nothing else should set it.
SPOTLIGHT_PLIST="${MACHINE_SETUP_SPOTLIGHT_PLIST:-/System/Volumes/Data/.Spotlight-V100/VolumeConfiguration.plist}"
# The sudoers drop-in that lets a run with no ticket read that file (sd:3232).
# The override exists for the tests, like the one above.
SPOTLIGHT_SUDOERS="${MACHINE_SETUP_SPOTLIGHT_SUDOERS:-/etc/sudoers.d/machine-setup-spotlight-read}"
# The sudo grace-period drop-in; the override exists for the tests too.
TIMEOUT_SUDOERS="${MACHINE_SETUP_TIMEOUT_SUDOERS:-/etc/sudoers.d/timestamp-timeout}"
# The SMB client's defaults; the override exists for the tests too.
NSMB_CONF="${MACHINE_SETUP_NSMB_CONF:-/etc/nsmb.conf}"
# Unattended sudo, on every machine (sd:6312): the operator installs the
# repair drop-in from machine-setup-repairs.sudoers.example and makes the
# login-Keychain item SUDO_KEYCHAIN, which sudo-askpass.sh reads; this script
# writes neither. The overrides exist for the tests too.
REPAIRS_SUDOERS="${MACHINE_SETUP_REPAIRS_SUDOERS:-/etc/sudoers.d/machine-setup-repairs}"
SUDO_KEYCHAIN=machine-setup-sudo
SECURITY="${MACHINE_SETUP_SECURITY:-security}"
# sd-serve's own python (sd:6492): uv installs it once at this fixed path,
# outside Homebrew, and the sd-serve plist's PATH starts with its bin. A brew
# upgrade then moves no binary the firewall allows, and brew python moves
# freely. The uv override exists for the tests, so none downloads a python.
SERVE_PYTHON_VERSION=3.15
SERVE_PYTHON_DIR="$HOME/.local/share/sd-serve/python"
UV="${MACHINE_SETUP_UV:-uv}"
# The PlistBuddy override exists for the tests too: CI runs on Linux, which
# has no PlistBuddy, and the tests put a stand-in there.
PLISTBUDDY="${MACHINE_SETUP_PLISTBUDDY:-/usr/libexec/PlistBuddy}"
# The applications folder the apps scan and the iterm2 stage look in. The
# override exists for the tests, so a capture there never lists this Mac's apps.
APPLICATIONS_DIR="${MACHINE_SETUP_APPLICATIONS_DIR:-/Applications}"

# Every mutation is opt-in. Without --apply the script only prints its plan,
# so `setup` on an unfamiliar machine is safe to run first and read.
APPLY=0
FAIL_ON_DRIFT=0
# Additive capture. Writes a manifest entry that appeared; refuses to write
# one away. The asymmetry is the point -- see write_manifest.
ADDITIVE=0
ADDITIVE_HELD=0
# capture only: write even though the checkout is behind its upstream.
STALE_OK=0
# dotfiles/agents: overwrite a file this machine edited, not only a stale one.
# envs never reads it: a live .env holds credentials the template omits.
FORCE=0

# Dotfiles safe to copy into the repo. ~/.bash_profile is deliberately absent:
# it holds ~40 live API keys, and capturing it would commit them.
# .local/bin/sdm-login signs StrongDM's sdm back in (sd:5919); only a profile
# with a copy gets it, and examples/dotfiles/work/ holds one.
DOTFILES=".zshrc .zshenv .zprofile .bash_aliases .gitconfig .gitignore_global .ssh/config .config/gh/config.yml .prism/.env .gito/.env .aws/config .vale.ini .local/bin/sdm-login"
# Dotfiles installed 0600 rather than with cp's default mode. Not because
# they hold secrets — capture refuses those — but because they sit where a
# secret would go, and a world-readable ~/.gito/.env is how the last one
# started.
DOTFILES_PRIVATE=".prism/.env .gito/.env .ssh/config .zshenv .aws/config"
DOTFILES_DENY=".bash_profile"

# Per-folder .env files this repo's tools read. Unlike DOTFILES these are not
# $HOME-relative: each tool reads $SYSTEM_TOOLS_CONFIG/<tool>/.env (see
# live_env), the template lives in envs/<profile>/<folder>.env, and the stage
# installs it. Only non-secret defaults go in — a template holding a
# credential would be committed, which is the leak the .env rule exists to
# stop. Secrets stay in ~/.config/shell/env.sh or a gitignored conf.
ENVS="local-notify local-statusline"

# Folders whose .env is expected to hold placeholder values — see the
# placeholder check in doctor for the rule about what belongs here. Set in
# machine-setup.env; none by default.
PLACEHOLDER_EXPECTED="${MACHINE_SETUP_PLACEHOLDER_EXPECTED:-}"

# Dotfiles are profile-scoped: dotfiles/<profile>/ wins, dotfiles/common/ is
# the fallback for the files that are genuinely the same on every machine.
# The directory used to be flat, which made capture a cross-machine hazard —
# capturing this machine's 158-line .zshrc would have overwritten the other
# machine's 31-line one in the repo, with nothing in the output saying so.
dotfile_src() {
  if [ -f "$DOTFILE_DIR/$PROFILE/$1" ]; then
    echo "$DOTFILE_DIR/$PROFILE/$1"
  elif [ -f "$DOTFILE_DIR/common/$1" ]; then
    echo "$DOTFILE_DIR/common/$1"
  fi
}

# Lines a tool writes into a tracked dotfile to record this machine's own state.
# `aws login --profile X` adds `login_session = <arn>` under the profile it
# signed in: the account and user the browser picked, renewed on every login.
# The stage compares and captures the file without them and keeps them when it
# overwrites, so a login is not drift and --force does not sign every profile
# out. Prints an ERE, or nothing for a dotfile no tool writes into.
dotfile_session_re() { # dotfile
  case "$1" in
    .aws/config) echo '^[[:space:]]*login_session[[:space:]]*=' ;;
  esac
}

# The file as the stage judges it: its session lines dropped into $3, or the
# file itself when there are none to drop. Prints the path to read.
dotfile_view() { # dotfile, path, scratch
  dv_re=$(dotfile_session_re "$1")
  if [ -z "$dv_re" ] || [ ! -f "$2" ]; then
    echo "$2"
    return 0
  fi
  grep -Ev "$dv_re" "$2" > "$3" || true
  echo "$3"
}

# Write the tracked file over the machine's, carrying each session line into
# the section it sat under. A session under a profile the tracked file no
# longer names is dropped with the profile.
keep_sessions_cp() { # dotfile, src, dst
  ks_re=$(dotfile_session_re "$1")
  # Called through run, where set -e is off: a failed awk must not move a
  # half-written merge over the dotfile.
  awk -v re="$ks_re" '
    function header(line) { sub(/^[[:space:]]+/, "", line); sub(/[[:space:]]+$/, "", line); return line }
    # FILENAME and not NR == FNR: an empty destination has no records, so
    # every line of the source satisfies NR == FNR and is dropped (sd:1361).
    FILENAME == ARGV[1] {
      if ($0 ~ /^[[:space:]]*\[/) { section = header($0); next }
      if ($0 ~ re) keep[section] = keep[section] $0 "\n"
      next
    }
    { print }
    /^[[:space:]]*\[/ { h = header($0); if (h in keep) printf "%s", keep[h] }
  ' "$3" "$2" > "$3.merge.$$" || { rm -f "$3.merge.$$"; return 1; }
  mv "$3.merge.$$" "$3"
}

# The mode copy_dotfile sets, or nothing when the file keeps cp's.
dotfile_mode() { # dotfile
  case " $DOTFILES_PRIVATE " in *" $1 "*) echo 600; return ;; esac
  # A command runs whatever mode its copy in the config folder kept: a copy
  # moved through a share mounted noowners loses the bit.
  case "$1" in .local/bin/*) echo 755 ;; esac
}

# Returns the first failed step, so the caller records nothing after it.
copy_dotfile() { # dotfile, src, dst
  if [ -n "$(dotfile_session_re "$1")" ] && [ -f "$3" ]; then
    run_ok keep_sessions_cp "$1" "$2" "$3" || return
  else
    run_ok cp "$2" "$3" || return
  fi
  cd_mode=$(dotfile_mode "$1")
  [ -z "$cd_mode" ] || run_ok chmod "$cd_mode" "$3"
}

env_src() {
  if [ -f "$ENV_DIR/$PROFILE/$1.env" ]; then
    echo "$ENV_DIR/$PROFILE/$1.env"
  elif [ -f "$ENV_DIR/common/$1.env" ]; then
    echo "$ENV_DIR/common/$1.env"
  fi
}

die() { echo "machine-setup.sh: $*" >&2; exit 1; }

# Call after `tmp=$(mktemp -d)`. EXIT removes the folder; a signal exits, and
# so runs EXIT. A TERM or INT trap that only removed it returned into the run,
# which then wrote into the folder it had just removed: capture --apply wrote
# manifests from empty bodies (sd:2660, sd:3545).
trap_tmp() {
  trap 'rm -rf "$tmp"' EXIT
  trap 'exit 129' HUP
  trap 'exit 130' INT
  trap 'exit 143' TERM
}

say() {
  if [ "$APPLY" -eq 1 ]; then printf '  %s\n' "$*"; else printf '  [dry-run] %s\n' "$*"; fi
}

# stdin comes from /dev/null on purpose. Every stage loop is fed by a pipe —
# `manifest brew | while read -r f` — and a command that reads stdin inside the
# loop eats the rest of the manifest. `brew install python@3.13` did exactly
# that on the work machine: ripgrep, rtk, shellcheck, smartmontools, uv and
# wget were swallowed, never installed, and never reported, because the loop
# had no lines left to read.
#
# A failed command is reported and the run goes on (sd:3543). do_stages calls
# each stage bare under `set -e`, so a bare `run` that returned its failure
# ended the stage loop, the stage and the run: one dead formula skipped every
# later formula, cask and stage. The failure goes to FAILED_LOG, and the run
# ends with the list and exit 1.
#
# `run` is for a step nothing later depends on. A step that later steps need
# (a copy before its record, a folder before the file in it) uses run_ok,
# which reports the same way and also returns the failure, so the caller
# skips the rest of that item: `if run_ok cp ...; then record ...; fi`.
# try_run is the bare form, for a caller that reports the failure itself.
run() {
  run_ok "$@" || :
}

run_ok() {
  try_run "$@" || { ro_rc=$?; [ "$1" != bound_step ] || shift 2; note_failure "$*" "$ro_rc"; return "$ro_rc"; }
}

try_run() {
  if [ "$APPLY" -eq 1 ]; then
    "$@" </dev/null
  else
    [ "$1" != bound_step ] || shift 2
    printf '  [dry-run] %s\n' "$*"
  fi
}

# A file, not a variable: most stage loops run in a pipeline subshell, where an
# assignment dies with the loop. Empty outside do_stages and decommission.
FAILED_LOG=""

note_failure() { # what, exit code
  echo "  FAILED  $1 (exit $2)"
  [ -z "$FAILED_LOG" ] || printf '%s\n' "$1" >> "$FAILED_LOG"
}

# Printed last, like the deferred list. Returns 1 when anything failed, so
# the run exits 1 under set -e and a nightly caller's report needs attention.
report_failures() {
  [ -n "$FAILED_LOG" ] || return 0
  rf_log="$FAILED_LOG"
  FAILED_LOG=""
  if [ ! -s "$rf_log" ]; then
    rm -f "$rf_log"
    return 0
  fi
  echo "failed — $(grep -c '^' "$rf_log") remediation(s) did not finish; fix them, then re-run:"
  sed 's/^/    /' "$rf_log"
  echo
  rm -f "$rf_log"
  return 1
}

# Which side moved? `cmp` answers "these differ", never "who changed", and every
# stage that copies a file into place used to guess the same way — the machine
# moved — and print "review, then 'capture'". Half the time that is backwards:
# #160 rewrote a comment in .zshrc and #161 added an alias to .bash_aliases, and
# capturing either would have written this machine's older text back over them.
#
# So record what was installed. The hash of the bytes this script last wrote
# turns a two-way compare into a three-way answer:
#
#   stale    machine still matches the record, so the REPO moved — safe to
#            refresh, and `update --apply` does
#   edited   machine no longer matches the record — a local edit, left alone
#            unless --force
#   unknown  nothing recorded yet; says so instead of guessing, and seeds
#            itself the next time the two agree
#
# Same model local-agent-prompt has used since #137, in the state directory
# machine-setup already keeps the profile in.
INSTALLED_DIR="$STATE_DIR/installed"

state_key() { printf '%s' "$1" | tr '/ ' '__'; }

# Only under --apply: a dry run must not leave state behind that changes what
# the next run decides. A record that cannot be written is reported like a
# failed step and the run goes on: under set -e it ended the run.
record_hash() { # key, file
  [ "$APPLY" -eq 1 ] || return 0
  [ -f "$2" ] || return 0
  { mkdir -p "$INSTALLED_DIR" &&
    shasum -a 256 < "$2" | awk '{print $1}' > "$INSTALLED_DIR/$(state_key "$1").sha"; } ||
    note_failure "record_hash $1" "$?"
}

has_record() { # key
  [ -f "$INSTALLED_DIR/$(state_key "$1").sha" ]
}

# "Is this file still the bytes we recorded?" Which file to ask about differs by
# stage, and so does what the answer means — see each caller.
matches_record() { # key, file
  has_record "$1" || return 1
  [ -f "$2" ] || return 1
  [ "$(shasum -a 256 < "$2" | awk '{print $1}')" = "$(cat "$INSTALLED_DIR/$(state_key "$1").sha")" ]
}

# One dated copy before any overwrite. Cheap, and the only thing standing
# between --force and a file nobody can get back. Copies go to one folder per
# machine, never beside the file: ten .bak- and .pre- copies sat among the
# live profiles (sd:6731). The copy's mtime is when it was made: prune
# reads it, and cp -p kept the file's own, so a copy of a file unedited
# for a month went at the next update.
BACKUP_DIR="$CONFIG_DIR/backups/$(hostname -s | tr '[:upper:]' '[:lower:]')"
backup_file() { # path
  [ -e "$1" ] || return 0
  mkdir -p "$BACKUP_DIR" || return
  bf_to=$(backup_dest "$(basename "$1").$(date +%Y%m%d%H%M%S)")
  cp -p "$1" "$bf_to" && touch "$bf_to"
}

# A path in BACKUP_DIR that holds nothing yet: <name>, else <name>-2, -3.
# Two copies of one name, made in one second or moved from two folders,
# would otherwise overwrite each other.
backup_dest() { # name
  bd_to="$BACKUP_DIR/$1" bd_n=1
  while [ -e "$bd_to" ] || [ -L "$bd_to" ]; do bd_n=$((bd_n + 1)); bd_to="$BACKUP_DIR/$1-$bd_n"; done
  printf '%s\n' "$bd_to"
}

# Moves a .bak- or .pre- copy left beside a config file into this machine's
# backups folder, its clock restarted so it gets its 30 days there, then
# removes copies older than that. Runs with every update --apply (sd:6731).
prune_backups() {
  [ "$APPLY" -eq 1 ] && [ -d "$CONFIG_DIR" ] || return 0
  find "$CONFIG_DIR" -path "$CONFIG_DIR/backups" -prune -o -mindepth 1 -maxdepth 2 \
      \( -name '*.bak-*' -o -name '*.pre-*' \) -print 2>/dev/null |
    while IFS= read -r pb; do
      mkdir -p "$BACKUP_DIR" && pb_to=$(backup_dest "$(basename "$pb")") &&
        touch "$pb" && mv "$pb" "$pb_to" &&
        echo "  moved   ${pb#"$CONFIG_DIR"/} to backups/$(basename "$BACKUP_DIR")/$(basename "$pb_to")" ||
        note_failure "move $pb to $BACKUP_DIR" "$?"
    done
  [ -d "$BACKUP_DIR" ] || return 0
  find "$BACKUP_DIR" -mindepth 1 -maxdepth 1 -mtime +30 -print 2>/dev/null |
    while IFS= read -r pb; do
      rm -rf "$pb" && echo "  pruned  backups/$(basename "$BACKUP_DIR")/$(basename "$pb") (older than 30 days)" ||
        note_failure "rm -rf $pb" "$?"
    done
}

# capture rewrites files in the config folder, which need not be a git
# checkout: without one, an overwrite had no way back (sd:3546). A file git
# tracks with no uncommitted change is left to git; any other gets the dated
# copy, and one that cannot be made stops the write. A failed git probe
# counts as not held, and so does a symlink: git holds its target path, and
# capture writes through it into the target.
capture_backup() { # path
  [ -e "$1" ] || return 0
  cb_dir=$(dirname "$1"); cb_name=$(basename "$1")
  if [ ! -L "$1" ] && command -v git >/dev/null 2>&1 \
      && git -C "$cb_dir" ls-files --error-unmatch -- "$cb_name" >/dev/null 2>&1 \
      && git -C "$cb_dir" diff --quiet HEAD -- "$cb_name" >/dev/null 2>&1; then
    return 0
  fi
  backup_file "$1" || die "capture: cannot keep a copy of $1; not written"
}

# Commands that could not run because sudo was refused, one per line, reported
# together at the end of the run. Never a hard failure: on a machine where the
# user is not an admin every one of these is expected to defer.
DEFERRED=""

# A refused sudo must not abort the run. do_stages calls each stage bare under
# `set -e`, and stage_system is 11th of 13 — a declined password used to take
# the obsidian and iterm2 stages down with it, silently, which is the same
# failure shape as the manifest-eating stdin bug above.
#
# $1 is what to show the user; the rest is the argv actually run, so the
# command printed in the summary is the command that was attempted. The
# `sudo -n true` probe means the password is asked for once per run rather
# than once per action — worth doing here because the timestamp_timeout
# drop-in that would otherwise cover it is itself one of the things being
# installed.
sudo_run() {
  shown="$1"; shift
  if [ "$APPLY" -ne 1 ]; then
    printf '  [dry-run] %s\n' "$shown"
    return 0
  fi
  if sudo -n true 2>/dev/null || sudo -v; then
    # sudo granted, so a failure is the command's, not a deferral (sd:3548).
    sr_rc=0
    sudo "$@" </dev/null || sr_rc=$?
    [ "$sr_rc" -eq 0 ] || note_failure "$shown" "$sr_rc"
    return 0
  fi
  DEFERRED="$DEFERRED    $shown
"
  echo "  DEFER   needs sudo: $shown"
}

# A config folder in a checkout may track its live configs as symlinks — git
# stores the target path, never the content, which is the only safe way to
# point at files holding tokens. Those targets are absolute and were written
# on the machine that captured them: <users>/<other-name>/.claude.json. They
# resolve on another machine only when <users>/<other-name> is a symlink to
# the real account. Without that alias the pointers dangle silently, so read
# the aliases the checkout actually depends on out of the symlinks rather
# than hardcoding one name.
#
# Prints the home paths (siblings of $HOME) the checkout's symlinks need and
# this machine does not have. They all want the same target — this account —
# since the whole point of the alias is to make another machine's path land
# here.
USERS_ROOT="$(dirname "$HOME")"
missing_home_aliases() {
  find "$ROOT" -type l -not -path "*/.git/*" -exec readlink {} + 2>/dev/null \
    | awk -v r="$USERS_ROOT/" 'index($0, r) == 1 { n = split(substr($0, length(r) + 1), p, "/"); if (n > 1) print r p[1] }' \
    | sort -u \
    | while read -r alias_path; do
        [ "$alias_path" = "$HOME" ] && continue
        [ -e "$alias_path" ] && continue
        echo "$alias_path"
      done
}

# Entries of one profile file, comments, blanks and `!name` lines stripped:
# a `!` line drops an entry and is none (sd:4916). `|| :` matters: under
# set -e a comment-only file makes grep exit 1, which aborted the
# assigned-list builder mid-loop and mislabeled everything as unassigned.
profile_entries() {
  f="$PROFILE_DIR/$1.$2"
  if [ -f "$f" ]; then
    sed -e 's/#.*//' -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' "$f" | grep -v -e '^$' -e '^!' || :
  fi
}

# The names the `!name` lines of one profile file drop (sd:4853).
profile_drops() {
  f="$PROFILE_DIR/$1.$2"
  if [ -f "$f" ]; then
    sed -n -e 's/#.*//' -e 's/^[[:space:]]*![[:space:]]*//p' "$f"
  fi
}

# Strips comments and blank lines from a manifest. A `!name` line in the
# profile's file drops the entry common lists as name (sd:4853), so a machine
# can do without one, or list its own spelling of it; no `!` line reaches the
# output.
manifest() {
  kind="$1"
  mf_neg=$(profile_drops "$PROFILE" "$kind")
  { profile_entries common "$kind" | drop_negated "$mf_neg"
    profile_entries "$PROFILE" "$kind"
  } | sort -u
}

# Copies stdin less each entry a name in $1 (one per line) drops. An entry's
# name is its first word up to a comma, a .release line's first bin; `!name`
# drops that name and its tap-qualified `<tap>/name` too, or with `exact`
# that name alone. Through ENVIRON: a newline in an awk -v value is an error
# in macOS awk.
drop_negated() { # names, [exact]
  DN_NEG="$1" awk -v exact="${2:-}" '
    function key(s) { sub(/^[[:space:]]+/, "", s); sub(/[[:space:]].*/, "", s); sub(/,.*/, "", s); return s }
    function bare(s) { sub(/.*\//, "", s); return s }
    BEGIN { n = split(ENVIRON["DN_NEG"], a, "\n"); for (i = 1; i <= n; i++) if (a[i] != "") drop[key(a[i])] = 1 }
    { k = key($0); if (!(k in drop) && (exact != "" || !(bare(k) in drop))) print }'
}

# The node majors fnm keeps: common.node and <profile>.node, one per line,
# without a line's `link` field, each once: common's `26` and a profile's
# `26 link` are two manifest lines. With neither file, the three every
# machine had before they moved (sd:3966).
node_versions() {
  nv=$(manifest node | awk '!seen[$1]++ { print $1 }')
  printf '%s\n' "${nv:-18
20
24}"
}

# The bin folder of the node that installs the .npm CLIs: Homebrew's, of the
# first node formula the brew manifest lists. Every Homebrew node keeps its
# globals under $(brew --prefix), whose bin is on PATH in every shell; an fnm
# node keeps them per version, so the npm first on PATH depends on the shell
# (sd:4739).
brew_node_bin() {
  bn=$(manifest brew | grep -xE 'node(@[0-9]+)?' | head -1)
  [ -n "$bn" ] && command -v brew >/dev/null 2>&1 || return 1
  bn="$(bound_step 60 brew --prefix)/opt/$bn/bin"
  [ -x "$bn/npm" ] && [ -x "$bn/node" ] && echo "$bn"
}

# The bundle id that answers mailto: links: the first line of <profile>.mailto,
# else of common.mailto, else Chrome, the handler before it moved (sd:3966).
# A profile `!id` line drops common's id, as in every other kind (sd:4942).
mailto_handler() {
  mh=$(profile_entries "$PROFILE" mailto | head -1)
  [ -n "$mh" ] || mh=$(profile_entries common mailto \
    | drop_negated "$(profile_drops "$PROFILE" mailto)" | head -1)
  echo "${mh:-com.google.Chrome}"
}

# Every verb that reads a profile needs the profile directory. A missing one is
# a machine that has not been given its configuration yet, which is worth one
# clear line rather than a run that finds every manifest empty.
require_config() {
  [ -d "$PROFILE_DIR" ] && return 0
  echo "machine-setup.sh: no profile directory at $PROFILE_DIR" >&2
  echo "  Create it, or start from the examples:" >&2
  echo "    mkdir -p $CONFIG_DIR && cp -R $DIR/examples/. $CONFIG_DIR/" >&2
  echo "  MACHINE_SETUP_PROFILE_DIR (or SYSTEM_TOOLS_CONFIG) points elsewhere." >&2
  exit "${1:-1}"
}

# The profile names: every <name>.<kind> in the profile directory but common,
# or MACHINE_SETUP_PROFILES when it lists them. Read from the directory so a
# new machine's profile needs a file, not an edit here.
known_profiles() {
  if [ -n "${MACHINE_SETUP_PROFILES:-}" ]; then
    # shellcheck disable=SC2086 # a space-separated list, split on purpose
    printf '%s\n' $MACHINE_SETUP_PROFILES
    return 0
  fi
  for kp in "$PROFILE_DIR"/*.*; do
    [ -f "$kp" ] || continue
    kp=$(basename "$kp")
    printf '%s\n' "${kp%%.*}"
  done | grep -vx common | sort -u || :
}

load_profile() {
  require_config
  if [ -n "${MACHINE_SETUP_PROFILE:-}" ]; then
    PROFILE="$MACHINE_SETUP_PROFILE"
  elif [ -f "$STATE_FILE" ]; then
    PROFILE=$(head -1 "$STATE_FILE" | tr -d '\n\r')
  else
    die "no profile recorded. Run: $(basename "$0") setup <$(known_profiles | tr '\n' '|' | sed 's/|$//')>"
  fi
  valid_profile "$PROFILE"
}

valid_profile() {
  require_config
  case "$1" in
    ''|common|*[!A-Za-z0-9_-]*) ;;
    *) known_profiles | grep -qxF "$1" && return 0 ;;
  esac
  die "unknown profile '$1' (want: $(known_profiles | tr '\n' ' ' | sed 's/ $//' | sed 's/ /, /g'))"
}

# ---------------------------------------------------------------- stages ----

# No python is pinned (operator ruling, sd:7059). Each upgrade of the
# python@X.Y sd-serve runs is a new Python.app, which the application
# firewall blocked, so sd-serve admitted no satellite (sd:6463); the brew
# stage and the upgrade sweep allow it again instead (python_firewall).
# sd-serve's own uv python (SERVE_PYTHON_DIR, sd:6492) is no formula.

# Runs python3 -c CODE as sd-serve does: on the PATH its installed plist
# sets, as launchd finds it, else on the caller's (sd:6463 review). Prints
# nothing when it fails.
serve_python3() { # code
  _pp=$(plutil -extract EnvironmentVariables.PATH raw -o - \
    "$HOME/Library/LaunchAgents/$LABEL_PREFIX.sd-serve.plist" 2>/dev/null) || _pp=$PATH
  PATH=$_pp python3 -c "$1" 2>/dev/null || :
}

# Where sd-serve's python3 is installed (sys.base_prefix); empty when it fails.
python_prefix() {
  serve_python3 'import sys; print(sys.base_prefix)'
}

# Each formula an older brew stage pinned (python@*, opencode, codex) that
# is still pinned, one per line. A bare
# `brew upgrade` fails on a dependent that needs a newer pinned python.
# Fails when brew cannot list its pins: unknown, not "none pinned" (sd:3536).
stale_pins() {
  _sp=$(bound_step 300 brew list --pinned 2>/dev/null) || return 1
  printf '%s\n' "$_sp" | sed 's|.*/||' | grep -E '^(python@.*|opencode|codex)$' \
    || :
}

# What sd-serve's python3 runs as, to the application firewall, when the
# firewall does not allow it: blocked, or not listed, as a new Python.app
# after an upgrade is (sd:7059 review). A framework build, as brew's, runs
# as its Python.app; any other, as the uv one (sd:6492), as its executable,
# listed by its real path. Nothing where sd-serve is not installed.
unallowed_python() {
  [ -f "$HOME/Library/LaunchAgents/$LABEL_PREFIX.sd-serve.plist" ] || return 0
  _fw=/usr/libexec/ApplicationFirewall/socketfilterfw
  if command -v socketfilterfw >/dev/null 2>&1; then _fw=$(command -v socketfilterfw); fi
  [ -x "$_fw" ] || return 0
  _app=$(python_prefix)
  case $_app in
    "") return 0 ;;
    */Python.framework/*) _app="$_app/Resources/Python.app" ;;
    *) _app=$(serve_python3 'import os, sys; print(os.path.realpath(sys.executable))')
       [ -n "$_app" ] || return 0 ;;
  esac
  firewall_allows "$_app" || echo "$_app"
}

# mosh-server, when the profile installs mosh, as the firewall must admit
# it (sd:6538). Each upgrade is a new unsigned Cellar file, and the firewall
# matches a rule only on a signature, so even an allowed listing drops it.
# Repairs under --apply: an ad-hoc signature (the file is the user's), then
# the rule again through the repair drop-in's sudo -n (sd:6312).
mosh_firewall() {
  manifest brew | grep -qx mosh || return 0
  _fw=/usr/libexec/ApplicationFirewall/socketfilterfw
  if command -v socketfilterfw >/dev/null 2>&1; then _fw=$(command -v socketfilterfw); fi
  [ -x "$_fw" ] || return 0
  _bp=$(bound_step 60 brew --prefix 2>/dev/null) || return 0
  [ -x "$_bp/opt/mosh/bin/mosh-server" ] || return 0
  _ms="$(cd -P "$_bp/opt/mosh/bin" && pwd)/mosh-server"
  if ! codesign -dv "$_ms" >/dev/null 2>&1; then
    echo "  DIFFERS firewall drops $_ms: unsigned"
    run_ok chmod u+w "$_ms" && run_ok codesign -s - -f "$_ms" || return 0
  elif firewall_allows "$_ms"; then
    echo "  ok      firewall allows $_ms"
    return 0
  else
    echo "  DIFFERS firewall drops $_ms: not allowed"
  fi
  run_ok sudo -n "$_fw" --remove "$_ms" && run_ok sudo -n "$_fw" --add "$_ms" \
    && run_ok sudo -n "$_fw" --unblockapp "$_ms" || :
}

# sd-serve's python when the firewall does not allow it, reported; a brew
# Python.app is added and allowed under --apply through the repair drop-in's
# sudo -n (sd:7059). A uv python's fix stays the operator's (sd:6492).
# Fails when the allow does, so the upgrade sweep counts it.
python_firewall() {
  _bp=$(unallowed_python)
  [ -n "$_bp" ] || return 0
  echo "  DIFFERS firewall does not allow $_bp; allow it: System Settings > Network > Firewall > Options"
  case $_bp in */Python.framework/*/Resources/Python.app) ;; *) return 0 ;; esac
  _fw=/usr/libexec/ApplicationFirewall/socketfilterfw
  if command -v socketfilterfw >/dev/null 2>&1; then _fw=$(command -v socketfilterfw); fi
  run_ok sudo -n "$_fw" --add "$_bp" && run_ok sudo -n "$_fw" --unblockapp "$_bp"
}

# formula_installed NAME: brew's name for the installed formula that NAME
# names, printed, and 0; 1 when none is; 2 when brew info gave no answer, or
# one that is not a formula record: a failed probe is no "no" (sd:3536).
# brew resolves an alias or an old name, as `python` names python@3.15,
# which `brew list` prints (sd:7093).
formula_installed() {
  fx_json=$(bound_step 300 brew info --json=v2 --formula "$1" 2>/dev/null) || return 2
  printf '%s' "$fx_json" | python3 -c '
import json, sys
try:
    f = json.load(sys.stdin)["formulae"][0]
    kegs, name = f["installed"], f["full_name"]
except (ValueError, KeyError, IndexError, TypeError):
    sys.exit(2)
if not isinstance(kegs, list) or not isinstance(name, str) or not name:
    sys.exit(2)
if not kegs:
    sys.exit(1)
print(name)'
}

# pours_from_bottle NAME: 0 when NAME and each formula its install would add
# or upgrade pour from a bottle here, 1 when one needs a source build, 2 when
# brew gave no answer (sd:7103). The dry run lists them, with the build
# dependencies of one that builds; brew info marks each `(bottled)` by this
# machine's bottle tag, or an older one it can pour. A formula that ships no
# bottle, as many in a tap, counts as a build.
pours_from_bottle() {
  # The nightly install's switches (stage_brew): no refresh, and no
  # dependents check, which rebuilds a dependent with broken linkage from
  # source, bottle or not; the weekly upgrade keeps it.
  pb_plan=$(bound_step 300 env HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_INSTALLED_DEPENDENTS_CHECK=1 \
    brew install --dry-run "$1" 2>/dev/null) || return 2
  # A dependent brew would rebuild from source all the same.
  ! printf '%s\n' "$pb_plan" | grep -q '^==> Reinstalling .* with broken linkage from source:' || return 1
  pb_names=$(printf '%s\n' "$pb_plan" | awk '/^==> Would (install|upgrade) / { on = 1; next }
      /^==>/ { on = 0 } on && NF { print $1 }' | sort -u)
  [ -n "$pb_names" ] || return 2
  # shellcheck disable=SC2086 # one word per formula name
  pb_info=$(bound_step 300 env HOMEBREW_NO_ANALYTICS=1 brew info --formula $pb_names 2>/dev/null) || return 2
  pb_heads=$(printf '%s\n' "$pb_info" | grep -E '^==> [^ ]+( [^ ]+)?: ' || :)
  # One heading per formula, or the answer is not about these.
  [ "$(printf '%s\n' "$pb_heads" | grep -c .)" = "$(printf '%s\n' "$pb_names" | grep -c .)" ] || return 2
  if printf '%s\n' "$pb_heads" | grep -qv '(bottled)'; then return 1; fi
  # brew builds one from source when an installed keg of it was built with
  # options, which the install keeps, though brew info marks it `(bottled)`
  # (formula_installer.rb install_bottle_for?).
  # shellcheck disable=SC2086 # one word per formula name
  pb_json=$(bound_step 300 env HOMEBREW_NO_ANALYTICS=1 brew info --json=v2 --formula $pb_names 2>/dev/null) || return 2
  printf '%s' "$pb_json" | python3 -c '
import json, sys
try:
    fs = json.load(sys.stdin)["formulae"]
    kegs = [k for f in fs for k in f["installed"]]
    opts = [k["used_options"] for k in kegs]
except (ValueError, KeyError, TypeError):
    sys.exit(2)
if len(fs) != int(sys.argv[1]) or not all(isinstance(o, list) for o in opts):
    sys.exit(2)
sys.exit(1 if any(opts) else 0)' "$(printf '%s\n' "$pb_names" | grep -c .)"
}

# formula_state LIST NAME [any]: the profile formula NAME as `state name
# [installed]`, given LIST, what `brew list --formula` printed. ok: installed
# under its bare name, as a tap-qualified one lists, or the one brew resolves
# it to; bottle: missing, and it pours from bottles; source: missing, and it
# needs a source build, which only the weekly upgrade runs (sd:7103);
# missing, with `any`, which asks no bottle; unknown: brew could not tell.
formula_state() {
  if printf '%s\n' "$1" | sed 's|.*/||' | grep -qx "${2##*/}"; then
    echo "ok $2 ${2##*/}"
    return 0
  fi
  fs_rc=0
  fs_name=$(formula_installed "$2") || fs_rc=$?
  case $fs_rc in
    0) echo "ok $2 ${fs_name##*/}"; return 0 ;;
    1) ;;
    *) echo "unknown $2"; return 0 ;;
  esac
  if [ "${3:-}" = any ]; then
    echo "missing $2"
    return 0
  fi
  fs_rc=0
  pours_from_bottle "$2" || fs_rc=$?
  case $fs_rc in
    0) echo "bottle $2" ;;
    1) echo "source $2" ;;
    *) echo "unknown $2" ;;
  esac
}

# formula_plan LIST [any]: formula_state for each profile formula.
formula_plan() {
  manifest brew | while read -r fp_f; do
    formula_state "$1" "$fp_f" "${2:-}"
  done
}

# The profile's formulae that are not installed, one per line, for the
# upgrade sweep: the brew stage leaves one that needs a source build to it
# (sd:7103). None with no usable profile. Fails when brew cannot tell for
# one; the rest still print.
formula_wants() {
  (load_profile) >/dev/null 2>&1 || return 0
  load_profile
  fw_list=$(bound_step 300 brew list --formula 2>/dev/null) || return 1
  fw_plan=$(formula_plan "$fw_list" any)
  printf '%s\n' "$fw_plan" | awk '$1 == "missing" { print $2 }'
  ! printf '%s\n' "$fw_plan" | grep -q '^unknown '
}

stage_brew() {
  echo "== brew"
  command -v brew >/dev/null 2>&1 || {
    echo "  brew MISSING — install first: https://brew.sh"
    return 0
  }

  # A list brew could not give is unknown, not empty: read as empty, it would
  # install every formula again, each up to its bound (sd:3536).
  if ! taps=$(bound_step 300 brew tap 2>/dev/null) \
      || ! formulae=$(bound_step 300 brew list --formula 2>/dev/null) \
      || ! installed_casks=$(bound_step 300 brew list --cask 2>/dev/null); then
    note_failure "brew stage: brew could not list what is installed; nothing changed" 1
    return 0
  fi


  manifest tap | while read -r t; do
    if printf '%s\n' "$taps" | grep -qx "$t"; then
      echo "  ok      tap $t"
    else
      echo "  MISSING tap $t"
      run bound_step 600 brew tap "$t"
    fi
  done

  # Each formula's bottle check and its install read one Homebrew state
  # (sd:7103 review): the check runs just before the install, after the
  # one before it, and both with auto-update off, so the install pulls no
  # newer version the check never saw, which might have no bottle. The
  # weekly upgrade's `brew update` refreshes that state. With the
  # dependents check off, the install rebuilds no dependent with broken
  # linkage from source; the weekly upgrade keeps that check. A subshell,
  # as the exports must not reach the stages after this one.
  (
    HOMEBREW_NO_AUTO_UPDATE=1
    HOMEBREW_NO_INSTALLED_DEPENDENTS_CHECK=1
    export HOMEBREW_NO_AUTO_UPDATE HOMEBREW_NO_INSTALLED_DEPENDENTS_CHECK
    manifest brew | while read -r f; do
      # shellcheck disable=SC2046 # `state name [installed]`, one word each
      set -- $(formula_state "$formulae" "$f")
      case $1 in
        ok) if [ "$3" = "${f##*/}" ]; then echo "  ok      $f"; else echo "  ok      $f ($3)"; fi ;;
        bottle) echo "  MISSING $f"
                run bound_step 1800 brew install "$f" ;;
        source) echo "  DIFFERS $f needs a source build; the weekly upgrade installs it" ;;
        *) echo "  MISSING $f"
           note_failure "brew stage: brew could not tell whether $f is installed or pours from a bottle; not installed" 1 ;;
      esac
    done
  )

  installed_casks=$(printf '%s\n' "$installed_casks" | sort)
  manifest cask | while read -r c; do
    if echo "$installed_casks" | grep -qx "$c"; then
      echo "  ok      cask $c"
    else
      # --adopt: a cask whose app is already sitting in /Applications from a
      # manual download aborts with "It seems there is already an App at
      # ...", and the drift never clears no matter how often setup runs.
      # Adopt takes the existing artifact under brew's management instead of
      # overwriting it; on a machine that does not have the app it changes
      # nothing. maccy and zoom were both hand-installed here and blocked the
      # brew stage until this.
      echo "  MISSING cask $c"
      run bound_step 1800 brew install --cask --adopt "$c"
    fi
  done
  # Every run, not only after an install: the nightly update clears a cask
  # moved by hand, or before this check existed.
  claude_quarantine || note_failure "brew stage: claude keeps com.apple.quarantine; launchd jobs would wait on a Gatekeeper prompt" 1
  codex_quarantine || note_failure "brew stage: codex keeps com.apple.quarantine; launchd jobs would wait on a Gatekeeper prompt" 1

  if ! pins=$(stale_pins); then
    note_failure "brew stage: brew could not list its pins; none unpinned" 1
    return 0
  fi
  for f in $pins; do
    echo "  DIFFERS $f pinned"
    run bound_step 300 brew unpin "$f"
  done
  python_firewall || :
  mosh_firewall
  tailscale_cli_link
}

# True when the profile runs Tailscale as the tailscale-app cask, not the
# brew formula (sd:6475).
tailscale_runs_app() {
  ! manifest brew | sed 's|.*/||' | grep -qx tailscale &&
    manifest cask | grep -qx tailscale-app
}

# The tailscale-app cask ships no CLI, and sd-db, repo-sync.sh and this
# script call `tailscale` from PATH (sd:6554). A wrapper goes in
# ~/.local/bin, first on JOB_PATH and the login PATH: through a symlink the
# app dies, its bundle identifier unknown (sd:6583). The wrapper is the
# user's own file, so no sudo; mv reported a link root-owned because it
# stats the app binary behind it. The formula's own CLI replaces it.
tailscale_cli_link() {
  tl_link="$HOME/.local/bin/tailscale"
  tl_app="$APPLICATIONS_DIR/Tailscale.app/Contents/MacOS/Tailscale"
  tl_exec="exec \"$tl_app\" \"\$@\""
  tl_ours=0 tl_stale=0
  if [ -L "$tl_link" ]; then
    [ "$(readlink "$tl_link")" != "$tl_app" ] || tl_stale=1
  elif [ -f "$tl_link" ] &&
       [ "$(cat "$tl_link")" = "$(printf '#!/bin/sh\n%s' "$tl_exec")" ]; then
    # Only the whole wrapper this writes is ours: one with the operator's
    # own lines around the exec line stays theirs, never deleted.
    tl_ours=1
  fi
  if tailscale_runs_app; then
    if [ "$tl_ours" -eq 1 ]; then
      echo "  ok      tailscale CLI wrapper $tl_link"
    elif [ "$tl_stale" -eq 0 ] && { [ -e "$tl_link" ] || [ -L "$tl_link" ]; }; then
      echo "  DIFFERS $tl_link is not a wrapper for $tl_app; move it aside"
    else
      if [ "$tl_stale" -eq 1 ]; then
        echo "  STALE   tailscale CLI link $tl_link; the app dies through a symlink, so a wrapper replaces it"
      else
        echo "  MISSING tailscale CLI wrapper $tl_link: $tl_exec"
      fi
      # rm first: a write through the old symlink would land in the app.
      run sh -c 'mkdir -p "${1%/*}" && rm -f "$1" &&
        printf "#!/bin/sh\n%s\n" "$2" > "$1" && chmod 755 "$1"' sh "$tl_link" "$tl_exec"
    fi
  elif [ "$tl_ours" -eq 1 ] || [ "$tl_stale" -eq 1 ]; then
    echo "  EXTRA   tailscale CLI wrapper $tl_link; the profile runs no tailscale-app cask"
    run rm "$tl_link"
  fi
}

# Tools from an upstream GitHub release archive, for a machine where brew
# builds them from source (sd:4853). A .release line is
# `<bins> <owner/repo> <asset> [<tag>]`: <bins> a comma list of the
# executables to take from the archive, the first naming the entry; <asset>
# the archive's name, where {version} is the tag less a leading v and {tag}
# the tag, or a bare executable's name when <bins> names one bin (herdr
# publishes one, sd:6439); <tag> pins the entry, and the upgrade sweep moves only unpinned
# ones. An entry is ok when each bin is in RELEASE_BIN and its record names
# its tag, owner/repo and asset. A record an older version wrote holds the
# tag alone, so that entry reinstalls once (sd:4916).
stage_release() {
  echo "== release"
  rel=$(manifest release)
  if [ -z "$rel" ]; then
    echo "  no release tools in this profile"
    return 0
  fi
  printf '%s\n' "$rel" | release_conflicts | while read -r rs_bin rs_n rs_lines; do
    note_failure "release: $rs_n lines take $rs_bin ($rs_lines); keep one, with a !<name> line in the profile's .release for common's; none installs until then" 1
  done
  rs_bad=$(printf '%s\n' "$rel" | release_conflicts names)
  printf '%s\n' "$rel" | while IFS= read -r line; do
    if ! release_parse "$line"; then
      note_failure "release: cannot read the line '$line' (want <bins> <owner/repo> <asset .tar.gz|.tgz|.zip, or a bare executable for one bin> [<tag>])" 1
      continue
    fi
    ! printf '%s\n' "$rs_bad" | grep -qxF "$rl_name" || continue
    read -r rs_have rs_src <<EOF
$(release_record "$rl_name")
EOF
    rs_gone=$(release_missing_bins "$rl_bins")
    if [ -n "$rs_gone" ]; then
      echo "  MISSING release $rl_name (no $rs_gone in $RELEASE_BIN)"
    elif [ -z "$rs_have" ]; then
      echo "  MISSING release $rl_name (no tag recorded)"
    elif [ -n "$rl_tag" ] && [ "$rs_have" != "$rl_tag" ]; then
      echo "  DIFFERS release $rl_name $rs_have, pinned $rl_tag"
    elif [ "$rs_src" != "$rl_repo $rl_asset" ]; then
      echo "  DIFFERS release $rl_name $rs_have from ${rs_src:-no recorded source}, now $rl_repo $rl_asset"
    else
      echo "  ok      release $rl_name $rs_have"
      continue
    fi
    run release_install "$rl_name" "$rl_bins" "$rl_repo" "$rl_asset" "$rl_tag"
  done
}

# Each bin that two or more .release lines on stdin take, as
# `<bin> <count> '<line>' and '<line>'...`; with `names`, the entry name of
# each such line, once. Such lines write one bin, and lines of one name read
# one record, so each run would install one over the other. Neither may be
# taken for the profile's choice: the stage and the sweep act on none of
# them, and the bins and record already there stay (sd:4942).
release_conflicts() { # [names]
  awk -v q="'" -v names="${1:-}" '
    { first[NR] = $1; sub(/,.*/, "", first[NR]); nb = split($1, b, ",")
      for (j = 1; j <= nb; j++) {
        if (own[NR, b[j]]++) continue
        if (!(b[j] in c)) o[++k] = b[j]
        c[b[j]]++; l[b[j]] = (c[b[j]] > 1 ? l[b[j]] " and " : "") q $0 q; bins[NR] = bins[NR] " " b[j]
      } }
    END {
      if (names == "") { for (i = 1; i <= k; i++) if (c[o[i]] > 1) print o[i], c[o[i]], l[o[i]]; exit }
      for (r = 1; r <= NR; r++) { nb = split(bins[r], b, " ")
        for (j = 1; j <= nb; j++) if (c[b[j]] > 1) { if (!seen[first[r]]++) print first[r]; break } }
    }'
}

# Splits one .release line into rl_bins, rl_repo, rl_asset, rl_tag and
# rl_name, or fails on a line of another shape. Each part lands in a path, a
# URL or a sed replacement, so each keeps to characters those take as written.
release_parse() { # line
  rl_tag=""; rl_extra=""
  read -r rl_bins rl_repo rl_asset rl_tag rl_extra <<EOF
$1
EOF
  rl_name=${rl_bins%%,*}
  [ -n "$rl_asset" ] && [ -z "$rl_extra" ] || return 1
  case ",$rl_bins," in *,,*|*,.,*|*,..,*|*[!A-Za-z0-9._,-]*) return 1 ;; esac
  case "$rl_repo" in */*/*|/*|*/|.*|*/.*|*[!A-Za-z0-9._/-]*) return 1 ;; ?*/?*) ;; *) return 1 ;; esac
  case "$rl_asset" in
    *[!A-Za-z0-9._{}+-]*) return 1 ;;
    *.tar.gz|*.tgz|*.zip) ;;
    *) case "$rl_bins" in *,*) return 1 ;; esac ;;  # a bare executable is one bin
  esac
  [ -z "$rl_tag" ] || release_tag_ok "$rl_tag"
}

release_tag_ok() { # tag
  case "$1" in ''|.|..|*[!A-Za-z0-9._+-]*) return 1 ;; esac
}

# The record of an entry, `<tag> <owner/repo> <asset>`, or nothing.
release_record() { # name
  cat "$RELEASE_STATE/$1.tag" 2>/dev/null || :
}

# The bins of a comma list that are not an executable file in RELEASE_BIN.
release_missing_bins() { # bins
  for rb_b in $(printf '%s\n' "$1" | tr ',' ' '); do
    [ -f "$RELEASE_BIN/$rb_b" ] && [ -x "$RELEASE_BIN/$rb_b" ] || printf '%s\n' "$rb_b"
  done | tr '\n' ' ' | sed 's/ $//'
}

# The tag GitHub's latest-release redirect names. An answer that names no
# release tag is unknown, not a tag, and fails.
release_latest() { # owner/repo
  rlt_url=$(curl -fsS --max-time 30 -o /dev/null -w '%{redirect_url}' "$RELEASE_BASE/$1/releases/latest" </dev/null) || return 1
  case "$rlt_url" in */releases/tag/?*) ;; *) return 1 ;; esac
  rlt_tag=${rlt_url##*/releases/tag/}
  release_tag_ok "$rlt_tag" || return 1
  printf '%s\n' "$rlt_tag"
}

# Installs an entry's bins at TAG, else at the latest tag, then records the
# tag, owner/repo and asset. Run through run_ok or a condition, where set -e is off, so each step
# checks its own result. No bin moves into RELEASE_BIN before every bin is
# found and copied beside its place; the old record goes before the first
# move and the new one comes last, so a run that stops between them leaves
# the entry MISSING for the next run.
release_install() { # name, bins, owner/repo, asset, [tag]
  ri_tag=$5
  if [ -z "$ri_tag" ] && ! ri_tag=$(release_latest "$3"); then
    echo "  release $1: no latest release tag at $RELEASE_BASE/$3/releases/latest"
    return 1
  fi
  ri_tmp=$(mktemp -d) || return 1
  ri_rc=0
  release_fetch "$1" "$2" "$3" "$4" "$ri_tag" || ri_rc=$?
  rm -rf "$ri_tmp"
  return "$ri_rc"
}

release_fetch() { # name, bins, owner/repo, asset, tag (into $ri_tmp)
  rf_asset=$(printf '%s\n' "$4" | sed -e "s/{version}/${5#v}/g" -e "s/{tag}/$5/g")
  rf_url="$RELEASE_BASE/$3/releases/download/$5/$rf_asset"
  if ! curl -fsSL --max-time 600 -o "$ri_tmp/$rf_asset" "$rf_url" </dev/null; then
    echo "  release $1: cannot download $rf_url"
    return 1
  fi
  # A checksum file that is not there is a release that publishes none; one
  # that could not be read is unknown, and installs nothing.
  if ! rf_code=$(curl -sSL --max-time 60 -o "$ri_tmp/sum" -w '%{http_code}' "$rf_url.sha256" </dev/null); then
    echo "  release $1: cannot read $rf_url.sha256; not installed"
    return 1
  fi
  case "$rf_code" in
    200)
      rf_want=$(awk 'NR == 1 { print tolower($1) }' "$ri_tmp/sum")
      rf_have=$(shasum -a 256 "$ri_tmp/$rf_asset" | awk '{ print $1 }')
      if [ "${#rf_want}" -ne 64 ] || [ "$rf_want" != "$rf_have" ]; then
        echo "  release $1: checksum mismatch for $rf_asset (got $rf_have, published ${rf_want:-nothing}); not installed"
        return 1
      fi ;;
    404) echo "  release $1: no checksum published for $rf_asset; not verified" ;;
    *)
      echo "  release $1: $rf_url.sha256 answered $rf_code; not installed"
      return 1 ;;
  esac
  mkdir "$ri_tmp/x" || return 1
  case "$rf_asset" in
    *.zip)
      if command -v unzip >/dev/null 2>&1; then
        unzip -q "$ri_tmp/$rf_asset" -d "$ri_tmp/x" </dev/null
      else
        ditto -x -k "$ri_tmp/$rf_asset" "$ri_tmp/x"
      fi ;;
    *.tar.gz|*.tgz) tar -xzf "$ri_tmp/$rf_asset" -C "$ri_tmp/x" ;;
    *) cp "$ri_tmp/$rf_asset" "$ri_tmp/x/$2" && chmod 755 "$ri_tmp/x/$2" ;;
  esac || { echo "  release $1: cannot unpack $rf_asset"; return 1; }
  rf_bins=$(printf '%s\n' "$2" | tr ',' ' ')
  for rf_b in $rf_bins; do
    find "$ri_tmp/x" -type f -name "$rf_b" -perm -100 > "$ri_tmp/path.$rf_b" || return 1
    rf_n=$(grep -c '^' "$ri_tmp/path.$rf_b" || :)
    if [ "$rf_n" -ne 1 ]; then
      echo "  release $1: $rf_asset holds $rf_n executable file(s) named $rf_b; want 1"
      return 1
    fi
    if [ -d "$RELEASE_BIN/$rf_b" ]; then
      echo "  release $1: $RELEASE_BIN/$rf_b is a folder; not replaced"
      return 1
    fi
  done
  mkdir -p "$RELEASE_BIN" || return 1
  # A copy an interrupted run left beside its place goes first.
  for rf_b in $rf_bins; do
    rm -f "$RELEASE_BIN/.$rf_b.release."*
    if ! cp "$(cat "$ri_tmp/path.$rf_b")" "$RELEASE_BIN/.$rf_b.release.$$" \
        || ! chmod 755 "$RELEASE_BIN/.$rf_b.release.$$"; then
      for rf_c in $rf_bins; do rm -f "$RELEASE_BIN/.$rf_c.release.$$"; done
      echo "  release $1: cannot copy $rf_b into $RELEASE_BIN"
      return 1
    fi
  done
  # The record goes before the first bin moves: a run that stops between two
  # moves, or before the record, leaves the entry MISSING, never ok over a
  # mix of two tags' bins, pinned or not (sd:4853, lane round 1).
  if ! rm -f "$RELEASE_STATE/$1.tag"; then
    for rf_c in $rf_bins; do rm -f "$RELEASE_BIN/.$rf_c.release.$$"; done
    echo "  release $1: cannot remove its tag record; nothing moved"
    return 1
  fi
  for rf_b in $rf_bins; do
    if ! mv -f "$RELEASE_BIN/.$rf_b.release.$$" "$RELEASE_BIN/$rf_b"; then
      for rf_c in $rf_bins; do rm -f "$RELEASE_BIN/.$rf_c.release.$$"; done
      echo "  release $1: cannot move $rf_b into place; no tag recorded"
      return 1
    fi
  done
  { mkdir -p "$RELEASE_STATE" && printf '%s %s %s\n' "$5" "$3" "$4" > "$RELEASE_STATE/$1.tag.new" \
      && mv -f "$RELEASE_STATE/$1.tag.new" "$RELEASE_STATE/$1.tag"; } || return 1
  echo "  installed release $1 $5"
}

# The profile the upgrade sweep reads the .release entries of: resolved as
# doctor does, without load_profile's exit, so a machine with no usable
# profile still gets its brew and App Store sweep.
release_profile() {
  [ -d "$PROFILE_DIR" ] || return 1
  doctor_profile
  [ -n "$PROFILE" ] && known_profiles | grep -qxF "$PROFILE"
}

# Each unpinned entry whose latest tag is not the one recorded, as
# `<name> <recorded> <latest> <line>`, and each whose latest tag could not be
# read, or whose line could not be, as `<name> unknown`; then fails when
# there was one of those. A here-document, not a pipe, so $rlo_rc survives.
release_outdated() {
  release_profile || return 0
  rlo_rc=0
  rlo_rel=$(manifest release)
  [ -n "$rlo_rel" ] || return 0
  rlo_bad=$(printf '%s\n' "$rlo_rel" | release_conflicts names)
  for rlo_name in $rlo_bad; do
    printf '%s unknown\n' "$rlo_name"; rlo_rc=1
  done
  while IFS= read -r rlo_line; do
    if ! release_parse "$rlo_line"; then
      printf '%s unknown\n' "${rlo_line%%[[:space:]]*}"; rlo_rc=1; continue
    fi
    ! printf '%s\n' "$rlo_bad" | grep -qxF "$rl_name" || continue
    [ -z "$rl_tag" ] || continue
    if ! rlo_new=$(release_latest "$rl_repo"); then
      printf '%s unknown\n' "$rl_name"; rlo_rc=1; continue
    fi
    rlo_have=$(release_record "$rl_name")
    rlo_have=${rlo_have%% *}
    [ "$rlo_new" = "$rlo_have" ] || printf '%s %s %s %s\n' "$rl_name" "${rlo_have:-none}" "$rlo_new" "$rlo_line"
  done <<EOF
$rlo_rel
EOF
  return "$rlo_rc"
}

# The names of the unpinned .release entries, space-separated; nothing read
# from the network.
release_unpinned() {
  release_profile || return 0
  manifest release | while IFS= read -r ru_line; do
    if release_parse "$ru_line"; then
      [ -n "$rl_tag" ] || printf '%s\n' "$rl_name"
    fi
  done | tr '\n' ' ' | sed 's/ $//'
}

# The upgrade sweep's .release half: each unpinned entry moves to its latest
# tag. Adds `release <name>` to $failed for a tag it could not read or install.
# A dry run reads no tag, as the brew half reads nothing: it names the
# entries it would check.
release_sweep() {
  if [ "$APPLY" -ne 1 ]; then
    rsw=$(release_unpinned)
    [ -z "$rsw" ] || echo "  [dry-run] release: check the latest tag of $rsw"
    return 0
  fi
  rsw=$(release_outdated) || :
  [ -n "$rsw" ] || return 0
  while read -r rsw_name rsw_old rsw_new rsw_line; do
    if [ "$rsw_old" = unknown ]; then
      echo "  FAILED  release $rsw_name: no latest release tag, a line of another shape, or a bin another line takes too"
      failed="$failed, release $rsw_name"
    elif release_parse "$rsw_line" \
        && release_install "$rl_name" "$rl_bins" "$rl_repo" "$rl_asset" "$rsw_new" </dev/null; then
      echo "  upgraded release $rsw_name $rsw_old -> $rsw_new"
    else
      failed="$failed, release $rsw_name"
    fi
  done <<EOF
$rsw
EOF
}

# Converge the login shell from macOS's default zsh to Homebrew bash. The
# repo's interactive tooling is bash-first (local-cswap, cron-jobs' `local`),
# and /bin/bash is the frozen 3.2 from 2007, so the shell worth converging to
# is brew's. Runs as part of setup and stands alone afterwards:
# `machine-setup.sh update shell --apply`.
#
# Ordered directly after stage_brew on purpose — it needs brew — but does not
# assume brew succeeded: a fresh macOS has no Homebrew at all, so this stage
# ensures it (PATH first, then the official installer) rather than telling the
# user to come back later. Paths come from `brew --prefix` — /opt/homebrew on
# Apple Silicon, /usr/local on Intel; hardcoding one already burned doctor's
# login-shell check once (see cmd_doctor).
#
# Switching the login shell does NOT orphan the credential layer: brew bash
# reads ~/.bash_profile, which sources ~/.config/shell/env.sh, same as
# ~/.zshenv does for zsh. The stage verifies that loader line instead of
# assuming it, since ~/.bash_profile is machine-local (never captured).
stage_shell() {
  echo "== shell"

  # brew may be installed but not on PATH yet (fresh machine, this stage before
  # any shell rc exists) — the prefix per architecture is fixed, so look there
  # before concluding it is absent.
  if ! command -v brew >/dev/null 2>&1; then
    for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do
      if [ -x "$b" ]; then
        eval "$(bound_step 60 "$b" shellenv)"
        break
      fi
    done
  fi
  if ! command -v brew >/dev/null 2>&1; then
    # NONINTERACTIVE skips the installer's confirmation prompt; it still
    # needs sudo internally, so on a no-admin machine this fails and the
    # stage reports MISSING rather than half-converting.
    # shellcheck disable=SC2016 # the inner bash expands it
    run bound_step 1800 /bin/bash -c 'NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL --max-time 300 https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
    for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do
      [ -x "$b" ] && eval "$(bound_step 60 "$b" shellenv)" && break
    done
  fi
  if ! command -v brew >/dev/null 2>&1; then
    echo "  brew MISSING — install failed or dry run; https://brew.sh, then re-run: $(basename "$SELF") update shell --apply"
    return 0
  fi
  echo "  ok      brew present ($(command -v brew))"

  BREW_BASH="$(bound_step 60 brew --prefix)/bin/bash"
  if [ -x "$BREW_BASH" ]; then
    echo "  ok      brew bash installed ($BREW_BASH)"
  else
    echo "  MISSING brew bash ($BREW_BASH)"
    run bound_step 1800 brew install bash
    if [ "$APPLY" -eq 1 ] && [ ! -x "$BREW_BASH" ]; then
      echo "  FAIL    brew install bash did not produce $BREW_BASH"
      return 0
    fi
  fi

  # chsh refuses a shell not listed in /etc/shells, and that file is
  # root-owned — the one genuinely privileged step here, so it rides the
  # DEFER machinery like every other sudo action.
  if grep -qx "$BREW_BASH" /etc/shells 2>/dev/null; then
    echo "  ok      $BREW_BASH in /etc/shells"
  else
    echo "  MISSING $BREW_BASH in /etc/shells"
    sudo_run "add $BREW_BASH to /etc/shells" sh -c "echo $BREW_BASH >> /etc/shells"
  fi

  # dscl, not $SHELL: $SHELL is what this process inherited, which after a
  # chsh in the same run is exactly the stale value.
  me="$(id -un)"
  current="$(dscl . -read "$DS_USERS/$me" UserShell 2>/dev/null | awk '{print $2}')"
  if [ "$current" = "$BREW_BASH" ]; then
    echo "  ok      login shell is brew bash"
  else
    echo "  DIFFERS login shell is ${current:-unset} — want $BREW_BASH"
    # sudo chsh rather than bare chsh: chsh for one's own account prompts
    # for the account password interactively, which no unattended run can
    # answer; sudo (cached or NOPASSWD) makes it non-interactive, and on a
    # no-sudo machine it defers visibly instead of hanging on a prompt.
    sudo_run "chsh -s $BREW_BASH $me  (login shell: ${current:-unknown} -> brew bash)" chsh -s "$BREW_BASH" "$me"
  fi

  if grep -q 'config/shell/env.sh' "$HOME/.bash_profile" 2>/dev/null; then
    echo "  ok      ~/.bash_profile loads ~/.config/shell/env.sh"
  else
    # ~/.bash_profile is machine-local (never captured), so converge it here
    # rather than just warning: without the loader a bash login shell sees
    # none of the credential layer.
    echo "  MISSING ~/.bash_profile loader for ~/.config/shell/env.sh"
    run sh -c "printf '%s\n' '[ -f \"\$HOME/.config/shell/env.sh\" ] && . \"\$HOME/.config/shell/env.sh\"' >> \"\$HOME/.bash_profile\""
  fi
}

stage_appstore() {
  echo "== appstore"
  # NOT named `want`/`have`: sh functions share globals, and `want` is the
  # stage filter do_stages is iterating on. Clobbering it made every later
  # stage execute even when a single stage was requested.
  apps=$(manifest mas)
  if [ -z "$apps" ]; then
    echo "  no App Store apps in this profile"
    return 0
  fi
  command -v mas >/dev/null 2>&1 || { echo "  mas MISSING (brew install mas)"; return 0; }

  # `mas list` prints "<id>  <name>  (<version>)"; compare on the id alone,
  # since names carry spaces and versions drift.
  # A listing mas could not give is unknown, not empty: read as empty, it
  # would try every install again, each up to its bound (sd:3536).
  if ! mas_listing=$(bound_step 300 mas list 2>/dev/null); then
    echo "  FAILED  mas list did not answer; App Store apps not checked"
    return 0
  fi
  installed_apps=$(printf '%s\n' "$mas_listing" | awk '{print $1}' | sort)
  # A beta or TestFlight build lists under id 0, so the id alone reads it as
  # missing. Its name still matches: "<id>  <name>  (<version>)", name trimmed.
  installed_names=$(printf '%s\n' "$mas_listing" \
    | sed -e 's/^[[:space:]]*[0-9][0-9]*[[:space:]]*//' -e 's/[[:space:]]*([^()]*)[[:space:]]*$//')
  echo "$apps" | while read -r line; do
    id=$(echo "$line" | awk '{print $1}')
    name=$(echo "$line" | cut -d' ' -f2-)
    case "$id" in
      ''|*[!0-9]*) echo "  SKIP    malformed entry: $line"; continue ;;
    esac
    if echo "$installed_apps" | grep -qx "$id"; then
      echo "  ok      $id $name"
    elif [ -n "$name" ] && printf '%s\n' "$installed_names" | grep -qxF "$name"; then
      echo "  ok      $id $name (installed under another id, such as a beta)"
    else
      # Said out loud, not left implicit in the `run` line. A dry run printed
      # only "[dry-run] mas install <id>", which `status` does not count, so a
      # missing App Store app added nothing to the drift number.
      echo "  MISSING $id $name"
      # `mas install` can need sudo, which a nightly run under launchd does
      # not have. A failure here used to end the loop and, under set -e, the
      # whole run: every later stage was skipped. Report it and go on.
      if ! try_run bound_step 1800 mas install "$id"; then
        echo "  FAILED  mas install $id $name — install it by hand"
      fi
    fi
  done
}

stage_bin() {
  echo "== bin"
  if [ -x "$ROOT/local-bin-links/bin-links.sh" ]; then
    if [ "$APPLY" -eq 1 ]; then
      # The pipe hides the tool's exit status; the braces keep it (sd:3543).
      { "$ROOT/local-bin-links/bin-links.sh" install || note_failure "bin-links.sh install" "$?"; } | sed 's/^/  /'
    else
      # Not tagged [dry-run]: `status` is already read-only and reports what
      # IS, so tagging it as a would-be action misread as "would link these"
      # and hid the MISSING/DIFFERS states from the drift count.
      "$ROOT/local-bin-links/bin-links.sh" status | sed 's/^/  /'
    fi
  else
    echo "  local-bin-links missing from the repo"
  fi
}

stage_dotfiles() {
  echo "== dotfiles"
  # Scratch for dotfile_view; the record is a hash of what the stage judges,
  # so a session line written after install does not read as a local edit.
  view=$(mktemp)
  for f in $DOTFILES; do
    src=$(dotfile_src "$f")
    dst="$HOME/$f"
    if [ -z "$src" ]; then
      echo "  --      $f not captured for $PROFILE yet"
    elif [ ! -e "$dst" ]; then
      # Said out loud, same lesson as the appstore stage: dry-run lines alone
      # are invisible to status's drift count.
      echo "  MISSING $f"
      # Entries may be nested (.ssh/config, .config/gh/config.yml), so the
      # parent directory may not exist on a fresh machine.
      if run_ok mkdir -p "$(dirname "$dst")" && copy_dotfile "$f" "$src" "$dst"; then
        record_hash "dotfiles/$f" "$(dotfile_view "$f" "$dst" "$view")"
      fi
    elif cmp -s "$src" "$(dotfile_view "$f" "$dst" "$view")"; then
      record_hash "dotfiles/$f" "$(dotfile_view "$f" "$dst" "$view")"
      # The content alone read ok after a chmod that failed past its copy
      # (sd:5940). find -perm is POSIX; a failed probe reads as wrong.
      # -H reads a symlink's target, the file cmp and chmod act on (sd:5960).
      mode=$(dotfile_mode "$f")
      if [ -n "$mode" ] && [ -z "$(find -H "$dst" -prune -perm "$mode" 2>/dev/null)" ]; then
        echo "  DIFFERS $f — mode is not $mode"
        run chmod "$mode" "$dst"
      else
        echo "  ok      $f"
      fi
    else
      # The machine file still being what we installed means nobody here
      # touched it, so the repo is what moved.
      if matches_record "dotfiles/$f" "$(dotfile_view "$f" "$dst" "$view")"; then
          echo "  STALE   $f — untouched here, repo moved ahead"
          if run_ok backup_file "$dst" && copy_dotfile "$f" "$src" "$dst"; then
            record_hash "dotfiles/$f" "$(dotfile_view "$f" "$dst" "$view")"
          fi
      elif has_record "dotfiles/$f"; then
          if [ "$FORCE" -eq 1 ]; then
            echo "  DIFFERS $f — edited here; --force overwrites (backup kept)"
            if run_ok backup_file "$dst" && copy_dotfile "$f" "$src" "$dst"; then
              record_hash "dotfiles/$f" "$(dotfile_view "$f" "$dst" "$view")"
            fi
          else
            echo "  DIFFERS $f — edited here; keep it with 'capture --apply', discard it with --force"
          fi
      elif [ "$FORCE" -eq 1 ]; then
          echo "  DIFFERS $f — nothing recorded; --force overwrites (backup kept)"
          if run_ok backup_file "$dst" && copy_dotfile "$f" "$src" "$dst"; then
            record_hash "dotfiles/$f" "$(dotfile_view "$f" "$dst" "$view")"
          fi
      else
          echo "  DIFFERS $f — machine and repo disagree, nothing recorded says which moved; review, then 'capture --apply' or --force"
      fi
    fi
  done
  rm -f "$view"
  for f in $DOTFILES_DENY; do
    echo "  skip    $f (holds live credentials — never tracked)"
  done
}

# Does the live .env still honour every assignment its template defines?
#
# This used to be `cmp -s`, byte equality, and it could never hold. The
# template deliberately omits NTFY_TOPIC — that value doubles as the ntfy.sh
# secret and this template is committed — while the live .env must carry it.
# So the check was red whether the machine was right or wrong, including for
# the entire period EMAIL_FROM pointed at the wrong Google account. A check
# that reads the same in both states cannot detect anything; that drift is
# precisely what it existed to catch.
#
# Keys the template does not mention are ignored: carrying a secret the
# template refuses to hold is the normal, intended state. Values ARE compared,
# because a template default silently replaced on the machine is the case worth
# a human look. Prints the offending key names, never their values.
env_template_diff() {
  _src=$1; _dst=$2; _bad=""
  while IFS= read -r _line || [ -n "$_line" ]; do
    case "$_line" in *=*) ;; *) continue ;; esac
    _k=${_line%%=*}
    _k=$(printf '%s' "$_k" | tr -d '[:space:]')
    case "$_k" in ''|\#*) continue ;; esac
    _want=${_line#*=}
    _have=$(sed -n "s/^[[:space:]]*$_k=//p" "$_dst" 2>/dev/null | head -1)
    if [ "$_have" != "$_want" ]; then _bad="$_bad $_k"; fi
  done < "$_src"
  [ -z "$_bad" ] && return 0
  printf '%s' "${_bad# }"
  return 1
}

# The live .env a repository folder's tool reads: the tool's config folder
# ($SYSTEM_TOOLS_CONFIG/<folder minus local->/.env), or the folder's own .env
# where an older checkout still keeps one and the config file does not exist.
live_env() {
  le="$SYSTEM_TOOLS_CONFIG/${1#local-}/.env"
  if [ ! -f "$le" ] && [ -f "$ROOT/$1/.env" ]; then
    le="$ROOT/$1/.env"
  fi
  echo "$le"
}

# Appends each key's template line to the live file, after one backup. Run
# under `if`, where set -e is off, so each step returns its own failure.
env_append() { # src, dst, key...
  ea_src=$1
  ea_dst=$2
  shift 2
  backup_file "$ea_dst" || return
  # One sed, no pipe: a pipe into head reported head's status, so a failed
  # read of the template counted as an append.
  for k in "$@"; do
    sed -n "/^[[:space:]]*$k=/{s/^[[:space:]]*//p;q;}" "$ea_src" >> "$ea_dst" || return
  done
}

# Tool .env files, seeded from the templates in envs/. Always 0600: they sit
# exactly where a secret would go, and the .env is what every tool here reads
# first.
stage_envs() {
  echo "== envs"
  for f in $ENVS; do
    src=$(env_src "$f")
    dst=$(live_env "$f")
    if [ -z "$src" ]; then
      echo "  --      $f/.env no template for $PROFILE"
    elif [ ! -d "$ROOT/$f" ]; then
      echo "  --      $f/ not in this repo"
    elif [ ! -e "$dst" ]; then
      echo "  MISSING $dst"
      # install -m 600: the file holds credentials, so it is never readable
      # by others, not even between a copy and a chmod. Recorded only once
      # it is in place; a failed step leaves it MISSING for the next run.
      if run_ok mkdir -p "$(dirname "$dst")" && run_ok install -m 600 "$src" "$dst"; then
        record_hash "envs/$f" "$src"
      fi
    elif bad=$(env_template_diff "$src" "$dst"); then
      echo "  ok      $f/.env"
      record_hash "envs/$f" "$src"
    else
      # This one cannot be a copy. A live .env holds credentials the committed
      # template deliberately omits, so overwriting it destroys them — the
      # recorded hash is of the TEMPLATE, and "repo moved" means the template
      # changed since this machine last agreed with it. The only safe repair is
      # to add keys the live file lacks; a key it already has keeps its value,
      # whatever the template now says.
      if ! has_record "envs/$f"; then
        echo "  DIFFERS $f/.env — differs from template at:$(printf ' %s' "$bad") (nothing recorded says which moved)"
      elif matches_record "envs/$f" "$src"; then
        # Template unchanged since this machine agreed with it, so the live
        # file is what differs — a filled-in value, or an edit. Left alone.
        echo "  DIFFERS $f/.env — differs from template at:$(printf ' %s' "$bad")"
      else
        missing=""
        for k in $bad; do
          grep -q "^[[:space:]]*$k=" "$dst" || missing="$missing $k"
        done
        if [ -n "$missing" ]; then
          echo "  STALE   $f/.env — template added:$missing"
          if [ "$APPLY" -eq 1 ]; then
            # Recorded only when every key went in: the record is what tells
            # the next run the repo moved, so a failed append retries.
            # shellcheck disable=SC2086 # a space-separated key list
            if env_append "$src" "$dst" $missing; then
              echo "          appended from the template; fill in any placeholder"
              record_hash "envs/$f" "$src"
            else
              note_failure "append$missing to $dst" "$?"
            fi
          else
            printf '  [dry-run] append%s to %s\n' "$missing" "$dst"
          fi
        else
          echo "  STALE   $f/.env — template changed values this machine overrides:$(printf ' %s' "$bad")"
          echo "          left alone; a live value outranks a template default"
          record_hash "envs/$f" "$src"
        fi
      fi
    fi
  done
}

# The shared agent system prompt, distributed into every coding agent's global
# instructions file. Delegates: local-agent-prompt owns the marker handling and
# knows which file each tool actually reads.
stage_prompts() {
  echo "== prompts"
  if [ -x "$ROOT/local-agent-prompt/agent-prompt.sh" ]; then
    if [ "$APPLY" -eq 1 ]; then
      # The pipe hides the tool's exit status; the braces keep it (sd:3543).
      # Exit 2 is a refusal for blocks edited in place alone: it waits for a
      # person, as an in-place dotfile does, and status asks for attention
      # for its DIFFERS line (sd:6730). Any other exit is a failure; output
      # cannot tell, as a target's read error goes to stderr only.
      ap_rc=0
      ap_out=$("$ROOT/local-agent-prompt/agent-prompt.sh" refresh --apply) || ap_rc=$?
      [ "$ap_rc" -eq 0 ] || [ "$ap_rc" -eq 2 ] ||
        ap_out="$ap_out
$(note_failure "agent-prompt.sh refresh --apply" "$ap_rc")"
      printf '%s\n' "$ap_out" | sed 's/^/  /'
    else
      "$ROOT/local-agent-prompt/agent-prompt.sh" status | sed 's/^/  /'
    fi
  else
    echo "  local-agent-prompt missing from the repo"
  fi
}

stage_repos() {
  echo "== repos"
  if [ -x "$ROOT/local-repo-sync/repo-sync.sh" ]; then
    if [ "$APPLY" -eq 1 ]; then
      # The pipe hides the tool's exit status; the braces keep it (sd:3543).
      { "$ROOT/local-repo-sync/repo-sync.sh" sync || note_failure "repo-sync.sh sync" "$?"; } | sed 's/^/  /'
    else
      # `check`, not `list`: list prints the configured fleet without looking
      # at disk, so it could not tell a missing checkout from a present one
      # and the stage sat outside the drift count. check is read-only and
      # touches no network — a repo that is merely behind still reports ok.
      "$ROOT/local-repo-sync/repo-sync.sh" check | sed 's/^/  /' || :
    fi
  else
    echo "  local-repo-sync missing from the repo"
  fi
  local_blocks
  leak_guards
}

# Whether GitHub serves a repository's refs to anyone: `public`, `private`,
# `offline` or `unknown <code>`. One unauthenticated request for the refs a
# clone reads first: -q skips ~/.curlrc and nothing sends a credential, so a
# private repository answers 401 (404 when gone). Any other answer is unknown,
# never private: a 403 is GitHub limiting the rate, not a verdict. `offline`
# is no connection at all, so the caller stops asking.
repo_visibility() { # owner/repo
  code=$(curl -q -s -L -o /dev/null -w '%{http_code}' --connect-timeout 3 --max-time 5 \
    "https://github.com/$1.git/info/refs?service=git-upload-pack" </dev/null) || :
  case $code in
    200) echo public ;;
    401|404) echo private ;;
    ''|000) echo offline ;;
    *) echo "unknown $code" ;;
  esac
}

# owner/repo for a remote on GitHub; nothing, and exit 1, for any other.
github_slug() { # remote url
  case $1 in
    git@github.com:*) s=${1#git@github.com:} ;;
    ssh://git@github.com/*) s=${1#ssh://git@github.com/} ;;
    https://github.com/*) s=${1#https://github.com/} ;;
    *) return 1 ;;
  esac
  s=${s%/}
  s=${s%.git}
  case $s in
    */*/*|/*|*/) return 1 ;;
    ?*/?*) printf '%s\n' "$s" ;;
    *) return 1 ;;
  esac
}

# local-leak-guard's pre-push hook in each public managed repo checked out
# here (sd:3384); a private one stays without it. Only the exact hook
# `leak-guard.sh install` writes, executable, is ok: text
# that names the guard does not prove it runs (review round 1). A hook with
# leak-guard's MARKER that differs is STALE and rewritten whatever the
# visibility. A hook another tool wrote is never replaced: in a public repo
# it is DIFFERS for the operator to review. Only a repo without the guard asks
# GitHub, so status touches the network for those alone; a failed list or
# probe is SKIP, never "private".
leak_guards() {
  if [ ! -f "$ROOT/local-leak-guard/leak-guard.sh" ]; then
    echo "  SKIP    leak guard hooks: local-leak-guard missing from the repo"
    return 0
  fi
  # The main checkout's guard, as cmd_install resolves it: a linked worktree's
  # path fails every push once the worktree is removed (sd:3390).
  . "$ROOT/lib/config.sh"
  if ! main=$(st_main_checkout "$ROOT"); then
    echo "  SKIP    leak guard hooks: git cannot name the main checkout of $ROOT"
    return 0
  fi
  guard="$main/local-leak-guard/leak-guard.sh"
  if [ ! -f "$guard" ]; then
    echo "  SKIP    leak guard hooks: $guard is missing from the main checkout"
    return 0
  fi
  if ! rows=$("$SD_PACK_ROOT/bin/sd-db" repo list --managed 2>/dev/null </dev/null); then
    echo "  SKIP    leak guard hooks: sd-db repo list --managed failed"
    return 0
  fi
  offline=0
  # `sd-db: <path>  <remote>  ...`: two spaces between fields, and a path may
  # hold one.
  printf '%s\n' "$rows" | sed -n 's/^sd-db: //p' |
  while IFS= read -r row; do
    repo=${row%%  *}
    url=${row#*  }
    url=${url%%  *}
    # shellcheck disable=SC2088 # a literal ~ in a value, expanded here
    case $repo in
      "~/"*) dir="$HOME/${repo#\~/}" ;;
      *) dir=$repo ;;
    esac
    [ -e "$dir/.git" ] || continue
    if ! hooks=$(git -C "$dir" rev-parse --git-path hooks 2>/dev/null </dev/null); then
      echo "  SKIP    $repo leak guard: git cannot name its hooks folder"
      continue
    fi
    case $hooks in /*) ;; *) hooks="$dir/$hooks" ;; esac
    hook="$hooks/pre-push"
    # What cmd_install in leak-guard.sh writes, MARKER and all;
    # test_leak_guard_hooks installs through it, so a change there fails here.
    if [ -f "$hook" ] && [ -x "$hook" ] &&
      printf '#!/bin/sh\n# installed by local-leak-guard\nexec sh '\''%s'\'' hook "$@"\n' "$guard" |
        cmp -s - "$hook"; then
      echo "  ok      $repo leak guard"
      continue
    fi
    if [ -f "$hook" ] && grep -qF "# installed by local-leak-guard" "$hook"; then
      echo "  STALE   $repo leak guard pre-push hook: not the executable hook leak-guard.sh install writes"
      try_run sh "$guard" install "$dir" ||
        echo "  FAILED  $repo leak guard: leak-guard.sh install did not finish"
      continue
    fi
    if ! slug=$(github_slug "$url"); then
      echo "  SKIP    $repo leak guard: its remote is not on GitHub, so its visibility is unknown"
      continue
    fi
    if [ "$offline" -eq 1 ]; then
      echo "  SKIP    $repo leak guard: GitHub did not answer; the next run asks again"
      continue
    fi
    visibility=$(repo_visibility "$slug")
    case $visibility in
      private) continue ;;
      offline)
        offline=1
        echo "  SKIP    $repo leak guard: GitHub did not answer; the next run asks again"
        continue
        ;;
      unknown*)
        echo "  SKIP    $repo leak guard: GitHub answered ${visibility#unknown }; the next run asks again"
        continue
        ;;
    esac
    if [ -e "$hook" ]; then
      echo "  DIFFERS $repo has a pre-push hook local-leak-guard did not write; review it, then merge it with \`sh '$guard' hook \"\$@\"\` or remove it"
      continue
    fi
    echo "  MISSING $repo leak guard pre-push hook"
    try_run sh "$guard" install "$dir" ||
      echo "  FAILED  $repo leak guard: leak-guard.sh install did not finish"
  done
}

# The shared CLAUDE.local.md keys, SHARED_LOCAL_KEYS in claude_settings.py, in
# each auto repo checked out here (sd:3030). The source wins for those keys;
# the repo's other keys, its comments and the lines outside the block stay.
# A file git does not ignore is reported and never written: the block is
# per machine, and a tracked copy would publish it.
local_blocks() {
  if ! rows=$("$SD_PACK_ROOT/bin/sd-db" repo list 2>/dev/null </dev/null); then
    echo "  SKIP    CLAUDE.local.md shared keys: sd-db repo list failed"
    return 0
  fi
  # `sd-db: <path>  <url>  ...  <runner merge>`: two spaces between fields,
  # and a path may hold one.
  printf '%s\n' "$rows" | sed -n 's/^sd-db: //p' | awk -F '  ' '$NF == "auto" {print $1}' |
  while IFS= read -r repo; do
    # shellcheck disable=SC2088 # a literal ~ in a value, expanded here
    case $repo in
      "~/"*) dir="$HOME/${repo#\~/}" ;;
      *) dir=$repo ;;
    esac
    [ -e "$dir/.git" ] || continue
    file="$repo/CLAUDE.local.md"
    if ! gaps=$(python3 "$DIR/claude_settings.py" local-missing "$dir/CLAUDE.local.md" 2>&1 </dev/null); then
      echo "  DIFFERS $file: ${gaps#claude_settings.py: }"
      continue
    fi
    if [ -z "$gaps" ]; then
      echo "  ok      $file shared keys"
      continue
    fi
    printf '%s\n' "$gaps" | while read -r word key; do
      printf '  %-7s %s %s\n' "$word" "$file" "$key"
    done
    if ! git -C "$dir" check-ignore -q CLAUDE.local.md </dev/null; then
      echo "  DIFFERS $file is not ignored by git; not written"
      continue
    fi
    run python3 "$DIR/claude_settings.py" local-merge "$dir/CLAUDE.local.md"
  done
}

# Jobs only this machine sees: those in its own folder, cron-jobs/jobs/<host>/.
# No profile names them, since a profile is shared between machines; where
# they live says they belong here. CRON_JOBS_EXTRA_DIRS is not included: those
# folders only define jobs, and the profile picks which of them run (sd:2221).
# The folder comes from cron_job_dirs in lib/system_tools_config.py, so the
# host rule is not repeated here. A folder that does not exist lists nothing;
# one that cannot be read, or a python3 or lib that fails, exits non-zero,
# because a caller that sweeps must not read a failure as "no host jobs".
# os.listdir, not os.path.lexists first: lexists reads a PermissionError on
# an ancestor folder as "absent", which failed open the same way.
host_cron_jobs() {
  PYTHONPATH="$ROOT/lib" python3 -c 'import os, system_tools_config as stc
dirs = stc.cron_job_dirs()
host = dirs[-2] if len(dirs) > 1 and dirs[-2].parent == dirs[-1] else None
names = []
if host is not None:
    try:
        names = os.listdir(host)
    except FileNotFoundError:
        pass
for name in sorted(names):
    if name.endswith(".job"):
        print(name[:-4])'
}

# Every job a job file defines, in any folder cron-jobs.sh reads: the extra
# dirs, this host's folder and the shared one. capture adopts only these
# (sd:3106): a retired or deleted job leaves its plist installed, and a profile
# naming it reads STALE in the cron stage every night, since verify cannot
# render a job with no file. Exits non-zero when a folder cannot be read.
defined_cron_jobs() {
  PYTHONPATH="$ROOT/lib" python3 -c 'import os, system_tools_config as stc
names = set()
for d in stc.cron_job_dirs():
    try:
        listed = os.listdir(d)
    except FileNotFoundError:
        continue
    names.update(n[:-4] for n in listed if n.endswith(".job"))
for name in sorted(names):
    print(name)'
}

# Did local-cron-jobs write this plist? The <prefix>.cron.<name> label does not
# say: another repository's installer used the same label shape, and the sweep
# below uninstalled its agent as an orphan (sd:2321). write_plist in
# local-cron-jobs runs every job as `/bin/bash <dir>/local-cron-jobs/cron-jobs.sh
# exec <job>` under the label <prefix>.cron.<job>, and that pair is the mark.
# Every plist local-cron-jobs has rendered carries it, so an agent installed
# before this check is recognised without being rewritten. A new marker key
# would have made every installed plist STALE and reloaded every job at once.
# Since sd:6456 the command starts with the job-launch binary; a plist from
# before it still counts as ours, so the stage finds it STALE and rewrites it.
# Exits 0 for ours, 1 for another installer's, and anything else when it
# cannot tell (an unreadable plist, no python3); callers treat that as not ours.
cron_plist_ours() { # plist path, job name
  python3 - "$1" "$LABEL_PREFIX.cron.$2" "$2" <<'PY'
import plistlib, sys
path, label, job = sys.argv[1:]
try:
    with open(path, "rb") as f:
        plist = plistlib.load(f)
except Exception as e:
    print(f"cannot read {path}: {e}", file=sys.stderr)
    sys.exit(2)
args = plist.get("ProgramArguments") if isinstance(plist, dict) else None
if (isinstance(args, list) and len(args) == 5 and isinstance(args[0], str)
        and args[0].endswith("/job-launch")):
    args = args[1:]
ours = (isinstance(plist, dict) and plist.get("Label") == label
        and isinstance(args, list) and len(args) == 4
        and args[0] == "/bin/bash"
        and isinstance(args[1], str) and args[1].endswith("/local-cron-jobs/cron-jobs.sh")
        and args[2] == "exec" and args[3] == job)
sys.exit(0 if ours else 1)
PY
}

# cron-jobs.sh install or uninstall for one job. A failure is reported and the
# stage goes on: cron-jobs.sh refuses both while a run holds the job's lock
# (sd:3317), and lane-run fires every five minutes, so ending the nightly run
# under set -e would skip every later stage. A refused job keeps its plist as
# it was, so the next update finds the same drift and retries.
cron_jobs() { # install|uninstall, job
  try_run "$ROOT/local-cron-jobs/cron-jobs.sh" "$1" "$2" ||
    echo "  FAILED  $2 — cron-jobs.sh $1 $2 did not finish; the next update retries it"
}

# The jobs local-cron-jobs/retired-jobs.txt lists, one per line. The cron
# stage and doctor leave them out of what a machine should run.
retired_cron_jobs() {
  rcj_list="$ROOT/local-cron-jobs/retired-jobs.txt"
  [ ! -f "$rcj_list" ] || sed 's/#.*//' "$rcj_list" | awk 'NF {print $1}'
}

stage_cron() {
  echo "== cron"
  # Without its host jobs, the sweep below read each one as an orphan and
  # uninstalled it every night (sd:2221). When they cannot be listed, the
  # wanted list is incomplete, so the sweep is skipped rather than guessed.
  sweep=1
  if ! host_jobs=$(host_cron_jobs); then
    echo "  MISSING host job list — could not read this host's cron-jobs folder; only retired jobs are uninstalled this run"
    host_jobs=
    sweep=0
  fi
  hub_ask
  jobs=$( { manifest cron; printf '%s\n' "$host_jobs"; if hub_role; then hub_role_labels cron; fi; } | awk 'NF && !seen[$0]++')
  # A retired job stays out of every machine's wanted list, even where a
  # profile or a host folder written before the retirement still names it:
  # the sweep below left those running, and verify read them ok (sd:3155).
  # This runs with the sweep off too. The config folder is not edited.
  retired=$(retired_cron_jobs)
  for r in $retired; do
    if printf '%s\n' "$jobs" | grep -qxF "$r"; then
      echo "  note    $r is retired, yet this profile or this host's jobs folder names it; remove it there"
    fi
    rp="$HOME/Library/LaunchAgents/$LABEL_PREFIX.cron.$r.plist"
    [ -f "$rp" ] || continue
    owner=0
    cron_plist_ours "$rp" "$r" 2>/dev/null || owner=$?
    case "$owner" in
      0) echo "  EXTRA   $r — retired in local-cron-jobs/retired-jobs.txt, still installed"
         cron_jobs uninstall "$r" ;;
      1) echo "  FOREIGN $r — retired, but not installed by local-cron-jobs; left in place" ;;
      *) echo "  UNKNOWN $r — retired, but cannot tell who installed it; left in place" ;;
    esac
  done
  # grep exits 1 when every wanted job is retired; the check below reports that.
  jobs=$(printf '%s\n' "$jobs" | grep -vxF "$retired" || :)
  if [ -z "$jobs" ]; then
    echo "  no jobs in this profile"
    return 0
  fi
  # Absence is not the only way a job can be wrong. This used to install only
  # when the plist FILE was missing, so a plist whose content had gone stale
  # reported ok forever — all 9 jobs here read healthy while every one carried
  # a PATH that #109 had already fixed in the generator, and nothing short of
  # reading a plist by hand could have told you. `verify` renders what the
  # generator produces now and compares; a mismatch reinstalls.
  # A profile can name another installer's agent: a capture before sd:2321
  # wrote one in, and verify then read it STALE and install, finding no job
  # file, ended the run before every later stage (sd:2538). The sweep's own
  # ownership test decides; only a plist proven another installer's (exit 1) is
  # skipped, so an unreadable one under a wanted name keeps the reinstall.
  echo "$jobs" | while read -r j; do
    # The agents stage's guard: skipped, and left to the satellite stage's
    # EXTRA when one is here, so the sweep below keeps it too.
    if sd_hub_only "$LABEL_PREFIX.cron.$j" && is_satellite && ! hub_holder; then
      echo "  SKIP    $j — hub only, and this machine is a satellite ($PROFILE.satellite or $SD_HUB_CONFIG)"
      continue
    fi
    jp="$HOME/Library/LaunchAgents/$LABEL_PREFIX.cron.$j.plist"
    owner=0
    [ ! -f "$jp" ] || cron_plist_ours "$jp" "$j" 2>/dev/null || owner=$?
    if [ ! -f "$jp" ]; then
      echo "  MISSING $j — no plist installed"
      cron_jobs install "$j"
    elif [ "$owner" = 1 ]; then
      echo "  FOREIGN $j — in this profile, but another installer's agent holds its label; left in place"
    elif ! "$ROOT/local-cron-jobs/cron-jobs.sh" verify "$j" >/dev/null 2>&1; then
      echo "  STALE   $j — installed plist no longer matches the generator"
      cron_jobs install "$j"
    # A plist can be present and current yet not registered with launchd — a
    # failed bootstrap, a bootout, files restored onto a machine that never
    # loaded them. No content check can see that; ask launchd. install
    # bootstraps, so re-running it is the repair as well as the install.
    elif ! launchctl print "gui/$(id -u)/$LABEL_PREFIX.cron.$j" >/dev/null 2>&1; then
      echo "  UNLOADED $j — plist current but launchd is not running it"
      cron_jobs install "$j"
    else
      echo "  ok      $j"
    fi
  done
  # The reverse direction: a job dropped from (or renamed in) the profile
  # leaves its installed plist firing forever — nothing above ever looks at
  # it again. A plist under <prefix>.cron.* whose job the manifest no longer
  # lists is an orphan only when local-cron-jobs wrote it: another installer
  # can share the namespace (sd:2321), so cron_plist_ours decides, and an
  # agent it cannot place is left alone. FOREIGN and UNKNOWN are not drift
  # words: this stage cannot fix another installer's agent.
  # uninstall derives label and plist from the name alone, so it works even
  # when the .job file itself is gone.
  [ "$sweep" = 1 ] || return 0
  for xp in "$HOME/Library/LaunchAgents/$LABEL_PREFIX.cron."*.plist; do
    [ -e "$xp" ] || continue
    xj=$(basename "$xp" .plist)
    xj=${xj#"$LABEL_PREFIX.cron."}
    # A retired name was handled above; a second pass would count it twice.
    if ! printf '%s\n%s\n' "$jobs" "$retired" | grep -qx "$xj"; then
      owner=0
      cron_plist_ours "$xp" "$xj" || owner=$?
      case "$owner" in
        0) echo "  EXTRA   $xj — installed but no longer in this profile"
           cron_jobs uninstall "$xj" ;;
        1) echo "  FOREIGN $xj — not installed by local-cron-jobs; left in place" ;;
        *) echo "  UNKNOWN $xj — cannot tell who installed it; left in place" ;;
      esac
    fi
  done
}

# A tracked plist as this machine installs it: @HOME@, @LABEL@, @ROOT@
# (this checkout) and @PACK@ (the command pack checkout) replaced, so one file serves any account name and any
# checkout location. A plist without placeholders comes out unchanged, which is
# what a captured one is. Writes into $3 and prints nothing.
sed_value() { printf '%s' "$1" | sed 's/[|&\\]/\\&/g'; }
agent_render() { # label, tracked plist, rendered plist
  sed -e "s|@HOME@|$(sed_value "$HOME")|g" \
      -e "s|@LABEL@|$(sed_value "$1")|g" \
      -e "s|@ROOT@|$(sed_value "$ROOT")|g" \
      -e "s|@PACK@|$(sed_value "$SD_PACK_ROOT")|g" "$2" > "$3"
}

# The Label a plist names; exits non-zero when it names none or cannot be
# read. python3, not plutil: the suite runs on Linux too.
plist_label() { # plist path
  python3 - "$1" <<'PY'
import plistlib, sys
try:
    with open(sys.argv[1], "rb") as f:
        label = plistlib.load(f).get("Label")
except Exception:
    sys.exit(2)
if not isinstance(label, str) or not label:
    sys.exit(1)
print(label)
PY
}

# LaunchAgent plists captured into launchagents/. Same never-overwrite rule
# as dotfiles: a machine-local plist edit wins until capture. A captured plist
# embeds absolute paths, so it only transplants between machines with the same
# username and checkout location; a plist written with the agent_render
# placeholders transplants anywhere.
# Boots a job out. Succeeds when launchd holds no job under the label
# afterwards: the bootout did it, or the job was never loaded, which makes
# bootout fail. A job still loaded is reported FAILED and returns 1, so the
# caller does not bootstrap over it or remove its plist. A bootout that
# succeeds can return while launchd still holds the job, and a bootstrap
# then fails with exit 5 (sd:6454), so it waits up to 10 s for the label to
# leave.
agent_bootout() { # label
  ab_rc=0
  try_run launchctl bootout "gui/$(id -u)/$1" 2>/dev/null || ab_rc=$?
  if [ "$ab_rc" -eq 0 ]; then
    [ "$APPLY" -eq 1 ] || return 0
    ab_n=0
    while launchctl print "gui/$(id -u)/$1" >/dev/null 2>&1; do
      if [ "$ab_n" -ge 10 ]; then
        note_failure "launchctl bootout gui/$(id -u)/$1 (still loaded after ${ab_n}s)" "$ab_rc"
        return 1
      fi
      ab_n=$((ab_n + 1))
      sleep 1
    done
    return 0
  fi
  launchctl print "gui/$(id -u)/$1" >/dev/null 2>&1 || return 0
  note_failure "launchctl bootout gui/$(id -u)/$1" "$ab_rc"
  return 1
}

# Replaces the plist of a loaded agent; launchd holds the plist it was handed,
# so the job goes out and back in. The new plist is staged beside the old one
# first: a failed copy (a full disk can leave a part file) leaves the job and
# its plist as they were. A job that will not go out keeps its old plist too.
# A rename cannot leave a part file, so after a failed rename the old plist is
# whole and is loaded back. Every failure records nothing, so the next run
# finds the same drift and retries.
agent_replace() { # label, src, dst
  run_ok backup_file "$3" || return 0
  if ! run_ok cp "$2" "$3.new" || ! agent_bootout "$1"; then
    rm -f "$3.new"
    return 0
  fi
  if run_ok mv "$3.new" "$3"; then
    record_hash "agents/$1" "$3"
  else
    rm -f "$3.new"
  fi
  run launchctl bootstrap "gui/$(id -u)" "$3"
}

stage_agents() {
  echo "== agents"
  hub_ask
  # brew services' homebrew.mxcl.herdr starts herdr from launchd, so macOS
  # charges every session's file access to herdr, whose grant each upgrade
  # drops. With no agent, the first herdr client starts the server from a
  # terminal, whose grant covers it (sd:6466). Reported, never stopped here:
  # stopping herdr ends every session on the machine.
  # A loaded service's print names it; an exit 0 alone is no evidence.
  if launchctl print "gui/$(id -u)/homebrew.mxcl.herdr" 2>/dev/null | grep -q 'homebrew\.mxcl\.herdr'; then
    echo "  DIFFERS homebrew.mxcl.herdr is loaded — launchd starts herdr, outside the terminal's grant; run 'brew services stop herdr' (it ends every herdr session); the next herdr client starts the server"
  fi
  agents=$(role_agents)
  if [ -z "$agents" ]; then
    echo "  no launch agents in this profile"
    return 0
  fi
  rendered=$(mktemp -d)
  echo "$agents" | while read -r label; do
    # A profile from before sd:6466 may still name a herdr agent, and its
    # template may sit in the config folder. Never install or start it: it
    # would contend with a terminal's server for the socket. Unloading it
    # ends every session, so that is the operator's step.
    case $label in
      *.herdr)
        echo "  DIFFERS $label — retired (sd:6466); drop it from the profile, delete $HOME/Library/LaunchAgents/$label.plist, and let logout unload it (unloading now ends every herdr session)"
        continue ;;
    esac
    # A guard: no satellite profile lists a hub-only agent today, and one
    # that adds it later must not start a second dashboard.
    # Skipped, not removed: the satellite stage reports one already here.
    if sd_hub_only "$label" && is_satellite && ! hub_holder; then
      echo "  SKIP    $label — hub only, and this machine is a satellite ($PROFILE.satellite or $SD_HUB_CONFIG)"
      continue
    fi
    src=$(agent_source "$label")
    [ -n "$src" ] || src="$AGENT_DIR/$label.plist"
    dst="$HOME/Library/LaunchAgents/$label.plist"
    if [ -f "$src" ]; then
      agent_render "$label" "$src" "$rendered/$label.plist"
      src="$rendered/$label.plist"
      # launchd loads a plist under its own Label, not its file name. One
      # that differs read UNLOADED every run, and its bootstrap failed on the
      # label already loaded (sd:3547). Not installed until it matches.
      src_label=$(plist_label "$src") || src_label=
      if [ "$src_label" != "$label" ]; then
        echo "  DIFFERS $label — $AGENT_DIR/$label.plist names Label '${src_label:-none readable}'; launchd would load it under that name; fix the Label, then rerun"
        continue
      fi
    fi
    if [ ! -f "$src" ]; then
      echo "  --      $label not captured in the repo yet"
    elif [ ! -e "$dst" ]; then
      # Every other branch below prints a word the drift counter greps for.
      # This one used to print only its `run` lines, so a launch agent that
      # was never installed showed a remediation and still counted as zero
      # drift -- how one metering agent stayed missing for a day.
      echo "  MISSING $label — no plist here yet"
      # Staged and renamed, as in agent_replace: a part file left by a failed
      # copy would read as a local edit on the next run.
      if run_ok cp "$src" "$dst.new" && run_ok mv "$dst.new" "$dst"; then
        record_hash "agents/$label" "$dst"
        run launchctl bootstrap "gui/$(id -u)" "$dst"
      else
        rm -f "$dst.new"
      fi
    # The pack's `dashboard.sh install` writes the dashboard's plist (its
    # bash start, --config and env) and verifies the server it loads, so its
    # bytes are the pack's, not drift; the template only bootstraps a
    # machine without one (sd:6707). Whether launchd runs it is still asked.
    elif [ "$label" = "$LABEL_PREFIX.sd-dashboard" ]; then
      if launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1; then
        echo "  ok      $label — the pack's dashboard installer owns this plist"
      else
        echo "  UNLOADED $label — plist present but launchd is not running it"
        run launchctl bootstrap "gui/$(id -u)" "$dst"
      fi
    elif ! cmp -s "$src" "$dst"; then
      if matches_record "agents/$label" "$dst"; then
          echo "  STALE   $label — untouched here, repo moved ahead"
          agent_replace "$label" "$src" "$dst"
      elif has_record "agents/$label"; then
          if [ "$FORCE" -eq 1 ]; then
            echo "  DIFFERS $label — edited here; --force overwrites (backup kept)"
            agent_replace "$label" "$src" "$dst"
          else
            echo "  DIFFERS $label — edited here; keep it with 'capture --apply', discard it with --force"
          fi
      elif [ "$FORCE" -eq 1 ]; then
          echo "  DIFFERS $label — nothing recorded; --force overwrites (backup kept)"
          agent_replace "$label" "$src" "$dst"
      else
          echo "  DIFFERS $label — machine and repo disagree, nothing recorded says which moved; review, then 'capture --apply' or --force"
      fi
    # Same blind spot as the cron stage: a plist present and matching says
    # nothing about whether launchd is actually running it.
    elif ! launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1; then
      echo "  UNLOADED $label — plist current but launchd is not running it"
      run launchctl bootstrap "gui/$(id -u)" "$dst"
    else
      echo "  ok      $label"
      record_hash "agents/$label" "$dst"
    fi
  done
  rm -rf "$rendered"
}

# Container names for a local-* service folder, space-separated. Most run one
# container named for the folder suffix; these do not. stage_services and
# cmd_candidates must agree on the answer, so there is one copy of the mapping
# rather than two that drift apart.
container_for() {
  case "$1" in
    local-milvus)                  echo zilliz ;;
    local-opentelemetry-collector) echo local-opentelemetry-collector ;;
    local-graphiti-mcp)            echo local-graphiti-mcp-graphiti-falkordb-1 ;;
    local-genai-traces)            echo local-genai-collector local-genai-phoenix ;;
    *)                             echo "${1#local-}" ;;
  esac
}

# Whether service $1 runs: every one of its containers is in $2, the running
# names one per line. One stopped container of several is a stopped service
# (sd:2852).
service_running() {
  for sr_name in $(container_for "$1"); do
    printf '%s\n' "$2" | grep -qx "$sr_name" || return 1
  done
}

stage_services() {
  echo "== services"
  svcs=$(manifest service)
  if [ -z "$svcs" ]; then
    echo "  no services in this profile"
    return 0
  fi
  command -v docker >/dev/null 2>&1 || { echo "  docker MISSING"; return 0; }
  echo "$svcs" | while read -r svc; do
    name=$(echo "$svc" | sed 's/^local-//')
    entry="$ROOT/$svc/$name.sh"
    if [ ! -x "$entry" ]; then
      echo "  SKIP    $svc (no entrypoint at $svc/$name.sh)"
    elif service_running "$svc" "$(docker ps --format '{{.Names}}' 2>/dev/null)"; then
      echo "  ok      $svc running"
    else
      echo "  MISSING $svc — not running"
      run "$entry" start
    fi
  done
}

# ----------------------------------------------------------------- verbs ----

# A .node line may carry `link` (sd:4853): RELEASE_BIN's node, npm and npx
# then point at fnm's newest installed node of that major, where launchd and
# cron jobs find it (cron-jobs.sh puts ~/.local/bin on JOB_PATH). One line
# may; with two, no link changes. A file there that is not a link stays.
node_link() {
  nl_major=$(manifest node | awk '$2 == "link" { print $1 }')
  [ -n "$nl_major" ] || return 0
  nl_n=$(printf '%s\n' "$nl_major" | grep -c '^')
  if [ "$nl_n" -gt 1 ]; then
    note_failure "node: $nl_n .node lines carry link; one may, so no link changed" 1
    return 0
  fi
  case "$nl_major" in *[!0-9]*)
    note_failure "node: '$nl_major link' names no major; no link changed" 1
    return 0 ;;
  esac
  nl_v=$(fnm ls 2>/dev/null | awk -v m="$nl_major" '
    { for (i = 1; i <= NF; i++) if ($i ~ ("^v" m "\\.[0-9]+\\.[0-9]+$")) {
        split(substr($i, 2), p, ".")
        if (best == "" || p[2] + 0 > b2 || (p[2] + 0 == b2 && p[3] + 0 > b3)) { best = $i; b2 = p[2] + 0; b3 = p[3] + 0 }
      } }
    END { if (best != "") print best }' || :)
  if [ -z "$nl_v" ]; then
    echo "  MISSING node link $nl_major: fnm has no v$nl_major"
    return 0
  fi
  nl_node=$(fnm exec --using="$nl_v" -- sh -c 'command -v node' 2>/dev/null </dev/null || :)
  nl_dir=${nl_node%/node}
  if [ "$nl_dir" = "$nl_node" ] || [ ! -x "$nl_node" ]; then
    note_failure "node link $nl_major: fnm names no node for $nl_v; no link changed" 1
    return 0
  fi
  nl_ok=1
  for nl_b in node npm npx; do
    nl_dst="$RELEASE_BIN/$nl_b"
    if [ ! -e "$nl_dir/$nl_b" ]; then
      nl_ok=0
      note_failure "node link $nl_major: no $nl_b in $nl_dir; $nl_dst not changed" 1
    elif [ -L "$nl_dst" ] && [ "$(readlink "$nl_dst")" = "$nl_dir/$nl_b" ]; then
      :
    elif [ -d "$nl_dst" ] || { [ -e "$nl_dst" ] && [ ! -L "$nl_dst" ]; }; then
      nl_ok=0
      echo "  DIFFERS node link $nl_major: $nl_dst is not a symlink; left alone"
    else
      nl_ok=0
      if [ -L "$nl_dst" ]; then
        echo "  DIFFERS node link $nl_major: $nl_dst -> $(readlink "$nl_dst"), not $nl_v"
      else
        echo "  MISSING node link $nl_major: $nl_dst -> $nl_dir/$nl_b"
      fi
      run node_link_one "$nl_dir/$nl_b" "$nl_dst"
    fi
  done
  [ "$nl_ok" -eq 0 ] || echo "  ok      node link $nl_major -> $nl_v"
}

# ln -s beside the link, then mv over it: the link is never absent. A
# temporary link an interrupted run left goes first.
node_link_one() { # target, link
  rm -f "$2.link."*
  { mkdir -p "${2%/*}" && ln -s "$1" "$2.link.$$" && mv -f "$2.link.$$" "$2"; } || { rm -f "$2.link.$$"; return 1; }
}

# Developer tooling that doctor already checks for: fnm node versions,
# rtk hooks, and the mailto handler (via duti, in common.brew). Also the
# global npm CLIs of the .npm files, which status reports through this stage.
# Everything is idempotent — present things are skipped, not reinstalled.
# The hub's OTLP endpoint and protocol, the one value both agents' exports
# take (sd:6944): `genai-traces.sh endpoint` answers it, on the hub its
# loopback one, on a satellite (hub.json) the hub's tailnet route
# (sd:6945). Sets TELEMETRY_ENDPOINT, TELEMETRY_PROTOCOL and
# TELEMETRY_OTEL (the claude_settings.py arguments); all three are empty
# on a machine that is neither. A hub that is down drops the export; the
# OTel exporters send in the background, so no session waits on it.
telemetry_endpoint() {
  TELEMETRY_ENDPOINT="" TELEMETRY_PROTOCOL="" TELEMETRY_OTEL=""
  if plain_hub || hub_holder; then
    te_kind=
  elif [ -e "$SD_HUB_CONFIG" ]; then
    te_kind=tailnet
  else
    return 0
  fi
  # Claude exports metrics and logs, codex logs: a collector with a traces
  # pipeline alone drops both (sd:6942 adds the other two).
  if ! awk '/^  pipelines:/ { p = 1; next } p && /^    metrics:/ { m = 1 } p && /^    logs:/ { l = 1 }
            END { exit !(m && l) }' "$ROOT/local-genai-traces/collector.yaml" 2>/dev/null; then
    echo "  SKIP    agent telemetry: genai-traces has no metrics and logs pipelines (sd:6942)"
    return 0
  fi
  # shellcheck disable=SC2086 # te_kind is empty or one word
  te_out=$(sh "$ROOT/local-genai-traces/genai-traces.sh" endpoint $te_kind 2>/dev/null </dev/null) || {
    echo "  SKIP    agent telemetry: genai-traces.sh endpoint${te_kind:+ $te_kind} did not answer"
    return 0
  }
  TELEMETRY_ENDPOINT=$(printf '%s\n' "$te_out" | sed -n 's/^OTEL_EXPORTER_OTLP_ENDPOINT=//p')
  TELEMETRY_PROTOCOL=$(printf '%s\n' "$te_out" | sed -n 's/^OTEL_EXPORTER_OTLP_PROTOCOL=//p')
  [ -n "$TELEMETRY_ENDPOINT" ] && [ -n "$TELEMETRY_PROTOCOL" ] || {
    TELEMETRY_ENDPOINT="" TELEMETRY_PROTOCOL=""
    return 0
  }
  TELEMETRY_OTEL="--otel $TELEMETRY_ENDPOINT $TELEMETRY_PROTOCOL $(hostname -s | tr '[:upper:]' '[:lower:]')"
}

stage_tooling() {
  echo "== tooling"
  if command -v fnm >/dev/null 2>&1; then
    have=$(fnm ls 2>/dev/null || :)
    for v in $(node_versions); do
      case "$have" in
        *"v$v."*) echo "  ok      node $v (fnm)" ;;
        *)
          echo "  MISSING node $v (fnm)"
          run fnm install "$v"
          ;;
      esac
    done
    node_link
  else
    echo "  SKIP    fnm not installed (brew stage provides it)"
  fi
  # A package is present when npm ls prints its path; npm's exit code says
  # neither way: npm 10 exits 0 on a miss, npm 11 exits 1 for a present
  # package in a tree with problems. Any other code is a failed probe.
  npm_pkgs=$(manifest npm)
  if [ -z "$npm_pkgs" ]; then
    :
  elif ! nb=$(brew_node_bin); then
    echo "  MISSING npm (none from a Homebrew node formula in the brew manifest; the brew stage installs it)"
  else
    # The keg's bin first, in a subshell: npm is a `#!/usr/bin/env node`
    # script, and a node@<major> is keg-only, so `env` would find fnm's node
    # or none.
    (
      PATH="$nb:$PATH"
      for p in $npm_pkgs; do
        np_rc=0
        np_out=$(bound_step 60 npm ls -g --depth=0 --parseable "$p" 2>/dev/null) || np_rc=$?
        if [ "$np_rc" -gt 1 ]; then
          note_failure "npm ls -g $p: presence unknown, nothing installed" "$np_rc"
        elif printf '%s\n' "$np_out" | grep -q .; then
          echo "  ok      npm $p"
        else
          echo "  MISSING npm $p"
          run bound_step 600 npm install -g "$p"
        fi
      done
    )
  fi
  if command -v rtk >/dev/null 2>&1; then
    if [ -f "$HOME/.claude/settings.json" ] && grep -q 'rtk' "$HOME/.claude/settings.json" 2>/dev/null; then
      echo "  ok      rtk hook present in ~/.claude/settings.json"
    else
      echo "  MISSING rtk hook in ~/.claude/settings.json"
      run rtk init -g
    fi
    if [ -f "$HOME/.gemini/hooks/rtk-hook-gemini.sh" ]; then
      echo "  ok      rtk hook present for Gemini CLI"
    else
      echo "  MISSING rtk hook for Gemini CLI"
      run rtk init -g --gemini
    fi
    # No codex branch, deliberately. rtk has no hook for Codex — `rtk init -g
    # --codex` writes only prose: its own ~/.codex/RTK.md plus an `@RTK.md`
    # include in AGENTS.md. local-agent-prompt already distributes the RTK
    # section of prompt/shared.md into that same AGENTS.md, so the two
    # collided: rtk's `## Verification` is an install-check while shared.md's
    # `# Verification` is the verification discipline, and codex was handed
    # both under one heading.
    #
    # It also recurred. The old guard here was "does ~/.codex/RTK.md exist",
    # so deleting the duplicate made this stage recreate it on the next run.
    # Twice in one day. The other two branches guard on a hook file and are
    # kept: the hook is the part that does work no prompt text can.
    echo "  ok      codex RTK instructions come from prompt/shared.md (see local-agent-prompt)"
  else
    echo "  SKIP    rtk not installed"
  fi
  # The Claude Code HUD, on every profile: the claude-hud plugin, a
  # statusLine that runs local-statusline, which wraps the plugin, and the
  # plugin's display options in ~/.claude/plugins/claude-hud/config.json.
  # Each gap prints MISSING so --fail-on-drift sees it. The config is seeded
  # from local-statusline's example only when nothing is at that path, so an
  # operator's own config is never overwritten (sd:2929), except that the
  # speed readout is switched off (sd:6069).
  if command -v claude >/dev/null 2>&1; then
    if ls -d "$HOME"/.claude/plugins/cache/*/claude-hud/*/ >/dev/null 2>&1; then
      echo "  ok      claude-hud plugin"
    else
      echo "  MISSING claude-hud plugin"
      if [ -d "$HOME/.claude/plugins/marketplaces/claude-hud" ] \
        || run_ok claude plugin marketplace add jarrodwatts/claude-hud; then
        run claude plugin install claude-hud@claude-hud
      fi
    fi
    hud_config="$HOME/.claude/plugins/claude-hud/config.json"
    if [ -e "$hud_config" ] || [ -L "$hud_config" ]; then
      echo "  ok      claude-hud config"
    else
      echo "  MISSING claude-hud config"
      if run_ok mkdir -p "${hud_config%/*}"; then
        run cp -n "$ROOT/local-statusline/claude-hud.config.example.json" "$hud_config"
      fi
    fi
    # One option is kept in an existing config too: the speed readout
    # (`out: N tok/s`) stays off on every profile (sd:6069); every other
    # option stays as the operator set it.
    if [ -f "$hud_config" ] && python3 -c 'import json, sys
sys.exit(0 if json.load(open(sys.argv[1])).get("display", {}).get("showSpeed") is True else 1)' "$hud_config" 2>/dev/null; then
      echo "  DIFFERS claude-hud showSpeed on"
      run python3 -c 'import json, sys
d = json.load(open(sys.argv[1])); d["display"]["showSpeed"] = False
open(sys.argv[1], "w").write(json.dumps(d, indent=2) + "\n")' "$hud_config"
    fi
    if [ ! -f "$HOME/.claude/settings.json" ]; then
      echo "  SKIP    statusLine not set (no ~/.claude/settings.json; start claude once)"
    elif grep -q 'local-statusline/statusline.sh render' "$HOME/.claude/settings.json" 2>/dev/null; then
      echo "  ok      statusLine -> local-statusline"
    else
      echo "  MISSING statusLine -> local-statusline"
      run sh "$ROOT/local-statusline/statusline.sh" install
    fi
    # The settings baseline in claude_settings.py, the same on every machine:
    # deny rules (secret reads, GitHub issues, claude-mem work state), no
    # attribution lines, the `sd today` hook, the checkout_guard.py hook
    # (sd:6001), and DISABLE_AUTOUPDATER (the
    # weekly `brew upgrade --cask --greedy` moves the claude-code@latest
    # cask). It adds missing entries after a
    # backup and never touches the operator's own; a STALE line is a rule an
    # older baseline added, which merge takes back out (RETIRED_DENY).
    # On the hub the baseline adds the OTel export to genai-traces (sd:6944);
    # telemetry_endpoint reads the one endpoint codex gets too.
    telemetry_endpoint
    if [ -f "$HOME/.claude/settings.json" ]; then
      # shellcheck disable=SC2086 # TELEMETRY_OTEL is empty or three words
      if gaps=$(python3 "$DIR/claude_settings.py" missing "$HOME/.claude/settings.json" $TELEMETRY_OTEL 2>/dev/null); then
        if [ -z "$gaps" ]; then
          echo "  ok      settings.json baseline"
        else
          printf '%s\n' "$gaps" | sed -e 's/^STALE /  STALE   settings.json /' -e t -e 's/^/  MISSING settings.json /'
          if run_ok backup_file "$HOME/.claude/settings.json"; then
            # shellcheck disable=SC2086
            run python3 "$DIR/claude_settings.py" merge "$HOME/.claude/settings.json" $TELEMETRY_OTEL
          fi
        fi
      else
        echo "  DIFFERS settings.json is not valid JSON; baseline not checked"
      fi
    fi
  else
    # Claude Code is the claude-code@latest cask on every profile, so a
    # machine without it is drift, not a SKIP (sd:4738). The brew stage
    # installs what the profile lists; this names the gap for status.
    echo "  MISSING claude (brew install --cask claude-code@latest)"
  fi
  # sd is the one work-state store, so claude-mem's own stays off
  # (sd:5995); its default is on. The plugin writes this file on its first
  # run, and a machine without it has no claude-mem to switch off.
  claude_mem="$HOME/.claude-mem/settings.json"
  if [ -f "$claude_mem" ]; then
    self_update_json "$claude_mem" CLAUDE_MEM_WORK_STATE_ENABLED '"false"'
  else
    echo "  SKIP    no $claude_mem (claude-mem not set up)"
  fi
  # Codex on the hub exports its logs to the same endpoint, prompts left out
  # (sd:6944). The [otel] table is the source's: another one is replaced.
  codex_toml="$HOME/.codex/config.toml"
  if [ -n "$TELEMETRY_ENDPOINT" ] && command -v codex >/dev/null 2>&1 && [ -f "$codex_toml" ]; then
    gap=$(python3 "$DIR/claude_settings.py" codex-missing "$codex_toml" "$TELEMETRY_ENDPOINT" "$TELEMETRY_PROTOCOL" 2>/dev/null) \
      || gap="DIFFERS codex config.toml does not read; [otel] not checked"
    case $gap in
      '') echo "  ok      codex config.toml [otel]" ;;
      DIFFERS*) echo "  $gap" ;;
      *)
        case $gap in MISSING*) echo "  MISSING codex config.toml [otel]" ;; *) echo "  STALE   codex config.toml [otel]" ;; esac
        if run_ok backup_file "$codex_toml"; then
          run python3 "$DIR/claude_settings.py" codex-merge "$codex_toml" "$TELEMETRY_ENDPOINT" "$TELEMETRY_PROTOCOL"
        fi
        ;;
    esac
  fi
  # opencode gets the same secret-read denies as a top-level permission.read
  # block, inserted as text so the operator's comments stay. A file with its
  # own top-level permission block is reported, not edited. Codex has no user
  # layer for this: its deny_read lives in the root-owned requirements.toml.
  opencode_json="${XDG_CONFIG_HOME:-$HOME/.config}/opencode/opencode.json"
  if command -v opencode >/dev/null 2>&1 && [ -f "$opencode_json" ]; then
    if gaps=$(python3 "$DIR/claude_settings.py" opencode-missing "$opencode_json" 2>/dev/null); then
      case $gaps in
        '') echo "  ok      opencode.json read denies" ;;
        has*) echo "  DIFFERS opencode.json $gaps" ;;
        *)
          printf '%s\n' "$gaps" | sed 's/^/  MISSING opencode.json /'
          if run_ok backup_file "$opencode_json"; then
            run python3 "$DIR/claude_settings.py" opencode-merge "$opencode_json"
          fi
          ;;
      esac
    else
      echo "  DIFFERS opencode.json does not parse; read denies not checked"
    fi
  fi
  if command -v duti >/dev/null 2>&1; then
    # duti -x only answers for file extensions; URL-scheme handlers have to
    # be read back from the LaunchServices store.
    # LaunchServices stores the bundle id lower-cased, hence -i.
    mh=$(mailto_handler)
    if defaults read com.apple.LaunchServices/com.apple.launchservices.secure LSHandlers 2>/dev/null \
       | grep -B2 'LSHandlerURLScheme = .*mailto' | grep -qiF "\"$mh\""; then
      echo "  ok      mailto handler is $mh"
    else
      echo "  DIFFERS mailto handler is not $mh"
      run duti -s "$mh" mailto:
    fi
  else
    echo "  SKIP    duti not installed (it is in common.brew)"
  fi
}

# The database and the dashboard's private route — the two pieces of
# one-database design (sd:234; `git show 8aef46ba:docs/work/2026-09-05-one-database-one-front-door/prd.md`) that the `agents` stage
# cannot build. A profile's .agent lists <prefix>.sd-dashboard, and that
# stage installs and loads it; what the agent cannot do for itself is create the database it opens or the tailscale
# serve route the dashboard's HTTPS front door sits behind. Gated on the
# profile listing the sd agent: a machine without it has no database to
# initialise and no port to route.
#
# Both comparisons print a word the drift counter greps for. Whether the
# dashboard answers and the schema versions agree is
# doctor's question (doctor_sd below); this stage asks only whether the
# pieces exist, which is the question `update --apply` can act on.
#
# One database path, and not an override: `sd-db init` creates this file
# and no other, and the library and the dashboard both default to
# it. An SD_DB_PATH used to move it here alone, so the stage and doctor
# could check a file no agent opened (sd:1177). A service config that names
# another `database` is reported instead; see sd_database_elsewhere.
SD_DB="$HOME/.local/share/sd/sd.db"
SD_DASHBOARD_PORT="${SD_DASHBOARD_PORT:-8767}"
SD_DASHBOARD_HTTPS_PORT="${SD_DASHBOARD_HTTPS_PORT:-8443}"
SD_PACK_ROOT="${SD_PACK_ROOT:-$HOME/repos/platypeeps/sd-ai-command-pack}"

sd_in_profile() { manifest agent | grep -qxF "$LABEL_PREFIX.sd-dashboard"; }
# The satellite's selector (`sd_db.hub`); on the hub its presence is drift.
SD_HUB_CONFIG="$HOME/.config/sd/hub.json"
# The hub's own launchd jobs, by label suffix: the one list of what a
# satellite never runs (prd R5 of the second-machine plan). The cron jobs
# are the backups and the nightly shadow sync, whose lock sits beside the
# database; the cron stage installs them under `<prefix>.cron.`.
# The satellite stage reports each one present as EXTRA; the agents and cron
# stages skip each one on a satellite.
# The full role (sd:5986) adds the account agent that holds the mail and
# Drive logins, its watch job, and the builder jobs, which write the
# database. The hub's other host jobs stay off the list: they pause until
# the hub returns (#31095).
SD_HUB_ONLY_AGENTS="sd-dashboard sd-serve google-workspace-mcp cron.sd-db-backup cron.sd-db-backup-hourly cron.offsite-verify cron.mirror-sync-nightly cron.shadow-sync-nightly cron.workspace-mcp-watch cron.night-build cron.build-dispatch"
sd_hub_only() { # label
  for sho in $SD_HUB_ONLY_AGENTS; do [ "$1" = "$LABEL_PREFIX.$sho" ] && return 0; done
  return 1
}
# This machine names an sd hub: its profile's `.satellite` or hub.json does.
# The one test the agents, cron and satellite stages share (sd:3547): the
# agents stage once keyed on hub.json alone, the cron stage on nothing, so a
# satellite installed a hub-only job the satellite stage then reported.
is_satellite() {
  [ -n "$(manifest satellite)" ] || [ -e "$SD_HUB_CONFIG" ]
}
# The hub role as a state, not a profile (hub failover, sd:4012). A machine
# holds it while hub.json is gone, the database is here and `sd-db fence`
# reads holder; `off`, an unset sd.hub_name, is not enough, since promote
# takes the name before it installs the role. Any profile can hold it: a
# repaired hub takes the role back with its hub profile (round 2). Asked once
# per run: call it in the main shell before any pipeline, where a subshell
# would ask again.
hub_holder() {
  if [ -z "${HUB_HOLDER:-}" ]; then
    HUB_HOLDER=no
    if [ ! -e "$SD_HUB_CONFIG" ] && [ -f "$SD_DB" ] &&
       hr_fence=$(bound_step 120 "$SD_PACK_ROOT/bin/sd-db" fence 2>/dev/null); then
      case "$hr_fence" in *"fence: holder:"*) HUB_HOLDER=yes ;; esac
    fi
  fi
  [ "$HUB_HOLDER" = yes ]
}
# A holder whose profile runs no sd agent gets the role's labels on top of
# its own profile; a hub profile lists them itself and asks nothing.
hub_role() { ! sd_in_profile && hub_holder; }
# A hub profile with no satellite in it, as the live hub: no stage needs the
# fence's answer there, so none asks it.
plain_hub() { sd_in_profile && ! is_satellite; }
# Ask the fence once, in the main shell, before any pipeline.
hub_ask() { plain_hub || hub_holder || :; }
# The hub-role labels: the hub-only list but the iCloud mirror, which copies
# the hub's own folders, which a satellite lacks. `agent` prints labels,
# `cron` job names.
SD_HUB_ROLE_EXCEPT="cron.mirror-sync-nightly"
hub_role_labels() { # agent|cron
  for hr in $SD_HUB_ONLY_AGENTS; do
    [ "$hr" != "$SD_HUB_ROLE_EXCEPT" ] || continue
    case "$1:$hr" in
      cron:cron.*) echo "${hr#cron.}" ;;
      agent:cron.*) ;;
      agent:*) echo "$LABEL_PREFIX.$hr" ;;
    esac
  done
}
# The logins the hub role needs (sd:5986). No machine copies a login or a
# secret, and readiness tests none (#31094): the operator logs in on the
# promoted standby and signs out at the hand-back. One per line:
# name|login|sign-out.
ROLE_ACCOUNTS="work Google account, in workspace-mcp|in a Claude session here, call workspace-mcp's start_google_auth with it as user_google_email, then open the link it returns|delete its <address>.json in ~/.google_workspace_mcp/credentials
personal Google account, in workspace-mcp|in a Claude session here, call workspace-mcp's start_google_auth with it as user_google_email, then open the link it returns|delete its <address>.json in ~/.google_workspace_mcp/credentials
Slack, the Slack plugin's MCP server|in Claude Code, run /mcp, pick the Slack plugin's server, then Authenticate|in Claude Code, run /mcp, pick the Slack plugin's server, then Clear authentication
Notion, the notion MCP server|in Claude Code, run /mcp, pick notion, then Authenticate|in Claude Code, run /mcp, pick notion, then Clear authentication"
role_accounts() { # login|drill|signout
  case "$1" in
    login) echo "  accounts    : log in to each here by hand; no login or secret is copied from the hub"; ra_field=2 ;;
    drill) echo "  accounts    : a promotion needs each login, made here by hand then; the drill makes none"; ra_field=2 ;;
    *) echo "  accounts    : sign out of each here by hand; the hand-back does not"; ra_field=3 ;;
  esac
  printf '%s\n' "$ROLE_ACCOUNTS" | awk -F'|' -v f="$ra_field" '{ print "    " $1 ": " $f }'
}
# The agents this machine runs: its profile's, plus the hub role's while it
# holds it.
role_agents() {
  { manifest agent; if hub_role; then hub_role_labels agent; fi; } | awk 'NF && !seen[$0]++'
}
# The plist a label installs from: the config folder's, else, for a label
# the hub role adds, the tracked example. A hub profile's own label keeps the
# config folder's alone, so a missing one stays the sd stage's MISSING.
# Prints nothing when there is neither.
agent_source() { # label
  as_src="$AGENT_DIR/$1.plist"
  if [ ! -f "$as_src" ] && sd_hub_only "$1" && hub_role; then
    as_src="$DIR/examples/launchagents/local.system-tools.${1#"$LABEL_PREFIX".}.plist"
  fi
  [ ! -f "$as_src" ] || printf '%s\n' "$as_src"
}
# The interpreter the satellite stage runs the pack's lib/sd_db under: the
# pack's.
SD_DB_PYTHON="${SD_DB_PYTHON:-$SD_PACK_ROOT/.venv/bin/python}"
# Runs a module of the pack's lib/sd_db under `-I`: argv is the lib/ folder,
# then the module's arguments. -I keeps PYTHONPATH and the working directory
# out, and lib/ goes first, so no copy installed in the interpreter answers
# (sd:3279): every sd_db caller runs that lib/.
SD_DB_RUN_LIB='import runpy, sys; sys.path.insert(0, sys.argv.pop(1)); runpy.run_module(sys.argv.pop(1), run_name="__main__", alter_sys=True)'


# One read of `tailscale serve status` for the dashboard's port. The route
# is the line `https://<node>.<tailnet domain>:8443 (tailnet only)` followed
# by `|-- / proxy http://127.0.0.1:8767`: the root handler, and a proxy
# target equal to the dashboard's, not one that merely contains it (a
# :87670 target contains :8767). Prints `route <origin>` for that pair;
# otherwise `held <origin> <handler>` when the port serves something else;
# otherwise nothing. Reads stdin, so the stage and doctor parse one status
# read the same way. Another route passes its HTTPS port and target.
sd_serve_scan() { # [https port, target]
  awk -v want=":${1:-$SD_DASHBOARD_HTTPS_PORT}" -v target="${2:-http://127.0.0.1:$SD_DASHBOARD_PORT}" '
    function ours(o) { return length(o) > length(want) && substr(o, length(o) - length(want) + 1) == want }
    /^[a-z][a-z0-9+.-]*:\/\// { origin = $1; mine = ours(origin); next }
    !mine { next }
    origin ~ /^https:\/\// && $1 == "|--" && $2 == "/" && $3 == "proxy" && $4 == target { route = origin; exit }
    $1 == "|--" && held == "" { held = origin " " $2 " " $3 " " $4 }
    END { if (route != "") print "route " route; else if (held != "") print "held " held }
  '
}
sd_route_origin() { sd_serve_scan "$@" | sed -n 's/^route //p'; }
sd_route_holder() { sd_serve_scan "$@" | sed -n 's/^held //p'; }
# Satellites' agent telemetry reaches the hub's genai-traces through this
# HTTPS port (sd:6945); `genai-traces.sh endpoint tailnet` names the same.
GENAI_TRACES_TAILNET_PORT="${GENAI_TRACES_TAILNET_PORT:-4339}"

# The `database` an sd service config names, or nothing when the file or the
# key is absent. plutil reads JSON; every profile with an sd agent is a Mac.
sd_config_database() { # config
  [ -f "$1" ] || return 0
  plutil -extract database raw -o - "$1" 2>/dev/null || :
}

# One line per config of this profile's sd agents that names a database
# other than $SD_DB: the dashboard's, at the path its tracked plist passes
# with --config. The service would then open a file
# this stage never built and doctor never checked.
sd_database_elsewhere() {
  # shellcheck disable=SC2043 # a list of one, kept a list
  for sde_name in dashboard; do
    manifest agent | grep -qxF "$LABEL_PREFIX.sd-$sde_name" || continue
    sde_conf="$HOME/.config/sd/$sde_name.json"
    sde_db=$(sd_config_database "$sde_conf")
    [ -z "$sde_db" ] || [ "$sde_db" = "$SD_DB" ] || printf '%s names %s\n' "$sde_conf" "$sde_db"
  done
}

# Whether the application firewall lists EXE as allowed incoming
# connections. A firewall that is off counts for nothing: the system stage
# turns it on (sd:6492 review). A probe that fails is no.
firewall_allows() { # exe
  _fw=/usr/libexec/ApplicationFirewall/socketfilterfw
  if command -v socketfilterfw >/dev/null 2>&1; then _fw=$(command -v socketfilterfw); fi
  _ls=$("$_fw" --listapps 2>/dev/null) || return 1
  printf '%s\n' "$_ls" | awk -v want="$1" '
    hit { ok = /Allow incoming/; done = 1; exit }
    { l = $0; sub(/^[0-9]+ : /, "", l); sub(/[ \t]+$/, "", l); hit = (l == want) }
    END { exit !(done && ok) }'
}

# sd-serve's python (SERVE_PYTHON_DIR), installed once by uv into uv-bin,
# where sd-serve never looks. Its python3 goes into bin, first on
# sd-serve's PATH, only once the firewall allows the binary: a binary it
# has not allowed admits no satellite (sd:6492 review). The link names the
# real binary, so a later uv install moves nothing sd-serve runs. Never
# upgraded here; until then sd-serve's PATH falls through to brew's python3.
serve_python() {
  if [ -x "$SERVE_PYTHON_DIR/bin/python3" ]; then
    echo "  ok      sd-serve python $SERVE_PYTHON_DIR/bin/python3"
    return 0
  fi
  if [ ! -x "$SERVE_PYTHON_DIR/uv-bin/python3" ]; then
    echo "  MISSING sd-serve python $SERVE_PYTHON_VERSION at $SERVE_PYTHON_DIR — sd-serve runs brew's python3 until it is there"
    if ! command -v "$UV" >/dev/null 2>&1; then
      echo "  --      no uv to install it (the brew stage installs uv); then: $(basename "$SELF") update sd --apply"
      return 0
    fi
    run_ok bound_step 900 env UV_PYTHON_INSTALL_DIR="$SERVE_PYTHON_DIR" UV_PYTHON_BIN_DIR="$SERVE_PYTHON_DIR/uv-bin" \
      "$UV" python install "$SERVE_PYTHON_VERSION" --default || return 0
    [ -x "$SERVE_PYTHON_DIR/uv-bin/python3" ] || return 0
  fi
  sp_exe=$("$SERVE_PYTHON_DIR/uv-bin/python3" -c 'import os, sys; print(os.path.realpath(sys.executable))' 2>/dev/null) || sp_exe=
  if [ -z "$sp_exe" ]; then
    note_failure "$SERVE_PYTHON_DIR/uv-bin/python3 (gave no executable path)" 1
  elif firewall_allows "$sp_exe"; then
    echo "  MISSING sd-serve python on its PATH, $SERVE_PYTHON_DIR/bin/python3 — the firewall allows $sp_exe"
    run_ok mkdir -p "$SERVE_PYTHON_DIR/bin" && run ln -s "$sp_exe" "$SERVE_PYTHON_DIR/bin/python3"
  else
    echo "  DIFFERS sd-serve python $sp_exe is not allowed in the firewall — sd-serve stays on brew's python3; allow it in System Settings > Network > Firewall > Options, then: $(basename "$SELF") update sd --apply"
  fi
}

stage_sd() {
  echo "== sd"
  hub_ask
  if ! sd_in_profile && ! hub_role; then
    echo "  SKIP    no sd agent in this profile ($LABEL_PREFIX.sd-dashboard)"
    return 0
  fi
  # An old hub demoted by the README keeps its hub profile until it takes the
  # role back; a database built here would stop that promote at step 1.
  if sd_in_profile && [ -e "$SD_HUB_CONFIG" ] && [ ! -f "$SD_DB" ]; then
    echo "  DIFFERS $SD_HUB_CONFIG — this hub profile is demoted (hub.json, no database): take the role back with $(basename "$SELF") promote --apply, or rejoin with a satellite profile (README, 'Hub failover')"
    return 0
  fi
  if [ -f "$SD_DB" ]; then
    echo "  ok      database $SD_DB"
  else
    # Named so --fail-on-drift sees it: an agent loaded against no database
    # restarts on its throttle interval forever and still reads `ok loaded`.
    echo "  MISSING database $SD_DB — no database yet"
    run "$SD_PACK_ROOT/bin/sd-db" init
  fi
  sd_database_elsewhere | while IFS= read -r elsewhere; do
    echo "  DIFFERS database: $elsewhere, not $SD_DB — this stage builds and doctor checks only $SD_DB; remove the key or point it there"
  done
  # The hub's server for satellites (step 8 of the second-machine plan). The
  # agents stage installs and loads it like any agent; this stage says when
  # the profile or the config folder lacks it, a gap the agents stage
  # reports with no drift word. A removed installed plist is the agents
  # stage's MISSING, counted once.
  if hub_role; then
    echo "  ok      $LABEL_PREFIX.sd-serve from the hub role (promoted)"
  elif ! manifest agent | grep -qxF "$LABEL_PREFIX.sd-serve"; then
    echo "  MISSING $LABEL_PREFIX.sd-serve in $PROFILE.agent — the hub serves no satellite; add the label, then: $(basename "$SELF") update agents --apply"
  elif [ ! -f "$AGENT_DIR/$LABEL_PREFIX.sd-serve.plist" ]; then
    echo "  MISSING $AGENT_DIR/$LABEL_PREFIX.sd-serve.plist — copy it from $DIR/examples/launchagents/"
  else
    echo "  ok      $LABEL_PREFIX.sd-serve in $PROFILE.agent"
  fi
  if hub_role || manifest agent | grep -qxF "$LABEL_PREFIX.sd-serve"; then
    serve_python
  fi
  # Removed by hand, never here: on the hub it refuses every sd verb beside
  # the database (HubConflict), and the operator decides which role is wrong.
  # It is also the README demotion's first step (sd:5986), so the line names
  # the second; removing it there would let the stale database write again.
  if [ -e "$SD_HUB_CONFIG" ]; then
    echo "  EXTRA   $SD_HUB_CONFIG — this machine is the sd hub, and that file makes it a satellite too; remove it, or, to demote this hub, move $SD_DB to sd.db.demoted-<date> next (README, 'Hub failover')"
  fi
  if role_agents | grep -qxF "$LABEL_PREFIX.sd-dashboard"; then
    if ! command -v tailscale >/dev/null 2>&1; then
      echo "  SKIP    tailscale not installed (brew stage provides it) — no private route to the dashboard"
    elif ! tailscale status >/dev/null 2>&1; then
      echo "  SKIP    tailscale not running or not logged in — see \`$(basename "$SELF") checklist\`, then: $(basename "$SELF") update sd --apply"
    else
      serve=$(tailscale serve status 2>/dev/null || :)
      origin=$(printf '%s\n' "$serve" | sd_route_origin)
      held=$(printf '%s\n' "$serve" | sd_route_holder)
      if [ -n "$origin" ]; then
        echo "  ok      tailscale serve $origin -> 127.0.0.1:$SD_DASHBOARD_PORT"
      elif [ -n "$held" ]; then
        # Not replaced: the port is somebody else's front door, and the
        # runtime refuses a :8443 that holds anything but its own route.
        echo "  DIFFERS tailscale serve https:$SD_DASHBOARD_HTTPS_PORT already serves $held — not replacing it; free the port, then: $(basename "$SELF") update sd --apply"
      else
        echo "  MISSING tailscale serve https:$SD_DASHBOARD_HTTPS_PORT -> 127.0.0.1:$SD_DASHBOARD_PORT — the dashboard has no private front door"
        # The flags the pack's dashboard/sd_dashboard/runtime.py adds and
        # disables the mount with, so either side can take it down again.
        run tailscale serve --bg "--https=$SD_DASHBOARD_HTTPS_PORT" --set-path=/ "http://127.0.0.1:$SD_DASHBOARD_PORT"
      fi
      # The satellites' OTLP/HTTP front door to genai-traces (sd:6945): the
      # collector stays on loopback. It follows the hub role as the
      # dashboard's route does.
      tr_target=$(sh "$ROOT/local-genai-traces/genai-traces.sh" endpoint http 2>/dev/null </dev/null |
        sed -n 's/^OTEL_EXPORTER_OTLP_ENDPOINT=//p') || tr_target=
      if [ -z "$tr_target" ]; then
        echo "  SKIP    telemetry route: genai-traces.sh endpoint http did not answer"
      else
        tr_origin=$(printf '%s\n' "$serve" | sd_route_origin "$GENAI_TRACES_TAILNET_PORT" "$tr_target")
        tr_held=$(printf '%s\n' "$serve" | sd_route_holder "$GENAI_TRACES_TAILNET_PORT" "$tr_target")
        if [ -n "$tr_origin" ]; then
          echo "  ok      tailscale serve $tr_origin -> ${tr_target#http://}"
        elif [ -n "$tr_held" ]; then
          echo "  DIFFERS tailscale serve https:$GENAI_TRACES_TAILNET_PORT already serves $tr_held — not replacing it; free the port, or set GENAI_TRACES_TAILNET_PORT here and on every satellite, then: $(basename "$SELF") update sd --apply"
        else
          echo "  MISSING tailscale serve https:$GENAI_TRACES_TAILNET_PORT -> ${tr_target#http://} — satellites' agent telemetry has no way in"
          run tailscale serve --bg "--https=$GENAI_TRACES_TAILNET_PORT" --set-path=/ "$tr_target"
        fi
      fi
    fi
  fi
}

# A satellite's Jev rows reach the hub's ledger, and no shadow file means live
# (source:local-jev/jev.py::shadow_on), so its sd-review ran on Jev's answer
# (sd:2838). Only a missing file is drift: a written `off` is the operator's.
# The comparison arms stay off: nothing here sets JEV_COMPARE_STAGES. Jev is
# optional, so a machine without it skips this silently.
stage_satellite_jev_shadow() {
  command -v jev >/dev/null 2>&1 || return 0
  # Where jev.py's shadow_file puts it: beside the kill switch.
  shadow="$(dirname "${JEV_FLAG_FILE:-${XDG_CONFIG_HOME:-$HOME/.config}/jev/enabled}")/shadow"
  if [ -e "$shadow" ]; then
    echo "  ok      jev shadow file $shadow ($(head -1 "$shadow"))"
  else
    echo "  MISSING $shadow — Jev would answer live on this satellite"
    run jev shadow on
  fi
}

# A satellite of the sd hub (step 9 of the second-machine plan): the
# profile's `.satellite` names the hub as `host` or `host:port`. The checks
# run in the pack's lib/ (`sd_db.satellite`, under `-I`), because the build
# that must match the hub's is the one every caller runs, not this checkout
# and not a copy in the pack's venv (sd:3279). The stage installs no
# LaunchAgent and no sd cron job.
stage_satellite() {
  echo "== satellite"
  hubs=$(manifest satellite)
  # A satellite runs no hub service (prd R5). Detect only: the operator
  # decides which role is wrong. On a hub profile the sd stage's one EXTRA
  # for hub.json names the conflict instead (criterion 7).
  if ! plain_hub && hub_holder; then
    echo "  SKIP    this machine holds the hub role (promoted): no hub.json, the database is here, and sd-db fence reads holder"
    return 0
  fi
  if ! sd_in_profile && is_satellite; then
    for sho in $SD_HUB_ONLY_AGENTS; do
      sho_label="$LABEL_PREFIX.$sho"
      sho_plist="$HOME/Library/LaunchAgents/$sho_label.plist"
      if [ -e "$sho_plist" ]; then
        echo "  EXTRA   $sho_plist — hub only, and this machine is a satellite; remove it by hand"
      elif launchctl print "gui/$(id -u)/$sho_label" >/dev/null 2>&1; then
        echo "  EXTRA   $sho_label loaded with no plist — hub only, and this machine is a satellite; boot it out by hand"
      fi
    done
  fi
  if [ -z "$hubs" ]; then
    echo "  SKIP    no sd hub in this profile ($PROFILE.satellite)"
    return 0
  fi
  if [ "$(printf '%s\n' "$hubs" | wc -l | tr -d ' ')" -gt 1 ]; then
    echo "  DIFFERS $PROFILE.satellite names more than one hub: $(printf '%s' "$hubs" | tr '\n' ' ')"
    return 0
  fi
  if sd_in_profile; then
    echo "  DIFFERS $PROFILE.satellite names the hub $hubs, and $PROFILE.agent runs the hub's agents; the hub cannot be its own satellite — remove $PROFILE.satellite"
    return 0
  fi
  host=${hubs%%:*}
  port=
  case "$hubs" in *:*) port=${hubs##*:} ;; esac
  stage_satellite_jev_shadow
  if [ ! -x "$SD_DB_PYTHON" ]; then
    echo "  MISSING sd_db: no interpreter at $SD_DB_PYTHON — install the pack (python3 bin/sd_install.py --user), then rerun"
    return 0
  fi
  # Without it the import falls through to a copy installed in the interpreter.
  if [ ! -f "$SD_PACK_ROOT/lib/sd_db/__init__.py" ]; then
    echo "  MISSING sd_db: no $SD_PACK_ROOT/lib/sd_db/__init__.py — move the pack to the hub's pin: local-repo-sync/repo-sync.sh follow"
    return 0
  fi
  set -- --hub "$host"
  [ -z "$port" ] || set -- "$@" --port "$port"
  if [ "$APPLY" -eq 1 ]; then set -- "$@" --apply; fi
  # On a build mismatch the remedy is `repo-sync.sh follow`, which moves the
  # pack to the hub's pin. No source checkout is named: self-install (sd:2802)
  # exported a source checkout, not the hub's build, and from lib/ it finds no
  # venv to install into, so it refuses (sd:3279).
  if out=$("$SD_DB_PYTHON" -I -c "$SD_DB_RUN_LIB" "$SD_PACK_ROOT/lib" sd_db.satellite "$@" 2>&1 </dev/null); then
    printf '%s\n' "$out"
  else
    rc=$?
    printf '%s\n' "$out" | sed 's/^/          /'
    echo "  DIFFERS sd_db satellite check failed under $SD_DB_PYTHON (exit $rc)"
  fi
}

# Root-owned one-off system state, formerly checklist-only. Needs sudo, so
# it prompts once under --apply (the sudoers grace period keeps it to once);
# everything already in the wanted state is skipped without sudo.
stage_system() {
  echo "== system"
  if [ -e /var/db/useLS ]; then
    echo "  ok      /var/db/useLS"
  else
    echo "  MISSING /var/db/useLS"
    sudo_run "sudo touch /var/db/useLS" sudo touch /var/db/useLS
  fi
  if [ -e "$TIMEOUT_SUDOERS" ]; then
    echo "  ok      sudo timestamp_timeout drop-in"
  else
    echo "  MISSING sudo timestamp_timeout drop-in"
    # Held in a variable so the command shown in the deferred summary cannot
    # drift from the command actually run.
    # The Spotlight drop-in's shape: checked before it goes in, so a failed
    # check leaves no drop-in behind (sd:3548). sudo skips the dotted name.
    ts_tmp="$(dirname "$TIMEOUT_SUDOERS")/.$(basename "$TIMEOUT_SUDOERS").tmp"
    sudoers_cmd="echo \"Defaults timestamp_timeout=60\" > $ts_tmp && chmod 440 $ts_tmp && visudo -cf $ts_tmp && mv $ts_tmp $TIMEOUT_SUDOERS || { rm -f $ts_tmp; exit 1; }"
    sudo_run "sudo sh -c '$sudoers_cmd'" sudo sh -c "$sudoers_cmd"
  fi
  # Reading Spotlight's exclusions needs root on every run, and a nightly run
  # or an agent shell has no ticket (sd:3232). This rule allows that one read
  # without a password and nothing else; adding a path still asks. visudo
  # checks the file before it goes in, and sudo skips names with a dot, so a
  # bad or half-written rule never takes effect.
  if [ -n "$(spotlight_wanted)" ]; then
    if [ -e "$SPOTLIGHT_SUDOERS" ]; then
      echo "  ok      Spotlight read sudoers drop-in"
    else
      # The one sign of this gap: without the drop-in the Spotlight read
      # below prints DEFER, which is not a drift word.
      echo "  MISSING Spotlight read sudoers drop-in"
      sr_rule="$(id -un) ALL=(root) NOPASSWD: /usr/bin/plutil -convert xml1 -o - $SPOTLIGHT_PLIST"
      sr_tmp="$(dirname "$SPOTLIGHT_SUDOERS")/.$(basename "$SPOTLIGHT_SUDOERS").tmp"
      sr_cmd="printf '%s\\n' '$sr_rule' > $sr_tmp && chmod 440 $sr_tmp && visudo -cf $sr_tmp && mv $sr_tmp $SPOTLIGHT_SUDOERS || { rm -f $sr_tmp; exit 1; }"
      sudo_run "sudo sh -c \"$sr_cmd\"" sudo sh -c "$sr_cmd"
    fi
  fi
  # One on PATH wins, so a test can stand in for it.
  fw=/usr/libexec/ApplicationFirewall/socketfilterfw
  if command -v socketfilterfw >/dev/null 2>&1; then fw=$(command -v socketfilterfw); fi
  if [ -x "$fw" ]; then
    if "$fw" --getglobalstate 2>/dev/null | grep -q enabled; then
      echo "  ok      application firewall on"
    else
      echo "  DIFFERS application firewall off"
      sudo_run "sudo $fw --setglobalstate on" sudo "$fw" --setglobalstate on
    fi
    if "$fw" --getstealthmode 2>/dev/null | grep -q on; then
      echo "  ok      firewall stealth mode on"
    else
      echo "  DIFFERS firewall stealth mode off"
      sudo_run "sudo $fw --setstealthmode on" sudo "$fw" --setstealthmode on
    fi
  fi
  # Owned by the user, so no sudo — a refused password does not skip this.
  for d in $HARDEN_DIRS; do
    hp="$HOME/$d"
    [ -d "$hp" ] || continue
    if [ "$(stat -f '%Lp' "$hp")" = "700" ]; then
      echo "  ok      ~/$d is 700"
    else
      echo "  DIFFERS ~/$d is not 700"
      run chmod 700 "$hp"
    fi
  done
  # /Users is root-owned, so this one does need sudo — and defers cleanly on a
  # machine where the user is not an admin.
  # Command substitution rather than a pipe into `while`: sudo_run records what
  # it could not run in DEFERRED, and a pipeline would run it in a subshell
  # where that assignment dies with the loop.
  aliases=$(missing_home_aliases)
  if [ -z "$aliases" ]; then
    echo "  ok      repo config symlinks resolve"
  else
    for alias_path in $aliases; do
      echo "  MISSING home alias $alias_path"
      sudo_run "sudo ln -s $(basename "$HOME") $alias_path" \
        sudo ln -s "$(basename "$HOME")" "$alias_path"
    done
  fi
  system_nsmb
  system_spotlight
}

# Soft SMB mounts: a dead NAS session hung shutdown twice in smbfs_unmount,
# waiting on a reconnect that never came (sd:4843). soft=yes lets a request
# to a gone server fail instead of waiting; kloglevel=1 logs the kernel side.
# Both apply to the next mount, not to one already up.
NSMB_WANT="soft=yes kloglevel=1"

# Reads the file the way the SMB client does (rc_parse in Apple's SMBClient
# lib/smb/rcfile.c), since that is what decides what a mount gets:
# - a carriage return is dropped anywhere, and leading space on a line;
# - a line that starts with # or ; is a comment, even inside a value's line;
# - a section name is the text between [ and ], case and spaces kept; the
#   rest of that line is ignored; [global] is [default], and a name that
#   comes back later adds to the same section;
# - a key is the text before the first =, with every space and tab removed,
#   compared case and all; the value is the rest, less its leading space;
#   a later value for the same key wins; a key before any section is ignored;
# - soft is read like rc_getbool, kloglevel like rc_getint (strtol base 0,
#   cast to int).
# A section header with no ] and a line over 2000 bytes are read differently
# by the client (it reads on to the next ] or drops the line), so either
# makes the state unknown and nothing is written.
#
# scan prints "missing key=value" or "differs key=value" for each wanted key
# not in force, or "unknown". merge prints the file with each of `add` put
# under the first [default] or [global] header, or a new [default] at the end;
# every line already there is kept as it was.
NSMB_AWK='
  function boolval(p,   c, lp) {
    sub(/^[[:space:]]+/, "", p); c = substr(p, 1, 1); lp = tolower(p)
    if (c == "0" || lp == "no" || lp == "false") return 0
    if (c == "1" || lp == "yes" || lp == "true") return 1
    return -1
  }
  # The int strtol(p, NULL, 0) gives after the cast: the first character
  # that is not a digit stops the read, a value past LONG_MAX is LONG_MAX and
  # one below LONG_MIN is LONG_MIN. The value is kept as two exact 32-bit
  # halves, so no double rounds it.
  function intval(p,   neg, base, digits, d, hi, lo, t, over, c) {
    sub(/^[[:space:]]+/, "", p); neg = 0
    c = substr(p, 1, 1)
    if (c == "+" || c == "-") { neg = (c == "-"); p = substr(p, 2) }
    base = 10; digits = "0123456789"
    if (p ~ /^0[xX][0-9a-fA-F]/) { base = 16; digits = "0123456789abcdef"; p = substr(p, 3) }
    else if (p ~ /^0/) { base = 8; digits = "01234567" }
    hi = 0; lo = 0; over = 0
    while (p != "" && (d = index(digits, tolower(substr(p, 1, 1)))) > 0) {
      if (!over) {
        t = lo * base + d - 1; lo = t % 4294967296; hi = hi * base + int(t / 4294967296)
        if (hi > 2147483648 || (hi == 2147483648 && lo > 0)) over = 1
      }
      p = substr(p, 2)
    }
    if (over || (!neg && hi == 2147483648)) return neg ? 0 : -1
    if (neg) lo = (4294967296 - lo) % 4294967296
    return lo >= 2147483648 ? lo - 4294967296 : lo
  }
  BEGIN { n = split(want, w, " "); for (i = 1; i <= n; i++) { split(w[i], kv, "="); k[i] = kv[1]; v[i] = kv[2] } }
  {
    l = $0; gsub(/\r/, "", l)
    if (length(l) > 2000) bad = 1
    sub(/^[[:space:]]+/, "", l); c = substr(l, 1, 1)
    if (mode == "merge") print
    if (c == "[") {
      e = index(l, "]")
      if (!e) { bad = 1; sec = ""; next }
      sec = substr(l, 2, e - 2); if (sec == "global") sec = "default"
      insec = 1
      if (mode == "merge" && sec == "default" && !done) { for (i = 1; i <= n; i++) print w[i]; done = 1 }
      next
    }
    if (l == "" || c == "#" || c == ";" || !insec || sec != "default" || !(e = index(l, "="))) next
    key = substr(l, 1, e - 1); gsub(/[ \t]/, "", key)
    val = substr(l, e + 1); sub(/^[ \t]+/, "", val)
    for (i = 1; i <= n; i++) if (key == k[i]) have[i] = val
  }
  END {
    if (mode == "merge") {
      if (!done) { if (NR) print ""; print "[default]"; for (i = 1; i <= n; i++) print w[i] }
      exit
    }
    if (bad) { print "unknown"; exit }
    for (i = 1; i <= n; i++) {
      if (!(i in have)) { print "missing " w[i]; continue }
      if (v[i] ~ /^[0-9]+$/) good = intval(have[i]) == v[i] + 0
      else good = boolval(have[i]) == boolval(v[i])
      if (!good) print "differs " k[i] "=" have[i]
    }
  }'

nsmb_scan() { # file
  [ -e "$1" ] || set -- /dev/null
  awk -v mode=scan -v want="$NSMB_WANT" "$NSMB_AWK" "$1"
}

nsmb_merge() { # file, "key=value ..."
  [ -e "$1" ] || set -- /dev/null "$2"
  awk -v mode=merge -v want="$2" "$NSMB_AWK" "$1"
}

system_nsmb() {
  # A failed read is unknown, not clean, so it counts as drift.
  if ! nc_scan=$(nsmb_scan "$NSMB_CONF" 2>&1); then
    echo "  DIFFERS nsmb.conf soft mounts: cannot read $NSMB_CONF, state unknown"
    return 0
  fi
  if [ "$nc_scan" = unknown ]; then
    echo "  DIFFERS nsmb.conf soft mounts: $NSMB_CONF has a [ with no ] or a line over 2000 bytes, state unknown; edit by hand"
    return 0
  fi
  nc_add=$(printf '%s\n' "$nc_scan" | sed -n 's/^missing //p' | tr '\n' ' ' | sed 's/ $//')
  nc_differs=$(printf '%s\n' "$nc_scan" | sed -n 's/^differs //p' | tr '\n' ' ' | sed 's/ $//')
  if [ -z "$nc_add" ] && [ -z "$nc_differs" ]; then
    echo "  ok      nsmb.conf soft mounts"
    return 0
  fi
  # The operator's own value stays: only a missing key is added.
  [ -z "$nc_differs" ] ||
    echo "  DIFFERS nsmb.conf soft mounts: [default] has $nc_differs in $NSMB_CONF; want $NSMB_WANT, edit by hand"
  [ -n "$nc_add" ] || return 0
  if [ -e "$NSMB_CONF" ]; then
    echo "  MISSING nsmb.conf soft mounts: $nc_add under [default] (applies at the next mount: eject and reconnect, or reboot)"
  else
    echo "  MISSING nsmb.conf soft mounts: no $NSMB_CONF (applies at the next mount: eject and reconnect, or reboot)"
  fi
  # Built as this user, then copied in by root beside the target and moved
  # over it, after a dated copy of the old file: a stop at any point leaves
  # the old file or the new one, and the next run finishes the job.
  nc_tmp=""
  if [ "$APPLY" -eq 1 ]; then
    if ! { nc_tmp=$(mktemp) && nsmb_merge "$NSMB_CONF" "$nc_add" > "$nc_tmp"; }; then
      note_failure "build the new $NSMB_CONF" 1
      [ -z "$nc_tmp" ] || rm -f "$nc_tmp"
      return 0
    fi
  fi
  nc_new="$(dirname "$NSMB_CONF")/.$(basename "$NSMB_CONF").tmp"
  nc_cmd="{ [ ! -e $NSMB_CONF ] || cp -p $NSMB_CONF $NSMB_CONF.bak-$(date +%Y%m%d%H%M%S); } && cp $nc_tmp $nc_new && chmod 644 $nc_new && mv $nc_new $NSMB_CONF || { rm -f $nc_new; exit 1; }"
  sudo_run "add $nc_add under [default] in $NSMB_CONF" sh -c "$nc_cmd"
  [ -z "$nc_tmp" ] || rm -f "$nc_tmp"
}

# The paths <profile>.spotlight wants kept out of the index, one per line.
# A leading `~` is this account's home, so one list serves any user name.
spotlight_wanted() {
  manifest spotlight | spotlight_expand
}

# Manifest entries on stdin, as the absolute paths the live list holds.
# capture runs common.spotlight through it too, so `~/repos/` in common and
# <users>/<name>/repos in the live list are the same entry.
spotlight_expand() {
  while IFS= read -r sw; do
    # The quoted `~` is the literal the manifest holds, matched on purpose.
    # shellcheck disable=SC2088
    case "$sw" in
      "~") sw="$HOME" ;;
      # ${sw#??} drops the two characters `~/`.
      "~/"*) sw="$HOME/${sw#??}" ;;
    esac
    # The store holds no trailing slash; `~/repos/` must still match.
    case "$sw" in /) ;; */) sw="${sw%/}" ;; esac
    printf '%s\n' "$sw"
  done
}

# Reads the live list into $1, one path per line, and returns 0. On failure it
# returns 1 with SPOTLIGHT_WHY and SPOTLIGHT_FIX set to the cause and the
# remedy. SPOTLIGHT_KEY says whether the plist has an Exclusions array at all.
#
# Runs in the caller's shell, not in $(...), so those globals survive. It is
# called inside `if`, where set -e is off, so every step checks for itself.
#
# One sudo call converts the store to XML in a 0600 temp file; PlistBuddy then
# reads the copy without sudo. PlistBuddy alone cannot tell an unreadable file
# from a missing key: it reports both as "Does Not Exist".
#
# Without --apply this never prompts: `sudo -n` has a cached ticket, the
# sudoers drop-in allows the read (sd:3232), or the read defers. The nightly
# `status` run is non-interactive.
spotlight_read() { # outfile
  SPOTLIGHT_KEY=0; SPOTLIGHT_WHY=""; SPOTLIGHT_FIX=""
  : > "$1"
  if ! sudo -n true 2>/dev/null &&
     ! sudo -n -l plutil -convert xml1 -o - "$SPOTLIGHT_PLIST" >/dev/null 2>&1; then
    if [ "$APPLY" -ne 1 ] || ! sudo -v; then
      SPOTLIGHT_WHY="reading $SPOTLIGHT_PLIST needs sudo, and no sudo ticket is cached"
      SPOTLIGHT_FIX="run 'sudo -v', then re-run (or re-run with --apply)"
      return 1
    fi
  fi
  sr_xml=$(mktemp); sr_err=$(mktemp)
  # The redirects run as this user on purpose: the copy is ours to delete.
  # shellcheck disable=SC2024
  if ! sudo -n plutil -convert xml1 -o - "$SPOTLIGHT_PLIST" > "$sr_xml" 2> "$sr_err"; then
    # Some plutil builds print the error on stdout (the CI runner's does), so
    # stderr first, then stdout.
    sr_msg=$(cat "$sr_err" "$sr_xml" | grep -m 1 . || :)
    # A plutil that says nothing at all: ask whether the file is there.
    if [ -z "$sr_msg" ] && ! sudo -n test -e "$SPOTLIGHT_PLIST"; then
      sr_msg="No such file"
    fi
    rm -f "$sr_xml" "$sr_err"
    case "$sr_msg" in
      *"no such file"*|*"No such file"*)
        SPOTLIGHT_WHY="$SPOTLIGHT_PLIST does not exist"
        SPOTLIGHT_FIX="check that Spotlight indexes this volume: mdutil -s /" ;;
      # Root that cannot open a file has hit TCC, not a file mode. The
      # message says "Operation not permitted" or "don't have permission".
      *"not permitted"*|*"permission"*)
        SPOTLIGHT_WHY="sudo cannot open $SPOTLIGHT_PLIST without Full Disk Access"
        SPOTLIGHT_FIX="grant this terminal Full Disk Access (System Settings > Privacy & Security > Full Disk Access), then re-run" ;;
      *)
        SPOTLIGHT_WHY="could not read $SPOTLIGHT_PLIST: ${sr_msg:-plutil failed}"
        SPOTLIGHT_FIX="read it by hand: sudo plutil -p $SPOTLIGHT_PLIST" ;;
    esac
    return 1
  fi
  # A missing key is an empty list, not an error: a volume that never had an
  # exclusion has no array. PlistBuddy prints an array as `Array {`, one
  # indented value per line, then `}`.
  if "$PLISTBUDDY" -c 'Print :Exclusions' "$sr_xml" > "$sr_err" 2>/dev/null; then
    SPOTLIGHT_KEY=1
    sed -e '1d' -e '$d' -e 's/^[[:space:]]*//' "$sr_err" | grep -v '^$' > "$1" || :
  fi
  rm -f "$sr_xml" "$sr_err"
  return 0
}

# Compares <profile>.spotlight with the live list: `ok` per present path,
# `MISSING` per absent one, `EXTRA` per path the profile does not name. Under
# --apply it adds each missing path and restarts mds once, all through sudo_run
# so a refused sudo defers. It never removes a path: an exclusion someone added
# by hand is a decision, and capture is how it reaches the profile.
#
# mds reads the list when it starts, so an edit without the restart does
# nothing until the next boot. After the restart the list is read again; a path
# still absent then means something wrote the file back.
system_spotlight() {
  wanted=$(spotlight_wanted)
  if [ -z "$wanted" ]; then
    echo "  --      no Spotlight exclusions in this profile"
    return 0
  fi
  ss_cur=$(mktemp)
  if ! spotlight_read "$ss_cur"; then
    rm -f "$ss_cur"
    echo "  DEFER   Spotlight exclusions not read: $SPOTLIGHT_WHY; $SPOTLIGHT_FIX"
    if [ "$APPLY" -eq 1 ]; then
      DEFERRED="$DEFERRED    $SPOTLIGHT_FIX  (Spotlight exclusions)
"
    fi
    return 0
  fi
  missing=""
  while IFS= read -r p; do
    [ -n "$p" ] || continue
    if grep -Fxq -- "$p" "$ss_cur"; then
      echo "  ok      Spotlight excludes $p"
    else
      echo "  MISSING Spotlight exclusion $p"
      missing="$missing$p
"
    fi
  done <<EOF_SPOTLIGHT
$wanted
EOF_SPOTLIGHT
  while IFS= read -r p; do
    [ -n "$p" ] || continue
    printf '%s\n' "$wanted" | grep -Fxq -- "$p" \
      || echo "  EXTRA   Spotlight exclusion $p (not in the profile; left in place, capture records it)"
  done < "$ss_cur"
  rm -f "$ss_cur"
  [ -n "$missing" ] || return 0

  added=0
  if [ "$SPOTLIGHT_KEY" -eq 0 ]; then
    sudo_run "sudo $PLISTBUDDY -c 'Add :Exclusions array' $SPOTLIGHT_PLIST" \
      "$PLISTBUDDY" -c 'Add :Exclusions array' "$SPOTLIGHT_PLIST"
  fi
  # A heredoc, not a pipe: `added` and DEFERRED must outlive the loop.
  while IFS= read -r p; do
    [ -n "$p" ] || continue
    # PlistBuddy parses its -c string itself: an apostrophe is "Unclosed
    # Quotes" (and it still exits 0), a double quote is dropped from the
    # value. The path stays MISSING, and the add is left to a person.
    case "$p" in
      *\'*|*\"*|*\\*)
        echo "  SKIP    cannot add $p: PlistBuddy mangles quotes and backslashes; add it in System Settings > Spotlight"
        continue ;;
    esac
    # `:Exclusions:` with no index appends to the array.
    ss_before="$DEFERRED"
    sudo_run "sudo $PLISTBUDDY -c 'Add :Exclusions: string $p' $SPOTLIGHT_PLIST" \
      "$PLISTBUDDY" -c "Add :Exclusions: string $p" "$SPOTLIGHT_PLIST"
    [ "$DEFERRED" = "$ss_before" ] && added=$((added + 1))
  done <<EOF_SPOTLIGHT
$missing
EOF_SPOTLIGHT
  [ "$added" -gt 0 ] || return 0
  sudo_run "sudo launchctl kickstart -k system/com.apple.metadata.mds" \
    launchctl kickstart -k system/com.apple.metadata.mds
  [ "$APPLY" -eq 1 ] || return 0
  ss_cur=$(mktemp)
  if spotlight_read "$ss_cur"; then
    while IFS= read -r p; do
      [ -n "$p" ] || continue
      grep -Fxq -- "$p" "$ss_cur" \
        || echo "  MISSING Spotlight exclusion $p (still absent after the add and the mds restart)"
    done <<EOF_SPOTLIGHT
$missing
EOF_SPOTLIGHT
  fi
  rm -f "$ss_cur"
}

# Report-only: community plugins are installed from inside Obsidian
# (Settings -> Community plugins), there is no sanctioned CLI. The vault
# gitignores .obsidian/ (it holds the Local REST API TLS key), so the
# profile manifest is the only portable record of the plugin set.
# The vault sits under ~/Documents, which the shell's TCC grant does not
# cover on this machine — the listing goes through python3 (which has
# Full Disk Access) instead of ls/find. PATH python3 (Homebrew) on purpose:
# it is the binary that actually holds the grant. Pinning to /usr/bin/python3
# looked tidier (its path survives brew upgrades) but macOS ignores user FDA
# grants for Xcode's python under launchd — tested 2026-08-29 as the bundle,
# the inner binary, toggled, and after a TCC reset; every variant fails.
stage_obsidian() {
  echo "== obsidian"
  entries=$(manifest obsidian)
  if [ -z "$entries" ]; then
    echo "  no Obsidian plugins in this profile"
    return 0
  fi
  vault="$(st_shared OBSIDIAN_VAULT)"
  if [ -z "$vault" ]; then
    echo "  SKIP    OBSIDIAN_VAULT not set (shared.env or the environment)"
    return 0
  fi
  py="${MACHINE_SETUP_PYTHON:-python3}"
  # Exit 1 is no vault; a vault with no plugins yet lists none. An empty
  # list used to read as no vault (sd:3548). Bounded: the vault may sit on a
  # synced or network folder that stalls (sd:4816); a timeout is no answer,
  # not a vault that is not there.
  ob_rc=0
  installed=$(bound_step 30 "$py" -c 'import pathlib, sys
vault = pathlib.Path(sys.argv[1]) / ".obsidian"
d = vault / "plugins"
try:
    if not vault.is_dir():
        sys.exit(1)
    names = sorted(p.name for p in d.iterdir() if p.is_dir()) if d.exists() else []
except OSError:
    sys.exit(1)
print("\n".join(names))' "$vault" 2>/dev/null) || ob_rc=$?
  if [ "$ob_rc" -eq 124 ]; then
    echo "  FAILED  the vault did not answer in time: $vault; its plugins were not checked"
    return 0
  elif [ "$ob_rc" -ne 0 ]; then
    echo "  SKIP    vault not found or unreadable: $vault (set OBSIDIAN_VAULT)"
    return 0
  fi
  echo "$entries" | while read -r p; do
    if echo "$installed" | grep -qx "$p"; then
      echo "  ok      plugin $p"
    else
      echo "  MISSING plugin $p — install in Obsidian: Settings -> Community plugins"
    fi
  done
  echo "$installed" | while read -r p; do
    [ -n "$p" ] || continue
    echo "$entries" | grep -qx "$p" \
      || echo "  extra   plugin $p (installed, not in profile — add it or remove the plugin)"
  done
}

# iTerm2 keeps everything — profiles, keymaps, colors — in one nested plist,
# so per-key `defaults write` is useless here. iTerm2's own answer is a custom
# prefs folder: point it at a synced directory and the whole plist travels
# with the machine. The manifest holds that one path, profile-scoped because
# the Drive mount differs per machine.
stage_iterm2() {
  echo "== iterm2"
  dir=$(manifest iterm2 | head -1)
  if [ -z "$dir" ]; then
    echo "  ok      iTerm2 prefs stay local (no synced folder in $PROFILE.iterm2)"
    return 0
  fi
  if [ ! -d "$APPLICATIONS_DIR/iTerm.app" ]; then
    echo "  SKIP    iTerm.app not installed (cask iterm2)"
    return 0
  fi
  # Exit 3 is no folder, 4 a folder with no plist. Bounded like the vault
  # read: the folder sits on a synced mount that stalls (sd:4832); a timeout
  # or a failed read is no answer, not a folder that is not there.
  py="${MACHINE_SETUP_PYTHON:-python3}"
  it_rc=0
  bound_step 30 "$py" -c 'import os, stat, sys
def mode(path):
    try:
        return os.stat(path).st_mode
    except (FileNotFoundError, NotADirectoryError):
        return 0
if not stat.S_ISDIR(mode(sys.argv[1])):
    sys.exit(3)
if not stat.S_ISREG(mode(os.path.join(sys.argv[1], "com.googlecode.iterm2.plist"))):
    sys.exit(4)' "$dir" 2>/dev/null || it_rc=$?
  if [ "$it_rc" -eq 3 ]; then
    echo "  MISSING prefs folder $dir — not synced down yet; re-run once it is"
    return 0
  fi
  # Repointing iTerm2 at an empty folder makes it write fresh defaults there,
  # and the synced settings are what get lost. Stop before touching prefs.
  if [ "$it_rc" -eq 4 ]; then
    echo "  MISSING $dir/com.googlecode.iterm2.plist — folder is empty, not repointing iTerm2"
    return 0
  fi
  if [ "$it_rc" -ne 0 ]; then
    it_why="could not be read"
    [ "$it_rc" -ne 124 ] || it_why="did not answer in time"
    echo "  DIFFERS prefs folder $dir $it_why, state unknown; iTerm2 not repointed"
    return 0
  fi
  echo "  ok      com.googlecode.iterm2.plist present"

  changed=0
  cur=$(defaults read com.googlecode.iterm2 PrefsCustomFolder 2>/dev/null || echo "(unset)")
  if [ "$cur" = "$dir" ]; then
    echo "  ok      PrefsCustomFolder = $cur"
  else
    defaults_write com.googlecode.iterm2 PrefsCustomFolder string "$dir"
    changed=1
  fi
  cur=$(defaults read com.googlecode.iterm2 LoadPrefsFromCustomFolder 2>/dev/null || echo "(unset)")
  if [ "$cur" = "1" ]; then
    echo "  ok      LoadPrefsFromCustomFolder = 1"
  else
    defaults_write com.googlecode.iterm2 LoadPrefsFromCustomFolder bool true
    changed=1
  fi

  if [ "$changed" -eq 1 ]; then
    echo "  NOTE    quit iTerm2 before applying: it rewrites its plist on quit"
    echo "          and a running instance would overwrite these keys"
  fi
}

# ds_ names: every stage shares these globals, and a stage that set `want`
# skipped every stage after it (sd:7041).
do_stages() {
  ds_want="$1"
  if [ -n "$ds_want" ]; then
    case " $STAGES " in
      *" $ds_want "*) ;;
      *) die "unknown stage '$ds_want' (stages: $STAGES)" ;;
    esac
  fi
  FAILED_LOG=$(mktemp)
  for ds_stage in $STAGES; do
    if [ -n "$ds_want" ] && [ "$ds_want" != "$ds_stage" ]; then continue; fi
    "stage_$ds_stage"
    echo
  done
  report_deferred
  report_failures
}

# Anything sudo_run could not run. Printed once, at the end, where it is still
# on screen after a long run — a DEFER line 200 lines up is a line nobody sees.
# Exit stays 0: deferring is the expected outcome on a machine where the user
# cannot sudo, and doctor reports these same items independently, so the drift
# stays visible without an exit code that would surprise a caller. $1 is the
# verb to re-run, `update system` by default.
report_deferred() {
  [ -n "$DEFERRED" ] || return 0
  n=$(printf '%s' "$DEFERRED" | grep -c '^' || :)
  echo "deferred — $n action(s) still need sudo. Run these, then re-run:"
  printf '%s' "$DEFERRED"
  echo "    $(basename "$0") ${1:-update system} --apply"
  echo
}

# Self-update off in every installed cask with `auto_updates true`, where the
# app documents a switch (sd:3062). The weekly upgrade-report runs
# `brew upgrade --cask --greedy`, so brew is the one updater: both machines
# change together, and the macOS privacy (TCC) grants an update drops are
# re-granted in one sitting. An app that is not installed is skipped.
# BREW_SKIPPED_CASKS below is the one exception.
#
# One row per app: cask|app|mechanism|target|key|value.
# - sparkle: target is the defaults domain. Sparkle keeps the user's choice
#   there under its Info.plist key names, SUEnableAutomaticChecks (scheduled
#   checks) and SUAutomaticallyUpdate (silent installs); both go false. Each
#   app ships Sparkle.framework; a build without it is skipped.
#   ChatGPT.app's domain is com.openai.codex, read from its bundle.
# - defaults: a documented boolean in the app's domain. Claude desktop reads
#   disableAutoUpdates from com.anthropic.claudefordesktop.
# - json: one top-level key in the app's settings file, through
#   claude_settings.py. VS Code documents update.mode none and Zed
#   auto_update false. Docker Desktop keeps its "Always download updates"
#   setting as AutoDownloadUpdates in settings-store.json.
# - env: an environment variable, for a CLI; app is the command, target the
#   shell env file every login shell and job sources. antigravity-cli's agy
#   reads AGY_CLI_DISABLE_AUTO_UPDATE (named in its binary, not its docs):
#   its updater replaced the cask's link with a bare binary (fleet sd:5566).
#
# No documented per-user switch, so left on (checked 2026-10-08): 1Password
# (a Settings > Advanced toggle only); Electron apps Antigravity, Beekeeper
# Studio, Discord, Dropbox, Notion, Obsidian, OpenWhispr, Postman and Slack;
# Beyond Compare, iStat Menus, Malwarebytes, Piezo and Sync with their own
# updaters; Firefox, Google Chrome, Google Drive and Zoom, whose switches are
# system-wide managed policy; and the CLIs copilot-cli and gcloud-cli. Updater agents are never removed to stop an app.
#
# The exception (sd:7058, operator ruling 2026-11-01): Google Chrome and Slack
# keep their own updaters on, and brew no longer upgrades them. Both sit
# root-owned in /Applications, which the operator keeps, so brew's upgrade
# needs sudo for them. BREW_SKIPPED_CASKS is the one list: the --greedy sweep,
# the askpass sweep and the outdated report all drop these names.
BREW_SKIPPED_CASKS="google-chrome slack"
drop_brew_skipped() { # stdin: one cask per line, as `brew outdated --quiet` prints
  awk -v skip="$BREW_SKIPPED_CASKS" '
    BEGIN { n = split(skip, s, " "); for (i = 1; i <= n; i++) drop[s[i]] = 1 }
    { name = $1; sub(/.*\//, "", name); if (!(name in drop)) print }'
}

self_update_rows() {
  printf '%s\n' \
    "appcleaner|AppCleaner.app|sparkle|net.freemacsoft.AppCleaner" \
    "betterdisplay|BetterDisplay.app|sparkle|pro.betterdisplay.BetterDisplay" \
    "bettertouchtool|BetterTouchTool.app|sparkle|com.hegenberg.BetterTouchTool" \
    "chatgpt|ChatGPT.app|sparkle|com.openai.codex" \
    "chatgpt-classic|ChatGPT Classic.app|sparkle|com.openai.chat" \
    "cleanshot|CleanShot X.app|sparkle|pl.maketheweb.cleanshotx" \
    "daisydisk|DaisyDisk.app|sparkle|com.daisydiskapp.DaisyDiskStandAlone" \
    "handbrake-app|HandBrake.app|sparkle|fr.handbrake.HandBrake" \
    "iterm2|iTerm.app|sparkle|com.googlecode.iterm2" \
    "languagetool-desktop|LanguageTool for Desktop.app|sparkle|org.languagetool.desktop" \
    "maccy|Maccy.app|sparkle|org.p0deje.Maccy" \
    "macwhisper|MacWhisper.app|sparkle|com.goodsnooze.MacWhisper" \
    "medis|Medis.app|sparkle|li.zihua.medis2" \
    "nordvpn|NordVPN.app|sparkle|com.nordvpn.macos" \
    "vlc|VLC.app|sparkle|org.videolan.vlc" \
    "claude|Claude.app|defaults|com.anthropic.claudefordesktop|disableAutoUpdates|true" \
    "visual-studio-code|Visual Studio Code.app|json|$HOME/Library/Application Support/Code/User/settings.json|update.mode|\"none\"" \
    "zed|Zed.app|json|$HOME/.config/zed/settings.json|auto_update|false" \
    "docker-desktop|Docker.app|json|$HOME/Library/Group Containers/group.com.docker/settings-store.json|AutoDownloadUpdates|false" \
    "antigravity-cli|agy|env|$HOME/.config/shell/env.sh|AGY_CLI_DISABLE_AUTO_UPDATE|1"
}

# Can this process read an app's data? macOS refuses another app's container
# to a process without Full Disk Access, and launchd's /bin/bash has none
# (sd:3145): Maccy's prefs read as unset and Docker's settings file as
# unparseable, so the nightly drift job failed on rows no run could clear.
# A path that does not exist is not blocked; the caller handles absence.
app_data_blocked() { # path
  if [ -d "$1" ]; then
    ! ls "$1" >/dev/null 2>&1
  elif [ -e "$1" ]; then
    ! head -c 1 "$1" >/dev/null 2>&1
  else
    return 1
  fi
}

app_data_defer() { # what
  echo "  DEFER   $1 not checked: macOS blocks this process from the app's data; run from a terminal with Full Disk Access"
}

# One boolean in a defaults domain: ok, or the write that converges it.
self_update_default() { # domain key true|false
  cur=$(defaults read "$1" "$2" 2>/dev/null || echo "(unset)")
  if norm_eq bool "$cur" "$3"; then
    echo "  ok      $1 $2 = $cur"
  else
    defaults_write "$1" "$2" bool "$3"
  fi
}

# A sandboxed app keeps its domain in its container, and macOS refuses a
# write there from a terminal without Full Disk Access (Maccy, sd:3103).
# Under set -e that one refusal ended the run, so the key is reported and the
# stage goes on.
defaults_write() { # domain key type value
  try_run defaults write "$1" "$2" "-$3" "$4" \
    || echo "  DIFFERS $1 $2 not written; set it by hand to $4, or grant the terminal Full Disk Access and re-run"
}

# The screen saver never starts (sd:3261). idleTime is a per-host key, so it
# lives in the currentHost domain; every profile gets it, no manifest line.
# The drift grep in status_stage counts the `defaults -currentHost write` line.
stage_screensaver_off() {
  cur=$(defaults -currentHost read com.apple.screensaver idleTime 2>/dev/null || echo "(unset)")
  if [ "$cur" = "0" ]; then
    echo "  ok      com.apple.screensaver idleTime = 0 (currentHost)"
  else
    try_run defaults -currentHost write com.apple.screensaver idleTime -int 0 \
      || echo "  DIFFERS com.apple.screensaver idleTime not written; set it by hand to 0 (defaults -currentHost write com.apple.screensaver idleTime -int 0)"
  fi
}

# App Store updates install at maintenance-nightly's time, not the App
# Store's own (sd:6958). macOS's own install settings stay as they are, by
# the operator's ruling. Root-owned: --apply writes it through the repair
# drop-in's sudo -n, else sudo_run asks once or defers with the command.
APPSTORE_PREFS=/Library/Preferences/com.apple.commerce
appstore_autoupdate_off() {
  norm_eq bool "$(defaults read "$APPSTORE_PREFS" AutoUpdate 2>/dev/null || :)" false
}

stage_appstore_autoupdate_off() {
  if appstore_autoupdate_off; then
    echo "  ok      $APPSTORE_PREFS AutoUpdate = 0"
    return 0
  fi
  echo "  DIFFERS App Store auto-update on: $APPSTORE_PREFS AutoUpdate is not 0"
  if [ "$APPLY" -eq 1 ] &&
     sudo -n defaults write "$APPSTORE_PREFS" AutoUpdate -bool false </dev/null 2>/dev/null; then
    return 0
  fi
  sudo_run "sudo defaults write $APPSTORE_PREFS AutoUpdate -bool false" \
    defaults write "$APPSTORE_PREFS" AutoUpdate -bool false
}

# One key in a JSON settings file: ok, or MISSING/DIFFERS and the merge.
self_update_json() { # file key json-value
  if app_data_blocked "$1"; then
    app_data_defer "$1 $2"
    return 0
  fi
  if ! gap=$(python3 "$DIR/claude_settings.py" pref-missing "$1" "$2" "$3" 2>/dev/null </dev/null); then
    echo "  DIFFERS $1 does not parse; $2 not checked"
    return 0
  fi
  case "$gap" in
    '') echo "  ok      $1 $2 = $3"; return 0 ;;
    unset) echo "  MISSING $1 $2 = $3" ;;
    *) echo "  DIFFERS $1 $2 $gap, want $3" ;;
  esac
  run_ok backup_file "$1" || return 0
  run_ok mkdir -p "$(dirname "$1")" || return 0
  try_run python3 "$DIR/claude_settings.py" pref-merge "$1" "$2" "$3" \
    || echo "  DIFFERS $1 $2 not changed; set it to $3 by hand"
}

# One `export KEY=VALUE` line in a shell env file: ok, or MISSING/DIFFERS and
# the append. A later line wins in sh, so a differing value is overridden, not
# edited; the file is created owner-only, as the shell env check wants it.
self_update_env() { # file key value
  want="export $2=$3"
  if [ -f "$1" ] && grep -qxF "$want" "$1"; then
    echo "  ok      $1 $2 = $3"
    return 0
  fi
  if [ -f "$1" ] && grep -q "^export $2=" "$1"; then
    echo "  DIFFERS $1 $2, want $3"
  else
    echo "  MISSING $1 $2 = $3"
  fi
  run_ok mkdir -p "$(dirname "$1")" || return 0
  # A file without a final newline would glue the export onto its last line.
  try_run sh -c 'umask 077; nl=; [ -s "$2" ] && [ -n "$(tail -c 1 "$2")" ] && nl="\n"
    printf "$nl%s\n" "$1" >> "$2"' sh "$want" "$1" \
    || echo "  DIFFERS $1 $2 not written; add '$want' by hand"
}

stage_self_update() {
  echo "  self-update off (casks that update themselves; the weekly upgrade moves them)"
  while IFS='|' read -r cask app how target key val; do
    case "$how" in
      env) command -v "$app" >/dev/null 2>&1 ;;
      *) [ -n "$app" ] && [ -d "$APPLICATIONS_DIR/$app" ] ;;
    esac || continue
    case "$how" in
      sparkle)
        # An App Store build ships no Sparkle and no updater of its own (the
        # work machine's Maccy, sd:3103): no key to turn off, so no drift.
        if [ ! -d "$APPLICATIONS_DIR/$app/Contents/Frameworks/Sparkle.framework" ]; then
          echo "  --      $app has no Sparkle.framework; no self-update to turn off"
          continue
        fi
        if app_data_blocked "$HOME/Library/Containers/$target/Data/Library/Preferences"; then
          app_data_defer "$target self-update"
          continue
        fi
        self_update_default "$target" SUEnableAutomaticChecks false
        self_update_default "$target" SUAutomaticallyUpdate false ;;
      defaults) self_update_default "$target" "$key" "$val" ;;
      json) self_update_json "$target" "$key" "$val" ;;
      env) self_update_env "$target" "$key" "$val" ;;
      *) echo "  DIFFERS $cask: unknown self-update mechanism '$how'" ;;
    esac
  done <<EOF_SELF_UPDATE
$(self_update_rows)
EOF_SELF_UPDATE
}

# .macos lines are "<domain> <key> <type> <value...>". Only keys named in a
# manifest are ever touched or captured: whole-domain exports rot across macOS
# releases and hit TCC-blocked domains (Safari, AddressBook) silently.
stage_macos() {
  echo "== macos"
  command -v defaults >/dev/null 2>&1 || { echo "  defaults MISSING (not macOS?)"; return 0; }
  stage_self_update
  stage_screensaver_off
  stage_appstore_autoupdate_off
  entries=$(manifest macos)
  if [ -z "$entries" ]; then
    echo "  no macOS settings in this profile"
    return 0
  fi

  changed=0
  # Not a pipeline: `changed` must survive the loop, and a pipeline subshell
  # would discard it.
  while read -r dom key typ val; do
    [ -z "$dom" ] && continue
    cur=$(defaults read "$dom" "$key" 2>/dev/null || echo "(unset)")
    if norm_eq "$typ" "$cur" "$val"; then
      echo "  ok      $dom $key = $cur"
    else
      defaults_write "$dom" "$key" "$typ" "$val"
      changed=1
    fi
  done <<EOF_MACOS
$entries
EOF_MACOS

  if [ "$changed" -eq 1 ] && [ "$APPLY" -eq 1 ]; then
    echo "  NOTE    restart affected apps to pick the changes up:"
    echo "          killall Dock; killall Finder; killall SystemUIServer"
  fi
}

# defaults read prints bools as 1/0; manifests may say true/false. Normalize
# before comparing so `autohide bool true` matches a read of `1`.
norm_eq() {
  typ="$1"; a="$2"; b="$3"
  if [ "$typ" = "bool" ]; then
    case "$a" in true|TRUE|yes|YES) a=1 ;; false|FALSE|no|NO) a=0 ;; esac
    case "$b" in true|TRUE|yes|YES) b=1 ;; false|FALSE|no|NO) b=0 ;; esac
  fi
  [ "$a" = "$b" ]
}

cmd_setup() {
  valid_profile "$1"
  PROFILE="$1"
  echo "profile : $PROFILE (new machine)"
  echo "repo    : $ROOT"
  [ "$APPLY" -eq 1 ] || echo "mode    : DRY RUN — nothing will change; re-run with --apply"
  echo
  if [ "$APPLY" -eq 1 ]; then
    mkdir -p "$STATE_DIR"
    echo "$PROFILE" > "$STATE_FILE"
  fi
  do_stages "$2"
}

# Homebrew runs `sudo --reset-timestamp` at the start of every brew command
# (brew.sh), unless HOMEBREW_NO_SUDO is set. A read-only run's own `brew list`
# therefore erased the ticket the operator cached with `sudo -v`, and the
# Spotlight read, which runs later, deferred on "no sudo ticket". A run that
# changes nothing needs no brew sudo, so it keeps the ticket.
keep_sudo_ticket() {
  HOMEBREW_NO_SUDO=1
  export HOMEBREW_NO_SUDO
}

cmd_update() {
  load_profile
  [ "$APPLY" -eq 1 ] || keep_sudo_ticket
  echo "profile : $PROFILE (recorded in $STATE_FILE)"
  echo "repo    : $ROOT"
  [ "$APPLY" -eq 1 ] || echo "mode    : DRY RUN — nothing will change; re-run with --apply"
  echo
  prune_backups
  do_stages "$1"
}

# capture is the only verb that writes tracked files from machine state, so a
# stale checkout costs more here than anywhere else: the manifests it rewrites
# wholesale are exactly the ones another machine may have just changed. #141 and
# #142 were that accident twice — a session captured from a checkout older than
# the profiles it overwrote, and the hand-written notes in them went with it.
#
# The checkout is the one that holds the profile directory, when that is a git
# work tree; a profile directory under no version control has nothing to be
# behind. Fetches first, because the local upstream ref is as stale as the
# checkout that holds it. ConnectTimeout ends a fetch on a machine that cannot
# reach the remote early, and bound_step ends one that connects and stalls; when it fails, this says so and compares against the ref on
# disk rather than pretending the checkout is current.
profile_repo() {
  command -v git >/dev/null 2>&1 || return 0
  git -C "$PROFILE_DIR" rev-parse --show-toplevel 2>/dev/null || :
}

check_repo_current() {
  repo=$(profile_repo)
  [ -n "$repo" ] || return 0
  up=$(git -C "$repo" rev-parse --abbrev-ref '@{u}' 2>/dev/null || echo origin/main)
  if ! bound_step 120 env GIT_SSH_COMMAND="ssh -o ConnectTimeout=5" \
       git -C "$repo" fetch --quiet "${up%%/*}" 2>/dev/null; then
    echo "  --      could not reach ${up%%/*}; comparing against the ref on disk"
  fi
  git -C "$repo" rev-parse --verify --quiet "$up" >/dev/null 2>&1 || return 0
  behind=$(git -C "$repo" rev-list --count "HEAD..$up" 2>/dev/null || echo 0)
  [ "$behind" -gt 0 ] || return 0
  echo "  STALE   checkout is $behind commit(s) behind $up"
  git -C "$repo" log --oneline -10 "HEAD..$up" | sed 's/^/          /'
  if [ "$APPLY" -eq 1 ] && [ "$STALE_OK" -eq 0 ]; then
    echo
    echo "  refusing to write: capture rewrites the profile manifests, and those"
    echo "  commits may be what changed them."
    echo "  pull first:  git -C $repo pull --ff-only"
    echo "  or write anyway: machine-setup.sh capture --apply --stale-ok"
    exit 1
  fi
  echo "  (dry run — capture --apply would refuse until the checkout is current)"
}

# Capture regenerates a manifest from scratch, so hand-written prose in one is
# destroyed on the next run: #98 recorded in work.cron why four common cron jobs
# are absent from the work machine, and a later capture erased it. These two
# carry the leading comment block across. What survives is every comment line
# above the first entry, minus the generated header, which is reprinted. Notes
# further down, between entries, are not carried — once the entry list is
# regenerated there is nothing left to anchor them to. Durable notes that no
# machine can invalidate still belong in common.<kind>, which capture never
# writes.
carry_comments() { # existing-manifest, header-file
  [ -f "$1" ] || return 0
  awk '/^[[:space:]]*#/ || /^[[:space:]]*$/ { print; next } { exit }' "$1" \
    | grep -Fxv -f "$2" \
    | awk '/^[[:space:]]*$/ { blank = blank + 1; next }
           { while (blank-- > 0) print ""; blank = 0; print }'
}

# Reads the old manifest before truncating it, hence the temp file and the mv:
# a redirect onto the target would empty it before carry_comments could look.
# The entries a manifest declares: comments, blank lines and surrounding space
# removed, sorted. Same shape as common_manifest, but reads an arbitrary path
# rather than a kind under PROFILE_DIR.
manifest_entries() {
  [ -f "$1" ] || return 0
  sed -e 's/#.*//' -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' "$1" \
    | grep -v '^$' | sort
}

write_manifest() { # target, body-file, header-line...
  wm_target="$1"; wm_body="$2"; shift 2

  # A `!name` line is the operator's, not the machine's: capture keeps each
  # one first, and writes no entry it names back as wanted (sd:4853). A
  # formula still installed under a `!` line stays drift that status names;
  # the profile's own `<tap>/name` stays an entry.
  wm_neg=$(manifest_entries "$wm_target" | grep '^!' || :)
  if [ -n "$wm_neg" ]; then
    { printf '%s\n' "$wm_neg"
      drop_negated "$(printf '%s\n' "$wm_neg" | sed 's/^![[:space:]]*//')" exact < "$wm_body"
    } > "$wm_body.neg" || die "capture: cannot keep the ! lines of $wm_target; not written"
    wm_body="$wm_body.neg"
  fi

  # Additive mode refuses to write a manifest that lost an entry.
  #
  # Capture is a mirror: the roster is rebuilt from what is installed right
  # now, and write_manifest replaces the file wholesale. In the adding
  # direction that is a faithful log of a deliberate act -- you installed a
  # job, the roster records it. In the removing direction it is a silent
  # deletion from the declared configuration, and the causes are rarely
  # deliberate: a job uninstalled to debug something and not reinstalled, a
  # plist that failed to load after an OS upgrade, an agent unloaded by hand.
  # Rebuild the machine afterwards and the entry is simply not there. Absence
  # is the hardest thing to notice, and with an unattended commit there is no
  # diff anyone reads.
  #
  # So the two directions get different handling: additions write themselves,
  # removals stop and wait for a person. That is what makes an unattended
  # capture safe to commit -- not that it is careful, but that the direction
  # which can destroy something is the direction it will not go.
  if [ "$ADDITIVE" -eq 1 ] && [ -f "$wm_target" ]; then
    # Both sides through the same normaliser, not one raw and one cleaned.
    # `mas list` right-aligns its IDs, so a 9-digit row arrives with a leading
    # space that capture's `s/  */ /g` collapses to one rather than removing.
    # Comparing a stripped manifest against an unstripped body reported 12 of
    # 18 App Store entries as removals on a machine that had lost none, which
    # would have held a manifest that was perfectly fine, every night.
    wm_old="$wm_target.old.$$"; wm_new_e="$wm_target.new.e.$$"
    wm_gone="$wm_target.gone.$$"
    manifest_entries "$wm_target" > "$wm_old"
    manifest_entries "$wm_body" > "$wm_new_e"
    comm -23 "$wm_old" "$wm_new_e" > "$wm_gone"
    if [ -s "$wm_gone" ]; then
      echo "    HELD    $(basename "$wm_target") — $(grep -c . "$wm_gone") entry(s) would be removed:"
      sed 's/^/              - /' "$wm_gone"
      echo "              not written. Re-run without --additive to accept, or"
      echo "              reinstall what went missing."
      ADDITIVE_HELD=$((ADDITIVE_HELD + 1))
      rm -f "$wm_old" "$wm_new_e" "$wm_gone"
      return 0
    fi
    rm -f "$wm_old" "$wm_new_e" "$wm_gone"
  fi

  # A roster that had entries is never written empty (sd:3106): capture read
  # "manifest: 0 label(s)" on a machine whose agents were all installed, and
  # the next update would have removed every one. An empty roster is a hand edit.
  # The kept `!` lines are no entry here: a body of those alone is empty.
  if [ -z "$(manifest_entries "$wm_body" | grep -v '^!' || :)" ] \
      && [ -n "$(manifest_entries "$wm_target" | grep -v '^!' || :)" ]; then
    echo "    REFUSED $(basename "$wm_target") — capture found no entries where it lists $(manifest_entries "$wm_target" | grep -vc '^!'); not written"
    ADDITIVE_HELD=$((ADDITIVE_HELD + 1))
    return 0
  fi
  wm_hdr="$wm_target.hdr.$$"; wm_new="$wm_target.new.$$"
  for wm_line in "$@"; do printf '%s\n' "$wm_line"; done > "$wm_hdr"
  {
    cat "$wm_hdr"
    carry_comments "$wm_target" "$wm_hdr"
    # no trailing blank on a manifest that has no entries at all
    if [ -s "$wm_body" ]; then echo; cat "$wm_body"; fi
  } > "$wm_new"
  rm -f "$wm_hdr"
  # Unchanged is not rewritten, so a nightly capture keeps no copy of it.
  if [ -f "$wm_target" ] && cmp -s "$wm_new" "$wm_target"; then
    rm -f "$wm_new"
    return 0
  fi
  capture_backup "$wm_target"
  mv "$wm_new" "$wm_target"
}

# Writes the machine's current state back into the profile manifests. This is
# the reverse direction: the machine is the source of truth, the repo catches up.
cmd_capture() {
  load_profile
  echo "profile : $PROFILE"
  echo "writing : $PROFILE_DIR/$PROFILE.*"
  [ "$APPLY" -eq 1 ] || echo "mode    : DRY RUN — nothing will be written; re-run with --apply"
  echo
  check_repo_current

  tmp=$(mktemp -d)
  trap_tmp

  # A profile file holds only what common does not already provide, so capture
  # subtracts the common manifest instead of dumping the whole machine.
  # Not piped: a pipeline's status is sort's, and a list brew could not give
  # would read as nothing installed (sd:3536). No brew lists nothing.
  # --installed-on-request: a build-only dependency is a leaf too, and no
  # profile should name it (sd:5049).
  : > "$tmp/raw.tap"; : > "$tmp/raw.brew"; : > "$tmp/raw.cask"
  if command -v brew >/dev/null 2>&1 \
      && { ! bound_step 300 brew tap > "$tmp/raw.tap" 2>/dev/null \
        || ! bound_step 300 brew leaves --installed-on-request > "$tmp/raw.brew" 2>/dev/null \
        || ! bound_step 300 brew list --cask > "$tmp/raw.cask" 2>/dev/null; }; then
    echo "capture: brew could not list what is installed; nothing captured" >&2
    exit 1
  fi
  sort "$tmp/raw.tap" > "$tmp/have.tap"
  sort -u "$tmp/raw.brew" > "$tmp/have.brew"
  common_manifest tap  > "$tmp/common.tap"
  common_manifest brew | sed 's|.*/||' | sort -u > "$tmp/common.brew"
  comm -23 "$tmp/have.tap"  "$tmp/common.tap"  > "$tmp/out.tap"
  # Compare bare names, but write the spelling brew printed: `leaves`
  # tap-qualifies a third-party formula, and a bare name in the profile could
  # install a core formula of the same name instead. FILENAME, not NR == FNR:
  # an empty common manifest would make every machine line look like common.
  awk 'FILENAME == ARGV[1] { common[$0] = 1; next }
       { n = $0; sub(/.*\//, "", n); if (!(n in common)) print }' \
    "$tmp/common.brew" "$tmp/have.brew" > "$tmp/out.brew"
  sort "$tmp/raw.cask" > "$tmp/have.cask"
  common_manifest cask > "$tmp/common.cask"
  comm -23 "$tmp/have.cask" "$tmp/common.cask" > "$tmp/out.cask"
  # Only agents local-cron-jobs installed: another installer's agent under the
  # same prefix has no job file, so a profile naming it could never install it.
  for p in "$HOME/Library/LaunchAgents/$LABEL_PREFIX.cron."*.plist; do
    [ -e "$p" ] || continue
    n=$(basename "$p" .plist)
    n=${n#"$LABEL_PREFIX.cron."}
    cron_plist_ours "$p" "$n" 2>/dev/null || continue
    printf '%s\n' "$n"
  done | sort > "$tmp/have.cron"
  # This host's own jobs stay out of the profile too: the cron stage installs
  # them from their folder, and another machine sharing the profile has no
  # such job file to install.
  if ! host_cron_jobs > "$tmp/host.cron"; then
    echo "capture: cannot list the host cron jobs in cron-jobs/jobs/<host>/; nothing captured" >&2
    exit 1
  fi
  # Not piped: a pipeline's status is sort's, which would read a failure as none.
  if ! defined_cron_jobs > "$tmp/defined.raw"; then
    echo "capture: cannot list the cron-jobs job files; nothing captured" >&2
    exit 1
  fi
  sort "$tmp/defined.raw" > "$tmp/defined.cron"
  comm -23 "$tmp/have.cron" "$tmp/defined.cron" | while read -r n; do
    echo "  SKIPPED $n — installed, but no job file defines it; not adopted (cron-jobs.sh uninstall $n removes it)"
  done
  comm -12 "$tmp/have.cron" "$tmp/defined.cron" > "$tmp/have.cron.defined"
  mv "$tmp/have.cron.defined" "$tmp/have.cron"
  { common_manifest cron; cat "$tmp/host.cron"; } | sort -u > "$tmp/common.cron"
  comm -23 "$tmp/have.cron" "$tmp/common.cron" > "$tmp/out.cron"

  if command -v mas >/dev/null 2>&1; then
    # Drop the trailing "(version)" column: it changes on every app update and
    # would make capture produce noise diffs.
    # Plain `sort`, not `sort -n`, because the next line subtracts
    # `common_manifest`, which sorts lexicographically -- and `comm` gives
    # silently wrong answers when its two inputs disagree on ordering. The two
    # orders diverge exactly when IDs differ in length: lexicographically
    # "1289583905" precedes "302584613", numerically it follows. Every other
    # kind here already sorts this way; mas was the only exception.
    # Id 0 is a beta or TestFlight build: `mas install 0` installs nothing, so
    # it has no place in a profile. A profile entry with the same name stays,
    # though: update counts it as installed, and a rebuild needs its store id.
    if ! bound_step 300 mas list > "$tmp/raw.mas" 2>/dev/null; then
      echo "capture: mas list did not answer; nothing captured" >&2
      exit 1
    fi
    sed -e 's/ *([^)]*)$//' -e 's/  */ /g' -e 's/^ *//' "$tmp/raw.mas" > "$tmp/all.mas"
    { grep -v '^0 ' "$tmp/all.mas" || :; } > "$tmp/have.pre"
    if [ -f "$PROFILE_DIR/$PROFILE.mas" ]; then
      sed -e 's/#.*//' -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e 's/  */ /g' \
        "$PROFILE_DIR/$PROFILE.mas" \
        | awk 'NR == FNR { if (sub(/^0 /, "")) beta[$0] = 1; next }
               NF { n = $0; sub(/^[0-9]+ /, "", n); if (n in beta) print }' \
          "$tmp/all.mas" - >> "$tmp/have.pre"
    fi
    sort -u "$tmp/have.pre" > "$tmp/have.mas"
    common_manifest mas > "$tmp/common.mas"
    comm -23 "$tmp/have.mas" "$tmp/common.mas" > "$tmp/out.mas"
  else
    : > "$tmp/out.mas"
  fi

  for kind in tap brew cask cron mas; do
    n=$(wc -l < "$tmp/out.$kind" | tr -d ' ')
    echo "  $kind: $n entries"
    if [ "$APPLY" -eq 1 ]; then
      write_manifest "$PROFILE_DIR/$PROFILE.$kind" "$tmp/out.$kind" \
        "# $PROFILE machine $kind. Captured by machine-setup.sh capture."
    fi
  done

  # Standalone app names, for cross-machine tracking. Same classification as
  # cmd_apps but without the per-app cask search (that is a report, not
  # state): through cmd_apps it ran one brew search per app, nightly under
  # --additive (sd:3548). A subshell, as the scan sets its own $tmp.
  app_ok=1
  ( standalone_scan "$tmp/scan.app" names ) 2>/dev/null || app_ok=0
  if [ "$app_ok" -eq 1 ]; then
    sort "$tmp/scan.app" > "$tmp/out.app"
    n=$(wc -l < "$tmp/out.app" | tr -d ' ')
    echo "  app: $n entries"
  else
    echo "  app: UNKNOWN — the standalone app scan failed; $PROFILE.app not written"
  fi
  if [ "$APPLY" -eq 1 ] && [ "$app_ok" -eq 1 ]; then
    write_manifest "$PROFILE_DIR/$PROFILE.app" "$tmp/out.app" \
      "# $PROFILE machine standalone apps (no App Store receipt, not brew-managed)." \
      "# Informational: nothing installs these; see machine-setup.sh apps."
  fi

  echo
  capture_agents "$tmp"

  # Additive mode captures rosters only. The remaining stages copy file
  # *content* -- dotfiles, macOS defaults, iTerm2 preferences, agent plists --
  # where "additive" has no meaning: a changed line is neither an addition nor
  # a removal, and there is no safe direction to automate. Those stay a
  # deliberate `capture --apply`, read by a person.
  if [ "$ADDITIVE" -eq 0 ]; then
    echo
    capture_macos
    echo
    capture_spotlight "$tmp"
    echo
    capture_iterm2 "$tmp"
    echo
    capture_dotfiles "$tmp"
  fi
  echo
  echo "  services are not auto-captured: which local-* services a machine should"
  echo "  run is a decision, not an observation. Edit $PROFILE.service by hand."
  [ "$APPLY" -eq 1 ] && echo && review_hint
  if [ "$ADDITIVE_HELD" -gt 0 ]; then
    echo
    echo "held    : $ADDITIVE_HELD manifest(s) not written — entries would have been"
    echo "          removed. A removal is a person's call, so this exits non-zero:"
    echo "          it is rare, which is what makes it worth a notification."
    return 3
  fi
  return 0
}

# Where to read what capture or adopt just wrote.
review_hint() {
  rh_repo=$(profile_repo)
  if [ -n "$rh_repo" ]; then
    echo "  review with: git -C $rh_repo diff"
  else
    echo "  review what changed under $CONFIG_DIR (not under version control)"
  fi
}

# Common's entries this profile keeps: capture subtracts these, so an entry a
# `!` line drops is the machine's to record again (sd:4853).
common_manifest() {
  cm_neg=$(profile_drops "$PROFILE" "$1")
  profile_entries common "$1" | sort | drop_negated "$cm_neg"
}

# A dotfile is only copied in after it passes a credential scan, so a key
# pasted into .zshrc cannot reach the repo by way of capture.
capture_dotfiles() {
  tmp="$1"
  echo "  dotfiles:"
  for f in $DOTFILES; do
    src="$HOME/$f"
    if [ ! -f "$src" ]; then
      echo "    --      $f absent on this machine"
      continue
    fi
    # A value of ${OTHER_VAR} is an indirection, not a credential — the tool
    # config files deliberately reference ~/.bash_profile instead of holding a
    # second copy, and refusing them would mean they could never be captured.
    hits=$(grep -cIE '(API_KEY|_TOKEN|SECRET|PASSWORD|AKIA|ghp_|github_pat_|sk-[A-Za-z0-9]{16,})[A-Za-z_]*[[:space:]]*=[[:space:]]*["'"'"']?[^$"'"'"'[:space:]]' "$src" 2>/dev/null || true)
    hits=${hits:-0}
    # Keyword scan misses a bare high-entropy value under an innocuous key
    # (that is how a 32-hex token once reached the repo via .gitconfig), so
    # also refuse long hex/base64-shaped assignment values.
    ent=$(grep -cIE '=[[:space:]]*["'"'"']?([0-9a-fA-F]{32,}|[A-Za-z0-9+/]{40,}={0,2})["'"'"']?[[:space:]]*$' "$src" 2>/dev/null || true)
    ent=${ent:-0}
    # Both patterns above are shell-assignment shaped. A JSON or YAML dotfile
    # writes "github_personal_access_token": "ghp_..." with a colon, which
    # slipped past both — so also match colon-shaped secret keys and any
    # recognizable provider token prefix anywhere on the line.
    js=$(grep -cIE '"?[A-Za-z_]*(api_?key|token|secret|password|credential)[A-Za-z_]*"?[[:space:]]*:' "$src" 2>/dev/null || true)
    js=${js:-0}
    pfx=$(grep -cIE '(gh[pousr]_|github_pat_|xox[baprs]-|sk-[A-Za-z0-9]|AKIA|AIza|glpat-)[A-Za-z0-9_-]{16,}' "$src" 2>/dev/null || true)
    pfx=${pfx:-0}
    hits=$((hits + ent + js + pfx))
    if [ "$hits" -gt 0 ]; then
      echo "    REFUSED $f — $hits credential-shaped assignment(s); not captured"
      continue
    fi
    # Captured without the lines a login writes; see dotfile_session_re.
    src=$(dotfile_view "$f" "$src" "$tmp/dotfile-view")
    prof="$DOTFILE_DIR/$PROFILE/$f"
    common="$DOTFILE_DIR/common/$f"
    if [ -f "$common" ] && cmp -s "$src" "$common"; then
      # common/ already carries this exact file. Writing a profile copy too
      # would be a duplicate that goes stale the next time common changes.
      if [ -f "$prof" ]; then
        if [ "$APPLY" -eq 1 ]; then
          capture_backup "$prof"
          rm -f "$prof"
        fi
        echo "    drop    $f — now identical to common/, profile copy removed"
      else
        echo "    ok      $f unchanged (common)"
      fi
    elif [ -f "$prof" ] && cmp -s "$src" "$prof"; then
      echo "    ok      $f unchanged"
    else
      if [ "$APPLY" -eq 1 ]; then
        mkdir -p "$(dirname "$prof")"
        capture_backup "$prof"
        cp "$src" "$prof"
      fi
      echo "    capture $f"
    fi
  done
  for f in $DOTFILES_DENY; do
    echo "    skip    $f (holds live credentials — never tracked)"
  done
}

# The LaunchAgent plists this repo owns, in one place because capture_agents
# and candidates_agent must agree on the answer: <prefix>.*, plus the label
# globs MACHINE_SETUP_AGENT_GLOBS names (space-separated, e.g. `org.example.*`)
# for agents some other installer writes under its own prefix. A label that
# once carried the account name needed a glob wider than one prefix, and the
# narrower one reported 0 on a machine with two such agents running.
# A label the profile's .agent already names is owned too, while its plist
# is installed: a capture run without the glob wrote the work roster empty
# (sd:3106).
# <prefix>.cron.* is excluded throughout: local-cron-jobs owns those and they
# are reported under `cron`.
owned_agent_plists() {
  # shellcheck disable=SC2086 # the globs expand here, on purpose
  for oap in "$HOME/Library/LaunchAgents/$LABEL_PREFIX".*.plist \
             $(for oag in ${MACHINE_SETUP_AGENT_GLOBS:-}; do printf '%s ' "$HOME/Library/LaunchAgents/$oag.plist"; done) \
             $(manifest agent | while read -r oal; do printf '%s ' "$HOME/Library/LaunchAgents/$oal.plist"; done); do
    [ -e "$oap" ] || continue
    case "$(basename "$oap" .plist)" in "$LABEL_PREFIX.cron."*) continue ;; esac
    cron_jobs_plist "$oap" 2>/dev/null && continue
    echo "$oap"
  done | sort -u
}

# Does this plist run a local-cron-jobs job, whatever its label? A glob wider
# than the prefix can reach one, and it belongs under `cron`, not the agent
# roster (sd:3106). The ProgramArguments shape is cron_plist_ours' mark.
cron_jobs_plist() { # plist path
  python3 - "$1" <<'PY'
import plistlib, sys
try:
    with open(sys.argv[1], "rb") as f:
        args = plistlib.load(f).get("ProgramArguments")
except Exception:
    sys.exit(1)
sys.exit(0 if isinstance(args, list) and len(args) == 4 and args[0] == "/bin/bash"
         and isinstance(args[1], str) and args[1].endswith("/local-cron-jobs/cron-jobs.sh")
         and args[2] == "exec" else 1)
PY
}

# Personal daemons captured into launchagents/. Same credential scan idea as
# dotfiles: a plist can carry secrets in EnvironmentVariables, so a
# credential-shaped value refuses the capture and keeps the label out of the
# manifest.
capture_agents() {
  tmp="$1"
  echo "  agents:"
  : > "$tmp/have.agent"
  # `launchctl disable` turns an agent off and leaves its plist installed; the
  # roster must not turn it back on (sd:3183). A failed read says nothing about
  # any label, so those agents are captured as before, with a line saying so.
  if ! disabled=$(launchctl print-disabled "gui/$(id -u)" 2>/dev/null); then
    disabled=
    echo "    UNKNOWN launchctl could not list the disabled agents; disabled ones are captured as installed"
  fi
  for pl in $(owned_agent_plists); do
    label=$(basename "$pl" .plist)
    if printf '%s\n' "$disabled" | grep -qF -e "\"$label\" => disabled" -e "\"$label\" => true"; then
      echo "    SKIPPED $label — launchctl reports it disabled; not captured"
      continue
    fi
    hits=$(grep -cIE '(API_KEY|_TOKEN|SECRET|PASSWORD|AKIA|ghp_|github_pat_|sk-[A-Za-z0-9]{16,})' "$pl" 2>/dev/null || true)
    ent=$(grep -cIE '>([0-9a-fA-F]{32,}|[A-Za-z0-9+/]{40,}={0,2})<' "$pl" 2>/dev/null || true)
    hits=$(( ${hits:-0} + ${ent:-0} ))
    if [ "$hits" -gt 0 ]; then
      echo "    REFUSED $label — $hits credential-shaped value(s); not captured"
      continue
    fi
    echo "$label" >> "$tmp/have.agent"
    # Against the tracked plist as the agents stage installs it, so a
    # placeholder file that renders to this plist is not a change.
    if [ -f "$AGENT_DIR/$label.plist" ]; then
      agent_render "$label" "$AGENT_DIR/$label.plist" "$tmp/rendered.plist"
    else
      rm -f "$tmp/rendered.plist"
    fi
    if [ -f "$tmp/rendered.plist" ] && cmp -s "$pl" "$tmp/rendered.plist"; then
      echo "    ok      $label unchanged"
    else
      # Additive mode records the label in the roster but does not copy the
      # plist: the file is content, and a changed plist is neither an addition
      # nor a removal. Copying it unattended is how a hand-edited agent, or one
      # rewritten by an installer, becomes the declared version with nobody
      # reading the diff.
      if [ "$APPLY" -eq 1 ] && [ "$ADDITIVE" -eq 0 ]; then
        mkdir -p "$AGENT_DIR"
        capture_backup "$AGENT_DIR/$label.plist"
        cp "$pl" "$AGENT_DIR/$label.plist"
        echo "    capture $label"
      elif [ "$ADDITIVE" -eq 1 ]; then
        echo "    roster  $label (plist not copied: --additive)"
      else
        echo "    capture $label"
      fi
    fi
  done
  sort -o "$tmp/have.agent" "$tmp/have.agent"
  common_manifest agent > "$tmp/common.agent"
  comm -23 "$tmp/have.agent" "$tmp/common.agent" > "$tmp/out.agent"
  n=$(wc -l < "$tmp/out.agent" | tr -d ' ')
  echo "    manifest: $n label(s)"
  if [ "$APPLY" -eq 1 ]; then
    write_manifest "$PROFILE_DIR/$PROFILE.agent" "$tmp/out.agent" \
      "# $PROFILE machine launch agents. Captured by machine-setup.sh capture." \
      "# Plists live in launchagents/; $LABEL_PREFIX.cron.* belongs to local-cron-jobs."
  fi
}

# Rewrites the values in <profile>.macos from the machine. The key LIST is
# curated by hand (see stage_macos comment); capture only refreshes values.
capture_macos() {
  f="$PROFILE_DIR/$PROFILE.macos"
  echo "  macos:"
  if [ ! -f "$f" ]; then
    echo "    --      no $PROFILE.macos manifest; add keys by hand or with triage"
    return 0
  fi
  out=""
  n=0
  while IFS= read -r raw; do
    line=$(echo "$raw" | sed -e 's/#.*//' -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')
    if [ -z "$line" ]; then out="$out$raw
"; continue; fi
    dom=$(echo "$line" | awk '{print $1}')
    key=$(echo "$line" | awk '{print $2}')
    typ=$(echo "$line" | awk '{print $3}')
    # `|| :`: an unset key exits 1, and a bare assignment under set -e
    # would end the whole capture there (sd:1432).
    cur=$(defaults read "$dom" "$key" 2>/dev/null || :)
    if [ -z "$cur" ]; then
      out="$out$raw
"
      echo "    keep    $dom $key (unset on this machine)"
    else
      if [ "$typ" = "bool" ]; then
        case "$cur" in 1) cur=true ;; 0) cur=false ;; esac
      fi
      out="$out$dom $key $typ $cur
"
      n=$((n + 1))
    fi
  done < "$f"
  echo "    refreshed $n value(s)"
  if [ "$APPLY" -eq 1 ] && ! printf '%s' "$out" | cmp -s - "$f"; then
    capture_backup "$f"
    printf '%s' "$out" > "$f"
  fi
}

# Absolute paths on stdin, with $HOME written as `~`: the form
# <profile>.spotlight is kept in.
spotlight_abbrev() {
  awk -v h="$HOME" '
    $0 == h { print "~"; next }
    index($0, h "/") == 1 { print "~" substr($0, length(h) + 1); next }
    { print }
  '
}

# Writes the live Spotlight privacy list into <profile>.spotlight, the way
# capture_macos refreshes <profile>.macos. Paths under $HOME are written back
# as `~/...` so the file reads the same on an account with another name, and
# entries common.spotlight already holds are left out. Reading needs sudo and
# Full Disk Access; without them this says why and writes nothing.
capture_spotlight() { # tmpdir
  cs_tmp="$1"
  cs_f="$PROFILE_DIR/$PROFILE.spotlight"
  echo "  spotlight:"
  if ! spotlight_read "$cs_tmp/have.spotlight"; then
    echo "    --      not read: $SPOTLIGHT_WHY; $SPOTLIGHT_FIX"
    return 0
  fi
  spotlight_abbrev < "$cs_tmp/have.spotlight" | sort -u > "$cs_tmp/live.spotlight"
  # Both sides in one form, or `~/repos/` in common misses the live path.
  common_manifest spotlight | spotlight_expand | spotlight_abbrev | sort -u \
    > "$cs_tmp/common.spotlight"
  comm -23 "$cs_tmp/live.spotlight" "$cs_tmp/common.spotlight" > "$cs_tmp/out.spotlight"
  n=$(grep -c . "$cs_tmp/out.spotlight" || :)
  echo "    $n exclusion(s)"
  # No list and no file: a header-only manifest would say nothing.
  if [ "$n" -eq 0 ] && [ ! -f "$cs_f" ]; then
    return 0
  fi
  if [ "$APPLY" -eq 1 ]; then
    write_manifest "$cs_f" "$cs_tmp/out.spotlight" \
      "# $PROFILE machine Spotlight privacy exclusions. Captured by machine-setup.sh capture." \
      "# One absolute path per line; a leading ~ is this account's home. The system" \
      "# stage adds a missing path and restarts mds. It never removes one."
  fi
}

# Side-by-side profile comparison. Reads raw profile files (not layered with
# common) so it answers "what does A have that B lacks", kind by kind.
# The prefs folder is an observation, not a decision: the Drive mount path
# differs per machine, so capture refreshes it like any other local value.
capture_iterm2() { # tmpdir
  it_tmp="$1"
  echo "  iterm2:"
  dir=$(defaults read com.googlecode.iterm2 PrefsCustomFolder 2>/dev/null || :)
  on=$(defaults read com.googlecode.iterm2 LoadPrefsFromCustomFolder 2>/dev/null || :)
  if [ -z "$dir" ] || [ "$on" != "1" ]; then
    echo "    --      iTerm2 is not loading prefs from a custom folder; nothing to capture"
    return 0
  fi
  if [ "$dir" = "$(manifest iterm2 | head -1)" ]; then
    echo "    ok      prefs folder unchanged"
    return 0
  fi
  echo "    capture prefs folder $dir"
  if [ "$APPLY" -eq 1 ]; then
    printf '%s\n' "$dir" > "$it_tmp/out.iterm2"
    write_manifest "$PROFILE_DIR/$PROFILE.iterm2" "$it_tmp/out.iterm2" \
      "# $PROFILE machine iTerm2 prefs folder. Captured by machine-setup.sh capture." \
      "# One path: the folder iTerm2 loads and saves com.googlecode.iterm2.plist from."
  fi
}

cmd_compare() {
  pa="$1"; pb="$2"
  valid_profile "$pa"; valid_profile "$pb"
  [ "$pa" = "$pb" ] && die "compare needs two different profiles"

  tmp=$(mktemp -d)
  trap_tmp

  echo "comparing: $pa vs $pb (profile files only; common applies to both)"
  echo
  for kind in $KINDS; do
    for pr in "$pa" "$pb"; do
      profile_entries "$pr" "$kind" | sort -u > "$tmp/$pr.$kind"
    done
    both=$(comm -12 "$tmp/$pa.$kind" "$tmp/$pb.$kind" | wc -l | tr -d ' ')
    onlya=$(comm -23 "$tmp/$pa.$kind" "$tmp/$pb.$kind")
    onlyb=$(comm -13 "$tmp/$pa.$kind" "$tmp/$pb.$kind")
    na=$(printf '%s' "$onlya" | grep -c . || true)
    nb=$(printf '%s' "$onlyb" | grep -c . || true)

    echo "== $kind (match: $both, only $pa: $na, only $pb: $nb)"
    [ "$both" -gt 0 ] && comm -12 "$tmp/$pa.$kind" "$tmp/$pb.$kind" | sed 's/^/  =       /'
    [ -n "$onlya" ] && printf '%s\n' "$onlya" | sed "s/^/  <$pa    /"
    [ -n "$onlyb" ] && printf '%s\n' "$onlyb" | sed "s/^/  >$pb    /"
    echo
  done
}

# Interactive triage: walk items that are on this machine but in NO profile
# (and not in common), and file each into a profile — or common, or skip.
# This is the "choose what goes where" step after a capture-less install spree.
# The profile an answer names: the full name, or a first letter that only one
# profile starts with. Anything else is empty, and triage skips the item.
triage_target() {
  if known_profiles | grep -qxF "$1"; then
    echo "$1"
    return 0
  fi
  case "$1" in ?) ;; *) return 0 ;; esac
  tt=$(known_profiles | grep "^$1" || :)
  [ "$(printf '%s\n' "$tt" | grep -c .)" -eq 1 ] && echo "$tt"
  return 0
}

cmd_triage() {
  only_kind="$1"
  case "$only_kind" in
    ""|brew|cask|mas|service|cron) ;;
    agent) die "triage: agent is filled by capture, which rewrites <profile>.agent wholesale — a hand-filed label would be overwritten on the next run. See: $(basename "$0") candidates agent" ;;
    *) die "triage: unknown kind '$only_kind' (brew, cask, mas, service, cron)" ;;
  esac
  # Interactive from a terminal, but answers may also be piped on stdin
  # (`printf 'p\ns\n' | machine-setup.sh triage brew --apply`), which is what
  # makes this testable.
  load_profile
  echo "triage : filing unassigned items (machine has them, no manifest does)"
  triage_keys=$(known_profiles | tr '\n' ' ')
  echo "keys   : c=common, a profile name or its unique first letter ($triage_keys), s=skip q=quit"
  echo

  tmp=$(mktemp -d)
  trap_tmp

  for kind in brew cask mas service cron; do
    if [ -n "$only_kind" ] && [ "$only_kind" != "$kind" ]; then continue; fi
    case "$kind" in
      brew) command -v brew >/dev/null 2>&1 || continue
            bound_step 300 brew leaves --installed-on-request 2>/dev/null | sed 's|.*/||' | sort > "$tmp/have" ;;
      cask) command -v brew >/dev/null 2>&1 || continue
            bound_step 300 brew list --cask 2>/dev/null | sort > "$tmp/have" ;;
      mas)  command -v mas >/dev/null 2>&1 || continue
            bound_step 300 mas list 2>/dev/null | sed -e 's/ *([^)]*)$//' -e 's/  */ /g' -e 's/^ *//' | sort > "$tmp/have" ;;
      # Neither of these comes from a package manager: services are the
      # local-* folders that can start a container, cron jobs are whatever
      # local-cron-jobs defines. Same discovery `candidates` reports from.
      service) discover_services | sort > "$tmp/have" ;;
      cron)    discover_cron_jobs | sort > "$tmp/have" ;;
    esac

    # Anything any profile (or common) already lists — compared on the same
    # normalized form as `have` — is assigned; the rest need a decision.
    for pr in common $(known_profiles); do
      profile_entries "$pr" "$kind"
    done | sed 's|.*/||' | sort -u > "$tmp/assigned"

    if [ "$kind" = "mas" ]; then
      # mas lines are "<id> <name>"; match on id.
      awk '{print $1}' "$tmp/assigned" | sort -u > "$tmp/assigned.ids"
      awk '{print $1}' "$tmp/have" > "$tmp/have.ids"
      unassigned=$(grep -vxF -f "$tmp/assigned.ids" "$tmp/have.ids" 2>/dev/null | while read -r id; do grep "^$id " "$tmp/have"; done)
    else
      unassigned=$(comm -23 "$tmp/have" "$tmp/assigned")
    fi
    [ -z "$unassigned" ] && { echo "== $kind: nothing unassigned"; continue; }

    echo "== $kind ($(printf '%s\n' "$unassigned" | grep -c .) unassigned)"
    # Items ride fd3 so stdin stays free for the answers; a while|pipe would
    # also have eaten the answers as items.
    printf '%s\n' "$unassigned" > "$tmp/items"
    while IFS= read -r item <&3; do
      # A service is a port decision as much as a name, so show them.
      if [ "$kind" = "service" ]; then
        printf '  %-30s %-16s [c/<profile>/s/q] ' "$item" "$(service_ports "$item")"
      else
        printf '  %-46s [c/<profile>/s/q] ' "$item"
      fi
      if ! read -r ans; then echo "(no more answers — stopping)"; return 0; fi
      case "$ans" in
        c|common) triage_file common "$kind" "$item"; triage_warn "$kind" "$item" ;;
        q) echo "quit"; return 0 ;;
        s|'') echo "skipped" ;;
        *)
          target=$(triage_target "$ans")
          if [ -n "$target" ]; then
            triage_file "$target" "$kind" "$item"; triage_warn "$kind" "$item"
          else
            echo "skipped"
          fi ;;
      esac
    done 3< "$tmp/items"
    echo
  done
}

# Filing a service whose port a profile-mate already claims produces a config
# that cannot start. Warn rather than refuse: the answer may be to remove the
# other one, and triage is not the place to decide that.
triage_warn() { # kind item
  [ "$1" = "service" ] || return 0
  service_clash_with_manifest "$2" | while IFS= read -r tw; do
    [ -n "$tw" ] || continue
    echo "        CLASH   port ${tw%%:*} is also wanted by ${tw#*:} in this profile"
  done
}

triage_file() {
  pr="$1"; kind="$2"; item="$3"
  f="$PROFILE_DIR/$pr.$kind"
  if [ "$APPLY" -eq 1 ]; then
    printf '%s\n' "$item" >> "$f"
    echo "        -> $pr.$kind"
  else
    echo "        [dry-run] would append to $pr.$kind"
  fi
}

# ------------------------------------------------------------ candidates ----

# What a machine COULD be told to run, as opposed to what it already has.
# `triage` answers that for brew, cask and mas by diffing installed packages
# against the manifests; service, cron and agent have no package manager to
# ask, so they are discovered from the filesystem, from local-cron-jobs and
# from launchd instead. Read-only in every kind — filing is a decision.

# Is this local-* folder a startable docker service? Decided from the file,
# not from a list kept here: an entrypoint with a `start)` arm that mentions
# docker. A list would go stale the first time a folder is added.
#
# Comments are stripped before the docker test, and that is not a nicety:
# one wrapper ran `brew services start nginx` and kept a `docker run -p
# 8083:80` line commented underneath. Reading comments called it a docker
# service on a port it does not publish, and invented a conflict with
# google_workspace_mcp that does not exist.
uncommented() { sed 's/#.*//' "$1"; }

is_docker_service() {
  cand_entry="$ROOT/$1/${1#local-}.sh"
  [ -x "$cand_entry" ] || return 1
  uncommented "$cand_entry" | grep -qE '^[[:space:]]*start\)' || return 1
  uncommented "$cand_entry" | grep -q docker
}

# The default of every overridable port variable: `PORT="${QDRANT_PORT:-6337}"`
# and compose's `"${GRAPHITI_UI_PORT:-3004}:3000"` both hide the literal inside
# the ${...:-N} default, so a `-p` scan alone now reports nothing for the
# services that gained an override.
port_defaults() { # reads stdin
  grep -oE '\$\{[A-Za-z_][A-Za-z0-9_]*PORT[A-Za-z0-9_]*:-[0-9]+\}' 2>/dev/null |
    sed -E 's/^\$\{([A-Za-z0-9_]+):-([0-9]+)\}$/\1 \2/' |
    while read -r cand_var cand_def; do
      # An exported override wins here exactly as it would when the script
      # runs, so BUSY reports the port the container would really try to bind
      # rather than the committed default. The variable name comes out of a
      # [A-Za-z0-9_]+ capture, so the eval has nothing to inject.
      eval "cand_val=\${$cand_var:-}"
      if [ -n "$cand_val" ]; then echo "$cand_val"; else echo "$cand_def"; fi
    done || :
}

# Host ports a service folder publishes: overridable defaults plus whatever is
# still written literally on a `docker run -p` flag or in a compose ports list.
# Read out of the files on every run for the same reason the folder list is —
# the port table in README.md is documentation, not a source.
service_ports() {
  cand_svc="$1"
  cand_entry="$ROOT/$cand_svc/${cand_svc#local-}.sh"
  {
    if [ -f "$cand_entry" ]; then
      uncommented "$cand_entry" | port_defaults
      # "-p 6379:6379" and "-p 127.0.0.1:4317:4317" both put the HOST port
      # second-to-last, which is what $(NF-1) picks out.
      uncommented "$cand_entry" | grep -oE -- '-p[[:space:]]+[0-9.:]+' 2>/dev/null |
        sed -E 's/^-p[[:space:]]+//' | awk -F: 'NF>=2 {print $(NF-1)}' || :
    fi
    for cand_yml in "$ROOT/$cand_svc"/docker-compose.y*ml "$ROOT/$cand_svc"/compose.y*ml; do
      [ -f "$cand_yml" ] || continue
      port_defaults < "$cand_yml"
      grep -oE '^[[:space:]]*-[[:space:]]*"?[0-9]+:[0-9]+' "$cand_yml" 2>/dev/null |
        grep -oE '[0-9]+:' | tr -d ':' || :
    done
  } | grep -E '^[0-9]+$' | sort -un | tr '\n' ' '
}

# Who is listening on a host port right now, as "<command> (pid N)" — empty
# when the port is free. CLASH is theory (two candidates want the same port);
# this is fact (something already has it, service or not, and the container
# will fail to bind). The blocker is often nothing in this repo at all:
# OpenWhispr ships its own qdrant, and other projects' compose files run
# postgres.
port_holder() {
  port_observation "$1" | sed -n 's/^LISTEN //p'
}

# Every TCP listener this user can see, from one lsof, reduced to lines a
# caller can look any number of ports up in:
#   LISTEN <port> <command> (pid N)   the first holder lsof lists for the port
#   HIDDEN <port>                     netstat lists a listener lsof did not name
#   ABSENT CLEAR|UNKNOWN              what a port with no other line means
# Preserve uncertainty: an unavailable lsof is not evidence that a port is free,
# so ABSENT is CLEAR only when lsof ran cleanly — exit 0 with rows it could
# read, or exit 1 with no output — and netstat's table was read, and UNKNOWN
# on anything else.
#
# Only stdout is parsed. Merging stderr in (2>&1) put this machine's
# "lsof: WARNING: can't stat() apfs file system …" line, from a Carbon Copy
# Cloner snapshot mount, where the header belongs, and turned every answer
# into UNKNOWN (sd:756). -w silences lsof's warnings, which are about naming
# files on a mount, not about the socket table; whatever still reaches stderr
# under -w is an error. An error keeps the listeners lsof did show, and clears
# no port.
#
# lsof run as this user does not show another user's sockets, and says
# nothing about leaving them out: launchd's 22 and 445 and tailscaled's 443
# and 8443 are root's, and lsof exits 1 for them as for a free port. netstat
# lists every socket without owners, in about 6 ms, so a LISTEN port there
# that lsof did not name is HIDDEN — occupied, holder unknown — never CLEAR.
# A netstat that is missing, fails, or prints a table other than macOS's
# (Linux's net-tools reads -p as "show PIDs" and prints a different header)
# clears no port.
#
# One scan, not one lsof per port. A query for one port costs what the full
# scan costs, 0.36-0.48 s on this machine, and a 27-port table spent 10-34 s
# running them (sd:756). A port looked up in the full list names the holder a
# per-port query names, because lsof lists rows in PID order either way. The
# one difference is a row this cannot read: it clears no port in the table,
# where a per-port query would have lost only the port on that row.
tcp_listeners() {
  # netstat's table comes first, closed by a "netstat-exit <code>" line.
  # lsof's stdout then goes straight to awk on fd 3; the substitution holds
  # stderr alone.
  { if cand_net=$(netstat -an -p tcp 2>/dev/null); then
      cand_net_code=0
    else
      cand_net_code=$?
    fi
    printf '%s\nnetstat-exit %s\n' "$cand_net" "$cand_net_code"
    if cand_scan_err=$(lsof -w -nP -iTCP -sTCP:LISTEN 2>&1 >&3 3>&-); then
      cand_scan_code=0
    else
      cand_scan_code=$?
    fi
    printf '%s %s\n' "$cand_scan_code" "${cand_scan_err:+stderr}"
  } 3>&1 | awk '
    # A macOS netstat row ends in its state and names the local address as
    # <address>.<port>: "*.445", "::1.8021", "192.0.2.10.8443".
    function netstat_row(   port) {
      if ($1 == "Proto" && $NF == "(state)") { net_header = 1; return }
      if ($NF != "LISTEN") return
      port = $4; sub(/.*\./, "", port)
      if (!net_header || port !~ /^[0-9]+$/) { net_odd = 1; return }
      listening[port] = 1
    }
    # lsof: the last line is "<exit code> [stderr]"; hold each line back one
    # read so it never parses as a row.
    function row(line,   f, n, port) {
      n = split(line, f, " ")
      if (lines++ == 0) { header = (f[1] == "COMMAND" && f[2] == "PID"); return }
      port = f[n - 1]; sub(/.*:/, "", port)
      if (f[2] !~ /^[0-9]+$/ || f[n] != "(LISTEN)" || port !~ /^[0-9]+$/) { odd = 1; return }
      if (!(port in held)) { held[port] = f[1] " (pid " f[2] ")"; order[++held_n] = port }
    }
    !in_lsof && $1 == "netstat-exit" { net_ok = ($2 == 0 && net_header && !net_odd); in_lsof = 1; next }
    !in_lsof { netstat_row(); next }
    kept { row(last) }
    { last = $0; kept = 1 }
    END {
      split(last, t, " ")
      listed = (t[1] == 0 && header)
      if (listed) for (i = 1; i <= held_n; i++) print "LISTEN " order[i] " " held[order[i]]
      for (port in listening) if (!listed || !(port in held)) print "HIDDEN " port
      clean = net_ok && t[2] == "" && ((listed && !odd && held_n > 0) || (t[1] == 1 && lines == 0))
      print "ABSENT " (clean ? "CLEAR" : "UNKNOWN")
    }'
}

# One port looked up in tcp_listeners output: "LISTEN <command> (pid N)",
# "CLEAR" or "UNKNOWN" — the vocabulary candidates prints and its read-only
# dashboard consumer distinguishes. Pass a scan already taken to look up many
# ports for the price of one lsof; without one, this takes its own. A HIDDEN
# port, and a missing or unreadable scan, are UNKNOWN.
port_observation() { # port [tcp_listeners output]
  if [ $# -lt 2 ]; then set -- "$1" "$(tcp_listeners)"; fi
  printf '%s\n' "$2" | awk -v p="$1" '
    $1 == "LISTEN" && $2 == p { sub(/^LISTEN [0-9]+ /, ""); print "LISTEN " $0; seen = 1; exit }
    $1 == "HIDDEN" && $2 == p { print "UNKNOWN"; seen = 1; exit }
    $1 == "ABSENT" { absent = $2 }
    END { if (!seen) print (absent == "CLEAR" ? "CLEAR" : "UNKNOWN") }'
}

# The two discovery lists candidates and triage both need. Kept as functions
# rather than inlined in each caller so "what is a service" has one answer.
discover_services() {
  for cand_dir in "$ROOT"/local-*/; do
    [ -d "$cand_dir" ] || continue
    cand_svc=$(basename "$cand_dir")
    is_docker_service "$cand_svc" || continue
    echo "$cand_svc"
  done
}

discover_cron_jobs() {
  cand_sh="$ROOT/local-cron-jobs/cron-jobs.sh"
  [ -x "$cand_sh" ] || return 0
  "$cand_sh" list 2>/dev/null | awk 'NR>1 && NF {print $1}' || :
}

# Ports a service wants that something already listed in <profile>.service
# also wants. Filing both into one profile is a config that cannot start.
service_clash_with_manifest() { # service -> "port:other" lines
  cs_svc="$1"
  cs_mine=$(service_ports "$cs_svc")
  [ -n "$cs_mine" ] || return 0
  manifest service | grep -v "^$cs_svc$" | while read -r cs_other; do
    [ -n "$cs_other" ] || continue
    for cs_p in $cs_mine; do
      case " $(service_ports "$cs_other") " in
        *" $cs_p "*) echo "$cs_p:${cs_other#local-}" ;;
      esac
    done
  done
}

candidates_service() {
  cand_listed=$(manifest service)
  # One docker ps answers both the service states and the CONTAINER lines the
  # dashboard's Health > Containers reads (sd:5840). Its stderr is dropped: the
  # reason printed is a fixed sentence, so nothing docker says reaches a page.
  cand_ps=""
  if ! command -v docker >/dev/null 2>&1; then
    cand_docker_observed=0
    cand_docker_why="docker is not installed"
  elif cand_ps=$(docker ps --no-trunc --format '{{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Label "com.docker.compose.project"}}\t{{.Label "com.docker.compose.project.working_dir"}}\t{{.Mounts}}' 2>/dev/null); then
    cand_docker_observed=1
  else
    cand_ps=""
    cand_docker_observed=0
    cand_docker_why="docker ps failed; the daemon is not running or not reachable"
  fi
  cand_running=$(printf '%s\n' "$cand_ps" | cut -f1)
  cand_pairs=""
  cand_have=0
  cand_in=0

  for cand_svc in $(discover_services); do
    cand_have=$((cand_have + 1))
    for cand_one in $(service_ports "$cand_svc"); do
      cand_pairs="$cand_pairs$cand_one $cand_svc
"
    done
  done

  # Ports claimed by more than one candidate. Only these can collide, and only
  # for services listed together in one profile.
  cand_clash=$(printf '%s' "$cand_pairs" | sort -u |
    awk '{n[$1]++; who[$1]=who[$1] " " substr($2,7)} END {for (p in n) if (n[p]>1) print p ":" who[p]}' |
    sort -n)

  echo "== service"
  for cand_svc in $(discover_services); do
    if printf '%s\n' "$cand_listed" | grep -qx "$cand_svc"; then
      cand_mark="+"; cand_in=$((cand_in + 1))
    else
      cand_mark="."
    fi
    if [ "$cand_docker_observed" -eq 0 ]; then
      cand_state="unknown"
    elif service_running "$cand_svc" "$cand_running"; then
      cand_state="running"
    else
      cand_state="stopped"
    fi
    printf '  %s %-32s %-40s %s\n' "$cand_mark" "$cand_svc" "$(service_ports "$cand_svc")" "$cand_state"
  done
  echo "  ---     $cand_have startable, $cand_in in this profile   (+ listed, . available)"
  if [ -n "$cand_clash" ]; then
    printf '%s\n' "$cand_clash" | while IFS= read -r cand_c; do
      echo "  CLASH   port ${cand_c%%:*} wanted by${cand_c#*:} — list at most one"
    done
  fi
  # One lsof for the whole table, looked up once per distinct port.
  cand_listeners=$(tcp_listeners)
  printf '%s' "$cand_pairs" | awk '{print $1}' | sort -un | while IFS= read -r cand_port; do
    cand_seen=$(port_observation "$cand_port" "$cand_listeners")
    case "$cand_seen" in
      'LISTEN '*)
        cand_who=${cand_seen#LISTEN }
        echo "  LISTEN  port $cand_port held by $cand_who"
        ;;
      CLEAR)
        echo "  CLEAR   port $cand_port no TCP listener observed"
        continue
        ;;
      *)
        echo "  UNKNOWN port $cand_port listener inspection unavailable"
        continue
        ;;
    esac
    cand_wants=$(printf '%s' "$cand_pairs" | awk -v p="$cand_port" '$1==p {print substr($2,7)}' | sort -u | tr '\n' ' ')
    echo "  BUSY    port $cand_port held by $cand_who — ${cand_wants% } cannot bind it"
  done
  if [ "$cand_docker_observed" -eq 1 ]; then
    echo "  DOCKER  read: $(printf '%s' "$cand_ps" | grep -c .) running"
    printf '%s\n' "$cand_ps" | grep . | sed 's/^/  CONTAINER	/' || :
  else
    echo "  DOCKER  unknown: $cand_docker_why"
  fi
  echo "  add by hand: $PROFILE_DIR/${PROFILE:-<profile>}.service  (.service is never auto-captured)"
  echo
}

candidates_cron() {
  echo "== cron"
  cand_sh="$ROOT/local-cron-jobs/cron-jobs.sh"
  if [ ! -x "$cand_sh" ]; then
    echo "  --      no local-cron-jobs/cron-jobs.sh — skipped"
    echo
    return 0
  fi
  cand_listed=$(manifest cron)
  cand_all=$(discover_cron_jobs)
  cand_have=0
  cand_in=0
  for cand_job in $cand_all; do
    cand_have=$((cand_have + 1))
    if printf '%s\n' "$cand_listed" | grep -qx "$cand_job"; then
      cand_mark="+"; cand_in=$((cand_in + 1))
    else
      cand_mark="."
    fi
    printf '  %s %s\n' "$cand_mark" "$cand_job"
  done
  echo "  ---     $cand_have defined, $cand_in in this profile   (+ listed, . available)"
  echo "  add by hand: $PROFILE_DIR/${PROFILE:-<profile>}.cron   installed state: cron-jobs.sh list"
  echo
}

candidates_agent() {
  echo "== agent"
  cand_listed=$(manifest agent)
  cand_have=0
  cand_in=0
  for cand_pl in $(owned_agent_plists); do
    cand_label=$(basename "$cand_pl" .plist)
    cand_have=$((cand_have + 1))
    if printf '%s\n' "$cand_listed" | grep -qx "$cand_label"; then
      cand_mark="+"; cand_in=$((cand_in + 1))
    else
      cand_mark="."
    fi
    printf '  %s %s\n' "$cand_mark" "$cand_label"
  done
  echo "  ---     $cand_have on this machine, $cand_in in this profile   (+ listed, . available)"
  echo "  fill with: machine-setup.sh capture --apply  (refuses a plist holding a credential)"
  echo
}

# Read-only, ignores --apply, and works on a machine with no recorded profile
# (where only `common` can be compared against).
cmd_candidates() {
  cand_kind="$1"
  case "$cand_kind" in
    ""|service|cron|agent) ;;
    brew|cask|mas) die "candidates: $cand_kind is triage's job — run: $(basename "$0") triage $cand_kind" ;;
    *) die "candidates: unknown kind '$cand_kind' (service, cron, agent)" ;;
  esac

  if [ -n "${MACHINE_SETUP_PROFILE:-}" ]; then
    PROFILE="$MACHINE_SETUP_PROFILE"
  elif [ -f "$STATE_FILE" ]; then
    PROFILE=$(head -1 "$STATE_FILE" | tr -d '\n\r')
  else
    PROFILE=""
  fi

  echo "candidates : what this machine could run, and what the profile already lists"
  if [ -n "$PROFILE" ]; then
    echo "profile    : $PROFILE (layered over common)"
  else
    echo "profile    : none recorded — comparing against common only"
  fi
  echo "read-only  : nothing here writes, with or without --apply"
  echo

  case "$cand_kind" in ""|service) candidates_service ;; esac
  case "$cand_kind" in ""|cron)    candidates_cron ;; esac
  case "$cand_kind" in ""|agent)   candidates_agent ;; esac
}

# Standalone application report: what is in /Applications that neither the
# App Store nor Homebrew put there, and which of those COULD be brew-managed.
# Classification is authoritative, not name-guessing: App Store apps carry a
# _MASReceipt, cask apps appear in the installed casks' artifact lists (one
# `brew info --json=v2` call), pkg-based casks are matched by token, and
# com.apple.* bundles are system apps.
# Writes "<app name>	<cask token>" (token empty when no cask exists) for
# every standalone app to $1. Shared by cmd_apps (report) and cmd_adopt.
# With `names` as $2 it writes the names alone and searches no cask:
# capture's form.
standalone_scan() { # out [names]
  out="$1"
  # Each step that could leave a partial list returns 1: capture runs this
  # where set -e is off, and writes <profile>.app from the list.
  tmp=$(mktemp -d) || return 1
  trap_tmp

  if command -v brew >/dev/null 2>&1; then
    bound_step 300 brew info --json=v2 --installed --cask 2>/dev/null | python3 -c '
import json,sys
d=json.load(sys.stdin)
apps=set()
def walk(a):
    if isinstance(a,str) and a.endswith(".app"): apps.add(a.split("/")[-1][:-4])
    elif isinstance(a,list):
        for x in a: walk(x)
    elif isinstance(a,dict):
        for v in a.values(): walk(v)
for c in d.get("casks",[]):
    walk(c.get("artifacts",[]))
print("\n".join(sorted(apps)))' > "$tmp/cask-apps" || : > "$tmp/cask-apps"
    bound_step 300 brew list --cask 2>/dev/null | tr -d '-' | tr '[:upper:]' '[:lower:]' > "$tmp/cask-tokens" || :
  else
    : > "$tmp/cask-apps"; : > "$tmp/cask-tokens"
  fi

  for app in "$APPLICATIONS_DIR"/*.app; do
    [ -e "$app" ] || continue
    name=$(basename "$app" .app)
    [ -e "$app/Contents/_MASReceipt/receipt" ] && continue
    grep -qxF "$name" "$tmp/cask-apps" && continue
    bid=$(defaults read "$app/Contents/Info" CFBundleIdentifier 2>/dev/null || :)
    case "$bid" in
      com.apple.*) continue ;;
      com.google.drivefs.shortcuts.*) continue ;;  # created by Google Drive, not installable
    esac
    norm=$(echo "$name" | tr -d ' -.' | tr '[:upper:]' '[:lower:]')
    grep -qxF "$norm" "$tmp/cask-tokens" && continue
    echo "$name"
  done | sort > "$tmp/standalone" || return 1

  : > "$out"
  if [ "${2:-}" = names ]; then
    cp "$tmp/standalone" "$out" || return 1
    return 0
  fi
  while IFS= read -r name; do
    tok=$(echo "$name" | tr '[:upper:]' '[:lower:]' | sed -e 's/ /-/g' -e 's/[^a-z0-9-]//g')
    hit=""
    # Vendors name casks inconsistently, so try the obvious variants:
    # cleanshot-x -> cleanshot, languagetool-for-desktop -> languagetool-desktop.
    # No first-word fallback: it matched "GitHub Copilot" to the `github`
    # cask, which is GitHub Desktop — a match must cover the whole name.
    for cand in "$tok" "${tok%-x}" "$(echo "$tok" | sed 's/-for-desktop/-desktop/')"; do
      hit=$(bound_step 120 brew search --cask "/^$cand\$/" 2>/dev/null | head -1 || :)
      [ -n "$hit" ] && break
    done
    printf '%s\t%s\n' "$name" "$hit" >> "$out"
  done < "$tmp/standalone"
}

cmd_apps() {
  scan=$(mktemp)
  standalone_scan "$scan"
  echo "standalone applications (no App Store receipt, not brew-managed):"
  while IFS="$(printf '\t')" read -r name hit; do
    if [ -n "$hit" ]; then
      printf '  %-28s cask available: %s (brew install --cask --adopt %s)\n' "$name" "$hit" "$hit"
    else
      printf '  %-28s no cask — manual install\n' "$name"
    fi
  done < "$scan"
  echo
  echo "total: $(grep -c . "$scan" || :)  (track them: capture writes <profile>.app)"
  rm -f "$scan"
}

# Adopt every standalone app that has a cask: brew takes over the existing
# install in place (no reinstall), the token is filed into <profile>.cask and
# the name leaves <profile>.app, so from then on setup installs it and brew
# upgrade maintains it. Dry-run by default like everything else. pkg-based
# casks (Google Drive was one) prompt for sudo during --apply.
cmd_adopt() {
  load_profile
  [ "$APPLY" -eq 1 ] || echo "mode    : DRY RUN — nothing will change; re-run with --apply"
  echo "profile : $PROFILE"
  scan=$(mktemp)
  standalone_scan "$scan"
  adopted=0; failed=0; manual=0
  while IFS="$(printf '\t')" read -r name tok; do
    if [ -z "$tok" ]; then
      manual=$((manual + 1))
      continue
    fi
    if [ "$APPLY" -eq 1 ]; then
      if bound_step 1800 brew install --cask --adopt "$tok"; then
        adopted=$((adopted + 1))
        cf="$PROFILE_DIR/$PROFILE.cask"
        if ! manifest cask | grep -qxF "$tok"; then
          { grep '^#' "$cf" 2>/dev/null || :
            { grep -v '^#' "$cf" 2>/dev/null || :; echo "$tok"; } | grep -v '^$' | sort -u
          } > "$cf.tmp" && mv "$cf.tmp" "$cf"
          echo "  filed   $tok -> $(basename "$cf")"
        fi
        af="$PROFILE_DIR/$PROFILE.app"
        if [ -f "$af" ] && grep -qxF "$name" "$af"; then
          grep -vxF "$name" "$af" > "$af.tmp" && mv "$af.tmp" "$af"
          echo "  removed $name from $(basename "$af")"
        fi
      else
        failed=$((failed + 1))
        echo "  FAILED  $tok — adopt by hand: brew install --cask --adopt $tok"
      fi
    else
      adopted=$((adopted + 1))
      printf '  would adopt %-28s as cask %s\n' "$name" "$tok"
    fi
  done < "$scan"
  rm -f "$scan"
  echo
  if [ "$APPLY" -eq 1 ]; then
    echo "adopted: $adopted  failed: $failed  no-cask (stay manual): $manual"
    [ "$adopted" -gt 0 ] && review_hint
  else
    echo "would adopt: $adopted  no-cask (stay manual): $manual"
  fi
}

# Readable overview of what each profile carries: a counts matrix, then per-
# profile sections listing every entry of every kind. Lists are columnized
# when `column` exists; cron and macos lines stay one per line (multi-field).
cmd_report() {
  require_config
  only="$1"
  [ -n "$only" ] && valid_profile "$only"

  report_list() {
    case "$2" in
      cron|macos|iterm2|spotlight|mailto)
        profile_entries "$1" "$2" | sed 's/^/      /' ;;
      *)
        if command -v column >/dev/null 2>&1; then
          profile_entries "$1" "$2" | column -x -c 90 | sed 's/^/      /'
        else
          profile_entries "$1" "$2" | sed 's/^/      /'
        fi ;;
    esac
  }

  report_section() {
    total=0
    for kind in $KINDS; do
      n=$(profile_entries "$1" "$kind" | grep -c . || :)
      total=$((total + n))
    done
    if [ "$1" = "common" ]; then
      echo "== common — applies to every profile ($total entries)"
    else
      echo "== $1 — on top of common ($total entries)"
    fi
    if [ "$total" -eq 0 ]; then
      echo "      (empty — captured on that machine, not guessed from this one)"
      echo
      return 0
    fi
    for kind in $KINDS; do
      n=$(profile_entries "$1" "$kind" | grep -c . || :)
      [ "$n" -eq 0 ] && continue
      echo "    $kind ($n)"
      report_list "$1" "$kind"
    done
    echo
  }

  if [ -n "$only" ]; then
    report_section common
    report_section "$only"
    return 0
  fi

  echo "entries per kind (common applies to every profile)"
  report_profiles="common $(known_profiles | tr '\n' ' ')"
  printf '    %-9s' kind
  for pr in $report_profiles; do printf ' %10s' "$pr"; done
  echo
  for kind in $KINDS; do
    printf '    %-9s' "$kind"
    for pr in $report_profiles; do
      n=$(profile_entries "$pr" "$kind" | grep -c . || :)
      printf ' %10s' "$n"
    done
    echo
  done
  echo

  for pr in $report_profiles; do
    report_section "$pr"
  done
}

# Names of the supported profiles; marks the one recorded on this machine.
cmd_profiles() {
  require_config
  rec=""
  [ -f "$STATE_FILE" ] && rec=$(head -1 "$STATE_FILE" | tr -d '\n\r')
  for pr in $(known_profiles); do
    if [ "$pr" = "$rec" ]; then
      echo "$pr (recorded on this machine)"
    else
      echo "$pr"
    fi
  done
  echo "(plus common, which layers under every profile)"
}

# Read-only environment sanity check: everything setup cannot install but a
# working machine depends on. Safe anywhere, ignores --apply, needs no
# recorded profile (agent/cron checks are skipped without one).
# Doctor runs outside the setup/update path, so nothing has resolved PROFILE
# for it. Checks that apply to one machine only (the sd pieces) need it,
# and an unset PROFILE would silently skip them everywhere —
# including on the machine that does want them. Same precedence as the rest
# of the script, without load_profile's die: a fresh machine gets a report.
doctor_profile() {
  if [ -n "${MACHINE_SETUP_PROFILE:-}" ]; then
    PROFILE="$MACHINE_SETUP_PROFILE"
  elif [ -f "$STATE_FILE" ]; then
    PROFILE=$(head -1 "$STATE_FILE" | tr -d '\n\r')
  else
    PROFILE=""
  fi
}

doctor_fail() { echo "  FAIL    $*"; fails=$((fails + 1)); }

# One read of the database. Not `sqlite3 -readonly`: the library keeps the
# file in WAL mode (the pack's lib/sd_db/database.py), and a read-only open of
# a WAL database whose -wal and -shm sidecars are absent — every database
# nothing currently holds open — fails with "unable to open database file
# (14)", which doctor would then report as a corrupted database. An ordinary
# open creates the sidecars and removes them on close; `query_only` is what
# keeps the statement a read.
sd_sqlite() { sqlite3 "$SD_DB" "PRAGMA query_only = 1; $1"; }

# Why a dashboard /health body says `"ok": false`. A failed database open
# carries an `error`; the ordinary unhealthy answer carries none and states
# its diagnostics as fields (the pack's dashboard/sd_dashboard/server.py,
# _health), so each field that can make it unhealthy is named. A body with
# neither says so rather than ending the line on a colon.
sd_health_reason() { # body file
  hr=$(sed -n 's/.*"error": *"\([^"]*\)".*/\1/p' "$1" | head -1)
  if [ -n "$hr" ]; then
    printf '%s\n' "$hr"
    return 0
  fi
  hr_schema=$(sed -n 's/.*"schema": *\([0-9][0-9]*\).*/\1/p' "$1" | head -1)
  hr_expected=$(sed -n 's/.*"expected_schema": *\([0-9][0-9]*\).*/\1/p' "$1" | head -1)
  if [ -n "$hr_schema" ] && [ -n "$hr_expected" ] && [ "$hr_schema" != "$hr_expected" ]; then
    hr="database at schema $hr_schema, the server built for $hr_expected"
  fi
  if grep -q '"code_changed": *true' "$1"; then
    hr="${hr:+$hr; }code changed since the server started, restart $LABEL_PREFIX.sd-dashboard"
  fi
  if grep -q '"direct_listener_ok": *false' "$1"; then
    hr="${hr:+$hr; }its direct listener is not healthy"
  fi
  printf '%s\n' "${hr:-no reason in the health body; read ~/Library/Logs/$LABEL_PREFIX.sd-dashboard.err}"
}

# An EnvironmentVariables value out of an installed plist, or nothing.
plist_env_value() { # plist, key
  [ -f "$1" ] || return 0
  plutil -extract "EnvironmentVariables.$2" raw -o - "$1" 2>/dev/null || :
}

# The checks requirement 9 of
# one-database design (sd:234) asks doctor for, less the
# runner's, and the version report beside them: the database opens and
# passes integrity_check; the dashboard answers over its HTTPS name; the
# serve route exists. Every failure is a FAIL, which is what
# doctor's exit code counts, and the line carries the word the stages use for
# the same gap (MISSING, DIFFERS) so one grep finds both.
# Not configured here — no sd agent in the profile, no tailscale — is a SKIP
# and not a finding, convention 6. A check doctor could not run on a machine
# that is configured is not that: a missing sqlite3 and a dashboard origin
# under Funnel are each a FAIL.
#
# Each check reads the state where it lives rather than asking a tool that
# might itself be broken: sqlite3 on the database file, tailscale for the
# route and curl for the answer.
doctor_sd() {
  # A promoted satellite holds the role with no sd agent in its profile (sd:4648).
  if [ -z "${PROFILE:-}" ] || { ! sd_in_profile && ! hub_holder; }; then
    echo "  SKIP    sd pieces not checked (no sd agent in this profile, and no hub role held)"
    return 0
  fi
  # Not a SKIP: every profile with an sd agent is a Mac, macOS ships
  # /usr/bin/sqlite3 and no profile installs another, so a doctor that
  # cannot find it runs on a broken PATH and has checked nothing (sd:1413).
  if ! command -v sqlite3 >/dev/null 2>&1; then
    doctor_fail "sqlite3 MISSING from PATH — database not checked; macOS ships /usr/bin/sqlite3, so put /usr/bin on PATH and re-run: $(basename "$SELF") doctor sd"
  elif [ ! -f "$SD_DB" ]; then
    doctor_fail "database MISSING at $SD_DB — run: $(basename "$SELF") update sd --apply"
  else
    integrity=$(sd_sqlite 'PRAGMA integrity_check;' 2>&1 | head -3 | tr '\n' ' ' | sed 's/ $//')
    if [ "$integrity" = ok ]; then
      echo "  ok      database $SD_DB passes integrity_check"
    else
      doctor_fail "database $SD_DB integrity_check: $integrity — restore a dated backup: sd-db restore <backup root>/<date> (SD_DB_BACKUP_ROOT, or the pack's default)"
    fi
  fi
  # A here-document and not a pipe, so doctor_fail counts in this shell.
  while IFS= read -r elsewhere; do
    [ -n "$elsewhere" ] || continue
    doctor_fail "database DIFFERS: $elsewhere, not $SD_DB — the checks here read $SD_DB; remove the key or point it there"
  done <<EOF
$(sd_database_elsewhere)
EOF

  if role_agents | grep -qxF "$LABEL_PREFIX.sd-dashboard"; then
    if ! command -v tailscale >/dev/null 2>&1 || ! tailscale status >/dev/null 2>&1; then
      echo "  SKIP    dashboard route and HTTPS answer not checked (tailscale not installed, not running or not logged in)"
    else
      serve=$(tailscale serve status 2>/dev/null || :)
      origin=$(printf '%s\n' "$serve" | sd_route_origin)
      held=$(printf '%s\n' "$serve" | sd_route_holder)
      if [ -z "$origin" ] && [ -n "$held" ]; then
        doctor_fail "dashboard route DIFFERS — https:$SD_DASHBOARD_HTTPS_PORT already serves $held, not 127.0.0.1:$SD_DASHBOARD_PORT; update sd --apply will not replace it, so free the port first"
        echo "  --      dashboard HTTPS answer not checked (no route to answer on)"
      elif [ -z "$origin" ]; then
        doctor_fail "dashboard route MISSING — no tailscale serve https:$SD_DASHBOARD_HTTPS_PORT -> 127.0.0.1:$SD_DASHBOARD_PORT; run: $(basename "$SELF") update sd --apply"
        echo "  --      dashboard HTTPS answer not checked (no route to answer on)"
      else
        echo "  ok      dashboard route $origin -> 127.0.0.1:$SD_DASHBOARD_PORT"
        # The front door is tailnet-only by design; Funnel on that authority
        # is the dashboard on the public internet behind a login header, the
        # state the runtime's own preflight refuses to install (sd:1413). The
        # remedy removes the mount with the flags the runtime disables it
        # with, then restores the tailnet-only route; `tailscale funnel reset`
        # would also drop every other serve and Funnel mount.
        if printf '%s\n' "$serve" | grep -F "$origin" | grep -q 'Funnel on'; then
          doctor_fail "dashboard origin $origin is PUBLIC under Funnel — the front door is tailnet-only; run: tailscale serve --bg --https=$SD_DASHBOARD_HTTPS_PORT --set-path=/ off, then: $(basename "$SELF") update sd --apply (not tailscale funnel reset, which drops every other mount too); see $SD_PACK_ROOT/dashboard/RUNTIME.md"
        fi
        body=$(mktemp)
        code=$(curl -sS -m 10 -o "$body" -w '%{http_code}' "$origin/health" 2>"$body.err") || code=000
        if [ "$code" = 000 ]; then
          doctor_fail "dashboard does not answer over $origin — $(head -1 "$body.err")"
        elif grep -q '"service": *"sd-dashboard"' "$body"; then
          if grep -q '"ok": *true' "$body"; then
            echo "  ok      dashboard answers over $origin/health"
          else
            doctor_fail "dashboard answers over $origin but reports unhealthy: $(sd_health_reason "$body")"
          fi
        elif grep -q 'Untrusted dashboard host' "$body"; then
          echo "  WARN    dashboard answers over $origin with HTTP $code (Untrusted dashboard host) — the installed service runs a local-only configuration; see the private front door in $SD_PACK_ROOT/dashboard/RUNTIME.md"
        else
          doctor_fail "dashboard answers over $origin with HTTP $code and not its health body"
        fi
        rm -f "$body" "$body.err"
      fi
    fi
  fi

  doctor_sd_versions
}

# The pack's lib/sd_db, which every caller runs (sd:3279), beside the schema
# version in the database, and the interpreters that run it: the pack's,
# which `dashboard.sh serve` defaults to, and whatever SD_DASHBOARD_PYTHON
# names, in the environment or in the installed plist. Each must import
# sd_db from that lib/ under `-I`, so neither a PYTHONPATH nor a copy
# installed in the interpreter stands in for it.
doctor_sd_versions() {
  lib="$SD_PACK_ROOT/lib"
  lib_schema=$(sed -n 's/^SCHEMA_VERSION *= *\([0-9][0-9]*\).*/\1/p' "$lib/sd_db/schema.py" 2>/dev/null | head -1)
  db_schema=""
  if [ -f "$SD_DB" ] && command -v sqlite3 >/dev/null 2>&1; then
    db_schema=$(sd_sqlite 'PRAGMA user_version;' 2>/dev/null || :)
  fi
  if [ -z "$lib_schema" ]; then
    doctor_fail "no sd_db at $lib — move the pack to a commit that holds lib/sd_db: local-repo-sync/repo-sync.sh refresh on the hub, follow on a satellite"
    return 0
  fi
  echo "  --      sd_db in the pack's lib/ $lib: built for schema $lib_schema; database at schema ${db_schema:-none}"
  if [ -n "$db_schema" ] && [ "$db_schema" != "$lib_schema" ]; then
    doctor_fail "database schema $db_schema DIFFERS from the pack's lib/ $lib_schema — after a backup, with sd-dashboard and sd-serve stopped: $SD_PACK_ROOT/bin/sd-db migrate"
  fi
  # An older sd-serve plist may still set SD_DB_LIBRARY, which no sd-db reads now.
  selector=$(plist_env_value "$HOME/Library/LaunchAgents/$LABEL_PREFIX.sd-serve.plist" SD_DB_LIBRARY)
  [ -z "$selector" ] || doctor_fail "$LABEL_PREFIX.sd-serve sets SD_DB_LIBRARY=$selector, which the pack's sd-db ignores — remove the key from its plist, then kickstart it"
  # The pack's own interpreter may be absent; one the operator named must exist.
  seen=" "
  plist_py=$(plist_env_value "$HOME/Library/LaunchAgents/$LABEL_PREFIX.sd-dashboard.plist" SD_DASHBOARD_PYTHON)
  for py in "$SD_PACK_ROOT/.venv/bin/python" "${SD_DASHBOARD_PYTHON:-}" "$plist_py"; do
    [ -n "$py" ] || continue
    case "$seen" in *" $py "*) continue ;; esac
    seen="$seen$py "
    if [ ! -x "$py" ]; then
      case " ${SD_DASHBOARD_PYTHON:-} $plist_py " in
        *" $py "*) doctor_fail "no interpreter at $py, which SD_DASHBOARD_PYTHON names" ;;
        *) echo "  --      no interpreter at $py (not checked)" ;;
      esac
      continue
    fi
    if ! file=$("$py" -I -c 'import sys; sys.path.insert(0, sys.argv[1]); import sd_db; print(sd_db.__file__)' "$lib" 2>&1); then
      doctor_fail "$py cannot import sd_db from $lib ($(printf '%s\n' "$file" | tail -1))"
      continue
    fi
    case "$file" in
      "$lib"/*) echo "  ok      $py imports sd_db from $lib" ;;
      *) doctor_fail "$py resolves sd_db from $file, not the pack's lib/ $lib" ;;
    esac
  done
}

# Config that still names a deleted folder: local-sd-db (sd:3281, sd:3371)
# and local-project-dashboard (pack sd:5987), both the pack's now. It looks in
# installed LaunchAgents, the plist templates and the cron job files. Each
# fails when it runs, since the folder is gone. A machine whose config names
# none passes; a folder that cannot be searched fails, because an unread
# folder says nothing.
doctor_retired_sd_db() {
  doctor_retired local-sd-db "$SD_PACK_ROOT/bin/sd-db"
  doctor_retired local-project-dashboard "$SD_PACK_ROOT/dashboard/dashboard.sh"
}
doctor_retired() { # the deleted folder, where its callers point now
  retired=""
  unknown=0
  for place in "$AGENT_DIR" "$SYSTEM_TOOLS_CONFIG/cron-jobs/jobs"; do
    [ -d "$place" ] || continue
    if hits=$(grep -rlF -D skip "$1" "$place" 2>/dev/null); then
      retired="$retired
$hits"
    elif [ $? -ne 1 ]; then
      doctor_fail "cannot search $place for config that names the deleted $1"
      unknown=1
    fi
  done
  for plist in "$HOME/Library/LaunchAgents/$LABEL_PREFIX".*.plist; do
    [ -f "$plist" ] || continue
    if grep -qF "$1" "$plist" 2>/dev/null; then
      retired="$retired
$plist"
    elif [ $? -ne 1 ]; then
      doctor_fail "cannot read $plist to check that it no longer names $1"
      unknown=1
    fi
  done
  if [ -z "$retired" ]; then
    [ "$unknown" -eq 0 ] && echo "  ok      no config names the deleted $1"
    return 0
  fi
  while IFS= read -r named; do
    [ -n "$named" ] || continue
    doctor_fail "$named names the deleted $1 — repoint it to $2, then reinstall it ($(basename "$SELF") update agents --apply for a plist, cron-jobs.sh install <job> for a job)"
  done <<EOF
$retired
EOF
}

# `doctor sd`: the checks above alone, for the test suite and for a quick
# read after touching one of the pieces. Same exit code rule as doctor.
cmd_doctor_sd() {
  fails=0
  echo "== doctor sd"
  doctor_profile
  doctor_sd
  doctor_retired_sd_db
  echo
  if [ "$fails" -gt 0 ]; then
    echo "  $fails hard failure(s)"
    exit 1
  fi
  echo "  no hard failures"
}

# `doctor gate`: the weekly gate run (sd:5986; design
# docs/work/2026-10-25-hub-failover-full-role, "Weekly gate run"). `make
# check` for the system checkout, then for the pack, each in the machine's gate
# queue. The record holds the newest decisive run only: a pass (both exit 0)
# or a real failure (an exit from 1 to 124 but 75). A slot timeout (75), a
# check that did not start (125, 127) or a signal (over 128) is no verdict
# and leaves the record as it was, so a later try never hides an earlier
# failure. hub-ready reads it.
DOCTOR_GATE_RECORD="${XDG_STATE_HOME:-$HOME/.local/state}/machine-setup/doctor-gate-last"
# The weekly job's stamp (cron-jobs.sh record_stamp); a test points it elsewhere.
DOCTOR_GATE_STAMP="${DOCTOR_GATE_STAMP:-$ROOT/local-cron-jobs/logs/.doctor-gate-weekly.stamp}"
DOCTOR_GATE_WAIT=3600

cmd_doctor_gate() {
  echo "== doctor gate"
  tmp=$(mktemp -d)
  # A signal exits before the record is written: no verdict. The exit also
  # drops the snapshots' worktree entries, which rm leaves behind.
  trap_tmp
  trap 'rm -rf "$tmp"; git -C "$ROOT" worktree prune; git -C "$SD_PACK_ROOT" worktree prune' EXIT
  dg_verdict=pass
  dg_fields=""
  dg_none=""
  for dg_name in system pack; do
    if [ "$dg_name" = system ]; then dg_dir=$ROOT; else dg_dir=$SD_PACK_ROOT; fi
    # The head is read once the slot is held, and the check runs in a
    # detached snapshot of it (sd:6395): repo-sync's refresh moves the
    # checkout during a run of 45 minutes, and a move voided the verdict.
    dg_rc=0
    dg_snap="$tmp/$dg_name.snap"
    # shellcheck disable=SC2016 # the inner script expands its own arguments
    "$SD_PACK_ROOT/bin/sd" gate run --max-wait "$DOCTOR_GATE_WAIT" --label "doctor gate: $dg_name make check" -- \
      sh -c 'h=$(git -C "$1" rev-parse HEAD) && echo "$h" > "$2" && git -C "$1" worktree add -q --detach "$3" "$h" && exec make -C "$3" check' \
      sh "$dg_dir" "$tmp/$dg_name" "$dg_snap" || dg_rc=$?
    [ ! -d "$dg_snap" ] || git -C "$dg_dir" worktree remove --force "$dg_snap" || :
    dg_head=$(cat "$tmp/$dg_name" 2>/dev/null) || dg_head=""
    # A stage with no verdict ends the run; a failure before it still stands.
    if [ "$dg_rc" -eq 75 ]; then
      dg_none="no gate slot came free in $DOCTOR_GATE_WAIT s (exit 75)"
    elif [ "$dg_rc" -ge 125 ] || [ -z "$dg_head" ]; then
      dg_none="make check in $dg_dir did not finish (exit $dg_rc)"
    fi
    if [ -n "$dg_none" ]; then
      if [ "$dg_verdict" = fail ]; then
        echo "  $dg_name: $dg_none; no $dg_name verdict; the system failure is recorded"
        break
      fi
      echo "  $dg_name: $dg_none; no verdict recorded"
      [ "$dg_rc" -eq 75 ] && exit 75
      exit 1
    fi
    if [ "$dg_rc" -eq 0 ]; then
      echo "  $dg_name: make check passed in $dg_dir at $dg_head"
    else
      echo "  $dg_name: make check FAILED in $dg_dir at $dg_head (exit $dg_rc)"
      dg_verdict=fail
    fi
    dg_fields="${dg_fields}$dg_name=$dg_head
${dg_name}_exit=$dg_rc
"
    # A failure is recorded at once, so a later stage killed with no
    # verdict cannot leave an older pass in its place.
    [ "$dg_verdict" = pass ] || [ "$dg_name" = pack ] || doctor_gate_record
  done
  doctor_gate_record
  [ "$dg_verdict" = pass ] || exit 1
}

# Written whole and renamed: a kill leaves the old record or the new one.
doctor_gate_record() {
  mkdir -p "$(dirname "$DOCTOR_GATE_RECORD")"
  if printf 'verdict=%s\nended=%s\n%s' "$dg_verdict" "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$dg_fields" \
       > "$DOCTOR_GATE_RECORD.new" && mv "$DOCTOR_GATE_RECORD.new" "$DOCTOR_GATE_RECORD"; then
    echo "  record: $dg_verdict, in $DOCTOR_GATE_RECORD"
  else
    echo "  record: NOT written to $DOCTOR_GATE_RECORD; the verdict was $dg_verdict"
    exit 1
  fi
}

# The hub-ready gate line, from the record and the job's stamp: `ok` only
# for a pass that ended under 8 days ago. A stamp whose run started after the
# record's end adds a note, and changes nothing else.
gate_ready_line() {
  python3 - "$DOCTOR_GATE_RECORD" "$DOCTOR_GATE_STAMP" "$(basename "$SELF")" <<'PYEOF'
import datetime, re, sys
record, stamp, script = sys.argv[1:4]
STAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
HEAD = re.compile(r"[0-9a-f]{40}([0-9a-f]{24})?")

def fields(path):
    with open(path, encoding="utf-8") as f:
        return dict(line.split("=", 1) for line in f.read().splitlines() if "=" in line)

def when(text):
    if not STAMP.fullmatch(text or ""):
        raise ValueError(f"no UTC time: {text!r}")
    return datetime.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)

now = datetime.datetime.now(datetime.timezone.utc)
rerun = f"run {script} doctor gate"
ended = None
try:
    r = fields(record)
    ended = when(r.get("ended"))
    if r.get("verdict") not in ("pass", "fail"):
        raise ValueError(f"verdict {r.get('verdict')!r}")
    # A pass names both stages; a failure names each stage that reached a
    # verdict, one of them failed, and a later one may have none.
    ran = [name for name in ("system", "pack") if name in r or name + "_exit" in r]
    for name in ran:
        if not HEAD.fullmatch(r.get(name, "")) or not re.fullmatch(r"[0-9]+", r.get(name + "_exit", "")):
            raise ValueError(f"no {name} head and exit")
    if r["verdict"] == "pass" and (len(ran) < 2 or any(r[n + "_exit"] != "0" for n in ran)):
        raise ValueError("a pass that names a stage with no pass")
    if r["verdict"] == "fail" and (not ran or ran[0] != "system" or all(r[n + "_exit"] == "0" for n in ran)):
        raise ValueError("a failure that names no failed stage")
    named = f"system {r['system'][:12]}, pack {r.get('pack', '')[:12]}"
    age = now - ended
    if r["verdict"] == "fail":
        stages = "; ".join(f"{n} {r[n][:12]} exit {r[n + '_exit']}" if n in ran else f"{n} no verdict"
                           for n in ("system", "pack"))
        line = f"  NOT-READY weekly gate FAILED, ended {r['ended']} ({stages}): fix it, then {rerun}"
    elif age < datetime.timedelta(minutes=-5):
        line = f"  NOT-READY weekly gate record {record} ends {r['ended']}, in the future: {rerun}"
    elif age >= datetime.timedelta(days=8):
        line = f"  NOT-READY weekly gate last passed {r['ended']}, 8 days ago or more ({named}): {rerun}"
    else:
        line = f"  ok      weekly gate passed, ended {r['ended']} ({named})"
except FileNotFoundError:
    line = f"  NOT-READY no weekly gate verdict in {record}: {rerun}"
except (OSError, ValueError, UnicodeDecodeError) as error:
    ended = None
    line = f"  NOT-READY weekly gate record {record} cannot be read ({error}): {rerun}"
# A try after the record that wrote none: the note says why, from its stamp.
try:
    s = fields(stamp)
    started = when(s.get("started"))
    if ended is None or started > ended:
        line += "; last try timed out" if s.get("exit") == "75" and s.get("ended") else "; last try did not finish"
except (OSError, ValueError, UnicodeDecodeError):
    pass
print(line)
PYEOF
}

# Sessions that export OTel need genai-traces up, or every export fails
# unseen (sd:6944). A WARN, not a FAIL: the sessions run the same without it.
doctor_telemetry() {
  grep -q '"CLAUDE_CODE_ENABLE_TELEMETRY": *"1"' "$HOME/.claude/settings.json" 2>/dev/null || return 0
  # A satellite exports to the hub (sd:6945): any HTTP answer from its
  # endpoint is the collector's receiver; 000 is none.
  if [ -e "$SD_HUB_CONFIG" ]; then
    dt_ep=$(sed -n 's/.*"OTEL_EXPORTER_OTLP_ENDPOINT": *"\([^"]*\)".*/\1/p' "$HOME/.claude/settings.json" | head -1)
    dt_code=$(curl -sS -m 5 -o /dev/null -w '%{http_code}' "$dt_ep/v1/logs" 2>/dev/null) || dt_code=000
    if [ -n "$dt_ep" ] && [ "$dt_code" != 000 ]; then
      echo "  ok      agent telemetry: the hub's collector answers over $dt_ep"
    else
      echo "  WARN    agent telemetry is on but ${dt_ep:-no endpoint} does not answer — sessions drop their telemetry until it does; on the hub run: $(basename "$SELF") update sd --apply, and local-genai-traces/genai-traces.sh start"
    fi
    return 0
  fi
  if sh "$ROOT/local-genai-traces/genai-traces.sh" status >/dev/null 2>&1 </dev/null; then
    echo "  ok      agent telemetry: genai-traces answers"
  else
    echo "  WARN    agent telemetry is on but genai-traces status is not 0 — run: local-genai-traces/genai-traces.sh start"
  fi
}

cmd_doctor() {
  fails=0
  echo "== doctor"
  doctor_profile

  if [ -f "$STATE_FILE" ]; then
    echo "  ok      profile recorded: $(head -1 "$STATE_FILE")"
  else
    echo "  --      no profile recorded (fresh machine — run setup first)"
  fi

  if xcode-select -p >/dev/null 2>&1; then
    echo "  ok      Xcode command line tools"
  else
    echo "  FAIL    Xcode CLT missing — run: xcode-select --install"
    fails=$((fails + 1))
  fi

  if command -v brew >/dev/null 2>&1; then
    # brew doctor exits 1 on a warning; Homebrew's Intel one is no finding,
    # so a run that warns of nothing else is clean (sd:4943). Any other exit,
    # a timeout among them, is no answer.
    bd_rc=0
    bd_out=$(bound_step 600 brew doctor 2>&1) || bd_rc=$?
    if [ "$bd_rc" -eq 1 ] && printf '%s\n' "$bd_out" | grep -q '^Warning:'; then
      command -v st_drop_platform_warning >/dev/null 2>&1 || . "$ROOT/lib/brew_unlinked.sh"
      if bd_out=$(printf '%s\n' "$bd_out" | st_drop_platform_warning) \
          && ! printf '%s\n' "$bd_out" | grep -q '^Warning:'; then
        bd_rc=0
      fi
    fi
    if [ "$bd_rc" -eq 0 ]; then
      echo "  ok      brew doctor clean"
    else
      echo "  WARN    brew doctor has warnings — run: brew doctor"
    fi
  else
    echo "  FAIL    brew missing — https://brew.sh"
    fails=$((fails + 1))
  fi

  # The daemon only matters if this profile actually runs docker services.
  # Warning unconditionally made the check permanently true and unactionable on
  # a machine with no services and no Docker installed — uninstalling Docker
  # made it fire harder, which is the opposite of what a health check should do.
  #
  # Note every profile currently lists zero services, so the first branch is
  # dormant. It is written against the manifest rather than deleted so that
  # adding a service to a profile restores the check by itself.
  if [ -n "$(manifest service)" ]; then
    if docker info >/dev/null 2>&1; then
      echo "  ok      docker daemon running"
    else
      echo "  WARN    docker not running (this profile's services need it)"
    fi
  elif command -v docker >/dev/null 2>&1; then
    # Installed but unused here: still worth a line, since a stopped daemon on
    # a machine where docker is driven by hand is a real thing to notice.
    if docker info >/dev/null 2>&1; then
      echo "  ok      docker daemon running (no services in this profile)"
    else
      echo "  --      docker installed but not running (no services in this profile)"
    fi
  else
    echo "  --      docker not checked (not installed, no services in this profile)"
  fi
  doctor_telemetry

  if command -v fnm >/dev/null 2>&1; then
    echo "  ok      fnm ($(fnm current 2>/dev/null || echo no default node))"
  else
    echo "  WARN    fnm missing (the tooling stage installs node versions with it)"
  fi
  # Claude Code is the claude-code@latest cask on every profile (sd:4738).
  if command -v claude >/dev/null 2>&1; then
    echo "  ok      claude ($(command -v claude))"
  else
    echo "  WARN    claude missing — run: brew install --cask claude-code@latest"
  fi

  # Key material no stage installs: copied by hand, never through git.
  # The ssh identity may be spelled per machine, so MACHINE_SETUP_SSH_KEYS
  # lists the names (relative to ~) and any one of them counts.
  if [ -z "${MACHINE_SETUP_SSH_KEYS:-}" ]; then
    echo "  --      ssh identity key not checked (MACHINE_SETUP_SSH_KEYS not set)"
  else
    ssh_key=""
    for cand in $MACHINE_SETUP_SSH_KEYS; do
      if [ -e "$HOME/$cand" ]; then
        ssh_key="$HOME/$cand"
        break
      fi
    done
    if [ -n "$ssh_key" ]; then
      echo "  ok      $ssh_key (used by local-mac-utils)"
    else
      echo "  WARN    no ssh identity key (used by local-mac-utils; copy one of ~/$(echo "$MACHINE_SETUP_SSH_KEYS" | sed 's| | or ~/|g') by hand — never in git)"
    fi
  fi

  # See HARDEN_DIRS: agent and cloud CLIs drop credentials into these in the
  # clear, and every one of them is created 755.
  loose=""
  for d in $HARDEN_DIRS; do
    hp="$HOME/$d"
    [ -d "$hp" ] || continue
    mode=$(stat -f '%Lp' "$hp")
    [ "$mode" = "700" ] || loose="$loose ~/$d($mode)"
  done
  if [ -z "$loose" ]; then
    echo "  ok      credential dirs are 700 (no other local account can read them)"
  else
    echo "  WARN    world-readable credential dirs:$loose — run: machine-setup.sh update system --apply"
  fi

  aliases=$(missing_home_aliases)
  if [ -z "$aliases" ]; then
    echo "  ok      repo config symlinks resolve"
  else
    for alias_path in $aliases; do
      echo "  WARN    $alias_path missing — config symlinks that name it dangle; run: sudo ln -s $(basename "$HOME") $alias_path"
    done
  fi

  # The environment layer: exports every shell AND every LaunchAgent needs.
  # It lived in ~/.bash_profile, which only login shells read — launchd starts
  # jobs from launchd, so cron ran with none of it and any MCP server
  # authenticating by ${VAR} resolved empty. Split into ~/.config/shell/env.sh,
  # sourced by the profile and by local-cron-jobs/cron-jobs.sh.
  shell_env="$HOME/.config/shell/env.sh"
  if [ ! -f "$shell_env" ]; then
    echo "  WARN    ~/.config/shell/env.sh missing — cron jobs run without any"
    echo "          credential; see local-machine-setup/shell-env.example"
    ex=""
  else
    mode=$(stat -f '%Lp' "$shell_env")
    if [ "$mode" = "600" ]; then
      echo "  ok      ~/.config/shell/env.sh present (600)"
    else
      echo "  WARN    ~/.config/shell/env.sh is $mode, want 600 — run: chmod 600 $shell_env"
    fi
    # Names the example lists that this machine never sets. Names only: the
    # values are live credentials and doctor output gets pasted around.
    # The operator's own list in the config folder wins over the generic
    # one shipped here.
    ex="$CONFIG_DIR/shell-env.example"
    [ -f "$ex" ] || ex="$DIR/shell-env.example"
    if [ -f "$ex" ]; then
      unset_names=$(
        sed -n 's/^\([A-Za-z_][A-Za-z0-9_]*\)=.*/\1/p' "$ex" | sort -u \
        | while read -r n; do
            grep -qE "^[[:space:]]*export[[:space:]]+$n=" "$shell_env" || printf '%s ' "$n"
          done
      )
      if [ -z "$unset_names" ]; then
        echo "  ok      every variable in shell-env.example is set here"
      else
        echo "  --      not set on this machine: $unset_names"
      fi
      # And the other direction. The example is the only tracked record of
      # what a machine needs, so a variable added to env.sh by hand and never
      # written down here is invisible until a rebuild comes up short — one
      # API key was added on one machine and missing from the example for a
      # day. PATH is exported by env.sh on purpose and is not a
      # credential, so it is never expected in the manifest.
      #
      # Only names with a VALUE are matched. An `export FOO=` with nothing
      # after the `=` is the documented way to say "this machine has no
      # business holding FOO" — it carries no credential, so there is nothing
      # for the manifest to record and nothing to report. Reporting them
      # anyway is how doctor spent a day telling this machine to write down an
      # empty token that no code has ever read.
      unrecorded=$(
        grep -oE '^[[:space:]]*export[[:space:]]+[A-Za-z_][A-Za-z0-9_]*=[^[:space:]]' "$shell_env" \
          | sed -E 's/.*export[[:space:]]+//; s/=.$//' | grep -v '^PATH$' | sort -u \
          | while read -r n; do
              grep -qE "^$n=" "$ex" || printf '%s ' "$n"
            done
      )
      if [ -z "$unrecorded" ]; then
        echo "  ok      every variable set here is recorded in shell-env.example"
      else
        echo "  WARN    set here but not in shell-env.example: $unrecorded"
        echo "          add the name (value change-me) so a rebuild gets it"
      fi
    fi
  fi

  # Neither file is ever captured, so a path baked in by hand on one machine
  # cannot be fixed by the repo — a stale bin folder under another account's
  # home sat on PATH here for months. Both are checked: the exports that carried those paths
  # moved to env.sh, so checking only the profile would now find nothing.
  # Report line numbers only: between them they hold ~40 live API keys and
  # doctor output gets pasted around.
  for envfile in "$HOME/.bash_profile" "$shell_env"; do
    [ -f "$envfile" ] || continue
    # -o keeps the match down to the account component, so a flagged line is
    # reported by number and never echoed. $USER and ${USER} are the fix being
    # asked for, Shared is not an account.
    foreign=$(grep -no "$USERS_ROOT"'/[^/"'"'"' ]*' "$envfile" \
      | grep -Ev ":$USERS_ROOT/($(id -un)|\\\$USER|\\\$\{USER\}|Shared)\$" \
      | cut -d: -f1 | sort -un | tr '\n' ' ')
    if [ -z "$foreign" ]; then
      echo "  ok      ~/${envfile#$HOME/} has no other account's home baked in"
    else
      echo "  WARN    ~/${envfile#$HOME/} line(s) $foreign reference a $USERS_ROOT path that is not $HOME — use \$HOME (edit by hand; never captured)"
    fi
  done

  # The same bug in a file that IS captured is worse: it ships to the other
  # machines. A tracked ~/.ssh/config once carried another account's home
  # twice, and tracked .gitconfig copies named a home that was not theirs. A
  # tracked dotfile should name no account at all — ~ and $HOME resolve per
  # machine, and a foreign home only resolves where the system stage
  # symlinks it.
  tracked_bad=""
  for tf in $(find "$DOTFILE_DIR" -type f 2>/dev/null | sort); do
    hits=$(grep -c "$USERS_ROOT"'/[^/"'"'"' ]*' "$tf" 2>/dev/null || true)
    [ "${hits:-0}" -gt 0 ] || continue
    case "$(grep -o "$USERS_ROOT"'/[^/"'"'"' ]*' "$tf" | sort -u)" in
      "$USERS_ROOT/Shared") continue ;;
    esac
    tracked_bad="$tracked_bad ${tf#"$DOTFILE_DIR"/}"
  done
  if [ -z "$tracked_bad" ]; then
    echo "  ok      tracked dotfiles name no account's home"
  else
    echo "  WARN    tracked dotfile(s) bake in a home:$tracked_bad — use ~ (these deploy to every machine on that profile)"
  fi

  # The MCP server set is a decision like any other, and the live configs are
  # untrackable (tokens inline). local-claude keeps a sanitized snapshot in the config folder; this
  # is the drift check for it. Output is server names only — claude.sh status
  # never prints a value.
  claude_sh="$ROOT/local-claude/claude.sh"
  if [ -x "$claude_sh" ]; then
    if mcp_out=$("$claude_sh" status 2>&1); then
      echo "  ok      MCP server set matches the local-claude snapshots"
    else
      echo "$mcp_out" | grep -E '^  (added|removed|changed|WARN|FAIL)' \
        | sed 's/^  /  WARN    MCP /'
      echo "          record it: local-claude/claude.sh capture --apply"
    fi
  fi

  # A failed or flagged job reaches Reports and Today without a channel
  # (sd:3454); notify.sh still serves one-off callers, so a machine without
  # one is a warning, not a failure. A topic left at change-me is no channel.
  notify_env=$(live_env local-notify)
  chans=""
  topic=$(sed -n 's/^[[:space:]]*NTFY_TOPIC=//p' "$notify_env" 2>/dev/null | tr -d '"')
  [ -z "$topic" ] && topic="${NTFY_TOPIC:-}"
  case "$topic" in ""|change-me*|xxx*) ;; *) chans="$chans ntfy" ;; esac
  # EMAIL_FROM only. EMAIL_TO used to be required here too, until the recipient
  # became a constant in notify.sh's send_email — a .env that omits it is now
  # the correct state, and demanding it would report the email channel as
  # unconfigured on a machine where it works. EMAIL_FROM stays because it names
  # the Google account authorized in google_workspace_mcp, which is genuinely
  # per-machine and cannot be baked into the script.
  if [ -f "$notify_env" ] &&
     grep -q '^[[:space:]]*EMAIL_FROM=..*' "$notify_env" 2>/dev/null; then
    chans="$chans email"
  fi
  if [ -n "$chans" ]; then
    echo "  ok      notification channels configured:$chans"
  else
    echo "  WARN    no notification channel configured — notify.sh can reach no one"
    echo "          set NTFY_TOPIC in $notify_env (see local-notify/README.md)"
  fi

  # One-off system state that no stage manages.
  if [ -e /var/db/useLS ]; then
    echo "  ok      /var/db/useLS (ssh agent uses launch services)"
  else
    echo "  WARN    /var/db/useLS missing — run: sudo touch /var/db/useLS"
  fi

  if [ -e "$TIMEOUT_SUDOERS" ]; then
    echo "  ok      sudo timestamp_timeout drop-in present"
  else
    echo "  WARN    $TIMEOUT_SUDOERS missing — run: machine-setup.sh update system --apply"
  fi
  doctor_unattended_sudo
  if command -v defaults >/dev/null 2>&1 && ! appstore_autoupdate_off; then
    echo "  WARN    App Store auto-update on — run: machine-setup.sh update macos --apply"
  fi

  fw=/usr/libexec/ApplicationFirewall/socketfilterfw
  if [ -x "$fw" ]; then
    if "$fw" --getglobalstate 2>/dev/null | grep -q "enabled"; then
      echo "  ok      application firewall enabled"
    else
      echo "  WARN    application firewall OFF — System Settings > Network > Firewall"
    fi
    if "$fw" --getstealthmode 2>/dev/null | grep -q "on"; then
      echo "  ok      firewall stealth mode on"
    else
      echo "  WARN    firewall stealth mode off"
    fi
  fi

  # Two bugs lived in one line here.
  #
  # The pattern was */homebrew/*bash, which only ever matches the Apple Silicon
  # prefix — brew's bash on an Intel machine is /usr/local/bin/bash, so that
  # machine was told to chsh to a shell it already could not satisfy, and the
  # warning would have survived the switch. Both prefixes are matched now.
  #
  # brew bash is what this repo converges on — the `shell` stage installs it,
  # adds it to /etc/shells and chsh's to it. So that is the ok case, and a
  # machine not yet on it should run: update shell --apply.
  #
  # zsh is nevertheless not a finding by itself. The reason for the old advice
  # was that the environment — PATH extras, ~40 exported keys — lived in
  # ~/.bash_profile, which only a bash login shell reads; that set moved to
  # ~/.config/shell/env.sh and ~/.zshenv loads it, so a zsh login shell reaches
  # the same environment. A machine mid-conversion, or one deliberately left on
  # zsh, is working — warning about it would report a machine that is fine.
  #
  # What is worth a warning is a login shell that cannot reach the credential
  # layer at all, which is the case the old check could not express: it passed
  # any */homebrew/*bash and warned about everything else, so a zsh with no
  # env.sh in ~/.zshenv — every credential invisible — read the same as a
  # correctly configured one.
  shell=$(dscl . -read "$DS_USERS/$(id -un)" UserShell 2>/dev/null | awk '{print $2}')
  case "$shell" in
    /opt/homebrew/bin/bash|/usr/local/bin/bash)
      echo "  ok      login shell is brew bash ($shell)" ;;
    */zsh)
      if grep -q 'config/shell/env.sh' "$HOME/.zshenv" 2>/dev/null; then
        echo "  ok      login shell is zsh, and ~/.zshenv loads the credential layer"
      else
        echo "  WARN    login shell is $shell but ~/.zshenv does not load ~/.config/shell/env.sh — every credential in it is invisible to anything started from a terminal"
      fi ;;
    *) echo "  WARN    login shell is $shell — expected brew bash, or a zsh whose ~/.zshenv loads ~/.config/shell/env.sh" ;;
  esac

  if command -v fnm >/dev/null 2>&1; then
    for v in $(node_versions); do
      if fnm ls 2>/dev/null | grep -q "v$v\."; then
        echo "  ok      fnm node $v installed"
      else
        echo "  WARN    fnm node $v missing — run: fnm install $v"
      fi
    done
  fi

  if command -v rtk >/dev/null 2>&1; then
    if grep -q rtk "$HOME/.claude/settings.json" 2>/dev/null; then
      echo "  ok      rtk hook wired into ~/.claude/settings.json"
    else
      echo "  WARN    rtk installed but hook not wired — run: rtk init -g"
    fi
  fi

  if defaults read com.googlecode.iterm2 LoadPrefsFromCustomFolder 2>/dev/null | grep -q 1; then
    pf=$(defaults read com.googlecode.iterm2 PrefsCustomFolder 2>/dev/null || :)
    if [ -f "$pf/com.googlecode.iterm2.plist" ]; then
      echo "  ok      iTerm2 loads prefs from custom folder ($pf)"
    else
      echo "  WARN    iTerm2 points at $pf but no com.googlecode.iterm2.plist is there"
    fi
  else
    # Whether prefs follow a synced folder is a per-machine decision recorded in
    # <profile>.iterm2, so reading it needs the profile — which doctor only
    # loads further down. Resolve it here, under the same guard, so a fresh
    # machine with no profile still just warns.
    if [ -f "$STATE_FILE" ] || [ -n "${MACHINE_SETUP_PROFILE:-}" ]; then
      load_profile
    fi
    if [ -n "${PROFILE:-}" ] && [ -z "$(manifest iterm2 | head -1)" ]; then
      # No folder in the profile is a decision, not a gap — the iterm2 stage
      # says the same thing and returns 0. Warning here contradicted it.
      echo "  ok      iTerm2 prefs stay local (no folder in the $PROFILE profile)"
    else
      echo "  WARN    iTerm2 not loading prefs from a custom folder — run: machine-setup.sh update iterm2 --apply"
    fi
  fi

  # Launchd state of everything the profile says should be running.
  if [ -f "$STATE_FILE" ] || [ -n "${MACHINE_SETUP_PROFILE:-}" ]; then
    load_profile
    uid=$(id -u)
    labels=$(manifest agent)
    # The cron stage's list: the profile's jobs and this host's, less the
    # retired ones. Doctor read the profile alone (sd:3548).
    if ! dr_host=$(host_cron_jobs); then
      echo "  WARN    cannot list this host's cron jobs (cron-jobs/jobs/<host>/); only the profile's are checked"
      dr_host=
    fi
    jobs=$( { manifest cron; printf '%s\n' "$dr_host"; } | awk 'NF && !seen[$0]++' \
      | { grep -vxF "$(retired_cron_jobs)" || :; } | awk -v p="$LABEL_PREFIX.cron." '{ print p $0 }')
    for label in $labels $jobs; do
      # The cron and agents stages' guard: a satellite never loads these, so
      # not loaded is the wanted state, not a warning (sd:4797).
      if sd_hub_only "$label" && is_satellite && ! hub_holder; then
        echo "  SKIP    $label — hub only, and this machine is a satellite"
      elif launchctl print "gui/$uid/$label" >/dev/null 2>&1; then
        echo "  ok      loaded: $label"
      else
        echo "  WARN    not loaded: $label"
      fi
    done
  fi

  # The database, the dashboard's front door and the
  # installed library versions. Loaded-or-not for the agents is above;
  # this is whether what they run against is sound.
  doctor_sd
  doctor_retired_sd_db

  echo
  echo "  folders without a filled .env (.env.example present, .env absent):"
  miss=0
  for d in "$ROOT"/*/; do
    [ -f "$d/.env.example" ] || continue
    if [ ! -f "$(live_env "$(basename "$d")")" ]; then
      echo "    --      $(basename "$d")/.env  (copy .env.example to $SYSTEM_TOOLS_CONFIG/$(basename "$d" | sed 's/^local-//')/.env if exported env or defaults are not enough)"
      miss=$((miss + 1))
    fi
  done
  [ "$miss" -eq 0 ] && echo "    none — every .env.example has its .env"

  # Presence is not configuration. A .env copied from the example and never
  # filled in reads as done to the check above, then fails at runtime with a
  # 401 or a DNS error far from the cause — which is exactly how one tool's
  # .env once sat holding all-zero UUIDs.
  #
  # MACHINE_SETUP_PLACEHOLDER_EXPECTED exempts a folder, deliberately, when
  # its .env names things that exist only on another account: on this machine
  # there is nothing real to put there and never will be, so the warning could
  # not be actioned and could not clear. Exempt a folder only when the same
  # two things are true: the placeholders are correct for this machine, and
  # the tool rejects placeholder values at startup, naming the variable, so
  # something fails loudly if they are ever actually used.
  echo
  echo "  .env files still holding placeholder values:"
  ph=0
  for d in "$ROOT"/*/; do
    denv=$(live_env "$(basename "$d")")
    [ -f "$denv" ] || continue
    case " $PLACEHOLDER_EXPECTED " in *" $(basename "$d") "*) continue ;; esac
    n=$(grep -cE '=[[:space:]]*"?(change-me|changeme|CHANGE_ME|your-|xxx+|placeholder|0{8}-0{4}-0{4}-0{4}-0{12}|0{8,})' "$denv" 2>/dev/null || true)
    n=${n:-0}
    if [ "$n" -gt 0 ]; then
      tot=$(grep -cE '^[[:space:]]*[A-Za-z_][A-Za-z0-9_]*=' "$denv" 2>/dev/null || true)
      echo "    WARN    $(basename "$d")/.env — $n of ${tot:-?} value(s) still placeholder"
      ph=$((ph + 1))
    fi
  done
  [ "$ph" -eq 0 ] && echo "    none — every .env that exists has real values"

  echo
  if [ "$fails" -gt 0 ]; then
    echo "  $fails hard failure(s)"
    exit 1
  fi
  echo "  no hard failures"
}

# Every brew, mas, curl and remote git call runs bounded (sd:2660, sd:3536):
# it logs the step as it starts and stops it after SECONDS, or after
# MACHINE_SETUP_STEP_TIMEOUT seconds when that is set. A call that hangs then
# names itself in the log and ends, where it used to run until the job's limit
# killed the whole job. A call to change the machine goes through `run
# bound_step N ...` or run_step, which keep the dry run; a read calls
# bound_step itself. A run from a terminal is not bounded (ATTENDED above).
# lib/bounded.sh is sourced on first use, not at the top:
# suites run copies of this script with only lib/config.sh beside them.
bound_step() { # seconds, command...
  _step_seconds=${MACHINE_SETUP_STEP_TIMEOUT:-$1}
  shift
  if [ "$ATTENDED" -eq 1 ]; then "$@" </dev/null; return; fi
  command -v st_step >/dev/null 2>&1 || . "$ROOT/lib/bounded.sh"
  # shellcheck disable=SC2034 # read by st_step in lib/bounded.sh
  (ST_STEP_FD3=1; st_step "$_step_seconds" "$@" </dev/null 3>&4)
}

# bound_step for the upgrade sweep, which prints its own dry run.
run_step() { # seconds, command...
  if [ "$APPLY" -eq 1 ]; then
    bound_step "$@"
  else
    shift
    printf '  [dry-run] %s\n' "$*"
  fi
}

# The cask claude, and each file in the codex cask's bin/, keeps no
# Gatekeeper quarantine that a launchd job would wait on, cleared only when
# its checks pass: lib/claude_quarantine.sh (sd:4787, sd:7095), which the
# cron runner runs for claude too (sd:4815). Sourced on first use like
# lib/bounded.sh. A check runs bounded and the clear keeps the dry run.
cq_check() { bound_step "$@"; }
cq_clear() { try_run xattr -d com.apple.quarantine "$1"; }
claude_quarantine() {
  # shellcheck source=/dev/null
  command -v st_claude_quarantine >/dev/null 2>&1 || . "$ROOT/lib/claude_quarantine.sh"
  st_claude_quarantine "$(command -v claude 2>/dev/null)"
}
codex_quarantine() {
  # shellcheck source=/dev/null
  command -v st_codex_quarantine >/dev/null 2>&1 || . "$ROOT/lib/claude_quarantine.sh"
  st_codex_quarantine "$(command -v codex 2>/dev/null)"
}

# An upgrade step: run_step, and a failure adds NAME to $failed. A cask with a
# pkg installer, and mas, call sudo; an unattended run has no terminal for its
# password, so the step fails with sudo's own refusal. That step is deferred
# like sudo_run's, not failed, and the report names what to run in a terminal
# (sd:5046). Any other failure, and a step stopped at its bound, stays failed.
# A failure that prints an `Error:` line sudo did not cause, such as a formula
# whose build fails, is also recorded in BUILD_FAILED as `kind line`: the
# report names that line, not sudo, for what the step left (sd:7056). A step
# can hold both, as `brew upgrade` also upgrades casks.
# The bound of a sweep step that may build from source: 6 h, twice the one
# run measured, `brew install node` on an Intel Mac with no bottles: 3h00m,
# llvm 1h31m of it (sd:7103). The brew stage builds nothing from source.
SOURCE_BUILD_BOUND=21600
SUDO_REFUSED='sudo: a terminal is required|sudo: a password is required|sudo: no tty present'
BUILD_FAILED=""

# build_error FILE...: the first `Error:` line that does not name sudo, with
# the line after it when it ends in a colon; empty when there is none.
build_error() {
  cat "$@" | awk '
    /^Error:/ && tolower($0) !~ /sudo/ && !n { n = 1; line = $0; want = ($0 ~ /:$/); next }
    want { sub(/^[ \t]+/, ""); if ($0 != "") { line = line " " $0; want = 0 } }
    END { print line }'
}

# build_failure KIND: the error line a step of that kind recorded, or nothing.
build_failure() {
  printf '%s' "$BUILD_FAILED" | sed -n "s/^$1 //p" | head -n 1
}

sweep_step() { # name, seconds, command...
  ss_name=$1; shift
  if [ "$ATTENDED" -eq 1 ]; then
    run_step "$@" || failed="$failed, $ss_name"
    return 0
  fi
  ss_out=$(mktemp)
  ss_rc=0
  run_step "$@" > "$ss_out.1" 2> "$ss_out.2" || ss_rc=$?
  cat "$ss_out.1"; cat "$ss_out.2" >&2
  # The command without its bound: matched on the bound, every error was a
  # formula's (sd:7071).
  shift
  ss_err=
  if [ "$ss_rc" -ne 0 ] && [ "$ss_rc" -ne 124 ]; then
    ss_err=$(build_error "$ss_out.1" "$ss_out.2")
  fi
  if [ -n "$ss_err" ]; then
    case "$*" in
      mas\ *) ss_kind=mas ;;
      brew\ upgrade\ --cask*) ss_kind=cask ;;
      *) ss_kind=formula ;;
    esac
    BUILD_FAILED="$BUILD_FAILED$ss_kind $ss_err
"
    failed="$failed, $ss_name"
  fi
  if [ "$ss_rc" -ne 0 ] && [ "$ss_rc" -ne 124 ] && cat "$ss_out.1" "$ss_out.2" | grep -Eq "$SUDO_REFUSED"; then
    DEFERRED="$DEFERRED    $*
"
    echo "  DEFER   needs sudo: $*"
  elif [ "$ss_rc" -ne 0 ] && [ -z "$ss_err" ]; then
    failed="$failed, $ss_name"
  fi
  rm -f "$ss_out" "$ss_out.1" "$ss_out.2"
}

# 0 when the login Keychain holds SUDO_KEYCHAIN, 1 when it does not, 2 when
# `security` gave no answer (its code in sk_rc): a failed probe is no "no".
# It reads the item's attributes, never its password.
sudo_keychain_item() {
  sk_rc=0
  bound_step 30 "$SECURITY" find-generic-password -a "$(id -un)" -s "$SUDO_KEYCHAIN" \
    > /dev/null 2>&1 < /dev/null || sk_rc=$?
  case $sk_rc in
    0) return 0 ;;
    44) return 1 ;;  # errSecItemNotFound
    *) return 2 ;;
  esac
}

# The casks that need sudo and that brew calls outdated, upgraded with
# SUDO_ASKPASS set to sudo-askpass.sh (sd:6312): an unattended run has no
# terminal for sudo's password, and the Keychain item holds it. Only on a
# run with no terminal; without the item, the greedy sweep after it defers
# them for sudo, as before. sudo_casks names them (sd:6927).
askpass_upgrade() {
  [ "$APPLY" -eq 1 ] && [ "$ATTENDED" -eq 0 ] || return 0
  if ! sudo_keychain_item; then
    echo "  --      no askpass: Keychain item $SUDO_KEYCHAIN not read (security exit $sk_rc)"
    return 0
  fi
  if ! au_want=$(sudo_casks); then
    failed="$failed, brew cask info"
    return 0
  fi
  [ -n "$au_want" ] || return 0
  if ! au_out=$(bound_step 300 brew outdated --cask --greedy --quiet 2>/dev/null); then
    failed="$failed, brew cask list"
    return 0
  fi
  au_casks=$(printf '%s\n' "$au_out" | drop_brew_skipped | awk -v want="$au_want" '
      BEGIN { n = split(want, w, " "); for (i = 1; i <= n; i++) ok[w[i]] = 1 }
      { name = $1; sub(/.*\//, "", name); if (name in ok) print name }' | tr '\n' ' ' | sed 's/ $//')
  [ -n "$au_casks" ] || return 0
  SUDO_ASKPASS="$DIR/sudo-askpass.sh"
  export SUDO_ASKPASS
  # shellcheck disable=SC2086 # one word per cask name
  sweep_step "brew upgrade --cask (askpass)" 1800 brew upgrade --cask --greedy $au_casks
  unset SUDO_ASKPASS
}

# sudo_casks: the installed casks whose upgrade calls sudo, space-separated
# (sd:6927): a pkg installer or a pkgutil uninstall, or an artifact whose
# target lies outside the brew prefix, $HOME and /Applications, such as
# docker-desktop's /usr/local/bin on Apple silicon. Plus the optional
# MACHINE_SETUP_SUDO_CASKS, for a cask the rule misses. Exit 1 when brew
# info does not answer.
sudo_casks() {
  sc_prefix=$(bound_step 60 brew --prefix 2>/dev/null) || return 1
  sc_info=$(bound_step 300 brew info --json=v2 --installed --cask 2>/dev/null) || return 1
  printf '%s' "$sc_info" | python3 -c '
import json, sys
extra = sys.argv[1].split() if sys.argv[1] != "change-me" else []
roots = [r.rstrip("/") + "/" for r in sys.argv[2:] if r]
def needs_sudo(artifacts):
    for a in artifacts:
        if not isinstance(a, dict):
            continue
        if "pkg" in a:
            return True
        if any(isinstance(u, dict) and "pkgutil" in u for u in a.get("uninstall", [])):
            return True
        t = a.get("target")
        if isinstance(t, str) and t.startswith("/") and not any(t.startswith(r) for r in roots):
            return True
    return False
casks = [c["token"] for c in json.load(sys.stdin).get("casks", []) if needs_sudo(c.get("artifacts", []))]
print(" ".join(casks + [c for c in extra if c not in casks]))' \
    "${MACHINE_SETUP_SUDO_CASKS:-}" "$sc_prefix" "$HOME" /Applications
}

# Doctor's lines for unattended sudo (sd:6312), on each machine: the operator
# makes the Keychain item and installs the repair drop-in there. A missing one
# is a finding naming that step, not a failure.
doctor_unattended_sudo() {
  if sudo_keychain_item; then
    echo "  ok      Keychain item $SUDO_KEYCHAIN (sudo askpass)"
  elif [ "$sk_rc" -eq 44 ]; then
    echo "  WARN    no Keychain item $SUDO_KEYCHAIN — run in a terminal: security add-generic-password -a \"\$(id -un)\" -s $SUDO_KEYCHAIN -T /usr/bin/security -w"
  else
    echo "  WARN    Keychain item $SUDO_KEYCHAIN unknown: security exited $sk_rc"
  fi
  if [ ! -e "$REPAIRS_SUDOERS" ]; then
    echo "  WARN    no repair sudoers drop-in at $REPAIRS_SUDOERS — install local-machine-setup/machine-setup-repairs.sudoers.example with visudo -cf (README: Unattended sudo)"
    return 0
  fi
  # Its brew line must name this machine's brew: one copied from an Apple
  # silicon Mac names /opt/homebrew, absent on an Intel Mac (sd:6475). sudo
  # lists the user's rules; `sudo -n -l <command>` passes any command that
  # (ALL) ALL allows, password or not.
  ds_brew=$(bound_step 60 brew --prefix 2>/dev/null) || ds_brew=
  if [ -z "$ds_brew" ]; then
    echo "  WARN    repair sudoers drop-in $REPAIRS_SUDOERS unknown: brew --prefix gave no answer"
    return 0
  fi
  ds_cmd="$ds_brew/bin/brew services restart tailscale"
  ds_rc=0
  ds_list=$(bound_step 30 sudo -n -l 2>/dev/null) || ds_rc=$?
  if [ "$ds_rc" -ne 0 ]; then
    echo "  WARN    repair sudoers drop-in $REPAIRS_SUDOERS unknown: sudo -n -l exited $ds_rc"
  elif tailscale_runs_app; then
    # The app's daemon is no brew service: the line repairs nothing here
    # (sd:6475), so its absence is ok and its presence a dead line.
    if printf '%s\n' "$ds_list" | grep -qF "services restart tailscale"; then
      echo "  WARN    repair sudoers drop-in $REPAIRS_SUDOERS allows brew services restart tailscale, dead with the tailscale-app cask — drop that line: sudo visudo -f $REPAIRS_SUDOERS"
    else
      echo "  ok      repair sudoers drop-in $REPAIRS_SUDOERS (tailscale-app: no brew restart line)"
    fi
  elif printf '%s\n' "$ds_list" | awk -v want="NOPASSWD: $ds_cmd" '{ sub(/[ \t]+$/, "") }
      substr($0, length($0) - length(want) + 1) == want { found = 1 } END { exit !found }'; then
    echo "  ok      repair sudoers drop-in $REPAIRS_SUDOERS"
  else
    echo "  WARN    repair sudoers drop-in $REPAIRS_SUDOERS does not allow $ds_cmd without a password — fix its brew path: sudo visudo -f $REPAIRS_SUDOERS"
  fi
}

# Maintenance sweep: refresh what is already installed. Dry run by default
# like every other mutating verb. A step that fails or times out does not stop
# the sweep; the steps after it still run, and the sweep exits 1 naming it.
# A step that needs sudo is deferred, not failed (sweep_step).
cmd_upgrade() {
  echo "upgrade : brew, App Store and .release maintenance sweep"
  [ "$APPLY" -eq 1 ] || echo "mode    : DRY RUN — nothing will change; re-run with --apply"
  echo
  failed=""
  if command -v brew >/dev/null 2>&1; then
    run_step 600 brew update || failed="$failed, brew update"
    # The brew stage's unpin (sd:6433) first: the weekly sweep can run
    # before the nightly update --apply, and a stale python pin fails
    # `brew upgrade` on a dependent that needs a newer one (sd:6448).
    if pins=$(stale_pins); then
      for f in $pins; do
        echo "  DIFFERS $f pinned"
        run_step 300 brew unpin "$f" || failed="$failed, brew unpin $f"
      done
    else
      failed="$failed, brew pin list"
    fi
    sweep_step "brew upgrade" "$SOURCE_BUILD_BOUND" brew upgrade
    # The profile's missing formulae: the brew stage leaves one that needs a
    # source build to this sweep (sd:7103).
    fw=$(formula_wants) || failed="$failed, brew formula list"
    # shellcheck disable=SC2086 # one word per formula name
    [ -z "$fw" ] || sweep_step "brew install" "$SOURCE_BUILD_BOUND" brew install $fw
    python_firewall || failed="$failed, python firewall"
    # --greedy takes casks with `auto_updates true` too, whose own updaters
    # stage_macos turns off where the app holds a TCC grant (sd:3062). It
    # moves Claude Code too, the claude-code@latest cask, whose own updater
    # the settings baseline turns off (sd:3033, sd:4738).
    # The cask names are listed, not left to brew, so that
    # BREW_SKIPPED_CASKS stay out of the sweep (sd:7058).
    askpass_upgrade
    if cu_out=$(bound_step 300 brew outdated --cask --greedy --quiet 2>/dev/null); then
      cu_casks=$(printf '%s\n' "$cu_out" | drop_brew_skipped | tr '\n' ' ' | sed 's/ $//')
      # shellcheck disable=SC2086 # one word per cask name
      [ -z "$cu_casks" ] || sweep_step "brew upgrade --cask --greedy" 1800 brew upgrade --cask --greedy $cu_casks
    else
      failed="$failed, brew cask list"
    fi
    claude_quarantine || failed="$failed, claude quarantine"
    codex_quarantine || failed="$failed, codex quarantine"
    run_step 600 brew cleanup --prune=all || failed="$failed, brew cleanup"
  else
    echo "  brew MISSING — https://brew.sh"
  fi
  if command -v mas >/dev/null 2>&1; then
    sweep_step "mas upgrade" 1200 mas upgrade
  else
    echo "  mas MISSING (brew install mas)"
  fi
  release_sweep
  echo
  report_deferred upgrade
  echo "  afterwards: $(basename "$0") status   # drift check"
  if [ -n "$failed" ]; then
    echo "  failed steps: ${failed#, }"
    return 1
  fi
}

# outdated_lists before|after: what brew and mas call outdated, one sorted
# file per kind under $tmp. Each query is a bounded step; one that fails or
# times out leaves its file empty and names its kind in unknown.<when>: an
# unknown list is not "nothing outdated" (sd:3536). Casks are asked with
# --greedy, as cmd_upgrade upgrades them, so a self-updating cask is in both.
outdated_lists() {
  : > "$tmp/unknown.$1"
  run_step 300 brew outdated --formula --quiet > "$tmp/raw" 2>/dev/null || echo formula >> "$tmp/unknown.$1"
  sort "$tmp/raw" > "$tmp/formula.$1"
  run_step 300 brew outdated --cask --greedy --quiet > "$tmp/raw" 2>/dev/null || echo cask >> "$tmp/unknown.$1"
  drop_brew_skipped < "$tmp/raw" | sort > "$tmp/cask.$1"
  : > "$tmp/mas.$1"
  if command -v mas >/dev/null 2>&1; then
    run_step 300 mas outdated > "$tmp/raw" 2>/dev/null || echo mas >> "$tmp/unknown.$1"
    sort "$tmp/raw" > "$tmp/mas.$1"
  fi
  # The .release entries whose latest tag is not the one installed (sd:4853).
  release_outdated > "$tmp/raw" || echo release >> "$tmp/unknown.$1"
  awk '$2 != "unknown" { print $1 }' "$tmp/raw" | sort > "$tmp/release.$1"
}

# sudo_kind formula|cask|mas: true when the sweep deferred that kind's upgrade
# for sudo (sweep_step); its leftovers are then commands to run in a terminal.
sudo_kind() {
  case $1 in
    formula) printf '%s' "$DEFERRED" | grep -Eq '^    brew upgrade($| [^-])' ;;
    cask) printf '%s' "$DEFERRED" | grep -q '^    brew upgrade --cask' ;;
    mas) printf '%s' "$DEFERRED" | grep -q '^    mas upgrade' ;;
    *) return 1 ;;
  esac
}

# sudo_commands formula|cask|mas: each leftover of that kind as the command
# that upgrades it. A `mas outdated` line is `<id> <name> (<versions>)`.
sudo_commands() {
  case $1 in
    formula) sed 's/^/brew upgrade /' "$tmp/formula.after" ;;
    cask) sed 's/^/brew upgrade --cask /' "$tmp/cask.after" ;;
    mas) awk '{ id = $1; $1 = ""; sub(/^ /, "")
                if (id ~ /^[0-9]+$/) print "mas upgrade " id "  # " $0; else print "mas upgrade  # " id " " $0 }' "$tmp/mas.after" ;;
  esac
}

# grant_paths before|after: the paths whose change drops a macOS grant, one
# file per kind under $tmp. The claude-code@latest cask links `claude` on PATH
# to a versioned Caskroom binary, and `brew upgrade python` moves the Cellar
# path (.claude/rules/macos-tcc.md).
grant_paths() {
  readlink "$(command -v claude 2>/dev/null)" > "$tmp/claude.$1" 2>/dev/null || :
  : > "$tmp/python.$1"
  if command -v brew >/dev/null 2>&1; then
    _cellar=$(bound_step 60 brew --cellar 2>/dev/null) || _cellar=""
    if [ -n "$_cellar" ]; then
      ls -d "$_cellar"/python@*/* 2>/dev/null | sort > "$tmp/python.$1" || :
    fi
  fi
}

# The AI-app inventory, captured after the upgrades and before the grants
# probe, in both paths: the ai-apps-nightly job is retired and this run takes
# its place (sd:3062). Writes $tmp/inventory, the mail's section, and returns
# capture's code; the caller reports a failure and goes on to the probe.
inventory_capture() {
  i_rc=0
  run_step 600 sh "$ROOT/local-ai-apps/ai-apps.sh" capture > "$tmp/inventory.out" 2>&1 || i_rc=$?
  {
    if [ "$i_rc" -eq 0 ]; then
      printf '\nai-apps inventory:\n'
    else
      printf '\nai-apps inventory capture FAILED (exit %s); the inventory was not rewritten:\n' "$i_rc"
    fi
    sed 's/^/  /' "$tmp/inventory.out"
  } > "$tmp/inventory"
  return "$i_rc"
}

# After the sweep, in both paths (sd:3062): run the dashboard's vault-grant
# probe and write $tmp/grants, the mail's section. $tmp/regrant gets one line
# per binary that lost Full Disk Access, and $tmp/changed the grant droppers
# that moved. This job runs under launchd, where the probe's answer is the
# binary's own. A missing grant is a push, not a failed job.
grants_report() {
  grant_paths after
  : > "$tmp/changed"
  if [ -s "$tmp/claude.after" ] && ! cmp -s "$tmp/claude.before" "$tmp/claude.after"; then
    printf '  Claude Code: %s -> %s\n' "$(cat "$tmp/claude.before")" "$(cat "$tmp/claude.after")" >> "$tmp/changed"
  fi
  comm -13 "$tmp/python.before" "$tmp/python.after" | sed 's/^/  new python Cellar path: /' >> "$tmp/changed"
  g_rc=0
  run_step 300 sh "$SD_PACK_ROOT/dashboard/dashboard.sh" grants > "$tmp/grants.out" 2>&1 || g_rc=$?
  sed -n 's/.*Grant Full Disk Access to \(.*\) in System Settings.*/  Full Disk Access: \1/p' \
    "$tmp/grants.out" > "$tmp/regrant"
  {
    printf '\nTCC grants (dashboard.sh grants, exit %s):\n' "$g_rc"
    sed 's/^/  /' "$tmp/grants.out"
    if [ -s "$tmp/changed" ]; then
      printf '\ngrant droppers that changed — check their grants:\n'
      cat "$tmp/changed"
    fi
  } > "$tmp/grants"
}

# When a grant is missing, print what to re-grant; the caller's
# SD_REPORT_ATTENTION line takes the run's report to Reports and Today (sd:3454).
grants_push() {
  [ -s "$tmp/regrant" ] || return 0
  {
    printf 'Re-grant in System Settings > Privacy & Security on %s:\n' "$(hostname -s)"
    cat "$tmp/regrant"
    if [ -s "$tmp/changed" ]; then
      printf '\nChanged this run:\n'
      cat "$tmp/changed"
    fi
  } > "$tmp/push"
  cat "$tmp/push"
}

# One SD_REPORT_ATTENTION line per finding: each line of $tmp/findings (what
# is left, and a failed step with a checksum of its output) and each grant to
# redo, not the date. It leaves out $tmp/changed, the update that dropped a
# grant: it names only the night of the update, and the grant stays to redo
# after it. The ingest's reason is the run's set of lines: the same set folds
# into the open report, a smaller one folds into it too (pack sd:4658), and a
# new finding opens its own report and closes the older one on this host, with
# the exceptions local-cron-jobs/README.md names (pack sd:4641). The host
# stays in each line, as every host files under one job name.
upgrade_attention() {
  { cat "$tmp/findings"; sed 's/^ */TCC re-grant needed: /' "$tmp/regrant"; } |
    awk -v p="SD_REPORT_ATTENTION: upgrade on $(hostname -s): " 'NF { print p $0 }'
}

# Cron flavor of upgrade: forces --apply, diffs the outdated lists before and
# after the sweep, and prints what got upgraded / what failed / what is still
# pending, with the AI-app inventory capture and the TCC grants probe's
# result. When nothing is outdated it only captures the inventory and probes
# the grants. Leftovers, a failed capture or a missing grant print an
# SD_REPORT_ATTENTION line, so the run's report reaches Reports and Today;
# nothing is mailed (sd:3454). Exits 1 when the quiet-week capture failed.
cmd_upgrade_report() {
  APPLY=1
  tmp=$(mktemp -d)
  trap_tmp
  # fd 3 is the job log. The sweep's output goes into the report, and each step
  # names itself here as it starts, so a hang names its step.
  exec 3>&2
  # shellcheck disable=SC2034 # read by st_step in lib/bounded.sh
  ST_STEP_FD3=1

  # `brew update` first so the outdated snapshot is accurate; cmd_upgrade
  # repeats it, but the second run is a fast no-op.
  run_step 600 brew update >/dev/null 2>&1 || :
  outdated_lists before
  grant_paths before
  # A formula the brew stage left to this sweep is no quiet week (sd:7103).
  formula_wants > "$tmp/wants.before" || echo missing >> "$tmp/unknown.before"

  if [ ! -s "$tmp/formula.before" ] && [ ! -s "$tmp/cask.before" ] \
      && [ ! -s "$tmp/mas.before" ] && [ ! -s "$tmp/release.before" ] \
      && [ ! -s "$tmp/wants.before" ] && [ ! -s "$tmp/unknown.before" ]; then
    rc=0
    echo "nothing outdated — no upgrade"
    inventory_capture || rc=1
    cat "$tmp/inventory"
    grants_report
    cat "$tmp/grants"
    grants_push
    : > "$tmp/findings"
    upgrade_attention
    return "$rc"
  fi

  rc=0
  cmd_upgrade > "$tmp/out" 2>&1 || rc=1
  cat "$tmp/out"
  inv_rc=0
  inventory_capture || inv_rc=1

  outdated_lists after
  : > "$tmp/wants.after"
  [ ! -s "$tmp/wants.before" ] || formula_wants > "$tmp/wants.after" || echo missing >> "$tmp/unknown.after"
  grants_report

  {
    printf 'machine-setup upgrade report — %s on %s\n' \
      "$(date '+%Y-%m-%d %H:%M')" "$(hostname -s)"
    for kind in formula cask mas release; do
      # A list that did not answer, before or after, has no upgraded diff.
      if grep -qx "$kind" "$tmp/unknown.before" "$tmp/unknown.after"; then
        printf '\noutdated (%s): unknown, the list did not answer\n' "$kind"
        continue
      fi
      comm -23 "$tmp/$kind.before" "$tmp/$kind.after" > "$tmp/$kind.done"
      if [ -s "$tmp/$kind.done" ]; then
        printf '\nupgraded (%s):\n' "$kind"
        sed 's/^/  /' "$tmp/$kind.done"
      fi
      bf=$(build_failure "$kind")
      if [ -s "$tmp/$kind.after" ] && [ -n "$bf" ]; then
        printf '\nbuild failure (%s) — %s:\n' "$kind" "$bf"
        sed 's/^/  /' "$tmp/$kind.after"
      elif [ -s "$tmp/$kind.after" ] && sudo_kind "$kind"; then
        printf '\nneeds sudo (%s) — run in a terminal:\n' "$kind"
        sudo_commands "$kind" | sed 's/^/  /'
      elif [ -s "$tmp/$kind.after" ]; then
        printf '\nstill outdated (%s) — held back or failed:\n' "$kind"
        sed 's/^/  /' "$tmp/$kind.after"
      fi
    done
    # A snapshot that did not answer leaves out what it could not tell:
    # diffed, a failed install read as installed (sd:7132).
    if grep -qx missing "$tmp/unknown.before" "$tmp/unknown.after"; then
      printf '\nmissing (formula): unknown, the list did not answer\n'
      : > "$tmp/wants.done"
      : > "$tmp/wants.after"
    else
      comm -23 "$tmp/wants.before" "$tmp/wants.after" > "$tmp/wants.done"
    fi
    if [ -s "$tmp/wants.done" ]; then
      printf '\ninstalled (formula):\n'
      sed 's/^/  /' "$tmp/wants.done"
    fi
    if [ -s "$tmp/wants.after" ]; then
      printf '\nstill missing (formula):\n'
      sed 's/^/  /' "$tmp/wants.after"
    fi
    if [ "$rc" -ne 0 ]; then
      printf '\nupgrade sweep exited nonzero; last output:\n'
      tail -30 "$tmp/out" | sed 's/^/  /'
    fi
    cat "$tmp/inventory"
    cat "$tmp/grants"
  } > "$tmp/body"

  cat "$tmp/body"
  grants_push
  # A clean upgrade is a receipt, not a problem: leftovers, a failed sweep
  # step (brew cleanup) or a failed capture need attention.
  # One finding per line: each package left under its list's name, and a
  # failed step with a checksum of its output without its step lines, which
  # carry the time of day.
  unknown=$(sort -u "$tmp/unknown.before" "$tmp/unknown.after" | tr '\n' ' ' | sed 's/ $//')
  {
    for kind in formula cask mas release; do
      bf=$(build_failure "$kind")
      if [ -n "$bf" ]; then
        awk -v k="$kind" -v e="$bf" '{ print "build failure (" k "): " $0 ": " e }' "$tmp/$kind.after"
      elif sudo_kind "$kind"; then
        sudo_commands "$kind" | sed 's/^/needs sudo, run in a terminal: /'
      else
        sed "s/^/still outdated ($kind): /" "$tmp/$kind.after"
      fi
    done
    sed 's/^/still missing (formula): /' "$tmp/wants.after"
    if [ "$rc" -ne 0 ]; then
      echo "upgrade sweep failed [$(tail -30 "$tmp/out" | grep -v '^\[step ' | cksum | cut -d' ' -f1)]"
    fi
    if [ "$inv_rc" -ne 0 ]; then
      echo "inventory capture failed [$(grep -v '^\[step ' "$tmp/inventory.out" | cksum | cut -d' ' -f1)]"
    fi
    [ -z "$unknown" ] || echo "outdated list failed ($unknown)"
  } > "$tmp/findings"
  upgrade_attention
}

# Manual new-machine steps that no stage can automate. Doctor checks the
# starred ones.
cmd_checklist() {
  cat <<'CHECKEOF'
manual setup checklist (nothing here is automated; * = doctor checks it)

accounts and cloud
  - Apple ID and internet accounts
  - cloud drive sync and browser logins, extensions and flags
  - password manager: login, add browser integration

system  (useLS, firewall/stealth, sudo grace period, soft SMB mounts and the mailto handler
are the `system` and `tooling` stages — run `update system --apply` /
`update tooling --apply` instead of doing them by hand)
  - login shell: the `shell` stage converges zsh -> brew bash (installs
    brew and its bash if missing, /etc/shells, chsh) — runs in setup, or
    alone: `machine-setup.sh update shell --apply`. Leaving zsh is also
    fine: ~/.zshenv loads the same ~/.config/shell/env.sh
  - network volumes and printers

notifications  (nothing provisions these: the ntfy topic doubles as the
secret on the public server, so it is never committed)
  - NTFY_TOPIC into local-notify's .env, 0600 —  *
    launchd jobs do not inherit the shell environment, so the .env is
    what they read
  - same NTFY_TOPIC exported in ~/.config/shell/env.sh, for interactive use
  - local-notify's .env comes from the `envs` stage where the profile has
    a template; email also needs its mail server running

tooling  (fnm node versions and rtk init are the `tooling` stage)
  - grant python3 Documents access when the vault TCC prompt appears
    (the iTerm2 prefs pointer is the `iterm2` stage once the drive has synced)
  - restore ~/.ssh from your password manager
  - tailscale: the brew stage installs the formula but nothing starts it
    (.service is docker-only, and the daemon must run as root):
      sudo brew services start tailscale
      sudo tailscale up --ssh --accept-routes --advertise-exit-node
    Never the App Store app or the tailscale-app cask — sandboxed GUI
    builds refuse to run the SSH server. Then in the admin console:
    approve the advertised 0.0.0.0/0 + ::/0 routes, and add an `ssh`
    block to the ACL or every Tailscale SSH connection is refused.

license keys
  - enter the license keys of the apps that need one

AI apps (UI-only settings; inventories are local-ai-apps' job)
  - desktop and web app connectors, directory grants and capability toggles
  - then: ai-apps.sh setup <profile> --apply + adopt for skills/MCPs
CHECKEOF
  # The machine's own items (accounts, licenses, restores) stay private.
  if [ -f "$CONFIG_DIR/checklist.txt" ]; then
    echo
    cat "$CONFIG_DIR/checklist.txt"
  fi
}

# Decommission helper. Read-only by default: shows unpushed/dirty repos and
# the manual sign-off list. --apply performs only the reversible cleanup
# (uninstall cron jobs, unload+remove manifest LaunchAgents). Destructive
# steps (removing ~/.ssh, wiping .env files) stay manual on purpose.
cmd_decommission() {
  echo "decommission : $(hostname -s)"
  [ "$APPLY" -eq 1 ] || echo "mode    : DRY RUN — nothing will change; re-run with --apply"
  echo

  # --apply dismantles the machine's automation (cron jobs, launch agents).
  # That must never happen from a stray shell history replay or a script
  # calling the wrong verb, so it demands the word typed back on a TTY.
  if [ "$APPLY" -eq 1 ]; then
    echo "  WARNING: this uninstalls ALL cron jobs and the manifest launch"
    echo "  agents on $(hostname -s). Only do this to retire the machine."
    if [ ! -t 0 ]; then
      echo "  refusing: --apply needs an interactive terminal to confirm" >&2
      exit 1
    fi
    printf '  type "decommission" to continue: '
    IFS= read -r answer
    if [ "$answer" != "decommission" ]; then
      echo "  aborted — nothing was changed" >&2
      exit 1
    fi
    echo
  fi

  echo "== repos with work that would be lost (commit/push these first)"
  found=0
  for g in "$HOME"/repos/.git "$HOME"/repos/*/.git "$HOME"/repos/*/*/.git; do
    [ -e "$g" ] || continue
    d=${g%/.git}
    dirty=$(git -C "$d" status --porcelain 2>/dev/null | head -1)
    ahead=$(git -C "$d" log --branches --not --remotes --oneline 2>/dev/null | head -1)
    if [ -n "$dirty" ] || [ -n "$ahead" ]; then
      found=1
      flags=""
      [ -n "$dirty" ] && flags="dirty"
      [ -n "$ahead" ] && flags="$flags unpushed"
      echo "  !!      ${d#"$HOME"/}: $flags"
    fi
  done
  [ "$found" -eq 0 ] && echo "  ok      everything committed and pushed"
  echo

  echo "== cron jobs (local-cron-jobs)"
  FAILED_LOG=$(mktemp)
  run "$ROOT/local-cron-jobs/cron-jobs.sh" uninstall --all
  echo

  echo "== launch agents (manifest)"
  # load_profile dies without a recorded profile, and the cron jobs are gone by
  # now: the run must reach the sign-off list. Resolve the profile as doctor
  # does, and report an unusable one as a failure (sd:3545).
  doctor_profile
  labels=""
  if [ -d "$PROFILE_DIR" ] && [ -n "$PROFILE" ] && known_profiles | grep -qxF "$PROFILE"; then
    labels=$(manifest agent)
  else
    note_failure "launch agents not removed: no usable profile (recorded: '${PROFILE:-none}'); boot out the ${LABEL_PREFIX}.* agents by hand" 1
  fi
  for label in $labels; do
    dst="$HOME/Library/LaunchAgents/$label.plist"
    if [ -f "$dst" ]; then
      # The plist goes only once launchd holds no job under the label: a
      # removed plist under a loaded job leaves nothing to boot it out from.
      if agent_bootout "$label"; then
        run rm "$dst"
      else
        echo "  KEPT    $dst — $label is still loaded; boot it out, then remove the file"
      fi
    else
      echo "  --      $label not installed"
    fi
  done
  echo

  cat <<'DECOMEOF'
== manual sign-offs and exports (nothing below is automated)
  - export app settings nothing else keeps; back up home folders
  - make sure every ~/.ssh key and certificate is in your password manager
  - sign out of every cloud, chat and store account; deregister devices
  - remove global accounts, then: rm -rf ~/.ssh   (LAST, by hand)
  - live secrets on this machine: ~/.config/shell/env.sh,
    ~/.bash_profile and the .env files under $SYSTEM_TOOLS_CONFIG — wipe
    by hand before the disk leaves you
DECOMEOF
  if [ -f "$CONFIG_DIR/decommission.txt" ]; then
    echo
    cat "$CONFIG_DIR/decommission.txt"
  fi
  report_failures
}

# Runs one stage for its report half only. APPLY is forced off for the call:
# stages mutate when it is set, and `status --apply` must stay read-only.
# Adds the stage's drift markers to $drift — called in this shell, not a
# subshell, so the counter survives.
status_stage() {
  _apply=$APPLY
  APPLY=0
  out=$("stage_$1")
  APPLY=$_apply
  printf '%s\n' "$out"
  # Five ways a stage reports divergence: a defaults write it would have
  # made, a file that disagrees, a manifest entry the machine lacks,
  # something installed that no manifest claims, and a launchd item whose
  # plist is fine but which launchd is not running.
  n=$(printf '%s\n' "$out" | grep -cE 'DIFFERS|MISSING|STALE|ABSENT|UNLOADED|EXTRA|defaults (-currentHost )?write|^  extra ' || :)
  drift=$((drift + ${n:-0}))
  # In-place drift, each line once: cmd_status asks for attention for it
  # rather than failing the run (sd:5064).
  d=$(printf '%s\n' "$out" | grep 'DIFFERS' | sed 's/^ *//' || :)
  [ -z "$d" ] || differs="$differs$d
"
}

# A drift line, printed only when it has something to say. An empty list used
# to print "missing : " with nothing after it, so a perfectly clean machine
# showed four blank-looking lines that read as a broken report rather than as
# good news.
status_line() { # label list
  [ -n "$2" ] || return 0
  printf '  %-8s: %s\n' "$1" "$(printf '%s' "$2" | tr '\n' ' ')"
}

cmd_status() {
  drift=0
  differs=""
  # No profile directory: nothing to compare, convention 6's 3 and not drift's 1.
  require_config 3
  # Nor is there with no profile recorded (sd:3548).
  if [ -z "${MACHINE_SETUP_PROFILE:-}" ] && [ ! -f "$STATE_FILE" ]; then
    echo "machine-setup.sh: no profile recorded; nothing to compare. Run: $(basename "$0") setup <profile>" >&2
    exit 3
  fi
  load_profile
  keep_sudo_ticket
  echo "profile : $PROFILE"
  echo "repo    : $ROOT"
  echo "state   : $STATE_FILE"
  # Backup copies per machine, outside the drift count (sd:6731).
  for sb in "$CONFIG_DIR"/backups/*/; do
    [ -d "$sb" ] || continue
    echo "backups : $(basename "$sb") $(find "$sb" -mindepth 1 -maxdepth 1 | grep -c . || :)"
  done
  echo

  if command -v brew >/dev/null 2>&1; then
    # Temp files rather than <(...): process substitution is a bashism and this
    # script is POSIX sh per convention #2.
    tmp=$(mktemp -d)
    trap_tmp

    # Missing only: the brew stage adds a tap and never removes one.
    bound_step 300 brew tap 2>/dev/null | sort -u > "$tmp/have.tap"
    manifest tap > "$tmp/want.tap"
    echo "taps"
    mt=$(comm -13 "$tmp/have.tap" "$tmp/want.tap")
    status_line missing "$mt"
    [ -n "$mt" ] || echo "  ok      in sync"

    # `brew list --formula`, not `brew leaves`: a manifest entry can be
    # installed as another formula's dependency (ripgrep is), and leaves omits
    # those, which reported them as missing when they were present.
    # Both sides normalized to bare names: brew list usually prints bare
    # names but tap-qualifies a formula whose name is ambiguous, and
    # manifests carry tap-qualified entries.
    # `sort -u`, not `sort`: manifest dedupes before this strips the tap, so a
    # profile naming one formula both ways left the same bare name twice and
    # comm reported the duplicate as missing for a formula that was installed.
    # formula_plan resolves the rest: an alias, as `python`, is installed as
    # python@3.15, which is then wanted, not extra (sd:7093). A list brew
    # could not give is unknown, not every formula missing.
    echo "brew formulae"
    ub=""
    if fl=$(bound_step 300 brew list --formula 2>/dev/null); then
      fp=$(formula_plan "$fl")
    else
      fp=""
      ub="brew list --formula did not answer"
    fi
    { manifest brew; printf '%s\n' "$fp" | awk '$1 == "ok" { print $3 }'; } | sed 's|.*/||' | sort -u > "$tmp/want.brew"
    mb=$(printf '%s\n' "$fp" | awk '$1 == "bottle" { sub(/.*\//, "", $2); print $2 }')
    # Waits for the weekly upgrade, by ruling (sd:7103): in-place drift, an
    # attention line, not a nightly failure.
    sb=$(printf '%s\n' "$fp" | awk '$1 == "source" { print $2 }')
    ub="$ub$(printf '%s\n' "$fp" | awk '$1 == "unknown" { sub(/.*\//, "", $2); print $2 }')"
    # Extra is what someone installed by name: a build-only dependency is a
    # leaf too, and was reported as drift (sd:5049).
    bound_step 300 brew leaves --installed-on-request 2>/dev/null | sed 's|.*/||' | sort -u > "$tmp/leaves.brew"
    xb=$(comm -23 "$tmp/leaves.brew" "$tmp/want.brew")
    status_line missing "$mb"
    status_line unknown "$ub"
    status_line extra "$xb"
    for f in $sb; do
      echo "  DIFFERS $f needs a source build; the weekly upgrade installs it"
      differs="${differs}DIFFERS $f needs a source build; the weekly upgrade installs it
"
    done
    [ -n "$mb$ub$xb$sb" ] || echo "  ok      in sync"

    bound_step 300 brew list --cask 2>/dev/null | sort > "$tmp/have.cask"
    manifest cask > "$tmp/want.cask"
    echo "casks"
    mc=$(comm -13 "$tmp/have.cask" "$tmp/want.cask")
    xc=$(comm -23 "$tmp/have.cask" "$tmp/want.cask")
    status_line missing "$mc"
    status_line extra "$xc"
    [ -n "$mc$xc" ] || echo "  ok      in sync"
    sp=$(stale_pins) || sp="unknown (brew could not list its pins)"
    status_line pinned "$sp"
    bp=$(unallowed_python)
    status_line "not allowed" "$bp"
    # Its report half only: status --apply stays read-only.
    mf=$(APPLY=0; mosh_firewall | grep DIFFERS || :)
    [ -z "$mf" ] || printf '%s\n' "$mf"
    for v in "$mt" "$mb" "$ub" "$sb" "$xb" "$mc" "$xc" "$sp" "$bp" "$mf"; do
      drift=$((drift + $(printf '%s' "$v" | grep -c . || :)))
    done
  else
    echo "brew    : MISSING"
  fi
  echo
  # Every reporting stage folds into the drift count. Before this, status
  # covered brew, casks and dotfiles only, so the nightly drift cron could not
  # see a macOS key, an Obsidian plugin or an iTerm2 prefs folder that had
  # diverged — and reported 0 while three macos keys were unset.
  # Every stage that compares something goes in. It used to be these six,
  # so the nightly drift job could not see a cron plist, a launch agent, an
  # App Store app, a bin link, a tooling hook or a system setting that had
  # diverged — including, for weeks, nine plists that were all stale.
  #
  # `repos` was the last one out, for want of a read-only verb — `list`
  # printed the configured fleet without touching disk and `sync` clones.
  # repo-sync grew `check` for exactly this, so every comparing stage is now
  # here and the nightly job sees all of them.
  # `sd` is the database and the dashboard's route: two of criterion 22's four
  # drift items (the one-database design, sd:234); the other
  # two were the agents stage's rows for <prefix>.sd-dashboard and the
  # runner, which is gone.
  # Every stage but brew, which has its block above; read from STAGES, as a
  # hand copy of it drifts (sd:3548).
  for st in $STAGES; do
    [ "$st" != brew ] || continue
    status_stage "$st"
    echo
  done
  # Read-only and outside the drift count: see hub_ready.
  if [ -e "$SD_HUB_CONFIG" ] || [ -f "$PROMOTE_MARKER" ] || [ -f "$UNDO_MARKER" ] || [ -e "$DRILL_HOME" ] || hub_role; then
    hub_ready
    echo
  fi
  # A 02:xx cron job runs only on a repeating wake (.claude/rules/cron-wake.md).
  # pmset lists it as "wakepoweron at"; a failed read is unknown, not clean.
  if ! sched=$(pmset -g sched 2>/dev/null); then
    echo "wake    : UNKNOWN (pmset -g sched failed)"; drift=$((drift + 1))
  elif ! printf '%s\n' "$sched" | sed -n '/^Repeating/,/^[^ ]/p' | grep -qE 'wake(poweron)? at'; then
    echo "wake    : MISSING (sudo pmset repeat wakeorpoweron MTWRFSU 02:40:00)"; drift=$((drift + 1))
  fi
  echo "drift   : $drift item(s)"
  # --fail-on-drift turns drift into exit 1, which is what lets a cron job
  # (as its last step) route "this machine drifted" into a failure report
  # that Today shows. In-place drift (DIFFERS: a file edited here, a setting
  # changed by hand) waits for a person to pick a side, so it failed the
  # nightly every night until then: 8 of 10 nightly failures (sd:5064). It
  # prints one SD_REPORT_ATTENTION line per finding instead, with no date, so
  # the same lines fold into the open report; the rest of the drift is what
  # the update before it could not fix, and still exits 1.
  [ "$FAIL_ON_DRIFT" -eq 1 ] || return 0
  printf '%s' "$differs" | awk -v p="SD_REPORT_ATTENTION: drift on $(hostname -s): " 'NF { print p $0 }'
  if [ "$drift" -gt "$(printf '%s' "$differs" | grep -c . || :)" ]; then
    exit 1
  fi
}

# ------------------------------------------------------------ promote ----
# Hub failover slice 4 (sd:4012; docs/work/2026-10-10-hub-failover/design.md,
# "The verb and its steps"): a satellite takes the hub role from the newest
# verified snapshot it can reach. Each step moves state once, and the intent
# marker names the next step, so a rerun after a kill or a refusal goes on
# from there; the failure table in the design names each step's recovery.
PROMOTE_MARKER="${XDG_STATE_HOME:-$HOME/.local/state}/sd/promote-intent"
PROMOTE_FROM=""
PROMOTE_UNDO=0
UNDO_MARKER="${XDG_STATE_HOME:-$HOME/.local/state}/sd/promote-undo"
PROMOTE_DRILL=0
# The drill's throwaway home, on the system disk; it holds a restored copy of
# the records only while a drill runs.
DRILL_HOME="${XDG_STATE_HOME:-$HOME/.local/state}/sd/promote-drill"
# The file a drill writes into its home first: only a home that holds it is
# deleted whole, by the drill or by undo.
DRILL_MARK=".sd-promote-drill"

marker_get() { # key
  [ ! -f "$PROMOTE_MARKER" ] || sed -n "s/^$1=//p" "$PROMOTE_MARKER" | tail -1
}
# Written whole and renamed, as follow-intent is: a kill leaves the old marker
# or the new one, never half of one.
marker_set() { # key value
  mkdir -p "$(dirname "$PROMOTE_MARKER")"
  { [ ! -f "$PROMOTE_MARKER" ] || grep -v "^$1=" "$PROMOTE_MARKER" || :; printf '%s=%s\n' "$1" "$2"; } \
    > "$PROMOTE_MARKER.new" && mv "$PROMOTE_MARKER.new" "$PROMOTE_MARKER"
}

# The roots promote restores from: each --from, then the NAS folders the
# nightly and hourly copies write, under OFFSITE_VERIFY_ROOT
# (<config>/mirror-sync/.env, offsite-verify's own setting). One per line.
promote_roots() {
  printf '%s' "$PROMOTE_FROM"
  nas_roots
}

nas_roots() {
  nr=$(nas_mount) || return 1
  [ -z "$nr" ] || printf '%s\n' "${nr%/}/sd-backups-hourly" "${nr%/}/sd-backups"
}
# nas_mount and rm_target's helpers, kept as text: this shell defines them,
# and rm_target runs them in a child shell under a bound, since each path
# they resolve may lie on a share, and a stalled SMB share hung a cd -P or a
# test of its mount point (sd:4816). A quoted here-document, so the text is
# the functions as written; printed by a function, as bash 3.2 misreads a
# case pattern in a here-document inside $().
rt_sh() {
  cat <<'RTEOF'
nas_mount() {
  PYTHONPATH="$ROOT/lib" python3 -c 'import os, system_tools_config as stc
print(os.environ.get("OFFSITE_VERIFY_ROOT") or stc.read_env("mirror-sync").get("OFFSITE_VERIFY_ROOT") or "")'
}

# The physical path of folder $1 in rt_dir. The sentinel keeps a trailing
# newline that $() would strip, which would name another folder.
rt_resolve() { # folder
  rt_dir=$(cd -P -- "$1" 2>/dev/null && pwd -P && echo .) || return 1
  rt_dir=${rt_dir%?.}
}

# Where the absolute path $1 lies, in rt_at: its folder resolved with cd -P,
# then its last part; a folder resolves, a symlink stays the link (rm
# removes the link, not its target). rt_at is empty when $1 is not there.
# Returns 2 when it cannot tell: a folder on the way it cannot enter.
rt_locate() { # path
  rt_at=
  case "$1" in /*) ;; *) echo "$1: not an absolute path" >&2; return 2 ;; esac
  rt_low=$1; rt_up=${1%/*}
  until rt_resolve "${rt_up:-/}"; do rt_low=$rt_up; rt_up=${rt_up%/*}; done
  if [ "$rt_low" != "$1" ]; then
    # The first part below a folder it entered: there, so it cannot tell.
    [ ! -d "$rt_low" ] || { echo "$1: cannot enter $rt_low" >&2; return 2; }
    return 0
  fi
  [ -e "$1" ] || [ -L "$1" ] || return 0
  if [ -d "$1" ] && [ ! -L "$1" ]; then
    rt_resolve "$1" || { echo "$1: cannot enter it" >&2; return 2; }
    rt_at=$rt_dir
  else
    rt_at=${rt_dir%/}/${1##*/}
  fi
}

# Where a recursive delete of the absolute path $1 lands, proven before any
# rm -rf runs: cd -P resolves `..` and every symlinked ancestor, so the
# checks see the real path, not its spelling. Prints the real path, or
# nothing when $1 is gone. Returns 1, the reason on stderr, when the real
# path holds a newline, is not under the resolved root $2, or is on the NAS
# mount or holds it (rm -rf crosses mounts); returns 2 when it cannot tell.
rt_target() { # path allowed-root
  case "${1:?}" in /*) ;; *) echo "$1: not an absolute path" >&2; return 1 ;; esac
  rt_locate "$1" || return 2
  [ -n "$rt_at" ] || return 0
  rt_real=$rt_at
  case "$rt_real" in *'
'*) echo "$1: its real path holds a newline" >&2; return 1 ;; esac
  rt_resolve "${2:?}" || { echo "$2: cannot resolve it" >&2; return 2; }
  case "$rt_real" in "${rt_dir%/}"/?*) ;; *) echo "$rt_real: not under $rt_dir" >&2; return 1 ;; esac
  rt_nas=$(nas_mount) || { echo "$1: could not read OFFSITE_VERIFY_ROOT" >&2; return 2; }
  [ -n "$rt_nas" ] || { printf '%s\n' "$rt_real"; return 0; }
  # The mount where it lies (a mount point is no link to follow), and where
  # it leads; a mount that is not there holds nothing.
  rt_locate "$rt_nas" || return 2
  rt_mount=$rt_at; rt_lead=
  if [ -n "$rt_mount" ] && [ -d "$rt_nas" ]; then
    rt_resolve "$rt_nas" || { echo "$rt_nas: cannot enter it" >&2; return 2; }
    rt_lead=$rt_dir
  fi
  for rt_n in "$rt_mount" "$rt_lead"; do
    [ -n "$rt_n" ] || continue
    case "$rt_real/" in "${rt_n%/}"/*) echo "$rt_real: on the NAS ($rt_nas)" >&2; return 1 ;; esac
    case "${rt_n%/}/" in "$rt_real"/*) echo "$rt_real: holds the NAS ($rt_nas)" >&2; return 1 ;; esac
  done
  printf '%s\n' "$rt_real"
}
RTEOF
}
# Exported, so the child reads the text from its environment, and the step
# line names the call, not the functions.
RT_SH=$(rt_sh)
export RT_SH
eval "$RT_SH"

# rt_target in a child shell, bounded as a listing is: its exit and its
# output, or 2, cannot tell, when it did not answer in time. A timeout is
# never a path that is not there.
rm_target() { # path allowed-root
  rm_rc=0
  # shellcheck disable=SC2016 # the child expands its own arguments
  rm_out=$(bound_step 30 sh -c 'eval "$RT_SH"; ROOT=$1; shift; rt_target "$@"' sh "$ROOT" "$@") || rm_rc=$?
  case $rm_rc in
    0) [ -z "$rm_out" ] || printf '%s\n' "$rm_out" ;;
    1|2) ;;
    *) echo "$1: where it lies was not read in time (exit $rm_rc)" >&2; rm_rc=2 ;;
  esac
  return "$rm_rc"
}

# The local root a hub-role backup job writes, from that job's own file: the
# job is sourced as cron-jobs.sh sources it, in a subshell. Prints nothing
# when no file defines the job or the file names no destination. Fails on
# a destination with a newline: a reader would split it into other paths.
job_destination() { # job, variable
  jd_file=$(PYTHONPATH="$ROOT/lib" python3 -c 'import sys, system_tools_config as stc
for d in stc.cron_job_dirs():
    f = d / (sys.argv[1] + ".job")
    if f.is_file():
        print(f)
        break' "$1")
  [ -n "$jd_file" ] || return 0
  # The job's ROOT is local-cron-jobs, as cron-jobs.sh sets it.
  # shellcheck disable=SC1090,SC2030,SC2154 # eval sets jd_value
  (ROOT="$ROOT/local-cron-jobs"; eval "$2=''"; . "$jd_file" >/dev/null 2>&1; eval "jd_value=\$$2"
   case "$jd_value" in
     *'
'*) echo "$jd_file: $2 holds a newline; name a folder without one" >&2; exit 1 ;;
     *) printf '%s\n' "$jd_value" ;;
   esac)
}
hourly_destination() { job_destination sd-db-backup-hourly SD_DB_BACKUP_HOURLY_DESTINATION; }

# hub.json's hub and port, as `<host> <port>`; nothing when it cannot be read.
hub_named() {
  python3 -c 'import json, sys
try:
    with open(sys.argv[1]) as f:
        data = json.load(f)
    print(data["hub"], data.get("port", 8769))
except Exception:
    pass' "$1"
}

# Exits 0 when host:port takes a TCP connection within the bound. The name
# lookup counts too: a name no server answers for took 30 s whatever the
# connect timeout, so the probe runs in a thread the bound outlives.
hub_answers() { # host port seconds
  python3 -c 'import os, socket, sys, threading, time
def probe():
    try:
        socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=float(sys.argv[3])).close()
        os._exit(0)
    except OSError:
        os._exit(1)
threading.Thread(target=probe, daemon=True).start()
time.sleep(float(sys.argv[3]))
os._exit(1)' "$1" "$2" "$3"
}

# The fence's name rule (sd_db hub.py `_matches`), case, blanks and trailing
# dots aside: two full names agree whole; a bare machine name matches the first
# label. Either side may be the bare one: hub.json and sd.hub_name both name the hub.
hub_name_matches() {
  hm_a=$(printf '%s' "$1" | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]' | sed 's/\.*$//')
  hm_b=$(printf '%s' "$2" | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]' | sed 's/\.*$//')
  case "$hm_a/$hm_b" in
    *.*/*.*) [ "$hm_a" = "$hm_b" ] ;;
    *) [ "${hm_a%%.*}" = "${hm_b%%.*}" ] ;;
  esac
}

# The hub-ready block (design, "Satellite readiness"): read-only, one line per
# check, `ok`, `--` when it cannot be read now, or NOT-READY. Not a drift word:
# a satellite that is no standby is not drifted, so the nightly drift count
# leaves it out, and promote's preflight refuses on any NOT-READY line.
hub_ready() {
  echo "== hub-ready"
  if [ -f "$PROMOTE_MARKER" ]; then
    echo "  NOT-READY promote stopped before its end: $PROMOTE_MARKER names step $(marker_get step); rerun: $(basename "$SELF") promote --apply"
  fi
  if [ -f "$UNDO_MARKER" ]; then
    echo "  NOT-READY promote --undo stopped before its end: $UNDO_MARKER names step $(PROMOTE_MARKER=$UNDO_MARKER; marker_get step); rerun: $(basename "$SELF") promote --undo --apply"
  fi
  if [ -e "$DRILL_HOME" ]; then
    echo "  NOT-READY a drill did not finish and left the records in $DRILL_HOME; remove it (rm -rf), or the next promote --drill does if it holds $DRILL_MARK"
  fi
  if hub_holder; then
    echo "  ok      this machine holds the hub role (promoted)"
    return 0
  fi
  if [ ! -e "$SD_HUB_CONFIG" ]; then
    echo "  NOT-READY no $SD_HUB_CONFIG: this machine is not a satellite"
    return 0
  fi
  # shellcheck disable=SC2046 # two words, host and port
  set -- $(hub_named "$SD_HUB_CONFIG")
  if [ -z "${1:-}" ]; then
    echo "  NOT-READY $SD_HUB_CONFIG names no hub"
    return 0
  fi
  echo "  ok      $SD_HUB_CONFIG names $1:$2"
  hr_hub=$1
  hr_hub_port=$2
  # The hub holds the setting; a dead hub cannot say, and promote checks it
  # again in the restored database before it takes the name.
  if hr_name=$(bound_step 30 sd config get sd.hub_name 2>/dev/null); then
    if hub_name_matches "$1" "$hr_name"; then
      echo "  ok      sd.hub_name is $hr_name"
    elif [ -z "$hr_name" ]; then
      echo "  NOT-READY sd.hub_name is unset: on the hub, sd config set sd.hub_name $1"
    else
      echo "  NOT-READY sd.hub_name is $hr_name, and $SD_HUB_CONFIG names $1; satellites would not find the promoted hub"
    fi
  else
    echo "  --      sd.hub_name not read from the hub; promote reads it from the restored database"
  fi
  # The checkouts at the hub-pin tag follow last fetched, so the restored
  # database meets the build the hub ran (README of local-repo-sync, "Satellites
  # follow the hub"); then the satellite check's own proof line.
  if hr_pin=$(git -C "$ROOT" rev-parse -q --verify 'refs/tags/hub-pin^{commit}' 2>/dev/null); then
    hr_pack=$(git -C "$ROOT" tag -l --format='%(contents)' hub-pin 2>/dev/null | sed -n 's/^pack=//p' | head -1)
    hr_sys=$(git -C "$ROOT" rev-parse HEAD 2>/dev/null) || hr_sys=unknown
    hr_have=$(git -C "$SD_PACK_ROOT" rev-parse HEAD 2>/dev/null) || hr_have=unknown
    if [ "$hr_sys" = "$hr_pin" ] && [ -n "$hr_pack" ] && [ "$hr_have" = "$hr_pack" ]; then
      echo "  ok      system and pack at hub-pin"
    else
      echo "  NOT-READY system at $hr_sys and pack at $hr_have, not hub-pin's $hr_pin and ${hr_pack:-no pack=}: local-repo-sync/repo-sync.sh follow"
    fi
  else
    echo "  NOT-READY no hub-pin tag in $ROOT: local-repo-sync/repo-sync.sh follow fetches it"
  fi
  # The weekly gate run's newest verdict (Q16): a standby that cannot pass
  # the gates it would run as hub is not ready.
  gate_ready_line
  if hr_build=$(bound_step 60 "$SD_DB_PYTHON" -I -c "$SD_DB_RUN_LIB" "$SD_PACK_ROOT/lib" sd_db.satellite --hub "$1" --port "$2" 2>&1); then
    case "$hr_build" in
      *"matches the hub's"*) echo "  ok      sd_db build matches the hub's" ;;
      *DIFFERS*) echo "  NOT-READY $(printf '%s\n' "$hr_build" | grep DIFFERS | head -1 | sed 's/^ *DIFFERS *//')" ;;
      *) echo "  --      the hub did not answer; the sd_db build was not compared" ;;
    esac
  else
    echo "  --      the satellite check did not run under $SD_DB_PYTHON; the sd_db build was not compared"
  fi
  # The lanes this machine runs as hub: every managed repository with no lane
  # host and every one it hosts, pushed with the one active gh login (design,
  # "One GitHub login for both sets").
  if hr_repos=$(bound_step 60 "$SD_PACK_ROOT/bin/sd-db" repo list --managed 2>/dev/null); then
    hr_host=$(hostname -s | tr '[:upper:]' '[:lower:]')
    # sd-db repo list: path, remote, managed, ci, [further fields,] lane host, merge; two spaces apart.
    # The remote is always field 2; the lane host and merge stay the last two.
    printf '%s\n' "$hr_repos" | awk -F '  ' -v host="$hr_host" 'NF >= 6 && ($(NF-1) == "hub" || $(NF-1) == host) {print $2}' |
      while read -r hr_remote; do github_slug "$hr_remote" || :; done | python3 -c 'import json, subprocess, sys
# A remote with several clones is one repository.
want = list(dict.fromkeys(tuple(line.split("/")) for line in sys.stdin.read().split()))
if not want:
    sys.exit(print("  ok      gh: no managed lane repository on GitHub"))
query = "query{viewer{login} " + " ".join(
    f"r{i}: repository(owner: {json.dumps(o)}, name: {json.dumps(n)}){{viewerPermission}}" for i, (o, n) in enumerate(want)) + "}"
try:
    # gh exits 1 when a repository is not found, and still prints the data.
    data = json.loads(subprocess.run(["gh", "api", "graphql", "-f", "query=" + query], capture_output=True,
                                     text=True, timeout=60, stdin=subprocess.DEVNULL).stdout)["data"]
    login = data["viewer"]["login"]
except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
    sys.exit(print("  --      gh api did not answer; push access not checked"))
lacking = [f"{o}/{n}" for i, (o, n) in enumerate(want)
           if (data.get(f"r{i}") or {}).get("viewerPermission") not in ("ADMIN", "MAINTAIN", "WRITE")]
if lacking:
    names = ", ".join(lacking)
    print(f"  NOT-READY gh login {login} cannot push to {len(lacking)} lane repositories: {names}")
else:
    print(f"  ok      gh login {login} pushes to all {len(want)} lane repositories")'
  else
    echo "  --      the repository list was not read from the hub; gh push access not checked"
  fi
  # A hub-role job mails a miss through notify's email channel (the pack's
  # sd-db backup, report_failure). A machine that keeps ntfy and drops email
  # is ready too (operator ruling, sd:4683); its first word says which.
  hr_unset=$(PYTHONPATH="$ROOT/lib" python3 -c 'import os, system_tools_config as stc
env = stc.read_env("notify")
def unset(k):
    v = os.environ[k] if k in os.environ else env.get(k, "")
    return not v or "change-me" in v
print("no-ntfy" if unset("NTFY_TOPIC") else "ntfy", *(k for k in ("NOTIFY_EMAIL_TO", "EMAIL_FROM") if unset(k)))')
  hr_ntfy=${hr_unset%% *}
  hr_unset=${hr_unset#"$hr_ntfy"}
  hr_unset=${hr_unset# }
  if [ -z "${SD_NOTIFY:-}" ] && ! command -v notify >/dev/null 2>&1; then
    echo "  NOT-READY no notify on PATH: a hub-role job could not mail a miss; $(basename "$SELF") update bin --apply"
  elif [ -z "$hr_ntfy" ]; then
    echo "  --      notify's settings were not read; its channels were not checked"
  elif [ -n "$hr_unset" ] && [ "$hr_ntfy" = ntfy ]; then
    echo "  ok      notify reaches you by ntfy; email is off here"
  elif [ -n "$hr_unset" ]; then
    echo "  NOT-READY notify's email channel lacks $hr_unset, and ntfy has no NTFY_TOPIC: copy local-notify/.env.example to $(st_config_dir notify)/.env and set either"
  else
    echo "  ok      notify's email channel is set"
  fi
  hr_roots=$(promote_roots)
  if [ -z "$hr_roots" ]; then
    echo "  NOT-READY no backup root: set OFFSITE_VERIFY_ROOT in $(st_config_dir mirror-sync)/.env, or pass --from DIR"
  fi
  hr_newest=""
  hr_unread=""
  hr_stalled=""
  hr_answered=""
  hr_listed=""
  # Every probe of a backup root or a NAS pair runs bounded: a stalled SMB
  # share hung the nightly run 2 h on one ls (sd:4786). 10 s for a listing or
  # a test, as the shares answer in 1 s when they answer; a timeout (124) is
  # no answer: never ok, an empty folder or a size. It is NOT-READY where
  # promote needs the answer (the hourly root's parent, the snapshot's size,
  # a snapshot from some root) and `--` where a read failure was before: one
  # root while another has a snapshot, and a NAS pair.
  for hr_root in $(printf '%s\n' "$hr_roots" | tr ' ' '\001'); do
    hr_root=$(printf '%s' "$hr_root" | tr '\001' ' ')
    hr_rc=0
    hr_names=$(bound_step 10 ls "$hr_root" 2>/dev/null) || hr_rc=$?
    if [ "$hr_rc" -eq 0 ]; then
      hr_answered=1
      hr_listed="$hr_listed$hr_root
"
      echo "  ok      backup root readable: $hr_root"
      # dated names only, as the backup writes them
      hr_last=$(printf '%s\n' "$hr_names" | grep -E '^[0-9]{4}-[0-9]{2}-[0-9]{2}(\.[0-9]+)?$' | sort -t. -k1,1 -k2,2n | tail -1)
      [ -z "$hr_last" ] || [ -n "$hr_newest" ] || hr_newest="$hr_root/$hr_last"
    elif [ "$hr_rc" -eq 124 ]; then
      hr_stalled="$hr_stalled$hr_root
"
    else
      hr_unread="$hr_unread$hr_root
"
    fi
  done
  # A stalled root is an unreadable one: `--` while another root has a
  # snapshot, NOT-READY when none has, so promote and the drill refuse.
  printf '%s' "$hr_stalled" | while IFS= read -r hr_root; do
    if [ -n "$hr_newest" ]; then
      echo "  --      backup root not readable now: $hr_root; its listing timed out"
    else
      echo "  NOT-READY backup root not readable: $hr_root; its listing timed out"
    fi
  done
  # One readable root with a snapshot is a path: restore --newest reports
  # each root it cannot read and goes on (round 3). With none, there is none.
  printf '%s' "$hr_unread" | while IFS= read -r hr_root; do
    if [ -n "$hr_newest" ]; then
      echo "  --      backup root not readable now: $hr_root; restore --newest reports it and takes the newest it can read"
    else
      echo "  NOT-READY backup root not readable: $hr_root"
    fi
  done
  for hr_label in $(hub_role_labels agent); do
    # As agent_source will once the role is held.
    hr_src="$AGENT_DIR/$hr_label.plist"
    [ -f "$hr_src" ] || hr_src="$DIR/examples/launchagents/local.system-tools.${hr_label#"$LABEL_PREFIX".}.plist"
    if [ ! -f "$hr_src" ]; then
      echo "  NOT-READY no plist for $hr_label in $AGENT_DIR or $DIR/examples/launchagents"
    else
      echo "  ok      $hr_label plist: $hr_src"
    fi
  done
  if hr_jobs=$(defined_cron_jobs); then
    for hr_job in $(hub_role_labels cron); do
      if printf '%s\n' "$hr_jobs" | grep -qxF "$hr_job"; then
        echo "  ok      job file $hr_job"
      else
        echo "  NOT-READY no job file $hr_job.job in $(st_config_dir cron-jobs)/jobs"
      fi
    done
  else
    echo "  NOT-READY the cron-jobs folders could not be read"
  fi
  hr_dest=$(hourly_destination) || hr_dest=
  hr_rc=0
  [ -z "$hr_dest" ] || bound_step 10 test -d "$(dirname "$hr_dest")" 2>/dev/null || hr_rc=$?
  if [ -z "$hr_dest" ]; then
    echo "  NOT-READY the hourly job file names no SD_DB_BACKUP_HOURLY_DESTINATION; on a satellite it names a local root"
  elif [ "$hr_rc" -eq 1 ]; then
    echo "  NOT-READY the hourly backup root's parent is missing: $hr_dest"
  elif [ "$hr_rc" -eq 124 ]; then
    echo "  NOT-READY the hourly backup root's parent not readable: $hr_dest; its test timed out"
  elif [ "$hr_rc" -ne 0 ]; then
    echo "  NOT-READY the hourly backup root's parent not readable: $hr_dest; its test exited $hr_rc"
  else
    echo "  ok      hourly backup root: $hr_dest"
  fi
  # A NAS pair whose destination holds a folder its source lacks loses it on
  # a pass with --delete, such as a share's other folders when the pair names
  # the share's parent; the dated snapshot folders are the history an
  # additive pass keeps. Read only: it deletes nothing (sd:4683).
  for hr_list in mirrors-nas.conf mirrors-nas-hourly.conf; do
    hr_conf="$(st_config_dir mirror-sync)/$hr_list"
    [ -f "$hr_conf" ] || continue
    sed -e 's/#.*//' -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' "$hr_conf" | grep -v '^$' |
      while IFS='|' read -r hr_src hr_dst _; do
        # A listing that fails or times out is no answer, not an empty folder.
        # ls -p marks each folder, so no stat of a name runs outside a bound.
        if ! hr_names=$(bound_step 10 ls -Ap "$hr_dst" 2>/dev/null); then
          echo "  --      $hr_list: $hr_dst not readable now; its folders were not compared"
          continue
        fi
        if ! bound_step 10 ls -A "$hr_src" >/dev/null 2>&1; then
          echo "  --      $hr_list: $hr_src not readable now; its folders were not compared"
          continue
        fi
        hr_cand=$(printf '%s\n' "$hr_names" | sed -n 's|/$||p' | grep -Ev '^([0-9]{4}-[0-9]{2}-[0-9]{2}(\.[0-9]+)?)?$') || :
        # The folders the source lacks, in one bounded probe. -e follows a
        # link: a dangling one is lacking, as the pass puts the link where
        # the folder was.
        # shellcheck disable=SC2016 # the inner script expands its own arguments
        if ! hr_lack=$(bound_step 10 sh -c 'printf "%s\n" "$2" | while IFS= read -r n; do
            [ -z "$n" ] || [ -e "$1/$n" ] || printf "%s\n" "$n"; done' sh "$hr_src" "$hr_cand" 2>/dev/null); then
          echo "  --      $hr_list: $hr_src not readable now; its folders were not compared"
          continue
        fi
        hr_extra=$(printf '%s' "$hr_lack" | awk 'NR > 1 {printf ", "} {printf "%s", $0}')
        if [ -n "$hr_extra" ]; then
          echo "  NOT-READY $hr_list: $hr_dst holds $hr_extra, which $hr_src lacks; a pass with --delete deletes them: pair the sd-backups folder itself"
        else
          echo "  ok      $hr_list: $hr_dst holds no folder $hr_src lacks"
        fi
      done
  done
  if [ -z "$hr_newest" ] && [ -n "$hr_stalled" ] && [ -z "$hr_answered" ]; then
    echo "  NOT-READY no backup root answered in ${MACHINE_SETUP_STEP_TIMEOUT:-10}s; no dated snapshot read"
  elif [ -z "$hr_newest" ]; then
    echo "  NOT-READY no dated snapshot under the backup roots"
  else
    # A size du could not read is no answer: never a size of zero. du walks
    # the whole snapshot, so it gets 60 s where a listing gets 10.
    hr_size=""
    hr_rc=0
    hr_du=$(bound_step 60 du -sk "$hr_newest" 2>/dev/null) || hr_rc=$?
    [ "$hr_rc" -ne 0 ] || hr_size=$(printf '%s\n' "$hr_du" | awk '{print $1}')
    hr_free=$(df -k "$HOME" | awk 'NR == 2 {print $4}')
    case "$hr_size" in ''|*[!0-9]*) hr_size="" ;; esac
    if [ "$hr_rc" -eq 124 ]; then
      echo "  NOT-READY the size of $hr_newest was not read in time (du timed out), so the free disk is not checked"
    elif [ -z "$hr_size" ]; then
      echo "  NOT-READY could not read the size of $hr_newest (du), so the free disk is not checked"
    elif hr_need=$((hr_size * 2)) && [ "${hr_free:-0}" -gt "$hr_need" ]; then
      echo "  ok      free disk ${hr_free} KiB, over twice $hr_newest"
    else
      echo "  NOT-READY free disk ${hr_free:-unknown} KiB, under twice $hr_newest ($hr_need KiB)"
    fi
    backup_age_line
  fi
}

# The backup age line (design, "Readiness inputs"): the checkpoint of the
# snapshot restore --newest would restore from the roots that answered. The
# pack's sd_db.backup.newest_restorable runs restore_newest's own walk, less
# the restore, under the pack's interpreter, as this repository opens no
# database (tests/test_one_store.py): newest checkpoint first, a local copy
# of each, its files hashed against the manifest, the schema stop,
# _check_backup and check_restorable (pack sd:6323). On the share the checks
# took 200 s; on a local copy of the 871 MB snapshot 1 s, and the copy 6 s.
# The whole line, with about 250 checkpoints read on the NAS, took 31 s to
# 62 s on the hub (2026-10-26), so it gets 180 s. `ok` under 24 h. Older, or
# none passing, is NOT-READY while the hub answers: its backups stalled. With
# the hub down it is the best there is, so `--`. A snapshot newer than this
# pack is NOT-READY either way: restore stops at it. A read that fails or
# times out is no age, and a pack without the walk no answer: `--`, never ok.
#
# The copies go in a folder this shell makes and removes, in a subshell with
# trap_tmp: the bound or a TERM kills Python before its own cleanup runs, and
# a copy of up to 871 MB stayed behind on each stalled probe (sd:6325).
backup_age_line() {
  # The first word after the folder is for the test's interpreter stub.
  # shellcheck disable=SC2016 # Python, not shell
  if ! hr_age=$(tmp=$(mktemp -d "${TMPDIR:-/tmp}/hub-ready-XXXXXX") && trap_tmp &&
       bound_step 180 "$SD_DB_PYTHON" -I -c 'import datetime, sys, tempfile
from pathlib import Path
sys.path.insert(0, sys.argv[1])
try:
    import sd_db.backup
except ImportError:
    pass
backup = sys.modules.get("sd_db.backup")  # the package names a function backup too
# Only this pack answers: a copy installed in the interpreter is no pack of this lib/.
if backup is None or not Path(backup.__file__).resolve().is_relative_to(Path(sys.argv[1]).resolve()):
    print("nolib")
    sys.exit(0)
if not hasattr(backup, "newest_restorable"):
    print("old")
    sys.exit(0)
tempfile.tempdir = sys.argv[4]  # the walk copies each candidate under it
found = backup.newest_restorable(list(filter(None, sys.argv[3].splitlines())))
if found.newer is not None:
    print("newer", found.newer[1], backup.SCHEMA_VERSION, found.newer[0])
elif found.snapshot is None:
    print("none")
else:
    age = datetime.datetime.now(datetime.timezone.utc) - found.taken_at
    print(int(age.total_seconds()), found.taken_at.isoformat(), found.snapshot)' "$SD_PACK_ROOT/lib" backup-age "$hr_listed" "$tmp" 2>/dev/null) ||
     [ -z "$hr_age" ]; then
    echo "  --      backup age not read: the snapshots were not checked under $SD_DB_PYTHON in time"
    return 0
  fi
  hr_secs=${hr_age%% *}
  hr_why="its backups have not reached the roots in 24 h"
  case "$hr_secs" in
    none) hr_what="backup age unknown: no snapshot under the roots that answered passes restore's checks" ;;
    nolib) echo "  --      backup age not read: $SD_DB_PYTHON imports no sd_db.backup from $SD_PACK_ROOT/lib"; return 0 ;;
    old) echo "  --      backup age not read: $SD_PACK_ROOT/lib has no sd_db.backup.newest_restorable (pack sd:6323); update the pack"; return 0 ;;
    newer)
      hr_rest=${hr_age#newer }
      hr_theirs=${hr_rest%% *}
      hr_rest=${hr_rest#* }
      echo "  NOT-READY backup age unknown: ${hr_rest#* } is schema $hr_theirs, newer than this pack (${hr_rest%% *}); restore --newest stops there: update the pack"
      return 0 ;;
    -[0-9]*|[0-9]*)
      hr_rest=${hr_age#* }
      hr_at="newest checkpoint ${hr_rest%% *} in ${hr_rest#* }"
      if [ "$hr_secs" -lt -300 ]; then
        hr_what="backup age unknown: $hr_at lies in the future"
        hr_why="a clock is wrong"
      elif [ "$hr_secs" -lt 86400 ]; then
        [ "$hr_secs" -ge 0 ] || hr_secs=0
        echo "  ok      backup age $((hr_secs / 3600)) h: $hr_at"
        return 0
      else
        hr_what="backup age $((hr_secs / 3600)) h, 24 h or more: $hr_at"
      fi ;;
    *) echo "  --      backup age not read: $hr_age"; return 0 ;;
  esac
  if hub_answers "$hr_hub" "$hr_hub_port" "${PROMOTE_PROBE_SECONDS:-30}"; then
    echo "  NOT-READY $hr_what; the hub answers, so $hr_why"
  else
    echo "  --      $hr_what; the hub does not answer, so promote restores the newest there is and its report names the checkpoint"
  fi
}

promote_refuse() { echo "promote: refused: $*" >&2; exit 1; }

# `sd-db backup` takes the first free name of the UTC day, so each name a root
# on stdin holds for today or later is held in $1 too, as an empty folder: the
# additive NAS copy changes nothing on the NAS for it, and retention leaves it.
# Prints each NAS root ($2, one per line) it cannot list, as today's names
# there are not held, and each snapshot in $1, of any day, that differs from
# the one of its name in the NAS hourly root, where the hourly copy would
# overwrite it. `sd-db backup` writes its manifest last. The roots are $3,
# one per line, not stdin: bound_step gives the command /dev/null. Bounded, as
# it lists the NAS roots and reads manifests there; a timeout is a failure,
# never a root with nothing to hold (sd:4816).
hold_names() { # dest nas-roots roots
  bound_step 60 python3 -c 'import datetime, os, re, sys
dest, nas = sys.argv[1], sys.argv[2].splitlines()
today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
def manifest(folder):
    try:
        with open(os.path.join(folder, "backup-manifest.json"), "rb") as f:
            return f.read()
    except OSError:
        return None
for root in filter(None, sys.argv[3].splitlines()):
    try:
        names = os.listdir(root)
    except OSError:
        if root in nas:
            print(root + " cannot be listed")
        continue
    for name in names:
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}([.][0-9]+)?", name):
            continue
        held = os.path.join(dest, name)
        if name[:10] >= today:
            os.makedirs(held, exist_ok=True)
        pair = root in nas and os.path.basename(root) == "sd-backups-hourly"
        if pair and os.path.isdir(held) and os.listdir(held) and (
                manifest(held) is None or manifest(held) != manifest(os.path.join(root, name))):
            print(held + " is not the snapshot of that name in " + root + "; move it aside")' "$1" "$2" "$3"
}

# Step 4, 5 and the seed are the ones that move files; each first records what
# it will move, so a rerun finds it.
cmd_promote() {
  load_profile
  echo "profile : $PROFILE"
  if [ "$PROMOTE_DRILL" = 1 ]; then
    [ "$PROMOTE_UNDO" = 0 ] || promote_refuse "--drill and --undo do not go together"
    promote_drill
    return 0
  fi
  if [ "$PROMOTE_UNDO" = 1 ]; then
    [ -z "$PROMOTE_FROM" ] || promote_refuse "--from belongs to a promotion, not to --undo"
    [ ! -f "$PROMOTE_MARKER" ] || promote_refuse "$PROMOTE_MARKER names step $(marker_get step): finish the promotion first"
    # The undo's own marker; the helpers read PROMOTE_MARKER.
    PROMOTE_MARKER=$UNDO_MARKER
  fi
  step=$(marker_get step)
  if [ "$APPLY" -ne 1 ] && [ "$PROMOTE_UNDO" = 1 ]; then
    echo "mode    : DRY RUN — nothing will change; re-run with --apply"
    if [ -n "$step" ]; then echo "marker  : $PROMOTE_MARKER, at step $step"; fi
    if hub_holder; then echo "role    : this machine holds the hub role"; else echo "role    : this machine does not hold the hub role"; fi
    echo "steps   : 1 stop the hub role and write the hand-back snapshot, 2 hub.json back and the tailnet name given up,"
    echo "          3 the new hub serves that snapshot, 4 delete the local records, 5 update satellite, 6 prove"
    return 0
  fi
  if [ "$APPLY" -ne 1 ]; then
    echo "mode    : DRY RUN — nothing will change; re-run with --apply"
    if [ -n "$step" ]; then echo "marker  : $PROMOTE_MARKER, at step $step"; fi
    echo
    hub_ready
    echo
    echo "steps   : 1 preflight, 2 drain the lanes, 3 marker, 4 hub.json aside, 5 restore --newest,"
    echo "          seed the hourly root, 7 take the hub's tailnet name, 8 install the hub role, 9 prove and report,"
    echo "          then hold while a NAS root cannot be listed (rerun --apply once it can)"
    return 0
  fi
  # Step 2: the lanes are held through refresh_drain.py, which runs this verb
  # again as its child, as follow does (sd:3100). Every apply holds them.
  if [ "${REPO_SYNC_LANES_HELD:-}" != 1 ]; then
    # The drain wants the verb first: `promote --apply`, however it was typed.
    for a in "$@"; do shift; [ "$a" = promote ] || set -- "$@" "$a"; done
    # shellcheck disable=SC2031 # ROOT moved only in hourly_destination's subshell
    exec python3 "$ROOT/local-repo-sync/refresh_drain.py" "$SELF" promote "$@"
  fi
  if [ "$PROMOTE_UNDO" = 1 ]; then
    promote_undo "$step"
    return 0
  fi
  if [ -z "$step" ] && [ -f "$PROMOTE_MARKER" ]; then
    # A marker with no step was cut short before step 3 ended: nothing moved.
    rm -f "$PROMOTE_MARKER"
  fi
  if [ -z "$step" ]; then
    # Step 1: nothing moves until every check passes.
    [ -e "$SD_HUB_CONFIG" ] || promote_refuse "no $SD_HUB_CONFIG: this machine is not a satellite. An old hub becomes one first: README, 'Hub failover'"
    [ ! -e "$SD_DB" ] || promote_refuse "a database is here already, $SD_DB, beside $SD_HUB_CONFIG; move one aside by hand"
    # shellcheck disable=SC2046 # two words, host and port
    set -- $(hub_named "$SD_HUB_CONFIG")
    [ -n "${1:-}" ] || promote_refuse "$SD_HUB_CONFIG names no hub"
    if hub_answers "$1" "$2" "${PROMOTE_PROBE_SECONDS:-30}"; then
      promote_refuse "the hub $1:$2 answers; promote only a dead hub's satellite"
    fi
    ready=$(hub_ready)
    printf '%s\n' "$ready"
    if printf '%s\n' "$ready" | grep -q 'NOT-READY'; then
      promote_refuse "a hub-ready line is not ok; nothing moved"
    fi
    # Step 3: one write, so a marker always names its step.
    mkdir -p "$(dirname "$PROMOTE_MARKER")"
    printf 'hub=%s\nport=%s\nstep=4\n' "$1" "$2" > "$PROMOTE_MARKER.new"
    mv "$PROMOTE_MARKER.new" "$PROMOTE_MARKER"
    step=4
  fi
  hub=$(marker_get hub)
  if [ "$step" = 4 ]; then
    moved=$(marker_get moved)
    if [ -z "$moved" ]; then
      moved="$SD_HUB_CONFIG.promoted-$(date -u +%Y%m%dT%H%M%SZ)"
      marker_set moved "$moved"
    fi
    # A kill after the move and before the marker: the file is already aside.
    if [ -e "$SD_HUB_CONFIG" ]; then mv "$SD_HUB_CONFIG" "$moved"; fi
    echo "step 4  : $SD_HUB_CONFIG moved to $moved"
    marker_set step 5
    step=5
  fi
  if [ "$step" = 5 ]; then
    set --
    for pr in $(promote_roots | tr ' ' '\001'); do set -- "$@" "$(printf '%s' "$pr" | tr '\001' ' ')"; done
    rc=0
    # Bounded: it reads the snapshot off the NAS, and a stalled share would
    # hold the run (sd:4816). A copy gets 30 min where a listing gets 10 s;
    # a timeout is a failed restore, handled as one.
    out=$(bound_step 1800 "$SD_PACK_ROOT/bin/sd-db" restore --newest "$@" 2>&1) || rc=$?
    printf '%s\n' "$out"
    if [ "$rc" -ne 0 ]; then
      if [ ! -e "$SD_DB" ]; then
        # Nothing restored: back to the satellite this machine was.
        mv "$(marker_get moved)" "$SD_HUB_CONFIG"
        rm -f "$PROMOTE_MARKER"
        echo "promote: restore --newest failed (exit $rc); nothing restored, $SD_HUB_CONFIG is back" >&2
      else
        echo "promote: restore --newest failed (exit $rc) after it wrote $SD_DB; rerun promote --apply to restore again" >&2
      fi
      exit 1
    fi
    snapshot=$(printf '%s\n' "$out" | sed -n 's/^sd-db: restored \(.*\) (checkpoint \(.*\)) to .*/\1/p' | tail -1)
    taken=$(printf '%s\n' "$out" | sed -n 's/^sd-db: restored .* (checkpoint \(.*\)) to .*/\1/p' | tail -1)
    marker_set snapshot "$snapshot"
    marker_set taken "$taken"
    marker_set released "$(printf '%s\n' "$out" | sed -n 's/^sd-db: released .*: //p' | tail -1)"
    unread=""
    for pr in "$@"; do
      ! printf '%s\n' "$out" | grep -qF "sd-db: refused $pr: " || unread="${unread:+$unread }$pr"
    done
    marker_set unread "$unread"
    marker_set step 6
    step=6
  fi
  if [ "$step" = 6 ]; then
    # The seed (sd:4012 note 18111): the hourly job names each snapshot after
    # the newest in its local root. An empty root restarts the day's numbers,
    # and the additive NAS copy then writes into the NAS directories of the
    # same names. The restored snapshot in the local root continues them.
    snapshot=$(marker_get snapshot)
    dest=$(hourly_destination) || dest=
    # As hub-ready probes it: only exit 1 says missing; a timeout or another
    # failure is no answer.
    dest_rc=0
    [ -z "$dest" ] || bound_step 10 test -d "$(dirname "$dest")" || dest_rc=$?
    if [ -z "$dest" ] || [ "$dest_rc" -eq 1 ]; then
      promote_refuse "the hourly job file names no local root to seed; set SD_DB_BACKUP_HOURLY_DESTINATION there, then rerun"
    elif [ "$dest_rc" -ne 0 ]; then
      promote_refuse "the hourly backup root's parent did not answer (exit $dest_rc): $dest; rerun"
    fi
    seeded="$dest/$(basename "$snapshot")"
    seed_rc=0
    bound_step 10 test -d "$seeded" || seed_rc=$?
    case $seed_rc in
      0|1) ;;
      *) promote_refuse "the seed $seeded did not answer (exit $seed_rc); rerun" ;;
    esac
    if [ "$seed_rc" -eq 1 ]; then
      bound_step 10 mkdir -p "$dest" || promote_refuse "could not make $dest (exit $?); rerun"
      stale=$(rm_target "$seeded.seeding" "$dest") ||
        promote_refuse "will not delete $seeded.seeding; the hourly root must be a local folder off the NAS"
      [ -z "$stale" ] || rm -rf -- "${stale:?}"
      # The snapshot is on the NAS: a stalled share ends at the bound, and
      # the rerun deletes the part copy.
      if ! bound_step 1800 cp -R "$snapshot" "$seeded.seeding" || ! mv "$seeded.seeding" "$seeded"; then
        promote_refuse "could not copy $snapshot into $dest; rerun"
      fi
    fi
    # A NAS root it cannot list now is listed again after the report.
    nas=$(nas_roots) || promote_refuse "could not read OFFSITE_VERIFY_ROOT for the NAS roots; rerun"
    hold_names "$dest" "$nas" "$PROMOTE_FROM$nas" >/dev/null ||
      promote_refuse "could not hold the NAS snapshot names in $dest (exit $?); rerun"
    echo "seed    : $seeded"
    marker_set step 7
    step=7
  fi
  if [ "$step" = 7 ]; then
    # The words count only with exit 0: a probe that fails is no answer.
    fence=$(bound_step 120 "$SD_PACK_ROOT/bin/sd-db" fence 2>&1) || fence="exit $?: $fence"
    holder=0
    case "$fence" in
      "exit "*) ;;
      *"fence: off:"*)
        promote_refuse "sd.hub_name is unset in the restored database, so no machine can hold the role: sd config set sd.hub_name $hub, then rerun" ;;
      *"fence: holder:"*) holder=1 ;;
    esac
    if [ "$holder" != 1 ]; then
      short=${hub%%.*}
      held=$(bound_step 60 tailscale status --json 2>/dev/null | python3 -c 'import json, sys
want = sys.argv[1].lower()
try:
    status = json.load(sys.stdin)
except ValueError:
    print("?")
    sys.exit()
for peer in (status.get("Peer") or {}).values():
    names = [peer.get("HostName") or "", (peer.get("DNSName") or "").split(".")[0]]
    if want in (n.lower() for n in names):
        print(peer.get("DNSName") or peer.get("HostName"))
        break' "$short")
      if [ "$held" = "?" ]; then
        promote_refuse "tailscale status did not answer; rerun once Tailscale is up"
      elif [ -n "$held" ]; then
        echo "promote: the tailnet node $held still holds the name $short." >&2
        echo "  Stop that machine first: power it off, or unload its sd-serve and lane agents." >&2
        echo "  If you cannot reach it, remove it in the Tailscale admin console." >&2
        echo "  Then rename or remove it in the admin console, and rerun: $(basename "$SELF") promote --apply" >&2
        exit 1
      fi
      bound_step 60 tailscale set "--hostname=$short" || promote_refuse "tailscale set --hostname=$short failed; rerun"
      fence=$(bound_step 120 "$SD_PACK_ROOT/bin/sd-db" fence 2>&1) || fence="exit $?: $fence"
      case "$fence" in
        "exit "*) promote_refuse "the fence does not read holder after the rename: $fence; rerun" ;;
        *"fence: holder:"*) ;;
        *) promote_refuse "the fence does not read holder after the rename: $fence; rerun" ;;
      esac
    fi
    echo "step 7  : $fence"
    marker_set step 8
    step=8
  fi
  if [ "$step" = 8 ]; then
    # The stages read the fence themselves, so they add the role only now.
    for st in sd cron agents; do
      sh "$SELF" update "$st" --apply || promote_refuse "the $st stage did not finish; rerun"
    done
    # A stage that cannot read the fence skips the role and still exits 0;
    # so a fresh probe here, and launchd for each label below.
    HUB_HOLDER=""
    hub_holder || promote_refuse "the hub role is not installed: sd-db fence does not read holder now; rerun"
    marker_set step 9
    step=9
  fi
  # Step 9: one session on this machine's sd-serve, by the hub's name. Asked
  # on every run: a rerun here may follow a change of holder. The session must
  # read this machine's own restore row, not any database a server by that name holds.
  HUB_HOLDER=""
  hub_holder || promote_refuse "this machine does not hold the hub role now (sd-db fence); rerun once it does"
  # Each role label, by launchd and not by its plist, on every run (round 4):
  # the cron stage writes a plist before its bootstrap and logs a failed one
  # without failing, and a reboot after step 8 can drop one. A gap sends the
  # marker back to step 8, whose stages load it. Only print's 113 is "not loaded".
  for hr_label in $(hub_role_labels agent) $(hub_role_labels cron | sed "s/^/$LABEL_PREFIX.cron./"); do
    if [ -f "$HOME/Library/LaunchAgents/$hr_label.plist" ]; then
      lr=0
      launchctl print "gui/$(id -u)/$hr_label" >/dev/null 2>&1 || lr=$?
      case "$lr" in
        0) continue ;;
        113) marker_set step 8
             promote_refuse "the hub role is not running: launchd has not loaded $hr_label; rerun, and the stages load it again" ;;
        *) promote_refuse "could not ask launchd whether $hr_label is loaded (launchctl print exited $lr); rerun" ;;
      esac
    fi
    marker_set step 8
    # A hub profile installs what it lists; the role adds nothing to it.
    if sd_in_profile; then
      promote_refuse "the hub role is not installed: no $hr_label plist, and this hub profile does not list it; add it to $PROFILE.agent or $PROFILE.cron, then rerun"
    fi
    promote_refuse "the hub role is not installed: no $hr_label plist; rerun"
  done
  port=$(marker_get port)
  if ! "$SD_DB_PYTHON" -I -c 'import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from sd_db import database, hub, remote
row = "SELECT body FROM state WHERE kind = ? AND key = ?"
local = database.open_local(Path(sys.argv[4]), write=False, create=False)
try:
    want = local.execute(row, ("restore", sys.argv[5])).fetchone()
finally:
    local.close()
session = remote.connect(sys.argv[2], int(sys.argv[3]), None, token="", write=False,
                         timeout=hub.CONNECT_TIMEOUT, answer_timeout=hub.ANSWER_TIMEOUT)
try:
    got = session.execute(row, ("restore", sys.argv[5])).fetchone()
finally:
    session.close()
if want is None or got is None or tuple(got) != tuple(want):
    sys.exit("promote: the session on " + sys.argv[2] + " reached a database without this restore")' \
      "$SD_PACK_ROOT/lib" "$hub" "$port" "$SD_DB" "$(basename "$(marker_get snapshot)")" </dev/null; then
    promote_refuse "no session on $hub:$port reads this machine's database; check sd-serve (launchctl print gui/$(id -u)/$LABEL_PREFIX.sd-serve) and who holds $hub, then rerun"
  fi
  # The dashboard by its answer, not its label (sd:4661): its /health on the
  # loopback port, then over the private route the sd stage adds. A route or
  # an answer that is not there sends the marker back to step 8, whose stages
  # add the route and load the agent; an unread serve status is no answer.
  serve=""
  { command -v tailscale >/dev/null 2>&1 && serve=$(bound_step 60 tailscale serve status 2>/dev/null); } ||
    promote_refuse "could not read tailscale serve status for the dashboard's route; rerun once Tailscale is up"
  origin=$(printf '%s\n' "$serve" | sd_route_origin)
  if [ -z "$origin" ]; then
    held=$(printf '%s\n' "$serve" | sd_route_holder)
    marker_set step 8
    promote_refuse "the dashboard has no private route: tailscale serve https:$SD_DASHBOARD_HTTPS_PORT ${held:+serves $held, }not 127.0.0.1:$SD_DASHBOARD_PORT; free the port if it is held, then rerun, and the sd stage adds it"
  fi
  body=$(mktemp)
  for url in "http://127.0.0.1:$SD_DASHBOARD_PORT/health" "$origin/health"; do
    # Emptied first: curl writes no file it got no answer for, so the loopback's
    # body would stand in for the route's. Retried for a few seconds: step 8
    # may have just loaded the agent.
    : > "$body"
    code=$(curl -q -sS --max-time 10 --retry 5 --retry-delay 2 --retry-connrefused --retry-max-time 30 \
      -o "$body" -w '%{http_code}' "$url" 2>"$body.err") || code=000
    # The body counts only with curl's exit 0 and HTTP 200: a cut-off answer is no answer.
    if [ "$code" = 200 ] && grep -q '"service": *"sd-dashboard"' "$body" && grep -q '"ok": *true' "$body"; then
      echo "step 9  : the dashboard answers on $url"
      continue
    fi
    if [ "$code" = 000 ]; then
      why=$(head -1 "$body.err")
      why="no answer${why:+: $why}"
    elif grep -q '"service": *"sd-dashboard"' "$body"; then
      why="HTTP $code, unhealthy: $(sd_health_reason "$body")"
    elif grep -q 'Untrusted dashboard host' "$body"; then
      why="HTTP $code, Untrusted dashboard host: the service runs a local-only configuration; see the private front door in $SD_PACK_ROOT/dashboard/RUNTIME.md"
    else
      why="HTTP $code and not its health body"
    fi
    rm -f "$body" "$body.err"
    marker_set step 8
    promote_refuse "the dashboard does not answer on $url ($why); check $LABEL_PREFIX.sd-dashboard and ~/Library/Logs/$LABEL_PREFIX.sd-dashboard.err, then rerun, and step 8's stages run again"
  done
  rm -f "$body" "$body.err"
  echo
  echo "hub failover report"
  echo "  snapshot    : $(marker_get snapshot)"
  echo "  checkpoint  : $(marker_get taken); writes after this time are lost"
  echo "  not backed up, so lost: evidence/ jev-corpus/ jev-snapshots/ runner-ending/ runtime-backups/ runtimes/ ship-locks/ and the lane queues"
  echo "  leases released: $(marker_get released)"
  [ -z "$(marker_get unread)" ] ||
    echo "  not read    : $(marker_get unread); a newer snapshot there is not in this restore"
  echo "  hub.json    : $(marker_get moved)"
  echo "  lanes       : enqueue each NULL-host repository's open pull requests again (sd-ship lane enqueue)"
  echo "  dispatch stays paused: read this, then run sd restore resume"
  role_accounts login
  # The hold step: a NAS root not listed holds names of today this machine's
  # hourly job may take again, and the NAS copy would overwrite them; so does
  # a snapshot the job took here while it waited. The marker stays, and
  # offsite-verify refuses the copies while it exists.
  dest=$(hourly_destination) || dest=
  [ -n "$dest" ] || promote_refuse "the hourly job file names no local root; set SD_DB_BACKUP_HOURLY_DESTINATION there, then rerun"
  # A failed read of the NAS roots is no answer, not none to hold.
  nas=$(nas_roots) || promote_refuse "could not read OFFSITE_VERIFY_ROOT for the NAS roots; rerun"
  unheld=$(hold_names "$dest" "$nas" "$nas") ||
    promote_refuse "could not hold the NAS snapshot names in $dest (exit $?); rerun"
  if [ -n "$unheld" ]; then
    marker_set step hold
    echo "promote: paused: the hub serves; the NAS copies wait until promote --apply runs again after this:" >&2
    printf '%s\n' "$unheld" | sed 's/^/  /' >&2
    exit 1
  fi
  rm -f "$PROMOTE_MARKER"
}

# The hand-back (design, "How the records leave"): a promoted satellite gives
# the role to a repaired or new hub and deletes the records it held. Each step
# moves state once and the marker names the next, as in promote; the records
# go only after the new hub serves the hand-back snapshot.
undo_restart() { # why
  echo "promote --undo: $1; the hub role starts again, nothing was deleted" >&2
  for st in sd cron agents; do sh "$SELF" update "$st" --apply || :; done
  # The stages log a failed bootstrap and go on, so launchd says what runs.
  for ul in $(hub_role_labels cron | sed "s/^/$LABEL_PREFIX.cron./") $(hub_role_labels agent); do
    launchctl print "gui/$(id -u)/$ul" >/dev/null 2>&1 ||
      echo "promote --undo: $ul did not start again; run $(basename "$SELF") update cron --apply and update agents --apply" >&2
  done
  rm -f "$PROMOTE_MARKER"
  exit 1
}

# The local roots the hub-role backup jobs wrote, the drill's home and the
# database folder, each passed to $1 as one word: no splitting, no glob.
undo_roots() { # function
  uh=$(hourly_destination) && un=$(job_destination sd-db-backup SD_DB_BACKUP_DESTINATION) ||
    promote_refuse "could not read a hub-role job's destination; fix its job file, then rerun"
  for ur in "$uh" "$un" "$DRILL_HOME" "$(dirname "$SD_DB")"; do
    [ -z "$ur" ] || "$1" "$ur"
  done
}

# A snapshot folder name as `sd-db backup` gives one: the pack's
# DATED_NAME, the day, then an unpadded .N.
undo_dated() { # name
  case "$1" in
    [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]) return 0 ;;
    [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].[1-9]*)
      case "${1#*.}" in *[!0-9]*) return 1 ;; esac
      return 0 ;;
  esac
  return 1
}

# A folder `sd-db backup` wrote, and nothing since: it holds
# backup-manifest.json as a regular file with the fields the pack always
# writes (format sd-db-backup, version 1, a 32-hex run_id), and its entries
# match the folder as the pack's _check_backup reads it: every file by
# sha256, every folder, no link, nothing on another device; Finder's
# metadata a copy picks up is left out. A name alone proves nothing, and a
# file added later is not the pack's. Unreadable counts as not one: kept;
# so does a proof that does not end in its bound (sd:4816).
undo_snapshot() { # folder
  bound_step 120 python3 -c 'import hashlib, json, os, re, stat, sys
folder = sys.argv[1]
path = os.path.join(folder, "backup-manifest.json")
try:
    details = os.lstat(path)
    if not stat.S_ISREG(details.st_mode) or details.st_size > 16 * 1024 * 1024:
        sys.exit(1)
    with open(path, "rb") as f:
        value = json.loads(f.read())
    if not (isinstance(value, dict) and value.get("format") == "sd-db-backup"
            and type(value.get("version")) is int and value["version"] == 1
            and isinstance(value.get("run_id"), str) and re.fullmatch(r"[0-9a-f]{32}", value["run_id"])
            and isinstance(value.get("entries"), dict)):
        sys.exit(1)
    device = os.lstat(folder).st_dev
    found = {}
    for top, folders, files in os.walk(folder):
        for name in folders + files:
            entry = os.path.join(top, name)
            if re.fullmatch(r"\.DS_Store|\._.*", name):
                continue
            details = os.lstat(entry)
            if stat.S_ISLNK(details.st_mode) or details.st_dev != device:
                sys.exit(1)
            if entry == path:
                continue
            key = os.path.relpath(entry, folder)
            if stat.S_ISREG(details.st_mode):
                with open(entry, "rb") as f:
                    found[key] = hashlib.sha256(f.read()).hexdigest()
            elif stat.S_ISDIR(details.st_mode):
                found[key] = None
            else:
                sys.exit(1)
except (OSError, ValueError):
    sys.exit(1)
sys.exit(0 if found == value["entries"] else 1)' "$1"
}

# Calls $2 on each item in root $1 that undo can name as hub-role output:
# the drill's home whole when it holds DRILL_MARK; in the database folder the
# database and its -wal, -shm and -journal files; in a backup root each
# folder with a dated name that undo_snapshot proves `sd-db backup` wrote.
# Anything else is not undo's to delete.
# Returns 2 when it cannot list a backup root: no answer, not none there.
undo_named() { # root function
  if [ "$1" = "$DRILL_HOME" ]; then
    [ ! -f "$1/$DRILL_MARK" ] || "$2" "$1"
  elif [ "$1" = "$(dirname "$SD_DB")" ]; then
    for un_e in "$SD_DB" "$SD_DB-wal" "$SD_DB-shm" "$SD_DB-journal"; do
      if [ -e "$un_e" ] || [ -L "$un_e" ]; then "$2" "$un_e"; fi
    done
  else
    # The folders in a bounded child: a root that leads onto a stalled share
    # ends at the bound, and no answer is no listing, not an empty root
    # (sd:4816). A name with a newline is no dated name, so one per line.
    # shellcheck disable=SC2016 # the child expands its own arguments
    un_list=$(bound_step 30 sh -c '{ [ -r "$1" ] && [ -x "$1" ]; } || exit 3
      for e in "$1"/*; do if [ -d "$e" ] && [ ! -L "$e" ]; then printf "%s\n" "${e##*/}"; fi; done' sh "$1") ||
      { echo "$1: cannot list it (exit $?)" >&2; return 2; }
    # In this shell, not a pipeline: a refusal in $2 must end the run. Only
    # digits, dashes and dots pass the grep, so the words neither split nor glob.
    for un_n in $(printf '%s\n' "$un_list" | grep -E '^[0-9][0-9.-]*$'); do
      if undo_dated "$un_n" && undo_snapshot "$1/$un_n"; then "$2" "$1/$un_n"; fi
    done
  fi
}

# Step 4 deletes only what undo_named names, each where rm_target proves it
# lies under $HOME and off the NAS: the NAS copies stay. Then the root goes
# if it is empty; one that is not is kept with what it holds. A path it
# cannot place stops the run.
undo_delete() { # root
  ud_rc=0; ud_real=$(rm_target "$1" "$HOME") || ud_rc=$?
  case $ud_rc in
    0) [ -n "$ud_real" ] || return 0 ;;
    1) echo "kept    : $1"; return 0 ;;
    *) promote_refuse "could not tell where $1 lies; rerun" ;;
  esac
  undo_named "$1" undo_rm || promote_refuse "could not list $1; rerun"
  [ -e "$1" ] || return 0
  # A root that is a link stays: rmdir takes no link, and its target is
  # where the items were.
  if [ -L "$ud_real" ]; then echo "kept    : $1 (a link)"; return 0; fi
  # The empty folders promote's hold step left hold nothing: rmdir takes
  # only an empty one, so any other stays.
  for ud_e in "$ud_real"/*; do
    if [ -d "$ud_e" ] && [ ! -L "$ud_e" ] && undo_dated "${ud_e##*/}"; then rmdir -- "$ud_e" 2>/dev/null || :; fi
  done
  ud_held=
  for ud_e in "$ud_real"/* "$ud_real"/.[!.]* "$ud_real"/..?*; do
    if [ -e "$ud_e" ] || [ -L "$ud_e" ]; then ud_held=1; echo "  holds : $ud_e"; fi
  done
  if [ -n "$ud_held" ]; then
    echo "kept    : $1 (not empty)"
  else
    rmdir -- "${ud_real:?}" || promote_refuse "could not remove the empty $ud_real; rerun"
    echo "deleted : $ud_real"
  fi
}

undo_rm() { # item
  ur_rc=0; ur_real=$(rm_target "$1" "$HOME") || ur_rc=$?
  case $ur_rc in
    0) if [ -n "$ur_real" ]; then
         rm -rf -- "${ur_real:?}" || promote_refuse "could not delete $ur_real; rerun"
         echo "deleted : $ur_real"
       fi ;;
    1) echo "kept    : $1" ;;
    *) promote_refuse "could not tell where $1 lies; rerun" ;;
  esac
}

# Step 6 names an item step 4 should have deleted and is still there. The
# database is never left, wherever its folder lies: this machine would still
# hold the records.
undo_left() { # root
  if [ "$1" = "$(dirname "$SD_DB")" ]; then
    for ul_f in "$SD_DB" "$SD_DB-wal" "$SD_DB-shm" "$SD_DB-journal"; do
      ul_rc=0; ul_real=$(rm_target "$ul_f" "$HOME" 2>/dev/null) || ul_rc=$?
      [ "$ul_rc" = 0 ] && [ -z "$ul_real" ] || printf '%s\n' "$ul_f"
    done
    return 0
  fi
  ul_rc=0; ul_real=$(rm_target "$1" "$HOME" 2>/dev/null) || ul_rc=$?
  case $ul_rc in
    0) [ -z "$ul_real" ] || undo_named "$1" undo_left_item || printf '%s\n' "$1 (cannot list)" ;;
    1) ;;
    *) printf '%s\n' "$1 (cannot tell)" ;;
  esac
}

# The report names each root still here: what it holds is not proven
# undo's to delete, such as a snapshot a killed backup left without its
# manifest. The operator checks it by hand.
undo_kept() { # root
  if [ -e "$1" ] || [ -L "$1" ]; then echo "  kept        : $1 holds what undo cannot name as its own; check it by hand"; fi
}

undo_left_item() { # item
  uli_rc=0; uli_real=$(rm_target "$1" "$HOME" 2>/dev/null) || uli_rc=$?
  case $uli_rc in
    0) [ -z "$uli_real" ] || printf '%s\n' "$1" ;;
    1) ;;
    *) printf '%s\n' "$1 (cannot tell)" ;;
  esac
}

promote_undo() { # marker step
  step=$1
  if [ -z "$step" ]; then
    rm -f "$PROMOTE_MARKER"
    hub_holder || promote_refuse "this machine does not hold the hub role: no hub.json, $SD_DB here and sd-db fence reading holder; nothing to hand back"
    # Undo hands a standby's role back; a hub profile runs the role as its own.
    ! sd_in_profile || promote_refuse "this hub profile holds the role as the hub, not as a promoted standby; demote it instead: README, 'Hub failover'"
    # shellcheck disable=SC2012 # the stamp sorts; the newest promotion is the one held
    moved=$(ls -d "$SD_HUB_CONFIG".promoted-* 2>/dev/null | sort | tail -1)
    [ -n "$moved" ] || promote_refuse "no $SD_HUB_CONFIG.promoted-* to put back"
    # shellcheck disable=SC2046 # two words, host and port
    set -- $(hub_named "$moved")
    [ -n "${1:-}" ] || promote_refuse "$moved names no hub"
    nas=$(PROMOTE_FROM=""; promote_roots | sed -n 1p)
    [ -n "$nas" ] || promote_refuse "no NAS hourly root: set OFFSITE_VERIFY_ROOT in $(st_config_dir mirror-sync)/.env"
    # Never created here: a share that is not mounted has no such folder.
    # Bounded, as hub-ready's probes are: only exit 1 says it is not there;
    # a stalled share is no answer (sd:4816).
    un_rc=0
    bound_step 10 test -d "$nas" || un_rc=$?
    case $un_rc in
      0) ;;
      1) promote_refuse "the NAS hourly root $nas is not there; mount the share, then rerun" ;;
      *) promote_refuse "the NAS hourly root $nas did not answer (exit $un_rc); rerun once the share answers" ;;
    esac
    mkdir -p "$(dirname "$PROMOTE_MARKER")"
    printf 'hub=%s\nport=%s\nmoved=%s\nnas=%s\nstep=1\n' "$1" "$2" "$moved" "$nas" > "$PROMOTE_MARKER.new"
    mv "$PROMOTE_MARKER.new" "$PROMOTE_MARKER"
    step=1
  fi
  hub=$(marker_get hub)
  port=$(marker_get port)
  moved=$(marker_get moved)
  # hub.json aside before the deletion: sd writes here may have landed after
  # the snapshot, so it no longer holds every record. Take a new one.
  if { [ "$step" = 2 ] || [ "$step" = 3 ]; } && [ -e "$moved" ]; then
    echo "step $step  : $moved is aside, so writes may follow the snapshot; a new one is taken"
    marker_set step 1
    step=1
  fi
  if [ "$step" = 1 ] && [ ! -e "$moved" ]; then
    # Killed after hub.json went back: no write followed the snapshot.
    [ -n "$(marker_get snapshot)" ] || promote_refuse "$moved is gone and no snapshot is recorded; put it back, then rerun"
  elif [ "$step" = 1 ]; then
    # Step 1: no hub-role job or agent runs after the hand-back snapshot. A
    # job that is running holds its lock, and its uninstall refuses. A label
    # launchd still runs is stopped whether or not its plist is there.
    for uj in $(hub_role_labels cron); do
      "$ROOT/local-cron-jobs/cron-jobs.sh" uninstall "$uj" </dev/null ||
        undo_restart "the $uj job did not stop; rerun once its run ends"
    done
    # Every role label is hub-only, so a satellite runs none of them, even
    # one its profile lists: each stops before the snapshot.
    for ua in $(hub_role_labels agent); do
      [ -f "$HOME/Library/LaunchAgents/$ua.plist" ] || launchctl print "gui/$(id -u)/$ua" >/dev/null 2>&1 || continue
      if agent_bootout "$ua"; then rm -f "$HOME/Library/LaunchAgents/$ua.plist"; else undo_restart "$ua did not stop"; fi
    done
    # cron-jobs.sh uninstall removes the plist even when its bootout fails, so
    # launchd, not the plist, says each label stopped (round 4). Only print's
    # 113 is "not loaded"; any other answer is none.
    for ul in $(hub_role_labels cron | sed "s/^/$LABEL_PREFIX.cron./") $(hub_role_labels agent); do
      lr=0
      launchctl print "gui/$(id -u)/$ul" >/dev/null 2>&1 || lr=$?
      case "$lr" in
        113) ;;
        0) undo_restart "launchd still runs $ul; rerun once it stops" ;;
        *) undo_restart "could not ask launchd whether $ul stopped (launchctl print exited $lr)" ;;
      esac
    done
    nas=$(marker_get nas)
    out=$(bound_step 900 "$SD_PACK_ROOT/bin/sd-db" backup --destination "$nas" --no-row-prune 2>&1) ||
      undo_restart "the hand-back snapshot failed: $out"
    printf '%s\n' "$out"
    snapshot=$(printf '%s\n' "$out" | sed -n 's/^sd-db backup: \(.*\) — .*/\1/p' | tail -1)
    # The snapshot is on the NAS: a manifest test that does not answer is no manifest.
    un_rc=0
    [ -z "$snapshot" ] || bound_step 10 test -f "$snapshot/backup-manifest.json" || un_rc=$?
    case $un_rc in
      0) [ -n "$snapshot" ] || undo_restart "the backup named no snapshot with a manifest: $out" ;;
      1) undo_restart "the backup named no snapshot with a manifest: $out" ;;
      *) undo_restart "the hand-back snapshot's manifest did not answer (exit $un_rc): $snapshot" ;;
    esac
    marker_set snapshot "$snapshot"
    # hub.json beside the database makes every sd command here refuse
    # (HubConflict) until step 4 deletes it, so no write follows the snapshot.
    mv "$moved" "$SD_HUB_CONFIG"
    echo "step 1  : $SD_HUB_CONFIG is back"
  fi
  if [ "$step" = 1 ]; then
    marker_set step 2
    step=2
  fi
  snapshot=$(marker_get snapshot)
  if [ "$step" = 2 ]; then
    # Step 2: the tailnet name goes back to this machine's own.
    bound_step 60 tailscale set --hostname= || promote_refuse "tailscale set --hostname= failed; rerun"
    echo "step 2  : the tailnet name ${hub%%.*} is given up"
    marker_set step 3
    step=3
  fi
  if [ "$step" = 3 ]; then
    # Step 3: three checks, and nothing is deleted unless each passes. The
    # fence reads the local database, opened as step 9 opens it: sd's own
    # open refuses it beside hub.json. Bounded: it reads the snapshot's
    # manifest off the NAS (sd:4816); a timeout deletes nothing.
    un_rc=0
    bound_step 120 "$SD_DB_PYTHON" -I -c 'import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from sd_db import database, hub, remote
local = database.open_local(Path(sys.argv[2]), write=False, create=False)
try:
    found = hub.role(local)
finally:
    local.close()
if found.state != "elsewhere":
    sys.exit(f"the fence here reads {found.state}, not elsewhere: {found.reason}")
snapshot = Path(sys.argv[5])
run_id = json.loads((snapshot / "backup-manifest.json").read_text())["run_id"]
try:
    session = remote.connect(sys.argv[3], int(sys.argv[4]), None, token="", write=False,
                             timeout=hub.CONNECT_TIMEOUT, answer_timeout=hub.ANSWER_TIMEOUT)
    try:
        newest = session.execute("SELECT key FROM state WHERE kind = ? ORDER BY timestamp DESC LIMIT 1",
                                 ("restore",)).fetchone()
        held = session.execute("SELECT 1 FROM state WHERE kind = ? AND key = ?", ("checkpoint", run_id)).fetchone()
    finally:
        session.close()
except Exception as error:
    sys.exit(f"the new hub {sys.argv[3]}:{sys.argv[4]} does not answer a session: {type(error).__name__}: {error}")
last = newest[0] if newest else "nothing"
if last != snapshot.name:
    sys.exit(f"the new hub last restored {last}, not the hand-back snapshot {snapshot.name}")
if held is None:
    sys.exit(f"the new hub holds no checkpoint {run_id}, the hand-back point")' \
      "$SD_PACK_ROOT/lib" "$SD_DB" "$hub" "$port" "$snapshot" || un_rc=$?
    if [ "$un_rc" -eq 124 ]; then
      promote_refuse "the hand-back checks did not answer in time; nothing deleted; rerun"
    elif [ "$un_rc" -ne 0 ]; then
      echo "promote --undo: refused; nothing deleted. On the new hub, as a satellite of $hub, run" >&2
      echo "  $(basename "$SELF") promote --apply: $snapshot is the newest snapshot there" >&2
      echo "  then rerun here: $(basename "$SELF") promote --undo --apply" >&2
      exit 1
    fi
    echo "step 3  : $hub serves the hand-back snapshot $snapshot"
    marker_set step 4
    step=4
  fi
  if [ "$step" = 4 ]; then
    # Step 4: rm -rf finishes what a killed run left.
    undo_roots undo_delete
    marker_set step 5
    step=5
  fi
  if [ "$step" = 5 ]; then
    # Step 5: the stage's exit does not say its install worked. Its dry run
    # after does, as for follow: no drift word, and its proof line.
    sh "$SELF" update satellite --apply || promote_refuse "update satellite --apply failed; rerun"
    sat=$(sh "$SELF" update satellite 2>&1) || sat="$sat
  FAILED  update satellite exited $?"
    printf '%s\n' "$sat"
    if printf '%s\n' "$sat" | grep -qE '^  (DIFFERS|MISSING|EXTRA|FAILED)' ||
       ! printf '%s\n' "$sat" | grep -q "matches the hub's"; then
      promote_refuse "the satellite stage did not prove this machine a satellite of $hub; rerun"
    fi
    marker_set step 6
  fi
  # Step 6: the proof the design names.
  left=$(undo_roots undo_left)
  [ -z "$left" ] || promote_refuse "still here after the delete: $left; rerun"
  rm -f "$PROMOTE_MARKER"
  echo
  echo "hand-back report"
  echo "  snapshot    : $snapshot"
  echo "  hub         : $hub:$port serves it"
  echo "  deleted     : the records and the snapshots sd-db backup wrote here; the NAS copies stay"
  # A root still here holds what undo cannot name as its own: say so.
  undo_roots undo_kept
  echo "  this machine: a satellite of $hub again"
  role_accounts signout
}

# The drill (design, "Drill"): promote's steps 1, 5, 6 and 9 against a
# throwaway home. hub.json, launchd and Tailscale stay as they are. The home
# goes when the run ends, pass or fail; one a killed drill left goes first.
drill_cleanup() {
  if [ -n "${DRILL_SERVE:-}" ]; then kill "$DRILL_SERVE" 2>/dev/null || :; wait "$DRILL_SERVE" 2>/dev/null || :; fi
  drill_remove || echo "drill   : left $DRILL_HOME; remove it by hand" >&2
}

# The home goes only where rm_target proves it lies under the state folder
# and off the NAS, and whole only when it holds DRILL_MARK; else only empty.
drill_remove() {
  dr_real=$(rm_target "$DRILL_HOME" "${XDG_STATE_HOME:-$HOME/.local/state}") || return 1
  [ -n "$dr_real" ] || return 0
  if [ -f "$dr_real/$DRILL_MARK" ]; then
    rm -rf -- "${dr_real:?}"
  else
    rmdir -- "${dr_real:?}" 2>/dev/null || { echo "$DRILL_HOME: no $DRILL_MARK, so not a drill's home" >&2; return 1; }
  fi
}

drill_time() { # label start
  echo "$1: $(( $(date +%s) - $2 )) s"
}

promote_drill() {
  if [ -e "$DRILL_HOME" ]; then
    drill_remove || promote_refuse "drill: will not delete $DRILL_HOME, which a drill that did not finish left; remove it by hand"
    echo "drill   : removed $DRILL_HOME, which a drill that did not finish left"
  fi
  [ -e "$SD_HUB_CONFIG" ] || promote_refuse "no $SD_HUB_CONFIG: the drill runs on a standby satellite"
  began=$(date +%s)
  ready=$(hub_ready)
  printf '%s\n' "$ready"
  ! printf '%s\n' "$ready" | grep -q 'NOT-READY' || promote_refuse "drill: a hub-ready line is not ok"
  drill_time "step 1  : preflight" "$began"
  at=$(date +%s)
  # The marker first: a home killed before it is empty, and goes by rmdir.
  mkdir -p "$DRILL_HOME"
  : > "$DRILL_HOME/$DRILL_MARK"
  mkdir -p "$DRILL_HOME/home"
  # A signal exits, and so runs EXIT, as trap_tmp does.
  trap drill_cleanup EXIT
  trap 'exit 129' HUP
  trap 'exit 130' INT
  trap 'exit 143' TERM
  set --
  for pr in $(promote_roots | tr ' ' '\001'); do set -- "$@" "$(printf '%s' "$pr" | tr '\001' ' ')"; done
  # The live state and config folders stay out: the throwaway home is the
  # only place the restore writes. Bounded as promote's step 5 (sd:4816).
  out=$(bound_step 1800 env -u XDG_STATE_HOME -u XDG_CONFIG_HOME -u XDG_DATA_HOME HOME="$DRILL_HOME/home" \
    "$SD_PACK_ROOT/bin/sd-db" restore --newest "$@" 2>&1) || promote_refuse "drill: restore --newest failed (exit $?): $out"
  printf '%s\n' "$out"
  snapshot=$(printf '%s\n' "$out" | sed -n 's/^sd-db: restored \(.*\) (checkpoint .*) to .*/\1/p' | tail -1)
  [ -n "$snapshot" ] || promote_refuse "drill: restore --newest named no snapshot"
  drill_time "step 5-6: restore and settle" "$at"
  at=$(date +%s)
  db="$DRILL_HOME/home/.local/share/sd/sd.db"
  port=$("$SD_DB_PYTHON" -I -c 'import socket
with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    print(s.getsockname()[1])') || promote_refuse "drill: no free loopback port"
  env -u XDG_STATE_HOME -u XDG_CONFIG_HOME -u XDG_DATA_HOME HOME="$DRILL_HOME/home" \
    "$SD_PACK_ROOT/bin/sd-db" serve --loopback --port "$port" --database "$db" </dev/null >"$DRILL_HOME/serve.log" 2>&1 &
  DRILL_SERVE=$!
  # One session reads the restore row the drill wrote, so it reached this copy.
  "$SD_DB_PYTHON" -I -c 'import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from sd_db import hub, remote, serve
db, token = Path(sys.argv[3]), Path(sys.argv[3] + serve.TOKEN_SUFFIX)
deadline = time.monotonic() + 30
while True:
    try:
        session = remote.connect("127.0.0.1", int(sys.argv[2]), db, token=serve.read_token(token), write=False,
                                 timeout=hub.CONNECT_TIMEOUT, answer_timeout=hub.ANSWER_TIMEOUT)
        break
    except Exception as error:
        if time.monotonic() > deadline:
            sys.exit(f"drill: no session on the loopback server: {type(error).__name__}: {error}")
        time.sleep(0.2)
try:
    row = session.execute("SELECT 1 FROM state WHERE kind = ? AND key = ?", ("restore", sys.argv[4])).fetchone()
finally:
    session.close()
if row is None:
    sys.exit("drill: the session reached a database without this restore")' \
    "$SD_PACK_ROOT/lib" "$port" "$db" "$(basename "$snapshot")" </dev/null ||
    promote_refuse "drill: the session failed; the server said: $(tail -3 "$DRILL_HOME/serve.log")"
  drill_time "step 9  : one session served" "$at"
  drill_time "total   " "$began"
  echo "drill   : passed on $snapshot; $DRILL_HOME is deleted"
  role_accounts drill
}

# ------------------------------------------------------------------ main ----

VERB=""
ARG=""
ARG2=""
FROM_NEXT=0
for a in "$@"; do
  if [ "$FROM_NEXT" = 1 ]; then PROMOTE_FROM="$PROMOTE_FROM$a
"; FROM_NEXT=0; continue; fi
  case "$a" in
    --apply) APPLY=1 ;;
    --from) [ "$VERB" = promote ] || die "--from belongs to promote"; FROM_NEXT=1 ;;
    --undo) [ "$VERB" = promote ] || die "--undo belongs to promote"; PROMOTE_UNDO=1 ;;
    --drill) [ "$VERB" = promote ] || die "--drill belongs to promote"; PROMOTE_DRILL=1 ;;
    --additive) ADDITIVE=1 ;;
    --fail-on-drift) FAIL_ON_DRIFT=1 ;;
    --stale-ok) STALE_OK=1 ;;
    --force) FORCE=1 ;;
    -h|--help|help)
      echo "usage: machine-setup.sh <verb> [args] [--apply]"
      echo "verbs: $VERBS"
      echo
      cat <<'HELPEOF'
  setup <profile>  provision a new machine and record the profile
                   profile is any <name> with a <name>.<kind> file in the
                   profile directory (see `profiles`)
  update [stage]   re-run the stages for this machine's recorded profile
                   a copied file (dotfile, .env, launch agent) that differs is
                   judged against what was last installed: STALE means the repo
                   moved and --apply refreshes it, DIFFERS means this machine
                   edited it and it is left alone
                   --force overwrites a DIFFERS dotfile or launch agent too,
                   keeping a .bak- copy; a .env is never overwritten, only
                   given the keys its template added
                   every copy goes to <config>/machine-setup/backups/<host>/;
                   --apply moves .bak- and .pre- copies found elsewhere in the
                   config folder there and removes copies over 30 days old
  capture          write this machine's current state back into its profile,
                   so a change made on one machine reaches the others
                   --additive writes roster entries that appeared and refuses
                   to write one away, exiting 3 if a removal was held. Skips
                   the stages that copy file content, where additive has no
                   meaning. This is what makes an unattended capture safe to
                   write: see profile-autocapture.sh
                   --apply actually writes; without it capture is a dry run
                   entries are regenerated; the comment block above the first
                   entry is carried across, comments between entries are not
                   when the profile directory is in a git checkout, refuses
                   to write from one behind its upstream, since
                   those commits may be what changed the manifests
                   --stale-ok writes anyway
                   a file it changes that git does not hold as it is keeps a
                   dated copy in backups/<host>/
  status           drift report: what the profile wants vs what is installed
                   --fail-on-drift exits 1 when drift exists (for cron);
                   in-place drift (DIFFERS) prints an SD_REPORT_ATTENTION
                   line per finding instead and alone exits 0
                   a drift report, not a convention-6 health status: it runs
                   every stage, so the health sweep leaves it out; exit 3
                   when there is no profile directory or no recorded profile
  promote          make this satellite the sd hub once the hub is dead
                   (sd:4012): refuses while the hub answers or a hub-ready
                   line is not ok; then holds the lanes, moves hub.json
                   aside, restores the newest verified snapshot under the
                   NAS roots and each --from DIR, seeds the hourly backup
                   root, takes the hub's tailnet name and installs the hub
                   role; while a NAS root cannot be listed the marker
                   stays at its hold step and the NAS copies refuse; a
                   rerun goes on at the step the promote-intent marker
                   names; its report lists each login to make by hand
                   (sd:5986); without --apply prints the hub-ready lines
                   --undo hands the role back from a promoted satellite:
                   stops the hub role, writes the hand-back snapshot to
                   the NAS hourly root, puts hub.json back and gives up the
                   tailnet name; once the new hub serves that snapshot, it
                   deletes the local records and runs `update satellite`,
                   and lists each login to sign out of by hand;
                   a rerun goes on at the step the promote-undo marker names
                   --drill rehearses steps 1, 5, 6 and 9 on a standby
                   satellite: restores the newest snapshot into a
                   throwaway home, serves it on loopback, opens one
                   session, prints each step's seconds, and deletes the
                   home, pass or fail; hub.json, launchd and Tailscale stay
  doctor           read-only sanity check: CLT, brew, docker, fnm, key
                   material, launchd agents, and which .env files still
                   need filling in on this machine; on a profile with an sd
                   agent, also the database (integrity_check), the
                   dashboard's tailscale route and its answer
                   over the HTTPS name, and the pack's lib/sd_db, the one
                   each sd interpreter imports, beside the database's
                   schema version; no
                   sqlite3 on PATH, the dashboard's origin under Funnel,
                   :8443 held by another route, a service config naming
                   another database, or an env.sh that fails when sourced,
                   is a FAIL
  doctor sd        those sd checks alone, exit 1 on any FAIL
  doctor gate      the weekly gate run: make check in a snapshot of this
                   checkout's head, then of the pack's, each inside
                   `sd gate run --max-wait 3600`; a pass or a real failure
                   is written to ~/.local/state/machine-setup/doctor-gate-last,
                   which hub-ready reads; a slot timeout exits 75, and it or
                   a signal writes nothing; exits 0 only on a pass
  test             run the unittest suite in tests/ (extra args go to unittest)
  upgrade          maintenance sweep: brew update/upgrade (casks with
                   --greedy, so self-updating ones move too, Claude Code's
                   claude-code@latest among them), brew cleanup, mas
                   upgrade, and each unpinned .release entry to its latest
                   tag (dry run without --apply, like everything else);
                   an apply logs each step as it starts and stops it at its
                   bound (300-1800 s, or MACHINE_SETUP_STEP_TIMEOUT seconds),
                   runs the rest, and exits 1 naming the steps that failed;
                   a step sudo refused for want of a terminal is deferred
  upgrade-report   cron flavor of upgrade: always applies, prints what got
                   upgraded / what failed / what is still outdated, with the
                   ai-apps inventory capture and the result of
                   the pack dashboard's `grants` probe; when nothing
                   is outdated it only runs the capture and the probe;
                   leftovers, a failed step or a missing grant print one
                   SD_REPORT_ATTENTION line for Reports and Today, and a
                   deferred step's leftovers each name the command to run
                   in a terminal; exits 1
                   when that quiet-week capture failed
  checklist        print the manual new-machine steps no stage automates
                   (accounts, licenses, key restores; plus checklist.txt from the config)
  decommission     retire this machine: list dirty/unpushed repos, uninstall
                   cron jobs and manifest launch agents (--apply, after
                   typing "decommission" at an interactive prompt), and
                   print the manual sign-off list; destructive steps stay
                   manual. Without a usable profile it cannot name the launch
                   agents: it says so, still prints the list and exits 1
  compare <a> <b>  side-by-side of two profiles: matches, only-a, only-b
  report [profile] readable overview of every profile's entries across all
                   kinds (or just common + one profile)
  profiles         list the supported profile names
  triage [kind]    interactively file unassigned items (present on this
                   machine but in no manifest) into a profile; kind limits it
                   to one of brew, cask, mas, service, cron. service and cron
                   have no package manager behind them, so they use the same
                   discovery `candidates` reports from; filing a service warns
                   when a profile-mate already wants one of its ports. agent
                   is deliberately absent — capture rewrites <profile>.agent
                   wholesale and would overwrite a hand-filed label
  candidates [k]   read-only: what this machine COULD run that the profile
                   does not list yet, for the kinds triage cannot see —
                   service (docker services discovered under local-*, with
                   their published ports and any port they would fight over),
                   cron (jobs local-cron-jobs defines), agent (LaunchAgent
                   plists present here). k limits it to one of those
  apps             standalone /Applications report: what neither the App Store
                   nor Homebrew installed, and which of those have a cask
  adopt            brew adopts every standalone app that has a cask, in
                   place (no reinstall); files each token into
                   <profile>.cask and drops the name from <profile>.app.
                   Dry run without --apply; pkg casks may prompt for sudo

  --apply          actually make changes. WITHOUT IT EVERYTHING IS A DRY RUN,
                   which is the default so an unfamiliar machine can be
                   inspected before it is touched.
HELPEOF
      # Printed from $STAGES rather than spelled out: this list was already
      # wrong once (it predated the envs stage), and a help text that recites
      # an inventory drifts the moment the inventory changes.
      echo
      echo "stages, in order: $STAGES"
      cat <<'HELPEOF'
  pass one as the second argument to run it alone, e.g. `update brew`
  (obsidian is report-only: community plugins are installed from inside
  Obsidian, the stage just compares the vault against <profile>.obsidian)
  (iterm2 points iTerm2 at the synced prefs folder named in <profile>.iterm2;
  quit iTerm2 first, it rewrites its plist on quit)
  (prompts writes the shared system prompt into each coding agent's global
  instructions file — see local-agent-prompt)
  (system also compares Spotlight's privacy list with <profile>.spotlight:
  ok, MISSING or EXTRA per path. Reading it needs sudo and Full Disk Access
  for the terminal; without them it prints DEFER and goes on. --apply adds
  each missing path, then restarts mds once. It never removes a path.)

Profiles layer: common.<kind> applies everywhere, <profile>.<kind> adds to it;
a "!name" line in <profile>.<kind> drops common's entry name (or <tap>/name).
Kinds are tap, brew, cask, mas, service, cron, agent, macos, app, obsidian, iterm2,
spotlight, satellite, node, npm, mailto, release — plain lists with # comments. macos lines are
"<domain> <key> <type> <value>"; only keys listed there are ever touched or captured.
iterm2 holds a single folder path.
spotlight holds absolute paths; a leading ~ is this account's home.
node lists the node majors fnm keeps (none listed anywhere: 18, 20, 24); one line
may add "link" ("26 link") to point ~/.local/bin/node, npm and npx at fnm's newest
node of that major. release lines are "<bins> <owner/repo> <asset> [<tag>]": the
release stage puts each comma-listed bin of that GitHub release archive in
~/.local/bin; {version} and {tag} in <asset> take the tag; a <tag> pins it. npm lists
global npm CLIs (pkg or pkg@version) the tooling stage installs with the npm of
the brew manifest's node formula. mailto holds one bundle id, the profile's
before common's (neither: com.google.Chrome).

configuration (outside this repo; nothing private is committed here):
  $SYSTEM_TOOLS_CONFIG/machine-setup/
    profiles/       common.<kind> and <profile>.<kind> manifests
    dotfiles/       common/ and <profile>/ trees, copied into $HOME
    envs/           common/ and <profile>/ <folder>.env templates
    launchagents/   <label>.plist templates; @HOME@, @LABEL@, @ROOT@ and @PACK@
                    are replaced with $HOME, the label, this checkout's root
                    and the command pack checkout ($SD_PACK_ROOT)
    machine-setup.env  optional, sourced first; see machine-setup.env.example
    shell-env.example  optional, doctor's list of shell variables
    checklist.txt, decommission.txt  optional, appended to those verbs
  Start one with: cp -R local-machine-setup/examples/. <that folder>/

environment:
  SYSTEM_TOOLS_CONFIG     config root (default ${XDG_CONFIG_HOME:-~/.config}/system)
  MACHINE_SETUP_PROFILE_DIR, MACHINE_SETUP_DOTFILE_DIR, MACHINE_SETUP_ENV_DIR,
  MACHINE_SETUP_AGENT_DIR  override one of the four folders above
  SYSTEM_TOOLS_LABEL_PREFIX  launchd label prefix (default local.system-tools);
                          with OBSIDIAN_VAULT, set once in shared.env
  MACHINE_SETUP_PROFILE   override the recorded profile for one run
  MACHINE_SETUP_PROFILES  the profile names, space-separated, when the
                          profile directory should not decide them
  MACHINE_SETUP_STATE     state dir (default ~/.config/machine-setup)
  MACHINE_SETUP_AGENT_GLOBS  extra LaunchAgent label globs capture owns,
                          beyond <prefix>.* (space-separated)
  MACHINE_SETUP_SSH_KEYS  doctor: ssh identity files under ~, any one of which
                          counts (space-separated); unset means not checked
  MACHINE_SETUP_PLACEHOLDER_EXPECTED  doctor: folders whose .env may keep
                          placeholder values (space-separated)
  OBSIDIAN_VAULT          vault path for the obsidian stage; unset skips it
                          (shared.env or the environment)
  MACHINE_SETUP_PYTHON    the python3 the obsidian stage reads the vault with
  MACHINE_SETUP_STEP_TIMEOUT  upgrade: one bound, in seconds, for every step
  MACHINE_SETUP_SUDO_CASKS  upgrade: casks an unattended run also upgrades
                          with the Keychain item's password, beyond those
                          brew info shows need sudo (space-separated)
  MACHINE_SETUP_RELEASE_BIN  release: where the bins go (default ~/.local/bin)
  MACHINE_SETUP_RELEASE_BASE  tests only: the URL that stands in for https://github.com
  SD_PACK_ROOT            the command pack checkout
                          (default ~/repos/platypeeps/sd-ai-command-pack)
  SD_DB_PYTHON            satellite: the interpreter sd_db runs under
                          (default the pack's .venv/bin/python)
  SD_DASHBOARD_PYTHON     doctor: another interpreter the dashboard may run
  SD_NOTIFY               hub-ready: the notify command sd-db reports with
                          (default notify on PATH)
  SD_DASHBOARD_PORT, SD_DASHBOARD_HTTPS_PORT  sd: the dashboard's loopback
                          port and its tailscale serve port (8767, 8443)
  GENAI_TRACES_TAILNET_PORT  sd: the hub's tailscale serve port to
                          genai-traces' OTLP HTTP port, for satellites (4339)
  MACHINE_SETUP_SPOTLIGHT_PLIST  tests only: the plist that stands in for
                          Spotlight's VolumeConfiguration.plist
  MACHINE_SETUP_PLISTBUDDY  tests only: the PlistBuddy that edits it
  MACHINE_SETUP_SPOTLIGHT_SUDOERS  tests only: the Spotlight read drop-in
  MACHINE_SETUP_REPAIRS_SUDOERS  tests only: the repair drop-in doctor checks
  MACHINE_SETUP_SECURITY  tests only: the command that reads the Keychain
  MACHINE_SETUP_UV  tests only: the uv that installs sd-serve's python
  MACHINE_SETUP_TIMEOUT_SUDOERS  tests only: the sudo grace-period drop-in
  MACHINE_SETUP_NSMB_CONF  tests only: the file that stands in for /etc/nsmb.conf
  MACHINE_SETUP_APPLICATIONS_DIR  tests only: the folder the apps scan reads
HELPEOF
      exit 0
      ;;
    # A mistyped option used to fall into the arm below as an argument, so
    # `status --fail-on-drft` ran and never failed on drift (sd:3545). `test`
    # hands its options to unittest.
    --*) [ "$VERB" = test ] || die "unknown option $a (see: $(basename "$0") help)" ;;
    *)
      if [ -z "$VERB" ]; then
        case " $VERBS " in *" $a "*) VERB="$a" ;; esac
      elif [ -z "$ARG" ]; then ARG="$a"; elif [ -z "$ARG2" ]; then ARG2="$a"; fi ;;
  esac
done

case "$VERB" in
  setup)
    [ -n "$ARG" ] || { require_config; die "setup needs a profile: $(known_profiles | tr '\n' ' ' | sed 's/ $//' | sed 's/ /, /g')"; }
    cmd_setup "$ARG" "$ARG2"
    ;;
  update)  cmd_update "$ARG" ;;
  capture) cmd_capture ;;
  status)  cmd_status ;;
  promote) cmd_promote "$@" ;;
  compare)
    [ -n "$ARG" ] && [ -n "$ARG2" ] || die "compare needs two profiles, e.g. compare <a> <b> (see: $(basename "$0") profiles)"
    cmd_compare "$ARG" "$ARG2"
    ;;
  triage)  cmd_triage "$ARG" ;;
  candidates) cmd_candidates "$ARG" ;;
  apps)    cmd_apps ;;
  adopt)   cmd_adopt ;;
  report)  cmd_report "$ARG" ;;
  profiles) cmd_profiles ;;
  doctor)
    case "$ARG" in
      "") cmd_doctor ;;
      sd) cmd_doctor_sd ;;
      gate) cmd_doctor_gate ;;
      *)  die "doctor takes no argument, 'sd' for the database and dashboard checks alone, or 'gate' for the weekly gate run" ;;
    esac
    ;;
  # Python, not sh, for the same reason local-repo-sync tests
  # a shell script from Python: the CI wrapper asserts a unittest summary and
  # refuses skips. PYTHON is CI's venv; a machine runs it with its own.
  # The suite seeds its fixture database through sd_db -- nothing outside
  # the library opens the database or creates a table, and
  # tests/test_one_store.py greps the repository to keep it so --
  # so the pack's lib/, the sd_db doctor reads, goes on PYTHONPATH (sd:3279).
  test)
    shift
    PYTHONPATH="$SD_PACK_ROOT/lib${PYTHONPATH:+:$PYTHONPATH}" \
      exec "${PYTHON:-python3}" -m unittest discover -s "$DIR/tests" -t "$DIR" "$@"
    ;;
  upgrade) cmd_upgrade ;;
  upgrade-report) cmd_upgrade_report ;;
  checklist) cmd_checklist ;;
  decommission) cmd_decommission ;;
  *)
    echo "usage: $(basename "$0") <verb> [args] [--apply]" >&2
    echo "verbs: $VERBS  (help describes each)" >&2
    exit 1
    ;;
esac

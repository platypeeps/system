#!/bin/sh
# Provision and maintain machines from a named profile.
# Usage: machine-setup.sh setup|update|capture|status|compare|triage|candidates|apps|adopt|report|profiles|doctor|upgrade
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

# Per-owner values the script needs and the repository cannot hold: the
# vault path, the ssh key names, extra LaunchAgent globs. Every one has a
# default that checks nothing, so a missing file is fine.
# See machine-setup.env.example for the names.
if [ -f "$CONFIG_DIR/machine-setup.env" ]; then
  # shellcheck disable=SC1091
  . "$CONFIG_DIR/machine-setup.env"
fi

# launchd labels this repository's tools install: <prefix>.<name>, and
# <prefix>.cron.<job> for local-cron-jobs.
LABEL_PREFIX="${SYSTEM_TOOLS_LABEL_PREFIX:-local.system-tools}"

# The Directory Service node that holds local user records (dscl paths, not
# file paths: the record is there whatever the home directory is).
DS_USERS=/Users

STATE_DIR="${MACHINE_SETUP_STATE:-$HOME/.config/machine-setup}"
STATE_FILE="$STATE_DIR/profile"

# `sd` runs before `agents`: the dashboard opens the
# database at startup, so an agent bootstrapped first restarts against a
# file that does not exist yet. `satellite` sits beside it: a machine runs
# one of the two, and neither loads an agent the other needs.
STAGES="brew shell appstore bin dotfiles envs prompts repos cron sd satellite agents services macos tooling system obsidian iterm2"
KINDS="tap brew cask mas service cron agent macos app obsidian iterm2 spotlight satellite"

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
# dotfiles/envs/agents: overwrite a file this machine edited, not only a stale one.
FORCE=0

# Dotfiles safe to copy into the repo. ~/.bash_profile is deliberately absent:
# it holds ~40 live API keys, and capturing it would commit them.
DOTFILES=".zshrc .zshenv .zprofile .bash_aliases .gitconfig .gitignore_global .ssh/config .config/gh/config.yml .prism/.env .gito/.env .aws/config .vale.ini"
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
ENVS="local-notify"

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
  ' "$3" "$2" > "$3.merge.$$"
  mv "$3.merge.$$" "$3"
}

copy_dotfile() { # dotfile, src, dst
  if [ -n "$(dotfile_session_re "$1")" ] && [ -f "$3" ]; then
    run keep_sessions_cp "$1" "$2" "$3"
  else
    run cp "$2" "$3"
  fi
  case " $DOTFILES_PRIVATE " in *" $1 "*) run chmod 600 "$3" ;; esac
}

env_src() {
  if [ -f "$ENV_DIR/$PROFILE/$1.env" ]; then
    echo "$ENV_DIR/$PROFILE/$1.env"
  elif [ -f "$ENV_DIR/common/$1.env" ]; then
    echo "$ENV_DIR/common/$1.env"
  fi
}

die() { echo "machine-setup.sh: $*" >&2; exit 1; }

say() {
  if [ "$APPLY" -eq 1 ]; then printf '  %s\n' "$*"; else printf '  [dry-run] %s\n' "$*"; fi
}

# stdin comes from /dev/null on purpose. Every stage loop is fed by a pipe —
# `manifest brew | while read -r f` — and a command that reads stdin inside the
# loop eats the rest of the manifest. `brew install python@3.13` did exactly
# that on the work machine: ripgrep, rtk, shellcheck, smartmontools, uv and
# wget were swallowed, never installed, and never reported, because the loop
# had no lines left to read.
run() {
  if [ "$APPLY" -eq 1 ]; then
    "$@" </dev/null
  else
    printf '  [dry-run] %s\n' "$*"
  fi
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
# the next run decides.
record_hash() { # key, file
  [ "$APPLY" -eq 1 ] || return 0
  [ -f "$2" ] || return 0
  mkdir -p "$INSTALLED_DIR"
  shasum -a 256 < "$2" | awk '{print $1}' > "$INSTALLED_DIR/$(state_key "$1").sha"
  return 0
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
# between --force and a file nobody can get back.
backup_file() { # path
  [ -e "$1" ] || return 0
  cp -p "$1" "$1.bak-$(date +%Y%m%d%H%M%S)"
  return 0
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
    if sudo "$@" </dev/null; then
      return 0
    fi
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

# Entries of one profile file, comments and blanks stripped. `|| :` matters:
# under set -e a comment-only file makes grep exit 1, which aborted the
# assigned-list builder mid-loop and mislabeled everything as unassigned.
profile_entries() {
  f="$PROFILE_DIR/$1.$2"
  if [ -f "$f" ]; then
    sed -e 's/#.*//' -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' "$f" | grep -v '^$' || :
  fi
}

# Strips comments and blank lines from a manifest.
manifest() {
  kind="$1"
  for p in common "$PROFILE"; do
    f="$PROFILE_DIR/$p.$kind"
    [ -f "$f" ] && sed -e 's/#.*//' -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' "$f"
  done | grep -v '^$' | sort -u
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
  exit 1
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

# Formulae kept pinned (sd:3062). Homebrew python is ad-hoc signed, so every
# patch upgrade drops the macOS grants its binary held; the operator unpins
# and re-grants on purpose. python@3.12 is left as it is.
BREW_PINNED="python@3.14"

# Each BREW_PINNED formula that is installed and not pinned, one per line.
unpinned_formulae() {
  _have=$(brew list --formula 2>/dev/null | sed 's|.*/||')
  _pinned=$(brew list --pinned 2>/dev/null | sed 's|.*/||')
  for _f in $BREW_PINNED; do
    printf '%s\n' "$_have" | grep -qx "$_f" || continue
    printf '%s\n' "$_pinned" | grep -qx "$_f" || echo "$_f"
  done
}

stage_brew() {
  echo "== brew"
  command -v brew >/dev/null 2>&1 || {
    echo "  brew MISSING — install first: https://brew.sh"
    return 0
  }

  manifest tap | while read -r t; do
    if brew tap 2>/dev/null | grep -qx "$t"; then
      echo "  ok      tap $t"
    else
      run brew tap "$t"
    fi
  done

  installed=$(brew list --formula 2>/dev/null | sed 's|.*/||' | sort)
  manifest brew | while read -r f; do
    # Casks and versioned formulae report under their bare name.
    if echo "$installed" | grep -qx "$(echo "$f" | sed 's|.*/||')"; then
      echo "  ok      $f"
    else
      run brew install "$f"
    fi
  done

  installed_casks=$(brew list --cask 2>/dev/null | sort)
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
      run brew install --cask --adopt "$c"
    fi
  done

  unpinned=$(unpinned_formulae)
  for f in $BREW_PINNED; do
    if printf '%s\n' "$unpinned" | grep -qx "$f"; then
      run brew pin "$f"
    elif brew list --formula 2>/dev/null | sed 's|.*/||' | grep -qx "$f"; then
      echo "  ok      pinned $f"
    fi
  done
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
        eval "$("$b" shellenv)"
        break
      fi
    done
  fi
  if ! command -v brew >/dev/null 2>&1; then
    # NONINTERACTIVE skips the installer's confirmation prompt; it still
    # needs sudo internally, so on a no-admin machine this fails and the
    # stage reports MISSING rather than half-converting.
    run /bin/bash -c 'NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
    for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do
      [ -x "$b" ] && eval "$("$b" shellenv)" && break
    done
  fi
  if ! command -v brew >/dev/null 2>&1; then
    echo "  brew MISSING — install failed or dry run; https://brew.sh, then re-run: $(basename "$SELF") update shell --apply"
    return 0
  fi
  echo "  ok      brew present ($(command -v brew))"

  BREW_BASH="$(brew --prefix)/bin/bash"
  if [ -x "$BREW_BASH" ]; then
    echo "  ok      brew bash installed ($BREW_BASH)"
  else
    echo "  MISSING brew bash ($BREW_BASH)"
    run brew install bash
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
  mas_listing=$(mas list 2>/dev/null || :)
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
      if ! run mas install "$id"; then
        echo "  FAILED  mas install $id $name — install it by hand"
      fi
    fi
  done
}

stage_bin() {
  echo "== bin"
  if [ -x "$ROOT/local-bin-links/bin-links.sh" ]; then
    if [ "$APPLY" -eq 1 ]; then
      "$ROOT/local-bin-links/bin-links.sh" install | sed 's/^/  /'
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
      run mkdir -p "$(dirname "$dst")"
      copy_dotfile "$f" "$src" "$dst"
      record_hash "dotfiles/$f" "$(dotfile_view "$f" "$dst" "$view")"
    elif cmp -s "$src" "$(dotfile_view "$f" "$dst" "$view")"; then
      echo "  ok      $f"
      record_hash "dotfiles/$f" "$(dotfile_view "$f" "$dst" "$view")"
    else
      # The machine file still being what we installed means nobody here
      # touched it, so the repo is what moved.
      if matches_record "dotfiles/$f" "$(dotfile_view "$f" "$dst" "$view")"; then
          echo "  STALE   $f — untouched here, repo moved ahead"
          run backup_file "$dst"
          copy_dotfile "$f" "$src" "$dst"
          record_hash "dotfiles/$f" "$(dotfile_view "$f" "$dst" "$view")"
      elif has_record "dotfiles/$f"; then
          if [ "$FORCE" -eq 1 ]; then
            echo "  DIFFERS $f — edited here; --force overwrites (backup kept)"
            run backup_file "$dst"
            copy_dotfile "$f" "$src" "$dst"
            record_hash "dotfiles/$f" "$(dotfile_view "$f" "$dst" "$view")"
          else
            echo "  DIFFERS $f — edited here; keep it with 'capture --apply', discard it with --force"
          fi
      elif [ "$FORCE" -eq 1 ]; then
          echo "  DIFFERS $f — nothing recorded; --force overwrites (backup kept)"
          run backup_file "$dst"
          copy_dotfile "$f" "$src" "$dst"
          record_hash "dotfiles/$f" "$(dotfile_view "$f" "$dst" "$view")"
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
      run mkdir -p "$(dirname "$dst")"
      run cp "$src" "$dst"
      run chmod 600 "$dst"
      record_hash "envs/$f" "$src"
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
            backup_file "$dst"
            for k in $missing; do
              sed -n "s/^[[:space:]]*\($k=.*\)/\1/p" "$src" | head -1 >> "$dst"
            done
            echo "          appended from the template; fill in any placeholder"
            record_hash "envs/$f" "$src"
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
      "$ROOT/local-agent-prompt/agent-prompt.sh" refresh --apply | sed 's/^/  /'
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
      "$ROOT/local-repo-sync/repo-sync.sh" sync | sed 's/^/  /'
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
}

# The shared CLAUDE.local.md keys, SHARED_LOCAL_KEYS in claude_settings.py, in
# each auto repo checked out here (sd:3030). The source wins for those keys;
# the repo's other keys, its comments and the lines outside the block stay.
# A file git does not ignore is reported and never written: the block is
# per machine, and a tracked copy would publish it.
local_blocks() {
  if ! rows=$("$ROOT/local-sd-db/sd-db.sh" repo list 2>/dev/null </dev/null); then
    echo "  SKIP    CLAUDE.local.md shared keys: sd-db.sh repo list failed"
    return 0
  fi
  # `sd-db: <path>  <url>  ...  <runner merge>`: two spaces between fields,
  # and a path may hold one.
  printf '%s\n' "$rows" | sed -n 's/^sd-db: //p' | awk -F '  ' '$NF == "auto" {print $1}' |
  while IFS= read -r repo; do
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

# Did local-cron-jobs write this plist? The <prefix>.cron.<name> label does not
# say: another repository's installer used the same label shape, and the sweep
# below uninstalled its agent as an orphan (sd:2321). write_plist in
# local-cron-jobs runs every job as `/bin/bash <dir>/local-cron-jobs/cron-jobs.sh
# exec <job>` under the label <prefix>.cron.<job>, and that pair is the mark.
# Every plist local-cron-jobs has rendered carries it, so an agent installed
# before this check is recognised without being rewritten. A new marker key
# would have made every installed plist STALE and reloaded every job at once.
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
ours = (isinstance(plist, dict) and plist.get("Label") == label
        and isinstance(args, list) and len(args) == 4
        and args[0] == "/bin/bash"
        and isinstance(args[1], str) and args[1].endswith("/local-cron-jobs/cron-jobs.sh")
        and args[2] == "exec" and args[3] == job)
sys.exit(0 if ours else 1)
PY
}

stage_cron() {
  echo "== cron"
  # Without its host jobs, the sweep below read each one as an orphan and
  # uninstalled it every night (sd:2221). When they cannot be listed, the
  # wanted list is incomplete, so the sweep is skipped rather than guessed.
  sweep=1
  if ! host_jobs=$(host_cron_jobs); then
    echo "  MISSING host job list — could not read this host's cron-jobs folder; nothing is uninstalled this run"
    host_jobs=
    sweep=0
  fi
  jobs=$( { manifest cron; printf '%s\n' "$host_jobs"; } | awk 'NF && !seen[$0]++')
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
    jp="$HOME/Library/LaunchAgents/$LABEL_PREFIX.cron.$j.plist"
    owner=0
    [ ! -f "$jp" ] || cron_plist_ours "$jp" "$j" 2>/dev/null || owner=$?
    if [ ! -f "$jp" ]; then
      echo "  MISSING $j — no plist installed"
      run "$ROOT/local-cron-jobs/cron-jobs.sh" install "$j"
    elif [ "$owner" = 1 ]; then
      echo "  FOREIGN $j — in this profile, but another installer's agent holds its label; left in place"
    elif ! "$ROOT/local-cron-jobs/cron-jobs.sh" verify "$j" >/dev/null 2>&1; then
      echo "  STALE   $j — installed plist no longer matches the generator"
      run "$ROOT/local-cron-jobs/cron-jobs.sh" install "$j"
    # A plist can be present and current yet not registered with launchd — a
    # failed bootstrap, a bootout, files restored onto a machine that never
    # loaded them. No content check can see that; ask launchd. install
    # bootstraps, so re-running it is the repair as well as the install.
    elif ! launchctl print "gui/$(id -u)/$LABEL_PREFIX.cron.$j" >/dev/null 2>&1; then
      echo "  UNLOADED $j — plist current but launchd is not running it"
      run "$ROOT/local-cron-jobs/cron-jobs.sh" install "$j"
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
    if ! echo "$jobs" | grep -qx "$xj"; then
      owner=0
      cron_plist_ours "$xp" "$xj" || owner=$?
      case "$owner" in
        0) echo "  EXTRA   $xj — installed but no longer in this profile"
           run "$ROOT/local-cron-jobs/cron-jobs.sh" uninstall "$xj" ;;
        1) echo "  FOREIGN $xj — not installed by local-cron-jobs; left in place" ;;
        *) echo "  UNKNOWN $xj — cannot tell who installed it; left in place" ;;
      esac
    fi
  done
}

# A tracked plist as this machine installs it: @HOME@, @LABEL@ and @ROOT@
# (this checkout) replaced, so one file serves any account name and any
# checkout location. A plist without placeholders comes out unchanged, which is
# what a captured one is. Writes into $3 and prints nothing.
sed_value() { printf '%s' "$1" | sed 's/[|&\\]/\\&/g'; }
agent_render() { # label, tracked plist, rendered plist
  sed -e "s|@HOME@|$(sed_value "$HOME")|g" \
      -e "s|@LABEL@|$(sed_value "$1")|g" \
      -e "s|@ROOT@|$(sed_value "$ROOT")|g" "$2" > "$3"
}

# LaunchAgent plists captured into launchagents/. Same never-overwrite rule
# as dotfiles: a machine-local plist edit wins until capture. A captured plist
# embeds absolute paths, so it only transplants between machines with the same
# username and checkout location; a plist written with the agent_render
# placeholders transplants anywhere.
stage_agents() {
  echo "== agents"
  agents=$(manifest agent)
  if [ -z "$agents" ]; then
    echo "  no launch agents in this profile"
    return 0
  fi
  rendered=$(mktemp -d)
  echo "$agents" | while read -r label; do
    # A guard: no satellite profile lists a hub-only agent today, and one
    # that adds it later must not start a second dashboard.
    # Skipped, not removed: the satellite stage reports one already here.
    if [ -e "$SD_HUB_CONFIG" ] && sd_hub_only "$label"; then
      echo "  SKIP    $label — hub only, and $SD_HUB_CONFIG makes this machine a satellite"
      continue
    fi
    src="$AGENT_DIR/$label.plist"
    dst="$HOME/Library/LaunchAgents/$label.plist"
    if [ -f "$src" ]; then
      agent_render "$label" "$src" "$rendered/$label.plist"
      src="$rendered/$label.plist"
    fi
    if [ ! -f "$src" ]; then
      echo "  --      $label not captured in the repo yet"
    elif [ ! -e "$dst" ]; then
      # Every other branch below prints a word the drift counter greps for.
      # This one used to print only its `run` lines, so a launch agent that
      # was never installed showed a remediation and still counted as zero
      # drift -- how one metering agent stayed missing for a day.
      echo "  MISSING $label — no plist here yet"
      run cp "$src" "$dst"
      record_hash "agents/$label" "$dst"
      run launchctl bootstrap "gui/$(id -u)" "$dst"
    elif ! cmp -s "$src" "$dst"; then
      if matches_record "agents/$label" "$dst"; then
          echo "  STALE   $label — untouched here, repo moved ahead"
          run backup_file "$dst"
          run cp "$src" "$dst"
          record_hash "agents/$label" "$dst"
          # launchd holds the plist it was handed; a replaced file on disk
          # changes nothing until the job is booted out and back in.
          run launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || :
          run launchctl bootstrap "gui/$(id -u)" "$dst"
      elif has_record "agents/$label"; then
          if [ "$FORCE" -eq 1 ]; then
            echo "  DIFFERS $label — edited here; --force overwrites (backup kept)"
            run backup_file "$dst"
            run cp "$src" "$dst"
            record_hash "agents/$label" "$dst"
            run launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || :
            run launchctl bootstrap "gui/$(id -u)" "$dst"
          else
            echo "  DIFFERS $label — edited here; keep it with 'capture --apply', discard it with --force"
          fi
      elif [ "$FORCE" -eq 1 ]; then
          echo "  DIFFERS $label — nothing recorded; --force overwrites (backup kept)"
          run backup_file "$dst"
          run cp "$src" "$dst"
          record_hash "agents/$label" "$dst"
          run launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || :
          run launchctl bootstrap "gui/$(id -u)" "$dst"
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
  # `svc`, not `s`: do_stages iterates `for s in $STAGES`. A pipeline subshell
  # happens to shield it here, but the appstore stage lost that bet already.
  echo "$svcs" | while read -r svc; do
    name=$(echo "$svc" | sed 's/^local-//')
    entry="$ROOT/$svc/$name.sh"
    if [ ! -x "$entry" ]; then
      echo "  SKIP    $svc (no entrypoint at $svc/$name.sh)"
    elif service_running "$svc" "$(docker ps --format '{{.Names}}' 2>/dev/null)"; then
      echo "  ok      $svc running"
    else
      run "$entry" start
    fi
  done
}

# ----------------------------------------------------------------- verbs ----

# Developer tooling that doctor already checks for: fnm node versions,
# rtk hooks, and Chrome as the mailto handler (via duti, in common.brew).
# Everything is idempotent — present things are skipped, not reinstalled.
stage_tooling() {
  echo "== tooling"
  if command -v fnm >/dev/null 2>&1; then
    have=$(fnm ls 2>/dev/null || :)
    for v in 18 20 24; do
      case "$have" in
        *"v$v."*) echo "  ok      node $v (fnm)" ;;
        *) run fnm install "$v" ;;
      esac
    done
  else
    echo "  SKIP    fnm not installed (brew stage provides it)"
  fi
  if command -v rtk >/dev/null 2>&1; then
    if [ -f "$HOME/.claude/settings.json" ] && grep -q 'rtk' "$HOME/.claude/settings.json" 2>/dev/null; then
      echo "  ok      rtk hook present in ~/.claude/settings.json"
    else
      run rtk init -g
    fi
    if [ -f "$HOME/.gemini/hooks/rtk-hook-gemini.sh" ]; then
      echo "  ok      rtk hook present for Gemini CLI"
    else
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
  # operator's own config is never overwritten (sd:2929).
  if command -v claude >/dev/null 2>&1; then
    if ls -d "$HOME"/.claude/plugins/cache/*/claude-hud/*/ >/dev/null 2>&1; then
      echo "  ok      claude-hud plugin"
    else
      echo "  MISSING claude-hud plugin"
      [ -d "$HOME/.claude/plugins/marketplaces/claude-hud" ] \
        || run claude plugin marketplace add jarrodwatts/claude-hud
      run claude plugin install claude-hud@claude-hud
    fi
    hud_config="$HOME/.claude/plugins/claude-hud/config.json"
    if [ -e "$hud_config" ] || [ -L "$hud_config" ]; then
      echo "  ok      claude-hud config"
    else
      echo "  MISSING claude-hud config"
      run mkdir -p "${hud_config%/*}"
      run cp -n "$ROOT/local-statusline/claude-hud.config.example.json" "$hud_config"
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
    # attribution lines, the `sd today` hook, and DISABLE_AUTOUPDATER (the
    # weekly upgrade updates Claude Code). It adds missing entries after a
    # backup and never touches the operator's own.
    if [ -f "$HOME/.claude/settings.json" ]; then
      if gaps=$(python3 "$DIR/claude_settings.py" missing "$HOME/.claude/settings.json" 2>/dev/null); then
        if [ -z "$gaps" ]; then
          echo "  ok      settings.json baseline"
        else
          printf '%s\n' "$gaps" | sed 's/^/  MISSING settings.json /'
          run backup_file "$HOME/.claude/settings.json"
          run python3 "$DIR/claude_settings.py" merge "$HOME/.claude/settings.json"
        fi
      else
        echo "  DIFFERS settings.json is not valid JSON; baseline not checked"
      fi
    fi
  else
    echo "  SKIP    claude not installed (HUD not set up)"
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
          run backup_file "$opencode_json"
          run python3 "$DIR/claude_settings.py" opencode-merge "$opencode_json"
          ;;
      esac
    else
      echo "  DIFFERS opencode.json does not parse; read denies not checked"
    fi
  fi
  # task-actions' digest email buttons need a public URL; the tailscale
  # funnel provides it and --bg persists across reboots. Only on a machine
  # whose profile installs the task-actions agent — funnel on a machine with
  # nothing listening would publish a dead endpoint. Needs a logged-in
  # tailscale, `tailscale status` also fails when the root daemon is not
  # running, and `tailscale up` is an interactive browser login, so such a
  # machine skips with a pointer to the checklist rather than failing.
  if manifest agent | grep -qxF "$LABEL_PREFIX.task-actions"; then
    if ! command -v tailscale >/dev/null 2>&1; then
      echo "  SKIP    tailscale not installed (brew stage provides it)"
    elif ! tailscale status >/dev/null 2>&1; then
      echo "  SKIP    tailscale not running or not logged in — nothing starts the daemon, see \`$(basename "$SELF") checklist\`, then: $(basename "$SELF") update tooling --apply"
    else
      ta_port="${TASK_ACTIONS_PORT:-8766}"
      if tailscale funnel status 2>/dev/null | grep -q "127.0.0.1:$ta_port"; then
        echo "  ok      tailscale funnel -> 127.0.0.1:$ta_port"
      else
        # Same reason as stage_agents: name the gap so --fail-on-drift sees
        # it. An absent funnel means the digest email buttons have no public
        # URL, which is worth a nightly failure.
        echo "  MISSING tailscale funnel -> 127.0.0.1:$ta_port"
        run tailscale funnel --bg "$ta_port"
      fi
    fi
  fi
  if command -v duti >/dev/null 2>&1; then
    # duti -x only answers for file extensions; URL-scheme handlers have to
    # be read back from the LaunchServices store.
    if defaults read com.apple.LaunchServices/com.apple.launchservices.secure LSHandlers 2>/dev/null \
       | grep -B2 'LSHandlerURLScheme = .*mailto' | grep -qi chrome; then
      echo "  ok      mailto handler is Chrome"
    else
      run duti -s com.google.Chrome mailto:
    fi
  else
    echo "  SKIP    duti not installed (it is in common.brew)"
  fi
}

# The database and the dashboard's private route — the two pieces of
# docs/work/2026-09-05-one-database-one-front-door that the `agents` stage
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
# One database path, and not an override: `sd-db.sh init` creates this file
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
# The satellite stage reports each one present as EXTRA; the agents stage
# skips each one while hub.json exists.
SD_HUB_ONLY_AGENTS="sd-dashboard sd-serve task-actions cron.sd-db-backup cron.sd-db-backup-hourly cron.offsite-verify cron.mirror-sync-nightly cron.shadow-sync-nightly"
sd_hub_only() { # label
  for sho in $SD_HUB_ONLY_AGENTS; do [ "$1" = "$LABEL_PREFIX.$sho" ] && return 0; done
  return 1
}
# The interpreter whose installed sd_db the satellite stage checks: the
# pack's, as sd-db.sh picks it.
SD_DB_PYTHON="${SD_DB_PYTHON:-$SD_PACK_ROOT/.venv/bin/python}"

# One read of `tailscale serve status` for the dashboard's port. The route
# is the line `https://<node>.<tailnet domain>:8443 (tailnet only)` followed
# by `|-- / proxy http://127.0.0.1:8767`: the root handler, and a proxy
# target equal to the dashboard's, not one that merely contains it (a
# :87670 target contains :8767). Prints `route <origin>` for that pair;
# otherwise `held <origin> <handler>` when the port serves something else;
# otherwise nothing. Reads stdin, so the stage and doctor parse one status
# read the same way.
sd_serve_scan() {
  awk -v want=":$SD_DASHBOARD_HTTPS_PORT" -v target="http://127.0.0.1:$SD_DASHBOARD_PORT" '
    function ours(o) { return length(o) > length(want) && substr(o, length(o) - length(want) + 1) == want }
    /^[a-z][a-z0-9+.-]*:\/\// { origin = $1; mine = ours(origin); next }
    !mine { next }
    origin ~ /^https:\/\// && $1 == "|--" && $2 == "/" && $3 == "proxy" && $4 == target { route = origin; exit }
    $1 == "|--" && held == "" { held = origin " " $2 " " $3 " " $4 }
    END { if (route != "") print "route " route; else if (held != "") print "held " held }
  '
}
sd_route_origin() { sd_serve_scan | sed -n 's/^route //p'; }
sd_route_holder() { sd_serve_scan | sed -n 's/^held //p'; }

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
  for sde_name in dashboard; do
    manifest agent | grep -qxF "$LABEL_PREFIX.sd-$sde_name" || continue
    sde_conf="$HOME/.config/sd/$sde_name.json"
    sde_db=$(sd_config_database "$sde_conf")
    [ -z "$sde_db" ] || [ "$sde_db" = "$SD_DB" ] || printf '%s names %s\n' "$sde_conf" "$sde_db"
  done
}

stage_sd() {
  echo "== sd"
  if ! sd_in_profile; then
    echo "  SKIP    no sd agent in this profile ($LABEL_PREFIX.sd-dashboard)"
    return 0
  fi
  if [ -f "$SD_DB" ]; then
    echo "  ok      database $SD_DB"
  else
    # Named so --fail-on-drift sees it: an agent loaded against no database
    # restarts on its throttle interval forever and still reads `ok loaded`.
    echo "  MISSING database $SD_DB — no database yet"
    run "$ROOT/local-sd-db/sd-db.sh" init
  fi
  sd_database_elsewhere | while IFS= read -r elsewhere; do
    echo "  DIFFERS database: $elsewhere, not $SD_DB — this stage builds and doctor checks only $SD_DB; remove the key or point it there"
  done
  # The hub's server for satellites (step 8 of the second-machine plan). The
  # agents stage installs and loads it like any agent; this stage says when
  # the profile or the config folder lacks it, a gap the agents stage
  # reports with no drift word. A removed installed plist is the agents
  # stage's MISSING, counted once.
  if ! manifest agent | grep -qxF "$LABEL_PREFIX.sd-serve"; then
    echo "  MISSING $LABEL_PREFIX.sd-serve in $PROFILE.agent — the hub serves no satellite; add the label, then: $(basename "$SELF") update agents --apply"
  elif [ ! -f "$AGENT_DIR/$LABEL_PREFIX.sd-serve.plist" ]; then
    echo "  MISSING $AGENT_DIR/$LABEL_PREFIX.sd-serve.plist — copy it from $DIR/examples/launchagents/"
  else
    echo "  ok      $LABEL_PREFIX.sd-serve in $PROFILE.agent"
  fi
  # Removed by hand, never here: on the hub it refuses every sd verb beside
  # the database (HubConflict), and the operator decides which role is wrong.
  if [ -e "$SD_HUB_CONFIG" ]; then
    echo "  EXTRA   $SD_HUB_CONFIG — this machine is the sd hub, and that file makes it a satellite too; remove it"
  fi
  if manifest agent | grep -qxF "$LABEL_PREFIX.sd-dashboard"; then
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
        # The flags local-project-dashboard/sd_dashboard/runtime.py adds and
        # disables the mount with, so either side can take it down again.
        run tailscale serve --bg "--https=$SD_DASHBOARD_HTTPS_PORT" --set-path=/ "http://127.0.0.1:$SD_DASHBOARD_PORT"
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
# run in the installed library (`sd_db.satellite`, under `-I`), because the
# build that must match the hub's is the one the pack runs, not this
# checkout. The stage installs no LaunchAgent and no sd cron job.
stage_satellite() {
  echo "== satellite"
  hubs=$(manifest satellite)
  # A satellite runs no hub service (prd R5). Detect only: the operator
  # decides which role is wrong. On a hub profile the sd stage's one EXTRA
  # for hub.json names the conflict instead (criterion 7).
  if ! sd_in_profile && { [ -n "$hubs" ] || [ -e "$SD_HUB_CONFIG" ]; }; then
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
  set -- --hub "$host"
  [ -z "$port" ] || set -- "$@" --port "$port"
  if [ "$APPLY" -eq 1 ]; then set -- "$@" --apply; fi
  # On a build mismatch, --apply installs the hub's build from origin/main of
  # this checkout (sd:2802). An environment variable, not a flag: an older
  # installed sd_db.satellite ignores it instead of refusing the flag.
  if out=$(SD_DB_SOURCE_CHECKOUT="${SD_DB_SOURCE_CHECKOUT:-$ROOT}" \
      "$SD_DB_PYTHON" -I -m sd_db.satellite "$@" 2>&1 </dev/null); then
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
    sudo_run "sudo touch /var/db/useLS" sudo touch /var/db/useLS
  fi
  if [ -e /etc/sudoers.d/timestamp-timeout ]; then
    echo "  ok      sudo timestamp_timeout drop-in"
  else
    # Held in a variable so the command shown in the deferred summary cannot
    # drift from the command actually run.
    sudoers_cmd='echo "Defaults timestamp_timeout=60" > /etc/sudoers.d/timestamp-timeout && chmod 440 /etc/sudoers.d/timestamp-timeout && visudo -c'
    sudo_run "sudo sh -c '$sudoers_cmd'" sudo sh -c "$sudoers_cmd"
  fi
  fw=/usr/libexec/ApplicationFirewall/socketfilterfw
  if [ -x "$fw" ]; then
    if "$fw" --getglobalstate 2>/dev/null | grep -q enabled; then
      echo "  ok      application firewall on"
    else
      sudo_run "sudo $fw --setglobalstate on" sudo "$fw" --setglobalstate on
    fi
    if "$fw" --getstealthmode 2>/dev/null | grep -q on; then
      echo "  ok      firewall stealth mode on"
    else
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
      sudo_run "sudo ln -s $(basename "$HOME") $alias_path" \
        sudo ln -s "$(basename "$HOME")" "$alias_path"
    done
  fi
  system_spotlight
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
# Without --apply this never prompts: `sudo -n` either has a cached ticket or
# the read defers. The nightly `status` run is non-interactive.
spotlight_read() { # outfile
  SPOTLIGHT_KEY=0; SPOTLIGHT_WHY=""; SPOTLIGHT_FIX=""
  : > "$1"
  if ! sudo -n true 2>/dev/null; then
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
  vault="${OBSIDIAN_VAULT:-}"
  if [ -z "$vault" ]; then
    echo "  SKIP    OBSIDIAN_VAULT not set (machine-setup.env or the environment)"
    return 0
  fi
  py="${MACHINE_SETUP_PYTHON:-python3}"
  installed=$("$py" - "$vault" 2>/dev/null <<'PYEOF'
import pathlib, sys
d = pathlib.Path(sys.argv[1]) / ".obsidian" / "plugins"
if d.is_dir():
    print("\n".join(sorted(p.name for p in d.iterdir() if p.is_dir())))
PYEOF
  ) || :
  if [ -z "$installed" ]; then
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
    echo "  no iTerm2 prefs folder in this profile"
    return 0
  fi
  if [ ! -d "$APPLICATIONS_DIR/iTerm.app" ]; then
    echo "  SKIP    iTerm.app not installed (cask iterm2)"
    return 0
  fi
  if [ ! -d "$dir" ]; then
    echo "  MISSING prefs folder $dir — not synced down yet; re-run once it is"
    return 0
  fi
  # Repointing iTerm2 at an empty folder makes it write fresh defaults there,
  # and the synced settings are what get lost. Stop before touching prefs.
  if [ ! -f "$dir/com.googlecode.iterm2.plist" ]; then
    echo "  MISSING $dir/com.googlecode.iterm2.plist — folder is empty, not repointing iTerm2"
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

do_stages() {
  want="$1"
  if [ -n "$want" ]; then
    case " $STAGES " in
      *" $want "*) ;;
      *) die "unknown stage '$want' (stages: $STAGES)" ;;
    esac
  fi
  for s in $STAGES; do
    if [ -n "$want" ] && [ "$want" != "$s" ]; then continue; fi
    "stage_$s"
    echo
  done
  report_deferred
}

# Anything sudo_run could not run. Printed once, at the end, where it is still
# on screen after a long run — a DEFER line 200 lines up is a line nobody sees.
# Exit stays 0: deferring is the expected outcome on a machine where the user
# cannot sudo, and doctor reports these same items independently, so the drift
# stays visible without an exit code that would surprise a caller.
report_deferred() {
  [ -n "$DEFERRED" ] || return 0
  n=$(printf '%s' "$DEFERRED" | grep -c '^' || :)
  echo "deferred — $n action(s) still need sudo. Run these, then re-run:"
  printf '%s' "$DEFERRED"
  echo "    $(basename "$0") update system --apply"
  echo
}

# Self-update off in every installed cask with `auto_updates true`, where the
# app documents a switch (sd:3062). The weekly upgrade-report runs
# `brew upgrade --cask --greedy`, so brew is the one updater: both machines
# change together, and the macOS privacy (TCC) grants an update drops are
# re-granted in one sitting. An app that is not installed is skipped.
#
# One row per app: cask|app|mechanism|target|key|value.
# - sparkle: target is the defaults domain. Sparkle keeps the user's choice
#   there under its Info.plist key names, SUEnableAutomaticChecks (scheduled
#   checks) and SUAutomaticallyUpdate (silent installs); both go false. Each
#   app ships Sparkle.framework. ChatGPT.app's domain is com.openai.codex,
#   read from its bundle.
# - defaults: a documented boolean in the app's domain. Claude desktop reads
#   disableAutoUpdates from com.anthropic.claudefordesktop.
# - json: one top-level key in the app's settings file, through
#   claude_settings.py. VS Code documents update.mode none and Zed
#   auto_update false. Docker Desktop keeps its "Always download updates"
#   setting as AutoDownloadUpdates in settings-store.json.
#
# No documented per-user switch, so left on (checked 2026-10-08): 1Password
# (a Settings > Advanced toggle only); Electron apps Antigravity, Beekeeper
# Studio, Discord, Dropbox, Notion, Obsidian, OpenWhispr, Postman and Slack;
# Beyond Compare, iStat Menus, Malwarebytes, Piezo and Sync with their own
# updaters; Firefox, Google Chrome, Google Drive and Zoom, whose switches are
# system-wide managed policy; and the CLIs antigravity-cli, copilot-cli and
# gcloud-cli. Updater agents are never removed to stop an app.
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
    "docker-desktop|Docker.app|json|$HOME/Library/Group Containers/group.com.docker/settings-store.json|AutoDownloadUpdates|false"
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
  run defaults write "$1" "$2" "-$3" "$4" \
    || echo "  DIFFERS $1 $2 not written; set it by hand to $4, or grant the terminal Full Disk Access and re-run"
}

# One key in a JSON settings file: ok, or MISSING/DIFFERS and the merge.
self_update_json() { # file key json-value
  if ! gap=$(python3 "$DIR/claude_settings.py" pref-missing "$1" "$2" "$3" 2>/dev/null </dev/null); then
    echo "  DIFFERS $1 does not parse; $2 not checked"
    return 0
  fi
  case "$gap" in
    '') echo "  ok      $1 $2 = $3"; return 0 ;;
    unset) echo "  MISSING $1 $2 = $3" ;;
    *) echo "  DIFFERS $1 $2 $gap, want $3" ;;
  esac
  run backup_file "$1"
  run mkdir -p "$(dirname "$1")"
  run python3 "$DIR/claude_settings.py" pref-merge "$1" "$2" "$3" \
    || echo "  DIFFERS $1 $2 not changed; set it to $3 by hand"
}

stage_self_update() {
  echo "  self-update off (casks that update themselves; the weekly upgrade moves them)"
  while IFS='|' read -r cask app how target key val; do
    [ -n "$app" ] && [ -d "$APPLICATIONS_DIR/$app" ] || continue
    case "$how" in
      sparkle)
        self_update_default "$target" SUEnableAutomaticChecks false
        self_update_default "$target" SUAutomaticallyUpdate false ;;
      defaults) self_update_default "$target" "$key" "$val" ;;
      json) self_update_json "$target" "$key" "$val" ;;
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
# checkout that holds it. ConnectTimeout bounds a fetch on a machine that cannot
# reach the remote; when it fails, this says so and compares against the ref on
# disk rather than pretending the checkout is current.
profile_repo() {
  command -v git >/dev/null 2>&1 || return 0
  git -C "$PROFILE_DIR" rev-parse --show-toplevel 2>/dev/null || :
}

check_repo_current() {
  repo=$(profile_repo)
  [ -n "$repo" ] || return 0
  up=$(git -C "$repo" rev-parse --abbrev-ref '@{u}' 2>/dev/null || echo origin/main)
  if ! GIT_SSH_COMMAND="ssh -o ConnectTimeout=5" \
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

  wm_hdr="$wm_target.hdr.$$"; wm_new="$wm_target.new.$$"
  for wm_line in "$@"; do printf '%s\n' "$wm_line"; done > "$wm_hdr"
  {
    cat "$wm_hdr"
    carry_comments "$wm_target" "$wm_hdr"
    # no trailing blank on a manifest that has no entries at all
    if [ -s "$wm_body" ]; then echo; cat "$wm_body"; fi
  } > "$wm_new"
  rm -f "$wm_hdr"
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
  trap 'rm -rf "$tmp"' EXIT INT TERM

  # A profile file holds only what common does not already provide, so capture
  # subtracts the common manifest instead of dumping the whole machine.
  brew tap 2>/dev/null | sort > "$tmp/have.tap"
  brew leaves 2>/dev/null | sort -u > "$tmp/have.brew"
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
  brew list --cask 2>/dev/null | sort > "$tmp/have.cask"
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
    mas list 2>/dev/null | sed -e 's/ *([^)]*)$//' -e 's/  */ /g' -e 's/^ *//' \
      > "$tmp/all.mas"
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
  # cmd_apps but without the per-app cask search (that is a report, not state).
  # Two sed expressions, not \| alternation — BSD sed has no BRE alternation,
  # and the GNU-ism silently matched nothing here.
  cmd_apps 2>/dev/null \
    | sed -n -e 's/^  \(.*[^ ]\)  *cask available.*/\1/p' -e 's/^  \(.*[^ ]\)  *no cask.*/\1/p' \
    | sort > "$tmp/out.app"
  n=$(wc -l < "$tmp/out.app" | tr -d ' ')
  echo "  app: $n entries"
  if [ "$APPLY" -eq 1 ]; then
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

common_manifest() {
  f="$PROFILE_DIR/common.$1"
  if [ -f "$f" ]; then
    sed -e 's/#.*//' -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' "$f" | grep -v '^$' | sort
  fi
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
    echo "$oap"
  done | sort -u
}

# Personal daemons captured into launchagents/. Same credential scan idea as
# dotfiles: a plist can carry secrets in EnvironmentVariables, so a
# credential-shaped value refuses the capture and keeps the label out of the
# manifest.
capture_agents() {
  tmp="$1"
  echo "  agents:"
  : > "$tmp/have.agent"
  for pl in $(owned_agent_plists); do
    label=$(basename "$pl" .plist)
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
  if [ "$APPLY" -eq 1 ]; then
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
  trap 'rm -rf "$tmp"' EXIT INT TERM

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
  trap 'rm -rf "$tmp"' EXIT INT TERM

  for kind in brew cask mas service cron; do
    if [ -n "$only_kind" ] && [ "$only_kind" != "$kind" ]; then continue; fi
    case "$kind" in
      brew) command -v brew >/dev/null 2>&1 || continue
            brew leaves 2>/dev/null | sed 's|.*/||' | sort > "$tmp/have" ;;
      cask) command -v brew >/dev/null 2>&1 || continue
            brew list --cask 2>/dev/null | sort > "$tmp/have" ;;
      mas)  command -v mas >/dev/null 2>&1 || continue
            mas list 2>/dev/null | sed -e 's/ *([^)]*)$//' -e 's/  */ /g' -e 's/^ *//' | sort > "$tmp/have" ;;
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
# local-nginx runs `brew services start nginx` and keeps a `docker run -p
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
  if cand_running=$(docker ps --format '{{.Names}}' 2>/dev/null); then
    cand_docker_observed=1
  else
    cand_running=""
    cand_docker_observed=0
  fi
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
standalone_scan() {
  out="$1"
  tmp=$(mktemp -d)
  trap 'rm -rf "$tmp"' EXIT INT TERM

  if command -v brew >/dev/null 2>&1; then
    brew info --json=v2 --installed --cask 2>/dev/null | python3 -c '
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
    brew list --cask 2>/dev/null | tr -d '-' | tr '[:upper:]' '[:lower:]' > "$tmp/cask-tokens" || :
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
  done | sort > "$tmp/standalone"

  : > "$out"
  while IFS= read -r name; do
    tok=$(echo "$name" | tr '[:upper:]' '[:lower:]' | sed -e 's/ /-/g' -e 's/[^a-z0-9-]//g')
    hit=""
    # Vendors name casks inconsistently, so try the obvious variants:
    # cleanshot-x -> cleanshot, languagetool-for-desktop -> languagetool-desktop.
    # No first-word fallback: it matched "GitHub Copilot" to the `github`
    # cask, which is GitHub Desktop — a match must cover the whole name.
    for cand in "$tok" "${tok%-x}" "$(echo "$tok" | sed 's/-for-desktop/-desktop/')"; do
      hit=$(brew search --cask "/^$cand\$/" 2>/dev/null | head -1 || :)
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
      if brew install --cask --adopt "$tok"; then
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
      cron|macos|iterm2|spotlight)
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
# file in WAL mode (local-sd-db/sd_db/database.py), and a read-only open of
# a WAL database whose -wal and -shm sidecars are absent — every database
# nothing currently holds open — fails with "unable to open database file
# (14)", which doctor would then report as a corrupted database. An ordinary
# open creates the sidecars and removes them on close; `query_only` is what
# keeps the statement a read.
sd_sqlite() { sqlite3 "$SD_DB" "PRAGMA query_only = 1; $1"; }

# Why a dashboard /health body says `"ok": false`. A failed database open
# carries an `error`; the ordinary unhealthy answer carries none and states
# its diagnostics as fields (local-project-dashboard/sd_dashboard/server.py,
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
# docs/work/2026-09-05-one-database-one-front-door asks doctor for, less the
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
  if [ -z "${PROFILE:-}" ] || ! sd_in_profile; then
    echo "  SKIP    sd pieces not checked (no sd agent in this profile)"
    return 0
  fi
  # Not a SKIP: every profile with an sd agent is a Mac, macOS ships
  # /usr/bin/sqlite3 and no profile installs another, so a doctor that
  # cannot find it runs on a broken PATH and has checked nothing (sd:1413).
  if ! command -v sqlite3 >/dev/null 2>&1; then
    doctor_fail "sqlite3 MISSING from PATH — database not checked; macOS ships /usr/bin/sqlite3, so put /usr/bin on PATH and re-run: $(basename "$SELF") doctor sd"
    db_ok=0
  elif [ ! -f "$SD_DB" ]; then
    doctor_fail "database MISSING at $SD_DB — run: $(basename "$SELF") update sd --apply"
    db_ok=0
  else
    integrity=$(sd_sqlite 'PRAGMA integrity_check;' 2>&1 | head -3 | tr '\n' ' ' | sed 's/ $//')
    if [ "$integrity" = ok ]; then
      echo "  ok      database $SD_DB passes integrity_check"
      db_ok=1
    else
      doctor_fail "database $SD_DB integrity_check: $integrity — restore a dated backup: local-sd-db/sd-db.sh restore /Volumes/local/Backup/sd-backups/<date>"
      db_ok=0
    fi
  fi
  # A here-document and not a pipe, so doctor_fail counts in this shell.
  while IFS= read -r elsewhere; do
    [ -n "$elsewhere" ] || continue
    doctor_fail "database DIFFERS: $elsewhere, not $SD_DB — the checks here read $SD_DB; remove the key or point it there"
  done <<EOF
$(sd_database_elsewhere)
EOF

  if manifest agent | grep -qxF "$LABEL_PREFIX.sd-dashboard"; then
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
        # would also drop task-actions' public 443.
        if printf '%s\n' "$serve" | grep -F "$origin" | grep -q 'Funnel on'; then
          doctor_fail "dashboard origin $origin is PUBLIC under Funnel — the front door is tailnet-only; run: tailscale serve --bg --https=$SD_DASHBOARD_HTTPS_PORT --set-path=/ off, then: $(basename "$SELF") update sd --apply (not tailscale funnel reset, which drops task-actions too); see local-project-dashboard/RUNTIME.md"
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
          echo "  WARN    dashboard answers over $origin with HTTP $code (Untrusted dashboard host) — the installed service runs a local-only configuration; see the private front door in local-project-dashboard/RUNTIME.md"
        else
          doctor_fail "dashboard answers over $origin with HTTP $code and not its health body"
        fi
        rm -f "$body" "$body.err"
      fi
    fi
  fi

  doctor_sd_versions
}

# The installed sd_db in each virtualenv, beside the schema version in the
# database and the one this checkout is built for, naming any that differs
# (prd.md:173-175). The virtualenvs are the pack's — what dashboard.sh serve
# defaults to — and whatever SD_DASHBOARD_PYTHON names, in the environment
# or in the installed plist.
# `-I` so a PYTHONPATH cannot stand in for the installed copy; the file path
# is checked for the same reason, criterion 1's last clause.
doctor_sd_versions() {
  checkout_schema=$(sed -n 's/^SCHEMA_VERSION *= *\([0-9][0-9]*\).*/\1/p' "$ROOT/local-sd-db/sd_db/schema.py" | head -1)
  checkout_dist=$(sed -n 's/^version *= *"\([^"]*\)".*/\1/p' "$ROOT/local-sd-db/pyproject.toml" | head -1)
  db_schema=""
  if [ -f "$SD_DB" ] && command -v sqlite3 >/dev/null 2>&1; then
    db_schema=$(sd_sqlite 'PRAGMA user_version;' 2>/dev/null || :)
  fi
  echo "  --      sd_db in this checkout: ${checkout_dist:-?} built for schema ${checkout_schema:-?}; database at schema ${db_schema:-none}"
  if [ -n "$db_schema" ] && [ "$db_schema" != "$checkout_schema" ]; then
    echo "  WARN    database schema $db_schema DIFFERS from this checkout's $checkout_schema — after a backup, with sd-dashboard and sd-serve stopped: local-sd-db/sd-db.sh migrate"
  fi
  seen=" "
  for py in "$SD_PACK_ROOT/.venv/bin/python" "${SD_DASHBOARD_PYTHON:-}" \
            "$(plist_env_value "$HOME/Library/LaunchAgents/$LABEL_PREFIX.sd-dashboard.plist" SD_DASHBOARD_PYTHON)"; do
    [ -n "$py" ] || continue
    case "$seen" in *" $py "*) continue ;; esac
    seen="$seen$py "
    if [ ! -x "$py" ]; then
      echo "  --      no interpreter at $py (sd_db version not read)"
      continue
    fi
    if ! info=$("$py" -I -c 'import importlib.metadata as m, sd_db, sd_db.schema as s; print(m.version("sd-db"), s.SCHEMA_VERSION, sd_db.__file__)' 2>&1); then
      doctor_fail "$py has no importable sd_db ($(printf '%s\n' "$info" | tail -1)) — install: local-sd-db/sd-db.sh install $(dirname "$(dirname "$py")")"
      continue
    fi
    dist=${info%% *}; rest=${info#* }; schema=${rest%% *}; file=${rest#* }
    case "$file" in
      "$ROOT"/*) doctor_fail "$py resolves sd_db from this checkout ($file), not an installed copy"; continue ;;
    esac
    if [ -n "$db_schema" ] && [ "$schema" != "$db_schema" ]; then
      doctor_fail "$py: sd_db $dist built for schema $schema DIFFERS from the database's $db_schema — install the matching build: local-sd-db/sd-db.sh install $(dirname "$(dirname "$py")")"
    elif [ "$schema" != "$checkout_schema" ]; then
      echo "  WARN    $py: sd_db $dist built for schema $schema DIFFERS from this checkout's $checkout_schema — migrate, then: local-sd-db/sd-db.sh install $(dirname "$(dirname "$py")")"
    else
      echo "  ok      $py: sd_db $dist, schema $schema"
    fi
  done
}

# `doctor sd`: the checks above alone, for the test suite and for a quick
# read after touching one of the pieces. Same exit code rule as doctor.
cmd_doctor_sd() {
  fails=0
  echo "== doctor sd"
  doctor_profile
  doctor_sd
  echo
  if [ "$fails" -gt 0 ]; then
    echo "  $fails hard failure(s)"
    exit 1
  fi
  echo "  no hard failures"
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
    if brew doctor >/dev/null 2>&1; then
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

  if command -v fnm >/dev/null 2>&1; then
    echo "  ok      fnm ($(fnm current 2>/dev/null || echo no default node))"
  else
    echo "  WARN    fnm missing (the tooling stage installs node versions with it)"
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

  # Each of these lives in the tool's config folder, or in the tool's own
  # folder where an older checkout keeps it.
  notify_conf="$SYSTEM_TOOLS_CONFIG/cron-jobs/notify.conf"
  [ -f "$notify_conf" ] || notify_conf="$ROOT/local-cron-jobs/notify.conf"
  notify_env=$(live_env local-notify)
  if [ -f "$notify_conf" ]; then
    echo "  ok      $notify_conf present (job failures reach the phone)"
  else
    echo "  WARN    cron-jobs notify.conf missing — no phone push on job failure"
  fi

  # Presence of the conf is not reach. A topic left at change-me, or a profile
  # whose only channel was email, still leaves a machine unable to tell anyone
  # a nightly job died — which is the failure nobody sees, because the thing
  # that would report it is the thing that is broken.
  chans=""
  topic=$(sed -n 's/^[[:space:]]*NTFY_TOPIC=//p' "$notify_conf" 2>/dev/null | tr -d '"')
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
    echo "  FAIL    no notification channel configured — nothing can report a failed job"
    echo "          set NTFY_TOPIC in $SYSTEM_TOOLS_CONFIG/cron-jobs/notify.conf (see local-notify/README.md)"
    fails=$((fails + 1))
  fi

  # One-off system state that no stage manages.
  if [ -e /var/db/useLS ]; then
    echo "  ok      /var/db/useLS (ssh agent uses launch services)"
  else
    echo "  WARN    /var/db/useLS missing — run: sudo touch /var/db/useLS"
  fi

  if [ -e /etc/sudoers.d/timestamp-timeout ]; then
    echo "  ok      sudo timestamp_timeout drop-in present"
  else
    echo "  WARN    /etc/sudoers.d/timestamp-timeout missing — run: sudo sh -c 'echo \"Defaults timestamp_timeout=60\" > /etc/sudoers.d/timestamp-timeout && chmod 440 /etc/sudoers.d/timestamp-timeout && visudo -c'"
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
    for v in 18 20 24; do
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
    jobs=$(manifest cron | awk -v p="$LABEL_PREFIX.cron." '{ print p $0 }')
    for label in $labels $jobs; do
      if launchctl print "gui/$uid/$label" >/dev/null 2>&1; then
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

# `run` with a bound, for the brew and mas calls of the upgrade sweep. An
# apply logs the step as it starts and stops it after SECONDS, or after
# MACHINE_SETUP_STEP_TIMEOUT seconds when that is set: a call that hangs then
# names itself in the log and ends, where it used to run until the job's limit
# killed the whole job (sd:2660). lib/bounded.sh is sourced here, not at the
# top: suites run copies of this script with no lib/ beside them.
run_step() { # seconds, command...
  _step_seconds=${MACHINE_SETUP_STEP_TIMEOUT:-$1}
  shift
  if [ "$APPLY" -eq 1 ]; then
    command -v st_step >/dev/null 2>&1 || . "$ROOT/lib/bounded.sh"
    st_step "$_step_seconds" "$@" </dev/null
  else
    printf '  [dry-run] %s\n' "$*"
  fi
}

# Maintenance sweep: refresh what is already installed. Dry run by default
# like every other mutating verb. A step that fails or times out does not stop
# the sweep; the steps after it still run, and the sweep exits 1 naming it.
cmd_upgrade() {
  echo "upgrade : brew + Claude Code + App Store maintenance sweep"
  [ "$APPLY" -eq 1 ] || echo "mode    : DRY RUN — nothing will change; re-run with --apply"
  echo
  failed=""
  if command -v brew >/dev/null 2>&1; then
    run_step 600 brew update || failed="$failed, brew update"
    run_step 1800 brew upgrade || failed="$failed, brew upgrade"
    # --greedy takes casks with `auto_updates true` too, whose own updaters
    # stage_macos turns off where the app holds a TCC grant (sd:3062).
    run_step 1800 brew upgrade --cask --greedy || failed="$failed, brew upgrade --cask --greedy"
  else
    echo "  brew MISSING — https://brew.sh"
  fi
  # Claude Code's native install updates here, weekly only: the settings
  # baseline turns its background updater off (sd:3033).
  if [ -x "$HOME/.local/bin/claude" ]; then
    run_step 600 "$HOME/.local/bin/claude" update || failed="$failed, claude update"
  fi
  if command -v brew >/dev/null 2>&1; then
    run_step 600 brew cleanup --prune=all || failed="$failed, brew cleanup"
  fi
  if command -v mas >/dev/null 2>&1; then
    run_step 1200 mas upgrade || failed="$failed, mas upgrade"
  else
    echo "  mas MISSING (brew install mas)"
  fi
  echo
  echo "  afterwards: $(basename "$0") status   # drift check"
  if [ -n "$failed" ]; then
    echo "  failed steps: ${failed#, }"
    return 1
  fi
}

# outdated_lists before|after: what brew and mas call outdated, one sorted
# file per kind under $tmp. Each query is a bounded step; one that fails or
# times out leaves its file empty. Casks are asked with --greedy, as
# cmd_upgrade upgrades them, so a self-updating cask is in both lists.
outdated_lists() {
  run_step 300 brew outdated --formula --quiet 2>/dev/null | sort > "$tmp/formula.all.$1" || :
  # A pinned formula with a newer version is held on purpose (BREW_PINNED),
  # not left over: it goes to its own list, out of the counts.
  run_step 300 brew list --pinned 2>/dev/null | sed 's|.*/||' | sort > "$tmp/pinned" || :
  comm -12 "$tmp/formula.all.$1" "$tmp/pinned" > "$tmp/held.$1"
  comm -23 "$tmp/formula.all.$1" "$tmp/pinned" > "$tmp/formula.$1"
  run_step 300 brew outdated --cask --greedy --quiet 2>/dev/null | sort > "$tmp/cask.$1" || :
  : > "$tmp/mas.$1"
  if command -v mas >/dev/null 2>&1; then
    run_step 300 mas outdated 2>/dev/null | sort > "$tmp/mas.$1" || :
  fi
}

# held_section before|after: the pinned formulae a newer version waits for.
held_section() {
  [ -s "$tmp/held.$1" ] || return 0
  printf '\nheld (pinned):\n'
  sed 's/^/  /' "$tmp/held.$1"
  printf '  unpin with brew unpin <formula>, upgrade, then re-grant its binary\n'
}

# grant_paths before|after: the paths whose change drops a macOS grant, one
# file per kind under $tmp. ~/.local/bin/claude links to a versioned binary,
# and `brew upgrade python` moves the Cellar path (.claude/rules/macos-tcc.md).
grant_paths() {
  readlink "$HOME/.local/bin/claude" > "$tmp/claude.$1" 2>/dev/null || :
  : > "$tmp/python.$1"
  if command -v brew >/dev/null 2>&1; then
    _cellar=$(brew --cellar 2>/dev/null) || _cellar=""
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
  run_step 300 sh "$ROOT/local-project-dashboard/dashboard.sh" grants > "$tmp/grants.out" 2>&1 || g_rc=$?
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

# One push, with mail, when a grant is missing. Returns 1 when it could not
# be delivered.
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
  if ! sh "$notify" -t "TCC: re-grant on $(hostname -s)" -k status -F -c ntfy,email -b "$(cat "$tmp/push")"; then
    echo "grant push FAILED — exiting 1 so the cron failure push fires" >&2
    return 1
  fi
}

# Cron flavor of upgrade: forces --apply, diffs the outdated lists before and
# after the sweep, and emails what got upgraded / what failed / what is still
# pending through local-notify's email channel, with the AI-app inventory
# capture and the TCC grants probe's result. When nothing is outdated it only
# updates Claude Code, captures the inventory and probes the grants. A missing grant sends one push naming each binary to re-grant.
# Exits 1 when the email or that push could not be delivered, or when the
# quiet-week Claude update or capture failed, so the cron failure push covers
# a lost report or a silent failure, not mere findings.
cmd_upgrade_report() {
  APPLY=1
  notify="$ROOT/local-notify/notify.sh"
  tmp=$(mktemp -d)
  # EXIT removes the folder; a signal exits, and so runs EXIT. A TERM trap
  # that only removed it returned into the run, which then wrote into the
  # folder it had just removed (sd:2660).
  trap 'rm -rf "$tmp"' EXIT
  trap 'exit 129' HUP
  trap 'exit 130' INT
  trap 'exit 143' TERM
  # fd 3 is the job log. The sweep's output goes into the mail, and each step
  # names itself here as it starts, so a hang names its step.
  exec 3>&2
  # shellcheck disable=SC2034 # read by st_step in lib/bounded.sh
  ST_STEP_FD3=1

  # `brew update` first so the outdated snapshot is accurate; cmd_upgrade
  # repeats it, but the second run is a fast no-op.
  run_step 600 brew update >/dev/null 2>&1 || :
  outdated_lists before
  grant_paths before

  if [ ! -s "$tmp/formula.before" ] && [ ! -s "$tmp/cask.before" ]      && [ ! -s "$tmp/mas.before" ]; then
    # Claude Code updates outside brew, and cmd_upgrade, which runs it, does
    # not run this week (sd:3033).
    rc=0
    if [ -x "$HOME/.local/bin/claude" ]; then
      run_step 600 "$HOME/.local/bin/claude" update || rc=1
    fi
    echo "nothing outdated — no upgrade, no email"
    held_section before
    inventory_capture || rc=1
    cat "$tmp/inventory"
    grants_report
    cat "$tmp/grants"
    grants_push || rc=1
    return "$rc"
  fi

  rc=0
  cmd_upgrade > "$tmp/out" 2>&1 || rc=1
  cat "$tmp/out"
  inv_rc=0
  inventory_capture || inv_rc=1

  outdated_lists after
  grants_report

  {
    printf 'machine-setup upgrade report — %s on %s\n' \
      "$(date '+%Y-%m-%d %H:%M')" "$(hostname -s)"
    held_section after
    for kind in formula cask mas; do
      comm -23 "$tmp/$kind.before" "$tmp/$kind.after" > "$tmp/$kind.done"
      if [ -s "$tmp/$kind.done" ]; then
        printf '\nupgraded (%s):\n' "$kind"
        sed 's/^/  /' "$tmp/$kind.done"
      fi
      if [ -s "$tmp/$kind.after" ]; then
        printf '\nstill outdated (%s) — held back or failed:\n' "$kind"
        sed 's/^/  /' "$tmp/$kind.after"
      fi
    done
    if [ "$rc" -ne 0 ]; then
      printf '\nupgrade sweep exited nonzero; last output:\n'
      tail -30 "$tmp/out" | sed 's/^/  /'
    fi
    cat "$tmp/inventory"
    cat "$tmp/grants"
  } > "$tmp/body"

  n_done=$(cat "$tmp"/*.done 2>/dev/null | wc -l | tr -d ' ')
  n_left=$(cat "$tmp/formula.after" "$tmp/cask.after" "$tmp/mas.after" 2>/dev/null | wc -l | tr -d ' ')
  subject="upgrade: $n_done upgraded, $n_left still outdated on $(hostname -s)"
  # A clean upgrade is a receipt, not a problem — only the leftovers are worth
  # flagging, so -F is conditional here where the other status jobs hardcode it.
  # Those only send at all when they have findings; this one always sends.
  fu=""
  if [ "$n_left" -gt 0 ] || [ "$inv_rc" -ne 0 ]; then fu="-F"; fi
  push_rc=0
  grants_push || push_rc=1
  if ! sh "$notify" -t "$subject" -k status $fu -c ntfy,email -b "$(cat "$tmp/body")"; then
    echo "email FAILED — exiting 1 so the cron failure push fires" >&2
    exit 1
  fi
  return "$push_rc"
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

system  (useLS, firewall/stealth, sudo grace period and the mailto handler
are the `system` and `tooling` stages — run `update system --apply` /
`update tooling --apply` instead of doing them by hand)
  - login shell: the `shell` stage converges zsh -> brew bash (installs
    brew and its bash if missing, /etc/shells, chsh) — runs in setup, or
    alone: `machine-setup.sh update shell --apply`. Leaving zsh is also
    fine: ~/.zshenv loads the same ~/.config/shell/env.sh
  - network volumes and printers

notifications  (nothing provisions these: the ntfy topic doubles as the
secret on the public server, so it is never committed)
  - NTFY_TOPIC into $SYSTEM_TOOLS_CONFIG/cron-jobs/notify.conf, 0600 —  *
    launchd jobs do not inherit the shell environment, so the conf is
    what they read
  - same NTFY_TOPIC exported in ~/.config/shell/env.sh, for interactive use
  - local-notify's .env comes from the `envs` stage where the profile has
    a template; email also needs its mail server running

tooling  (fnm node versions and rtk init are the `tooling` stage)
  - grant python3 Documents access when the vault TCC prompt appears
    (task-actions install/start is the `agents` stage; its tailscale
    funnel is the `tooling` stage once tailscale is logged in; the
    iTerm2 prefs pointer is the `iterm2` stage once the drive has synced)
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
  run "$ROOT/local-cron-jobs/cron-jobs.sh" uninstall --all
  echo

  echo "== launch agents (manifest)"
  load_profile 2>/dev/null || true
  uid=$(id -u)
  for label in $(manifest agent 2>/dev/null); do
    dst="$HOME/Library/LaunchAgents/$label.plist"
    if [ -f "$dst" ]; then
      run launchctl bootout "gui/$uid/$label"
      run rm "$dst"
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
  n=$(printf '%s\n' "$out" | grep -cE 'DIFFERS|MISSING|STALE|ABSENT|UNLOADED|EXTRA|defaults write|^  extra ' || :)
  drift=$((drift + ${n:-0}))
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
  load_profile
  keep_sudo_ticket
  echo "profile : $PROFILE"
  echo "repo    : $ROOT"
  echo "state   : $STATE_FILE"
  echo

  if command -v brew >/dev/null 2>&1; then
    # Temp files rather than <(...): process substitution is a bashism and this
    # script is POSIX sh per convention #2.
    tmp=$(mktemp -d)
    trap 'rm -rf "$tmp"' EXIT INT TERM

    # `brew list --formula`, not `brew leaves`: a manifest entry can be
    # installed as another formula's dependency (ripgrep is), and leaves omits
    # those, which reported them as missing when they were present.
    # Both sides normalized to bare names: brew list usually prints bare
    # names but tap-qualifies a formula whose name is ambiguous, and
    # manifests carry tap-qualified entries.
    # `sort -u`, not `sort`: manifest dedupes before this strips the tap, so a
    # profile naming one formula both ways left the same bare name twice and
    # comm reported the duplicate as missing for a formula that was installed.
    brew list --formula 2>/dev/null | sed 's|.*/||' | sort -u > "$tmp/have.brew"
    manifest brew | sed 's|.*/||' | sort -u > "$tmp/want.brew"
    echo "brew formulae"
    mb=$(comm -13 "$tmp/have.brew" "$tmp/want.brew")
    brew leaves 2>/dev/null | sed 's|.*/||' | sort -u > "$tmp/leaves.brew"
    xb=$(comm -23 "$tmp/leaves.brew" "$tmp/want.brew")
    status_line missing "$mb"
    status_line extra "$xb"
    [ -n "$mb$xb" ] || echo "  ok      in sync"

    brew list --cask 2>/dev/null | sort > "$tmp/have.cask"
    manifest cask > "$tmp/want.cask"
    echo "casks"
    mc=$(comm -13 "$tmp/have.cask" "$tmp/want.cask")
    xc=$(comm -23 "$tmp/have.cask" "$tmp/want.cask")
    status_line missing "$mc"
    status_line extra "$xc"
    [ -n "$mc$xc" ] || echo "  ok      in sync"
    up=$(unpinned_formulae)
    echo "pins"
    status_line unpinned "$up"
    [ -n "$up" ] || echo "  ok      $BREW_PINNED pinned or not installed"
    for v in "$mb" "$xb" "$mc" "$xc" "$up"; do
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
  # drift items (docs/work/2026-09-05-one-database-one-front-door); the other
  # two were the agents stage's rows for <prefix>.sd-dashboard and the
  # runner, which is gone.
  for st in shell bin dotfiles envs prompts repos appstore cron sd satellite agents services tooling system macos obsidian iterm2; do
    status_stage "$st"
    echo
  done
  echo "drift   : $drift item(s)"
  # --fail-on-drift turns drift into exit 1, which is what lets the cron job
  # machine-setup-update-nightly (its last step) route "this machine drifted"
  # into the failure notification path (banner + ntfy push).
  if [ "$FAIL_ON_DRIFT" -eq 1 ] && [ "$drift" -gt 0 ]; then
    exit 1
  fi
}

# ------------------------------------------------------------------ main ----

VERB=""
ARG=""
ARG2=""
for a in "$@"; do
  case "$a" in
    --apply) APPLY=1 ;;
    --additive) ADDITIVE=1 ;;
    --fail-on-drift) FAIL_ON_DRIFT=1 ;;
    --stale-ok) STALE_OK=1 ;;
    --force) FORCE=1 ;;
    -h|--help|help)
      cat <<'HELPEOF'
usage: machine-setup.sh setup <profile> [stage]|update [stage]|capture [--apply]|status|adopt|doctor [sd]|test|upgrade [--apply]|upgrade-report|checklist|decommission [--apply]

  setup <profile>  provision a new machine and record the profile
                   profile is any <name> with a <name>.<kind> file in the
                   profile directory (see `profiles`)
  update [stage]   re-run the stages for this machine's recorded profile
                   a copied file (dotfile, .env, launch agent) that differs is
                   judged against what was last installed: STALE means the repo
                   moved and --apply refreshes it, DIFFERS means this machine
                   edited it and it is left alone
                   --force overwrites a DIFFERS file too, keeping a .bak- copy
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
  status           drift report: what the profile wants vs what is installed
                   --fail-on-drift exits 1 when drift exists (for cron)
  doctor           read-only sanity check: CLT, brew, docker, fnm, key
                   material, launchd agents, and which .env files still
                   need filling in on this machine; on a profile with an sd
                   agent, also the database (integrity_check), the
                   dashboard's tailscale route and its answer
                   over the HTTPS name, and the sd_db build in each
                   virtualenv beside the database's schema version; no
                   sqlite3 on PATH, the dashboard's origin under Funnel,
                   :8443 held by another route, a service config naming
                   another database, or an env.sh that fails when sourced,
                   is a FAIL
  doctor sd        those sd checks alone, exit 1 on any FAIL
  test             run the unittest suite in tests/ (extra args go to unittest)
  upgrade          maintenance sweep: brew update/upgrade (casks with
                   --greedy, so self-updating ones move too), claude update
                   (when ~/.local/bin/claude exists), brew cleanup + mas
                   upgrade (dry run without --apply, like everything else);
                   an apply logs each step as it starts and stops it at its
                   bound (300-1800 s, or MACHINE_SETUP_STEP_TIMEOUT seconds),
                   runs the rest, and exits 1 naming the steps that failed
  upgrade-report   cron flavor of upgrade: always applies, emails what got
                   upgraded / what failed / what is still outdated, with the
                   ai-apps inventory capture and the result of
                   local-project-dashboard's `grants` probe; when nothing
                   is outdated it only runs claude update, the capture and
                   the probe; a missing grant sends one push naming each
                   binary to re-grant; exits 1 when the email or that push
                   could not be delivered or that quiet-week claude update
                   or capture failed
  checklist        print the manual new-machine steps no stage automates
                   (accounts, licenses, key restores; plus checklist.txt from the config)
  decommission     retire this machine: list dirty/unpushed repos, uninstall
                   cron jobs and manifest launch agents (--apply, after
                   typing "decommission" at an interactive prompt), and
                   print the manual sign-off list; destructive steps stay
                   manual
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

Profiles layer: common.<kind> applies everywhere, <profile>.<kind> adds to it.
Kinds are tap, brew, cask, mas, service, cron, agent, macos, app, obsidian, iterm2,
spotlight — plain lists with # comments. macos lines are "<domain> <key> <type> <value>";
only keys listed there are ever touched or captured. iterm2 holds a single folder path.
spotlight holds absolute paths; a leading ~ is this account's home.

configuration (outside this repo; nothing private is committed here):
  $SYSTEM_TOOLS_CONFIG/machine-setup/
    profiles/       common.<kind> and <profile>.<kind> manifests
    dotfiles/       common/ and <profile>/ trees, copied into $HOME
    envs/           common/ and <profile>/ <folder>.env templates
    launchagents/   <label>.plist templates; @HOME@, @LABEL@ and @ROOT@ are
                    replaced with $HOME, the label and this checkout's root
    machine-setup.env  optional, sourced first; see machine-setup.env.example
    shell-env.example  optional, doctor's list of shell variables
    checklist.txt, decommission.txt  optional, appended to those verbs
  Start one with: cp -R local-machine-setup/examples/. <that folder>/

environment:
  SYSTEM_TOOLS_CONFIG     config root (default ${XDG_CONFIG_HOME:-~/.config}/system)
  MACHINE_SETUP_PROFILE_DIR, MACHINE_SETUP_DOTFILE_DIR, MACHINE_SETUP_ENV_DIR,
  MACHINE_SETUP_AGENT_DIR  override one of the four folders above
  SYSTEM_TOOLS_LABEL_PREFIX  launchd label prefix (default local.system-tools)
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
  MACHINE_SETUP_SPOTLIGHT_PLIST  tests only: the plist that stands in for
                          Spotlight's VolumeConfiguration.plist
  MACHINE_SETUP_PLISTBUDDY  tests only: the PlistBuddy that edits it
HELPEOF
      exit 0
      ;;
    setup|update|capture|status|compare|triage|candidates|apps|adopt|report|profiles|doctor|test|upgrade|upgrade-report|checklist|decommission)
      if [ -z "$VERB" ]; then VERB="$a"; elif [ -z "$ARG" ]; then ARG="$a"; else ARG2="$a"; fi ;;
    *)
      if [ -n "$VERB" ]; then
        if [ -z "$ARG" ]; then ARG="$a"; elif [ -z "$ARG2" ]; then ARG2="$a"; fi
      fi ;;
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
      *)  die "doctor takes no argument, or 'sd' for the database and dashboard checks alone" ;;
    esac
    ;;
  # Python, not sh, for the same reason local-repo-sync tests
  # a shell script from Python: the CI wrapper asserts a unittest summary and
  # refuses skips. PYTHON is CI's venv; a machine runs it with its own.
  # The suite seeds its fixture database through sd_db -- nothing outside
  # the library opens the database or creates a table, and
  # local-sd-db/tests/test_one_store.py greps the repository to keep it so --
  # so this checkout's local-sd-db goes on PYTHONPATH.
  test)
    shift
    PYTHONPATH="$DIR/../local-sd-db${PYTHONPATH:+:$PYTHONPATH}" \
      exec "${PYTHON:-python3}" -m unittest discover -s "$DIR/tests" -t "$DIR" "$@"
    ;;
  upgrade) cmd_upgrade ;;
  upgrade-report) cmd_upgrade_report ;;
  checklist) cmd_checklist ;;
  decommission) cmd_decommission ;;
  *)
    echo "usage: $(basename "$0") setup <profile>|update|capture|status|compare <a> <b>|triage|candidates [kind]|apps|adopt|report [profile]|profiles|doctor [sd]|test|upgrade [--apply]" >&2
    exit 1
    ;;
esac

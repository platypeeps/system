#!/bin/sh
# Link the repo's command-line tools into ~/bin so they land on PATH.
# Usage: bin-links.sh install|status|remove
set -e

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

BIN_DIR="${BIN_LINKS_DIR:-$HOME/bin/common}"

# Where the sd-ai-command-pack checkout lives. Every command it ships is linked
# from that checkout, rather than copied into this repository: the pack's
# installer renders surfaces and links no executable, so PATH is ours to wire.
# Overridable, and a missing checkout SKIPs with a message rather than failing:
# the pack is a different repository and may simply not be cloned here.
SD_PACK_ROOT="${SD_PACK_ROOT:-$HOME/repos/platypeeps/sd-ai-command-pack}"

# "<link name> <target>", one per line. A bare target is relative to this repo's
# root; one starting with `/` or `$` is used as-is, for a tool that lives in
# another checkout.
LINKS="
repo-sync  local-repo-sync/repo-sync.sh
gito       local-gito/gito.sh
prism      local-prism/prism.sh
mac-utils  local-mac-utils/mac-utils.sh
notify     local-notify/notify.sh
agent-prompt  local-agent-prompt/agent-prompt.sh
adversarial-gate  local-adversarial-gate/adversarial-gate.sh
ha-mcp     local-ha-mcp/ha-mcp.sh
llama-cpp  local-llama-cpp/llama-cpp.sh
jev        local-jev/jev.sh
"

# The pack's commands, read from its bin/ on every run rather than listed here:
# two hand-written rows once stood for a checkout shipping seventeen. The rule
# is the one the pack's own `sd_install.py --status` counts with -- `sd*`,
# no extension, a regular executable file -- so the `sd_*.py` modules beside
# them are not commands. Printed as rows in the LINKS format.
pack_links() {
  for pc in "$SD_PACK_ROOT"/bin/sd*; do
    pn=$(basename "$pc")
    case "$pn" in *.*) continue ;; esac
    [ -f "$pc" ] && [ -x "$pc" ] || continue
    # shellcheck disable=SC2016 # the row carries $SD_PACK_ROOT literally, like LINKS
    printf '%s $SD_PACK_ROOT/bin/%s\n' "$pn" "$pn"
  done
}

all_links() {
  echo "$LINKS"
  pack_links
}

pack_note() {
  [ -d "$SD_PACK_ROOT/bin" ] ||
    printf '%-16s SKIP    sd-ai-command-pack not found: %s\n' "sd*" "$SD_PACK_ROOT"
}

# A target is either repo-relative or absolute. `$SD_PACK_ROOT` is the one
# variable a row may name, expanded here rather than by `eval` on the row --
# eval on a table line would run whatever a future row happened to contain.
resolve_target() {
  case "$1" in
    /*)             echo "$1" ;;
    '$SD_PACK_ROOT'/*) echo "$SD_PACK_ROOT${1#\$SD_PACK_ROOT}" ;;
    *)              echo "$ROOT/$1" ;;
  esac
}

# Absolute path with symlinks resolved, so a relative link still compares equal
# to the target it points at.
real_path() {
  TARGET="$1" python3 -c 'import os,sys; sys.stdout.write(os.path.realpath(os.environ["TARGET"]))'
}

install_links() {
  mkdir -p "$BIN_DIR"
  all_links | grep -v '^$' | while read -r name rel; do
    target="$(resolve_target "$rel")"
    link="$BIN_DIR/$name"

    if [ ! -f "$target" ]; then
      # "not found" rather than "missing in repo": a row may name another
      # checkout now, and that checkout simply may not be cloned here.
      printf '%-16s SKIP    not found: %s\n' "$name" "$target"
      continue
    fi
    if [ ! -x "$target" ]; then
      chmod +x "$target"
    fi

    if [ -L "$link" ]; then
      if [ "$(real_path "$link")" = "$(real_path "$target")" ]; then
        printf '%-16s ok      already linked\n' "$name"
        continue
      fi
      # A link pointing somewhere else is still this script's to retarget.
      rm -f "$link"
    elif [ -e "$link" ]; then
      # A real file is somebody else's, and replacing it would destroy content.
      printf '%-16s SKIP    real file in the way: %s\n' "$name" "$link"
      continue
    fi

    ln -s "$target" "$link"
    printf '%-16s linked  -> %s\n' "$name" "$rel"
  done

  pack_note
  sweep_retired remove
  sweep_stale_pack remove

  echo
  case ":$PATH:" in
    *":$BIN_DIR:"*) echo "PATH  ok      $BIN_DIR is on PATH" ;;
    *)              echo "PATH  TODO    add to your shell rc:  export PATH=\"$BIN_DIR:\$PATH\"" ;;
  esac
}

status() {
  echo "repo    : $ROOT"
  echo "bin dir : $BIN_DIR"
  echo
  all_links | grep -v '^$' | while read -r name rel; do
    target="$(resolve_target "$rel")"
    link="$BIN_DIR/$name"

    # The bad states are spelled in machine-setup's marker vocabulary
    # (MISSING / DIFFERS), because `status` counts this output towards the
    # drift number the nightly cron job reports on. A lowercase "missing" read
    # as prose and counted as nothing.
    if [ -L "$link" ] && [ "$(real_path "$link")" = "$(real_path "$target")" ]; then
      state="linked"
    elif [ -L "$link" ]; then
      state="DIFFERS (wrong target)"
    elif [ -e "$link" ]; then
      state="DIFFERS (real file)"
    else
      state="MISSING"
    fi
    printf '%-16s %-22s %s\n' "$name" "$state" "$rel"
  done

  pack_note
  sweep_retired report
  sweep_stale_pack report

  echo
  case ":$PATH:" in
    *":$BIN_DIR:"*) echo "PATH         on PATH" ;;
    *)              echo "PATH         MISSING ($BIN_DIR is not on PATH)" ;;
  esac
}

# Links this repo used to create and no longer does. `install` cannot clean these
# up by walking LINKS -- a row that is gone names nothing -- so they are listed
# until every machine has run `install` once past the change that dropped them.
# Only a symlink whose target is inside this repo is touched. Empty since
# sd:1156: every machine ran `install` past the research-kit move.
# One row per link: `<name>  <path relative to this repo>`.
RETIRED="
"

sweep_retired() {
  echo "$RETIRED" | grep -v '^$' | while read -r name rel; do
    link="$BIN_DIR/$name"
    [ -L "$link" ] || continue
    case "$(readlink "$link")" in
      "$ROOT/$rel") ;;
      *) continue ;;
    esac
    if [ "$1" = "remove" ]; then
      rm -f "$link"
      printf '%-16s removed retired link -> %s\n' "$name" "$rel"
    else
      printf '%-16s RETIRED stale link -> %s\n' "$name" "$rel"
    fi
  done
}

# A command the pack stops shipping leaves a link to nothing, and no row names it
# any more. Only a dangling symlink whose target is inside the pack's bin/ is
# touched; STALE is in machine-setup's drift vocabulary.
sweep_stale_pack() {
  for sl in "$BIN_DIR"/*; do
    [ -L "$sl" ] && [ ! -e "$sl" ] || continue
    st=$(readlink "$sl")
    case "$st" in
      "$SD_PACK_ROOT/bin/"*) ;;
      *) continue ;;
    esac
    if [ "$1" = "remove" ]; then
      rm -f "$sl"
      printf '%-16s removed stale link -> %s\n' "$(basename "$sl")" "$st"
    else
      printf '%-16s %-22s %s\n' "$(basename "$sl")" "STALE (target gone)" "$st"
    fi
  done
}

remove_links() {
  all_links | grep -v '^$' | while read -r name rel; do
    target="$(resolve_target "$rel")"
    link="$BIN_DIR/$name"

    # Only ever remove links this repo owns; a real file or a foreign link is
    # left alone.
    if [ -L "$link" ] && [ "$(real_path "$link")" = "$(real_path "$target")" ]; then
      rm -f "$link"
      printf '%-16s removed\n' "$name"
    else
      printf '%-16s skipped (not a link into this repo)\n' "$name"
    fi
  done

  sweep_retired remove
  sweep_stale_pack remove
}

case "$1" in
  test)
    shift
    exec "${PYTHON:-python3}" -m unittest discover -s "$DIR/tests" -t "$DIR" "$@"
    ;;
  install)
    install_links
    ;;
  status)
    status
    ;;
  remove)
    remove_links
    ;;
  -h|--help|help)
    cat <<'HELPEOF'
usage: bin-links.sh install|status|remove|test

  install   symlink this repo's CLI tools into ~/bin/common, creating the
            directory if needed; existing correct links are left alone and a
            real file in the way is skipped rather than overwritten, and links
            this repo has retired are swept away
  status    report each link's state, any retired link still present, and
            whether the bin dir is on PATH
  remove    delete only the symlinks that point into this repo or the pack,
            retired and stale ones included
  test      run the unittest suite in tests/

Linked tools: repo-sync, gito, prism, mac-utils, notify, agent-prompt,
adversarial-gate, ha-mcp, llama-cpp, and every command in the sd-ai-command-pack
checkout's bin/ (sd, sd-status, sd-review, ...), read from it on each run. A
link to a pack command that no longer exists is STALE and swept.

environment:
  BIN_LINKS_DIR   directory to link into (default ~/bin/common)
  SD_PACK_ROOT    sd-ai-command-pack checkout whose bin/sd* commands are linked
                  (default ~/repos/platypeeps/sd-ai-command-pack)
HELPEOF
    exit 0
    ;;
  *)
    echo "usage: $(basename "$0") install|status|remove|test" >&2
    exit 1
    ;;
esac

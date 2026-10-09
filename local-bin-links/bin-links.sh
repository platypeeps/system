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

# Where the sd-ai-command-pack checkout lives. Its installer owns the pack's
# commands (links in ~/.local/bin), so this script links none of them and only
# sweeps the links it made earlier: they shadow the installed commands.
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

all_links() {
  echo "$LINKS"
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

  sweep_retired remove
  sweep_pack remove

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

  sweep_retired report
  sweep_pack report

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

# The pack's installer owns its commands (it links them into ~/.local/bin), so a
# link into the pack checkout is one this script made before that, and it shadows
# the installed command. It goes only once the installer's copy of the same name
# runs on its own and its directory is on PATH, or when it dangles (the pack
# dropped the command): until then the old link is the only working copy. Only a
# symlink to a regular file is touched; a link to a directory is not one this
# script made, and a path through it could break. `all` (the remove verb) skips
# the check. MISSING and STALE are in machine-setup's drift vocabulary.
PACK_BIN="$HOME/.local/bin"

# Prints why a pack link must stay; prints nothing when it may go. The
# replacement must be an executable regular file reached by a symlink chain that
# never stops in the bin dir: a hop there may be the link about to be removed.
# Each hop's directory is resolved with `cd -P`, so a relative target or a
# directory link into the bin dir counts too. Any failed probe keeps the link.
pack_keep_reason() {
  pk="$PACK_BIN/$1"
  if [ ! -e "$pk" ] && [ ! -L "$pk" ]; then
    echo "no installed replacement (run make setup in the pack)"; return
  fi
  not_file="installed replacement is not an executable file ($PACK_BIN/$1)"
  bin_real=$(cd -P "$BIN_DIR" 2>/dev/null && pwd -P) || { echo "cannot resolve $BIN_DIR"; return; }
  hops=0
  while :; do
    hd=$(cd -P "$(dirname "$pk")" 2>/dev/null && pwd -P) || { echo "$not_file"; return; }
    [ "$hd" = "$bin_real" ] && { echo "installed replacement resolves through $BIN_DIR"; return; }
    [ -L "$pk" ] || break
    # A loop never ends; 40 hops is the kernel's own limit.
    hops=$((hops + 1))
    [ "$hops" -le 40 ] || { echo "$not_file"; return; }
    t=$(readlink "$pk") || { echo "cannot read link $pk"; return; }
    case "$t" in
      /*) pk="$t" ;;
      *)  pk="$hd/$t" ;;
    esac
  done
  [ -f "$pk" ] && [ -x "$pk" ] || { echo "$not_file"; return; }
  case ":$PATH:" in
    *":$PACK_BIN:"*) ;;
    *) echo "installed replacement not on PATH ($PACK_BIN)" ;;
  esac
}

sweep_pack() {
  # The installer's own directory holds its links; never sweep those.
  [ -d "$BIN_DIR" ] && [ "$(real_path "$BIN_DIR")" = "$(real_path "$PACK_BIN")" ] && return 0
  for sl in "$BIN_DIR"/*; do
    [ -L "$sl" ] || continue
    st=$(readlink "$sl")
    case "$st" in
      "$SD_PACK_ROOT/"*) ;;
      *) continue ;;
    esac
    [ -e "$sl" ] && [ ! -f "$sl" ] && continue
    sn=$(basename "$sl")
    why=""
    if [ "$1" != all ] && [ -e "$sl" ]; then
      why=$(pack_keep_reason "$sn")
    fi
    if [ -n "$why" ] && [ "$1" = remove ]; then
      printf '%-16s kept pack link %s: %s\n' "$sn" "$sn" "$why"
    elif [ -n "$why" ]; then
      printf '%-16s MISSING pack link kept: %s\n' "$sn" "$why"
    elif [ "$1" = report ]; then
      printf '%-16s %-22s %s\n' "$sn" "STALE (pack installs it)" "$st"
    else
      rm -f "$sl"
      printf '%-16s removed pack link -> %s\n' "$sn" "$st"
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
  sweep_pack all
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
            this repo has retired, or the pack now installs, are swept away
  status    report each link's state, any retired or pack link still present,
            and whether the bin dir is on PATH
  remove    delete only the symlinks that point into this repo or the pack,
            retired and pack ones included
  test      run the unittest suite in tests/

Linked tools: repo-sync, gito, prism, mac-utils, notify, agent-prompt,
adversarial-gate, ha-mcp, llama-cpp, jev. The sd-ai-command-pack's commands
(sd, sd-status, ...) are not linked: its installer links them into
~/.local/bin. An older link into the pack checkout is swept once that
installed copy runs without it and ~/.local/bin is on PATH; until then it is
kept.

environment:
  BIN_LINKS_DIR   directory to link into (default ~/bin/common)
  SD_PACK_ROOT    sd-ai-command-pack checkout whose old links are swept
                  (default ~/repos/platypeeps/sd-ai-command-pack)
HELPEOF
    exit 0
    ;;
  *)
    echo "usage: $(basename "$0") install|status|remove|test" >&2
    exit 1
    ;;
esac

#!/bin/sh
# Small macOS maintenance one-liners, collected from the loose scripts that
# used to live in ~/bin/common and ~/bin/darwin.
# Usage: mac-utils.sh flushdns|resetvideo|unquarantine|net|addkey|brew-update
set -e

# The identity key is spelled differently per machine (identity_work on the
# work machine, identity_personal.pk on the personal one) — take the first
# one that exists, and let the environment override either.
SSH_KEY="${MAC_UTILS_SSH_KEY:-}"
if [ -z "$SSH_KEY" ]; then
  for cand in "$HOME/.ssh/identity_work" "$HOME/.ssh/identity_personal.pk"; do
    if [ -f "$cand" ]; then
      SSH_KEY="$cand"
      break
    fi
  done
fi

case "$1" in
  flushdns)
    dscacheutil -flushcache
    sudo killall -HUP mDNSResponder
    say DNS cache flushed
    ;;
  resetvideo)
    # VDCAssistant owns the built-in camera; killing it frees a wedged webcam.
    sudo killall VDCAssistant
    ;;
  unquarantine)
    shift
    if [ "$#" -eq 0 ]; then
      echo "usage: $(basename "$0") unquarantine <path>..." >&2
      exit 1
    fi
    xattr -d com.apple.quarantine "$@"
    ;;
  net)
    lsof -P -i -n | tail -n +2 | cut -f 1 -d " " | sort -u
    ;;
  addkey)
    if [ -z "$SSH_KEY" ]; then
      echo "mac-utils.sh: no identity key found: neither ~/.ssh/identity_work nor ~/.ssh/identity_personal.pk exists (set MAC_UTILS_SSH_KEY)" >&2
      exit 1
    fi
    if [ ! -f "$SSH_KEY" ]; then
      echo "mac-utils.sh: no such key: $SSH_KEY (set MAC_UTILS_SSH_KEY)" >&2
      exit 1
    fi
    # --apple-use-keychain keeps the passphrase in the login keychain, so a
    # job that starts after a reboot can load the key without a prompt
    # (`ssh-add --apple-load-keychain` in repo-sync.sh, sd:2160).
    ssh-add --apple-use-keychain "$SSH_KEY"
    ;;
  brew-update)
    brew update && brew upgrade && brew cleanup
    ;;
  -h|--help|help)
    cat <<'HELPEOF'
usage: mac-utils.sh flushdns|resetvideo|unquarantine|net|addkey|brew-update

  flushdns              flush the DNS cache and restart mDNSResponder (sudo)
  resetvideo            kill VDCAssistant to free a wedged built-in camera (sudo)
  unquarantine <path>.. strip com.apple.quarantine from the given paths
  net                   list the processes currently holding network sockets
  addkey                ssh-add this machine's identity key, keeping its
                        passphrase in the keychain across reboots
  brew-update           brew update && brew upgrade && brew cleanup

environment:
  MAC_UTILS_SSH_KEY     key used by addkey; without it, the first of
                        ~/.ssh/identity_work or ~/.ssh/identity_personal.pk
                        that exists
HELPEOF
    exit 0
    ;;
  *)
    echo "usage: $(basename "$0") flushdns|resetvideo|unquarantine|net|addkey|brew-update" >&2
    exit 1
    ;;
esac

# shellcheck shell=sh
# Readers of a `brew doctor` output. Source it; it defines functions and runs
# nothing else.
#
#   . "$DIR/../lib/brew_unlinked.sh"
#   st_unlinked_kegs "$tmp/doctor" | while read -r keg; do ...
#
# local-brew-doctor links the unlinked kegs; local-repo-sync names an unlinked
# python when the pack's make setup fails (sd:4864). local-brew-doctor and
# machine-setup's doctor drop the Intel platform warning. One parser each, so
# they agree.

# st_unlinked_kegs FILE: print each unlinked keg in doctor output FILE, one per
# line. Prints nothing, and succeeds, when the output lists none.
st_unlinked_kegs() {
    sed -n '/You have unlinked kegs/,/^$/p' "$1" | grep '^  [a-z0-9]' | tr -d ' ' || true
}

# st_drop_platform_warning [FILE]: print doctor output FILE, or stdin, less
# Homebrew's Intel x86_64 warning. Homebrew dropped Intel in September 2026
# and warns on every Intel Mac; no fix clears it, so it is no finding
# (sd:4785, sd:4943). Run it on an answered output only: it can leave a
# warning exit with no warning.
st_drop_platform_warning() {
    awk '/^Warning:/ { skip = /^Warning: You are using macOS on Intel x86_64\./ } !skip' "$@"
}

# st_drop_unlinked_keg KEG [FILE]: print doctor output FILE, or stdin, less
# KEG's line in the unlinked kegs warning, and less the warning when it lists
# no other keg. KEG matches a whole line, never a part of one.
st_drop_unlinked_keg() {
    st_keg_line="  $1"; shift
    awk -v k="$st_keg_line" '
      function flush() { if (keep) printf "%s", buf; buf = ""; inblk = 0 }
      /^Warning:/ { flush(); inblk = /^Warning: You have unlinked kegs/; keep = 0 }
      !inblk { print; next }
      $0 == k { next }
      /^  [^ ]/ { keep = 1 }
      { buf = buf $0 "\n" }
      END { flush() }' "$@"
}

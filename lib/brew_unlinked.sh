# The kegs a `brew doctor` output names as unlinked. Source it; it defines one
# function and runs nothing else.
#
#   . "$DIR/../lib/brew_unlinked.sh"
#   st_unlinked_kegs "$tmp/doctor" | while read -r keg; do ...
#
# local-brew-doctor links them; local-repo-sync names an unlinked python when
# the pack's make setup fails (sd:4864). One parser, so they agree.

# st_unlinked_kegs FILE: print each unlinked keg in doctor output FILE, one per
# line. Prints nothing, and succeeds, when the output lists none.
st_unlinked_kegs() {
    sed -n '/You have unlinked kegs/,/^$/p' "$1" | grep '^  [a-z0-9]' | tr -d ' ' || true
}

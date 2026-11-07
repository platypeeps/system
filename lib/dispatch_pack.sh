# shellcheck shell=sh
# The build-dispatch pack guard (sd:7932). Source it; it sets one value and
# defines one function.
#
#   . "$DIR/../lib/dispatch_pack.sh"
#   st_dispatch_pack_held "$SD_PACK_ROOT" || echo "skip"
#
# A machine other than the hub on a pack before host scoping (pack sd:7933)
# would dispatch the hub's lanes a second time. machine-setup's cron stage
# and doctor read it, and so does cron-jobs.sh before each build-dispatch
# tick, so the commit lives here once. Tests point it at a fixture commit.
SD_DISPATCH_PACK_MIN="${SD_DISPATCH_PACK_MIN:-f6ace045c3eeb4525c45cabfe1ff7b61a3874961}"

# st_dispatch_pack_held <pack root>: whether its HEAD holds
# SD_DISPATCH_PACK_MIN. A pack git cannot read is no answer, so it fails too.
st_dispatch_pack_held() {
    git -C "$1" merge-base --is-ancestor "$SD_DISPATCH_PACK_MIN" HEAD >/dev/null 2>&1
}

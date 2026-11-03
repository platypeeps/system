# shellcheck shell=sh
# Quit a running App Store app before its `mas upgrade`, and open it again
# after. Source it after lib/bounded.sh; it defines functions and runs
# nothing else.
#
#   if st_mas_quit "$apps_dir" "$id" "$name"; then
#     st_step 600 mas upgrade "$id" || ...
#   else
#     ...  # still running: skip this app's upgrade, and say so
#   fi
#   st_mas_reopen || ...
#
# The App Store installer puts up a dialog for an app that runs, and nothing
# under launchd answers it: the nightly upgrade of PeakHour, a menu-bar app,
# failed each night (sd:7330). The quit is a TERM, as a logout sends, and
# never osascript: under launchd an Automation prompt waits forever
# (.claude/rules/macos-tcc.md). An app that ignores the TERM keeps running;
# it is never KILLed, so it loses no work.

# st_mas_quit DIR ID NAME: DIR/NAME.app, NAME as `mas outdated` lists it,
# when Spotlight gives it App Store id ID and its executable runs, gets a
# TERM and up to ST_MAS_QUIT_WAIT seconds (default 15) to exit. Sets
# ST_MAS_APP to the bundle when it was running, else to nothing. Returns 1
# when it still runs, else 0. A bundle not found, a Spotlight read that
# fails and a failed process probe each leave the app alone and return 0:
# the upgrade then runs as it did before this check.
st_mas_quit() {
    ST_MAS_APP=
    _mq_app="$1/$3.app"
    [ -n "$3" ] && [ -d "$_mq_app/Contents/MacOS" ] || return 0
    _mq_id=$(st_bounded 30 mdls -raw -name kMDItemAppStoreAdamID "$_mq_app" < /dev/null 2>/dev/null) || return 0
    [ "$_mq_id" = "$2" ] || return 0
    _mq_pids=$(_st_mas_pids "$_mq_app") || return 0
    [ -n "$_mq_pids" ] || return 0
    ST_MAS_APP=$_mq_app
    # shellcheck disable=SC2086 # one word per pid
    kill -TERM $_mq_pids 2>/dev/null || :
    _mq_left=${ST_MAS_QUIT_WAIT:-15}
    while [ "$_mq_left" -gt 0 ]; do
        sleep 1
        _mq_left=$((_mq_left - 1))
        _mq_pids=$(_st_mas_pids "$_mq_app") || continue
        [ -n "$_mq_pids" ] || return 0
    done
    return 1
}

# st_mas_reopen: open in the background the app st_mas_quit found running,
# upgraded or not. One that never quit is running still, and `open -g`
# leaves it so; one that exited after the wait comes back. Returns 1 when
# `open` failed or did not answer in 60 s.
st_mas_reopen() {
    _mr_app=$ST_MAS_APP
    ST_MAS_APP=
    [ -n "$_mr_app" ] || return 0
    st_bounded 60 open -g "$_mr_app" < /dev/null > /dev/null 2>&1
}

# _st_mas_pids BUNDLE: the pids whose arguments name an executable in
# BUNDLE/Contents/MacOS/, one per line; nothing when none runs. Returns 1
# when pgrep failed, an unknown answer. pgrep reads its pattern as an
# extended regex, so the path's special characters are escaped.
_st_mas_pids() {
    _mp_re=$(printf '%s/Contents/MacOS/' "$1" | sed 's/[]\[\\.*^$+?(){}|]/\\&/g')
    _mp_rc=0
    pgrep -f "$_mp_re" < /dev/null || _mp_rc=$?
    [ "$_mp_rc" -le 1 ]
}

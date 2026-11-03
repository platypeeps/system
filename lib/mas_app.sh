# shellcheck shell=sh
# Quit a running App Store app before its `mas upgrade`, and open it again
# after. Source it after lib/bounded.sh; it defines functions and runs
# nothing else.
#
#   rc=0; st_mas_quit "$apps_dir" "$id" "$name" || rc=$?
#   case $rc in
#     0) st_step 600 mas upgrade "$id" || ... ;;
#     1) ... ;;  # quit, but still running: skip its upgrade, and say so
#     2) ... ;;  # running, may hold documents: skip it, ask the operator
#   esac
#   st_mas_reopen || ...
#
# The App Store installer puts up a dialog for an app that runs, and nothing
# under launchd answers it: the nightly upgrade of PeakHour, a menu-bar app,
# failed each night (sd:7330). The quit is a TERM, as a logout sends, and
# never osascript: under launchd an Automation prompt waits forever
# (.claude/rules/macos-tcc.md). An app that ignores the TERM keeps running;
# it is never KILLed. An app that may hold documents gets no TERM at all.

# st_mas_quit DIR ID NAME: quit DIR/NAME.app, NAME as `mas outdated` lists
# it, when Spotlight gives it App Store id ID and its executable runs.
# Only a menu-bar or background app is quit (LSUIElement or
# LSBackgroundOnly true in its Info.plist): one with a Dock icon may hold
# documents, and a TERM skips its save flow, so it is left alone and
# returns 2. A quit app gets a TERM and up to ST_MAS_QUIT_WAIT seconds
# (default 15) to exit, and returns 1 when it still runs.
# Sets ST_MAS_APP to the bundle when it was quit, else to nothing. Returns 0
# when nothing runs. A bundle not found, and a Spotlight, plist or process
# read that fails, each leave the app alone and return 0: the upgrade then
# runs as it did before this check.
st_mas_quit() {
    ST_MAS_APP=
    _mq_app="$1/$3.app"
    _mq_plist="$_mq_app/Contents/Info.plist"
    [ -n "$3" ] && [ -f "$_mq_plist" ] || return 0
    _mq_id=$(st_bounded 30 mdls -raw -name kMDItemAppStoreAdamID "$_mq_app" < /dev/null 2>/dev/null) || return 0
    [ "$_mq_id" = "$2" ] || return 0
    _mq_exe=$(_st_mas_key "$_mq_plist" CFBundleExecutable) || return 0
    case $_mq_exe in ''|*/*) return 0 ;; esac
    _mq_pids=$(_st_mas_pids "$_mq_app" "$_mq_exe") || return 0
    [ -n "$_mq_pids" ] || return 0
    _st_mas_agent "$_mq_plist" || return 2
    ST_MAS_APP=$_mq_app
    # Only the pids _st_mas_pids matched by their executable's full path.
    # shellcheck disable=SC2086 # one word per pid
    kill -TERM $_mq_pids 2>/dev/null || :
    _mq_left=${ST_MAS_QUIT_WAIT:-15}
    while [ "$_mq_left" -gt 0 ]; do
        sleep 1
        _mq_left=$((_mq_left - 1))
        # An unknown answer is no exit: wait on.
        _mq_pids=$(_st_mas_pids "$_mq_app" "$_mq_exe") || continue
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

# _st_mas_pids BUNDLE EXECUTABLE: the pids of BUNDLE's own executable, one
# per line; nothing when none runs. A candidate from `pgrep -x`, which
# matches the process name, counts only when `ps -o comm=` gives the full
# path BUNDLE/Contents/MacOS/EXECUTABLE exactly: a process of that name
# in a copy elsewhere, such as a backup volume, is not the app, nor is one
# whose arguments name the path. A candidate ps cannot read is not counted.
# Returns 1 when pgrep failed, an unknown answer. pgrep reads its pattern
# as an extended regex, so the name's special characters are escaped.
_st_mas_pids() {
    _mp_path="$1/Contents/MacOS/$2"
    _mp_re=$(printf '%s' "$2" | sed 's/[]\[\\.*^$+?(){}|]/\\&/g')
    _mp_rc=0
    _mp_found=$(pgrep -x "$_mp_re" < /dev/null) || _mp_rc=$?
    [ "$_mp_rc" -le 1 ] || return 1
    for _mp_pid in $_mp_found; do
        if [ "$(ps -o comm= -p "$_mp_pid" 2>/dev/null)" = "$_mp_path" ]; then
            echo "$_mp_pid"
        fi
    done
}

# _st_mas_key PLIST KEY: KEY's value in PLIST; exit 1 when it holds none.
_st_mas_key() {
    st_bounded 30 plutil -extract "$2" raw -o - "$1" < /dev/null 2>/dev/null
}

# _st_mas_agent PLIST: true when the app is a menu-bar or background app.
# A read that fails counts as no: the app is then left alone.
_st_mas_agent() {
    for _ma_key in LSUIElement LSBackgroundOnly; do
        case $(_st_mas_key "$1" "$_ma_key") in true|1|YES) return 0 ;; esac
    done
    return 1
}

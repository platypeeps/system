# shellcheck shell=sh
# Quit a running App Store app the operator named before its `mas
# upgrade`, and open it again after. Source it after lib/config.sh and
# lib/bounded.sh; it defines functions and runs nothing else.
#
#   st_mas_bad_ids | while IFS= read -r bad; do ...; done  # report each
#   rc=0; st_mas_quit "$apps_dir" "$id" "$name" || rc=$?
#   case $rc in
#     0) st_step 600 mas upgrade "$id" || ... ;;
#     1) ... ;;  # quit, but still running: skip its upgrade, and say so
#     2) ... ;;  # running, not named: skip it, ask the operator to quit it
#   esac
#   st_mas_reopen || ...
#
# The App Store installer puts up a dialog for an app that runs, and nothing
# under launchd answers it: the nightly upgrade of PeakHour, a menu-bar app,
# failed each night (sd:7330). A TERM can lose an app's unsaved state, a
# menu-bar app's too, so only the App Store ids the operator lists in
# MAS_QUIT_APP_IDS, in <config>/shared.env, are quit; an empty or unset
# list quits nothing. The quit is a TERM, as a logout sends, and never
# osascript: under launchd an Automation prompt waits forever
# (.claude/rules/macos-tcc.md). An app that ignores the TERM keeps running;
# it is never KILLed.

# st_mas_bad_ids: each MAS_QUIT_APP_IDS entry that is not an App Store id,
# all digits, one per line. Such an entry is never compared or passed on.
st_mas_bad_ids() {
    _st_mas_ids | grep -v '^[0-9][0-9]*$' || :
}

# st_mas_quit DIR ID NAME: quit DIR/NAME.app, NAME as `mas outdated` lists
# it, when Spotlight gives it App Store id ID and its executable runs.
# Only an app whose ID is in MAS_QUIT_APP_IDS is quit; any other that runs
# is left alone and returns 2. A quit app gets a TERM and up to
# ST_MAS_QUIT_WAIT seconds (default 15) to exit, and returns 1 when it
# still runs. Sets ST_MAS_APP to the bundle when it was quit, else to
# nothing. Returns 0 when nothing runs. A bundle not found, and a
# Spotlight, plist or process read that fails, each leave the app alone
# and return 0: the upgrade then runs as it did before this check.
st_mas_quit() {
    ST_MAS_APP=
    _mq_app="$1/$3.app"
    _mq_plist="$_mq_app/Contents/Info.plist"
    [ -n "$3" ] && [ -f "$_mq_plist" ] || return 0
    _mq_id=$(st_bounded 30 mdls -raw -name kMDItemAppStoreAdamID "$_mq_app" < /dev/null 2>/dev/null) || return 0
    [ "$_mq_id" = "$2" ] || return 0
    _mq_exe=$(st_bounded 30 plutil -extract CFBundleExecutable raw -o - "$_mq_plist" < /dev/null 2>/dev/null) ||
        return 0
    case $_mq_exe in ''|*/*) return 0 ;; esac
    _mq_pids=$(_st_mas_pids "$_mq_app" "$_mq_exe") || return 0
    [ -n "$_mq_pids" ] || return 0
    _st_mas_ids | grep -qxF -- "$2" || return 2
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
# matches the process name, counts only when _st_mas_exe gives the full
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
        if [ "$(_st_mas_exe "$_mp_pid")" = "$_mp_path" ]; then
            echo "$_mp_pid"
        fi
    done
}

# _st_mas_exe PID: the full path of PID's executable, nothing when ps
# cannot read it. `-ww`, as on a terminal plain ps cuts each line to the
# window width, and a path cut short never matches (sd:8580).
_st_mas_exe() {
    ps -ww -o comm= -p "$1" 2>/dev/null
}

# _st_mas_ids: the MAS_QUIT_APP_IDS entries, one per line, as written.
# Split by tr, not by the shell, so an entry such as `*` is no glob.
_st_mas_ids() {
    st_shared MAS_QUIT_APP_IDS | tr -s ' \t' '\n\n' | grep -v '^$' || :
}

# shellcheck shell=sh
# Shared config location for every tool in this repository. Source it; it
# defines functions and one variable and runs nothing else.
#
#   . "$DIR/../lib/config.sh"
#   CONF_DIR="$(st_config_dir repo-sync)"
#
# Private, per-machine values (.env files, *.conf lists, job definitions)
# live under one directory outside the checkout, one subfolder per tool:
#
#   ${SYSTEM_TOOLS_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/system}/<tool>/
#
# <tool> is the folder name minus `local-` (`repo-sync`, `notify`); a
# `mezmo-*` folder keeps its full name (`mezmo-pipeline`). The repository
# keeps only the committed `.example` files.

SYSTEM_TOOLS_CONFIG="${SYSTEM_TOOLS_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/system}"

# st_shared <NAME>: the value of a name every tool shares, kept once in
# <config>/shared.env (SYSTEM_TOOLS_LABEL_PREFIX, OBSIDIAN_VAULT; sd:3990). A
# variable that is set wins, an empty one too; else the file, read in a
# subshell so none of its other names reach the caller; else nothing. A tool's
# own .env, loaded by st_source_env, sets the variable and so still overrides
# the file. A file that does not source is named on stderr and gives nothing.
st_shared() {
    eval "_st_set=\${$1+x}"
    if [ -n "$_st_set" ]; then
        unset _st_set
        eval "printf '%s\n' \"\$$1\""
        return 0
    fi
    unset _st_set
    [ -f "$SYSTEM_TOOLS_CONFIG/shared.env" ] || return 0
    _st_out=$(
        . "$SYSTEM_TOOLS_CONFIG/shared.env" >/dev/null 2>&1 </dev/null || exit 1
        eval "printf '%s\n' \"\${$1-}\""
    ) || {
        printf 'st_shared: %s does not source; its values are ignored\n' "$SYSTEM_TOOLS_CONFIG/shared.env" >&2
        unset _st_out
        return 0
    }
    printf '%s\n' "$_st_out"
    unset _st_out
}

# st_label_prefix: the launchd label prefix of every job and agent here
# (convention 7): SYSTEM_TOOLS_LABEL_PREFIX, else local.system-tools.
st_label_prefix() {
    _st_prefix=$(st_shared SYSTEM_TOOLS_LABEL_PREFIX)
    printf '%s\n' "${_st_prefix:-local.system-tools}"
    unset _st_prefix
}

# st_obsidian_vault: the Obsidian vault path of every tool that reads one:
# OBSIDIAN_VAULT, else ~/Documents/Obsidian Vault. machine-setup keeps
# st_shared, since an unset vault skips its stage.
st_obsidian_vault() {
    _st_vault=$(st_shared OBSIDIAN_VAULT)
    printf '%s\n' "${_st_vault:-$HOME/Documents/Obsidian Vault}"
    unset _st_vault
}

# st_shell_env: what a headless claude needs from ~/.config/shell/env.sh,
# loaded in the shell that execs it. The file is sourced quietly when it
# exists. It exports the setup token as CUSTOM_CLAUDE_CODE_OAUTH_TOKEN, so a
# day-to-day claude keeps its own login (sd:5786); this exports it as
# CLAUDE_CODE_OAUTH_TOKEN when that is unset or empty (sd:5791). The token
# goes in no argv, file or log. Returns 0. The pack's dashboard keeps the
# same mapping in sd_dashboard/claude_env.py, since its package may run outside
# this checkout.
st_shell_env() {
    # env.sh is never dotted in this shell. A subshell, the same shell as
    # this one, sources it once with set -e off and output on /dev/null,
    # then prints what changed; this shell evals that only when the
    # subshell lived to its marker. A bare `.` here ended every cron run
    # with stdout on /dev/null for 10 minutes, and no job log said why
    # (sd:9652); a syntax error in any file of the chain, an `exit`, or a
    # set -e it turns on ends a POSIX shell whatever set -e says. Such a
    # chain is named and skipped. The one run decides both the import and
    # the warning: a second, fresh run to name a failed command missed
    # what hung on an unexported variable, a function or a load guard,
    # and a file edited between runs still ended the runner. The
    # subshell is the same shell, since bash in POSIX mode lives through
    # a syntax error that ends dash, and its output evals back in the
    # shell that wrote it. The assignment is bare, with the caller's
    # set -e off: left of `||`, bash 5 ignored a set -e the chain turns
    # on. An ERR trap, which fires where set -e would, names a failed
    # command, even one a later export hides from `$?`; it only warns.
    # ponytail: dash has no ERR trap, so there a failed command goes
    # unnamed; macOS /bin/sh, the runner's shell, is bash.
    # shell-env.example assigns without `export`, so the subshell sources
    # under set -a and every assignment crosses, exported: the jobs and
    # the claude they start are what read these values.
    # Tracing stays off to the end, in the subshell too after the chain
    # returns: under `sh -x`, or a set -x in env.sh, the values and the
    # mapped token went to stderr, which a job log keeps.
    case $- in *x*) set +x; _st_x=1 ;; *) _st_x= ;; esac
    _st_env="$HOME/.config/shell/env.sh"
    if [ -f "$_st_env" ]; then
        case $- in *e*) _st_e=1 ;; *) _st_e= ;; esac
        set +e
        # shellcheck source=/dev/null
        # The chain's stderr goes ahead of the exports marker: bash, POSIX
        # mode too, returns from a file with a syntax error and goes on, so
        # its message is the one sign that only part of the chain ran. It
        # counts only as `<file>: line <n>: syntax error` naming a file
        # that fails `sh -n`: a tool that printed the phrase dropped every
        # export of a chain that ran to its end.
        # ponytail: matches bash's English message; a translated one would
        # pass, set LC_MESSAGES for the probe if that ever bites.
        # A relative name is tried from this directory and from each one
        # the ERR trap logged: after a `cd` in the chain, this directory
        # alone found no file and imported the partial settings.
        _st_ok=$(
            # Only what the chain adds, changes or unsets goes back, in
            # this shell's own quoting, and never a readonly name:
            # replaying the whole `export -p` assigned an exported
            # readonly name, bash's SHELLOPTS too, and that ended a POSIX
            # caller. The dumps run in a subshell, so its `set -o posix`
            # binds POSIXLY_CORRECT there alone.
            # Each pattern opens with `(`: bash 3.2 misparses a bare one
            # inside `$( )`.
            # ponytail: a name is read off each dump line; a value with a
            # line that looks like `export NAME` only adds a name to check.
            # Builtins only: the scan runs under the PATH env.sh set, and
            # one without sed imported nothing and said so nowhere.
            _st_names() {
                # shellcheck disable=SC3040 # bash only, behind the guard
                ( [ -z "${BASH_VERSION:-}" ] || set -o posix; "$@" -p ) |
                    while IFS= read -r _st_l; do
                        case $_st_l in ("$1 "*) ;; (*) continue ;; esac
                        _st_l=${_st_l#"$1 "}
                        _st_l=${_st_l%%=*}
                        case $_st_l in
                            (''|[0-9]*|*[!A-Za-z0-9_]*) ;;
                            (*) printf '%s\n' "$_st_l" ;;
                        esac
                    done
            }
            _st_before=$(_st_names export)
            for _st_n in $_st_before; do
                eval "_st_b_$_st_n=\${$_st_n-}; _st_h_$_st_n=1"
            done
            # The trap counts each failed command; when the chain's last
            # status is not 0, the `.` itself counts once more, so one
            # less is the chain's own count: a false `[ ] && x` last
            # line fails no command. errtrace carries the trap into a
            # function: without it a failed read inside one went unseen.
            # The trap logs its directory ahead of the marker, where a
            # relative name in a syntax error resolves.
            _st_fail=0
            # shellcheck disable=SC3047,SC3041 # bash only, behind the guard
            ! (trap : ERR) 2>/dev/null || { set -E; trap '_st_fail=$((_st_fail + 1)); printf "#st_shell_env dir %s\\n" "$PWD" >&2' ERR; }
            set -a
            . "$_st_env" 2>&1 >/dev/null </dev/null
            _st_rc=$?
            set +a +x +v
            [ "$_st_rc" = 0 ] || [ "$_st_fail" = 0 ] || _st_fail=$((_st_fail - 1))
            # shellcheck disable=SC3047
            ! (trap : ERR) 2>/dev/null || trap - ERR
            echo '#st_shell_env exports'
            _st_ro=' '
            for _st_n in $(_st_names readonly); do _st_ro="$_st_ro$_st_n "; done
            for _st_n in $_st_before; do
                eval "[ -z \"\${$_st_n+x}\" ]" && printf 'unset %s\n' "$_st_n"
            done
            for _st_n in $(_st_names export); do
                case $_st_ro in (*" $_st_n "*) continue ;; esac
                eval "[ -n \"\${$_st_n+x}\" ]" || continue
                ! eval "[ -n \"\${_st_h_$_st_n-}\" ] && [ \"\$_st_b_$_st_n\" = \"\$$_st_n\" ]" || continue
                eval "_st_s=\$$_st_n"
                _st_q=
                while :; do
                    case $_st_s in
                        (*"'"*) _st_q="$_st_q${_st_s%%"'"*}'\\''"; _st_s=${_st_s#*"'"} ;;
                        (*) break ;;
                    esac
                done
                printf "export %s='%s'\n" "$_st_n" "$_st_q$_st_s"
            done
            echo "#st_shell_env ok $_st_fail"
        )
        case $_st_ok in
            (*': line '[0-9]*': syntax error'*'#st_shell_env exports'*)
                _st_pre=${_st_ok%%'#st_shell_env exports'*}
                _st_dirs=$(printf '%s\n' "$_st_pre" | while IFS= read -r _st_l; do
                    case $_st_l in
                        ('#st_shell_env dir '*) printf '%s\n' "${_st_l#'#st_shell_env dir '}" ;;
                    esac
                done)
                while IFS= read -r _st_l; do
                    case $_st_l in
                        (*': line '[0-9]*': syntax error'*)
                            _st_f=${_st_l%%': line '[0-9]*}
                            # The first, empty line is this directory.
                            while IFS= read -r _st_d; do
                                case $_st_f in
                                    (/*) _st_p=$_st_f ;;
                                    (*) _st_p=${_st_d:+$_st_d/}$_st_f ;;
                                esac
                                [ ! -f "$_st_p" ] || "${BASH:-sh}" -n "$_st_p" 2>/dev/null || _st_ok=
                            done <<EOD

$_st_dirs
EOD
                            ;;
                    esac
                done <<EOF
$_st_pre
EOF
                ;;
        esac
        case $_st_ok in
            *'#st_shell_env ok '[0-9]*)
                eval "${_st_ok#*'#st_shell_env exports'}"
                _st_rc=${_st_ok##*'#st_shell_env ok '}
                [ "$_st_rc" = 0 ] ||
                    echo "st_shell_env: a command in $_st_env failed ($_st_rc in all); a value it sets may be missing" >&2
                ;;
            *) echo "st_shell_env: $_st_env or a file it sources ends the shell; skipped, so its exports are missing" >&2 ;;
        esac
        [ -z "$_st_e" ] || set -e
    fi
    unset _st_env _st_e _st_ok _st_rc _st_l _st_f _st_d _st_p _st_pre _st_dirs
    [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] || [ -z "${CUSTOM_CLAUDE_CODE_OAUTH_TOKEN:-}" ] ||
        export CLAUDE_CODE_OAUTH_TOKEN="$CUSTOM_CLAUDE_CODE_OAUTH_TOKEN"
    [ -z "$_st_x" ] || set -x
    unset _st_x
    return 0
}

# st_config_dir <tool>: the tool's config directory. It may not exist.
st_config_dir() {
    printf '%s/%s\n' "$SYSTEM_TOOLS_CONFIG" "$1"
}

# st_state_dir <tool>: where the tool keeps what its runs write and nobody
# edits (a last-run marker, a lock, a push hash): the XDG state folder, never
# the config folder, which holds values only (sd:3990). It may not exist.
st_state_dir() {
    printf '%s/system/%s\n' "${XDG_STATE_HOME:-$HOME/.local/state}" "$1"
}

# st_config_file <tool> <file>: where the tool reads <file>, the copy of its
# committed <file>.example. Most files sit in the tool's config directory;
# privacy-patterns sits at the root, where every clone's pre-push guard finds
# it, and shared.env sits there for every tool that reads it. The reader and
# local-config-check both resolve paths here.
st_config_file() {
    case "$2" in
        privacy-patterns|shared.env) printf '%s/%s\n' "$SYSTEM_TOOLS_CONFIG" "$2" ;;
        *) printf '%s/%s/%s\n' "$SYSTEM_TOOLS_CONFIG" "$1" "$2" ;;
    esac
}

# st_source_env <tool> [file]: source <config>/<tool>/<file> (default .env,
# machine-setup reads machine-setup.env) when it exists, with
# `set -a` so its assignments are exported. Returns 0 either way; a missing
# file is fine when the values are exported. A value in the environment wins
# over the file, an empty one too, so `VAR=x tool start` overrides it for one
# run (sd:3553): each exported name the file assigns is saved before sourcing
# and put back. Every `NAME=` and `NAME:=` in the file counts, so a second
# assignment on a line and a `${X:=v}` default are caught; a match inside a
# value only saves an exported value, which wins anyway. Only the environment
# counts: a default the script assigned to an unexported variable before this
# call loses to the file.
st_source_env() {
    _st_env="$SYSTEM_TOOLS_CONFIG/$1/${2:-.env}"
    if [ -f "$_st_env" ]; then
        _st_n=0
        for _st_name in $(grep -o -E '[A-Za-z_][A-Za-z0-9_]*:?=' "$_st_env" | sed 's/:*=$//'); do
            if printenv "$_st_name" >/dev/null 2>&1; then
                _st_val=$(printenv "$_st_name")
                _st_n=$((_st_n + 1))
                eval "_st_name_$_st_n=\$_st_name; _st_val_$_st_n=\$_st_val"
            fi
        done
        set -a
        # shellcheck disable=SC1090
        . "$_st_env"
        set +a
        while [ "$_st_n" -gt 0 ]; do
            eval "_st_name=\$_st_name_$_st_n"
            eval "$_st_name=\$_st_val_$_st_n; export $_st_name"
            unset "_st_name_$_st_n" "_st_val_$_st_n"
            _st_n=$((_st_n - 1))
        done
        unset _st_n _st_name _st_val
    fi
    unset _st_env
}

# st_missing <what> <tool> <file> [folder]: print the standard remedy for a
# missing value or file to stderr. <what> is a variable name or a file name;
# <file> is the config file that holds it (`.env`, `repos.personal.conf`);
# [folder] is the repository folder with the `.example` (default local-<tool>).
st_missing() {
    _st_folder="${4:-local-$2}"
    printf '%s: %s is not set.\n' "${0##*/}" "$1" >&2
    printf '  Export it, or copy %s/%s.example to %s/%s/%s and fill it in.\n' \
        "$_st_folder" "$3" "$SYSTEM_TOOLS_CONFIG" "$2" "$3" >&2
    unset _st_folder
}

# st_main_checkout <root>: the main checkout of the git repository whose top
# level is <root>, so a hook or job names a path that outlives a linked
# worktree (sd:3390). git gives the path with symlinks resolved. A <root>
# without .git is a copy outside git and prints unchanged. Returns 1 when git
# cannot name the main checkout.
st_main_checkout() {
    if [ ! -e "$1/.git" ]; then
        printf '%s\n' "$1"
        return 0
    fi
    _st_common=$(git -C "$1" rev-parse --path-format=absolute --git-common-dir 2>/dev/null </dev/null) ||
        { unset _st_common; return 1; }
    case "$_st_common" in
        */.git) printf '%s\n' "${_st_common%/.git}" ;;
        *) unset _st_common; return 1 ;;
    esac
    unset _st_common
}

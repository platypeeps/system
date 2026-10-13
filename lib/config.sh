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

# st_source_env <tool>: source <config>/<tool>/.env when it exists, with
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
    _st_env="$SYSTEM_TOOLS_CONFIG/$1/.env"
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

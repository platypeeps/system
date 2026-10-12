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

# st_label_prefix: the launchd label prefix of every job and agent here
# (convention 7): SYSTEM_TOOLS_LABEL_PREFIX, else local.system-tools.
st_label_prefix() {
    printf '%s\n' "${SYSTEM_TOOLS_LABEL_PREFIX:-local.system-tools}"
}

# st_config_dir <tool>: the tool's config directory. It may not exist.
st_config_dir() {
    printf '%s/%s\n' "$SYSTEM_TOOLS_CONFIG" "$1"
}

# st_config_file <tool> <file>: where the tool reads <file>, the copy of its
# committed <file>.example. Most files sit in the tool's config directory;
# privacy-patterns sits at the root, where every clone's pre-push guard finds
# it. The reader and local-config-check both resolve paths here.
st_config_file() {
    case "$2" in
        privacy-patterns) printf '%s/%s\n' "$SYSTEM_TOOLS_CONFIG" "$2" ;;
        *) printf '%s/%s/%s\n' "$SYSTEM_TOOLS_CONFIG" "$1" "$2" ;;
    esac
}

# st_source_env <tool>: source <config>/<tool>/.env when it exists, with
# `set -a` so its assignments are exported. Returns 0 either way; a missing
# file is fine when the values are exported. A value in the environment wins
# over the file, an empty one too, so `VAR=x tool start` overrides it for one
# run (sd:3553): each exported name the file assigns is saved before sourcing
# and put back. Only the environment counts: a default the script assigned to
# an unexported variable before this call loses to the file.
st_source_env() {
    _st_env="$SYSTEM_TOOLS_CONFIG/$1/.env"
    if [ -f "$_st_env" ]; then
        _st_n=0
        for _st_name in $(sed -n 's/^[[:space:]]*\(export[[:space:]][[:space:]]*\)\{0,1\}\([A-Za-z_][A-Za-z0-9_]*\)=.*/\2/p' "$_st_env"); do
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

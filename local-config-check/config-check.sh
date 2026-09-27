#!/bin/sh
# Check each tool's private config against the example it commits.
# Usage: config-check.sh check [tool...]|status|list|test|help
#
# The manifest is the tree itself: every top-level `<folder>/*.example` maps
# to `<config>/<tool>/<file>`. Nothing here lists the tools, so a new tool
# with a `.env.example` is checked without an edit. Output names variables
# and files only; a value never reaches stdout or stderr.
set -e

# Walk symlinks so a linked copy still finds ../lib.
cc_self="$0"
while [ -L "$cc_self" ]; do
    cc_link=$(readlink "$cc_self")
    case "$cc_link" in
        /*) cc_self="$cc_link" ;;
        *) cc_self="$(dirname "$cc_self")/$cc_link" ;;
    esac
done
DIR="$(cd "$(dirname "$cc_self")" && pwd)"
. "$DIR/../lib/config.sh"

# CONFIG_CHECK_ROOT points the scan at another tree (the tests use it).
cc_root="${CONFIG_CHECK_ROOT:-$DIR/..}"
cc_root="$(cd "$cc_root" && pwd)"

usage() {
    cat <<'USAGE'
config-check.sh — check each tool's config against its committed example.

Usage: config-check.sh <command>

  check [tool...]  Check every tool with a committed *.example, or the named
                   ones (tool name or folder name). Per tool: whether
                   <config>/<tool>/.env exists; required variables (the
                   uncommented ones in .env.example) that are unset or still
                   a placeholder (change-me, /path/to/..., example.test);
                   *_DIR, *_REPO, *_FILE, *_SRC, *_SOURCE, *_DESTINATION
                   values whose path does not exist; *_SD_KEY values that
                   `sd config get` cannot read (skipped when sd is not on
                   PATH). Other *.example files are reported present or
                   absent. Prints names only, never a value. Exits 1 when a
                   configured tool is broken, else 0.
  status           One line. Exits 0 when every configured tool is healthy,
                   3 when no tool is configured, 1 when a configured tool is
                   broken -- local-health-check reads these codes.
  list             Each example file and the config path it maps to.
  test             Run this folder's tests.
  help             This text.

A tool is configured when its .env or another mapped file exists. A tool
whose .env.example variables are all commented out is fine without a .env.
<config> is ${SYSTEM_TOOLS_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/system};
<tool> is the folder name minus local-.
USAGE
}

# Folders with at least one top-level *.example, one per line.
tool_folders() {
    for cc_d in "$cc_root"/*/; do
        cc_d=${cc_d%/}
        for cc_f in "$cc_d"/*.example "$cc_d"/.*.example; do
            if [ -f "$cc_f" ]; then
                basename "$cc_d"
                break
            fi
        done
    done
}

tool_name() {
    printf '%s\n' "${1#local-}"
}

# Example files of one folder, basenames, one per line.
examples() {
    for cc_f in "$cc_root/$1"/.*.example "$cc_root/$1"/*.example; do
        [ -f "$cc_f" ] && basename "$cc_f"
    done
    return 0
}

# Variable names in an .env.example: "R NAME" for an uncommented assignment
# (required), "O NAME" for a commented one (optional).
example_vars() {
    awk '
        { line = $0; sub(/^[ \t]+/, "", line) }
        line ~ /^#/ {
            sub(/^#[ \t]*/, "", line)
            if (line ~ /^(export[ \t]+)?[A-Za-z_][A-Za-z0-9_]*=/) {
                sub(/^export[ \t]+/, "", line); sub(/=.*/, "", line); print "O " line
            }
            next
        }
        line ~ /^(export[ \t]+)?[A-Za-z_][A-Za-z0-9_]*=/ {
            sub(/^export[ \t]+/, "", line); sub(/=.*/, "", line); print "R " line
        }
    ' "$1"
}

# evaluate <envfile> <required> <optional> <sd>: source the .env the way
# st_source_env does, in a subshell, and print one finding per line:
# missing|placeholder|path|sdkey|sdskip NAME, then "done". A missing "done"
# means the file did not source.
evaluate() {
    (
        cc_env=$1; cc_req=$2; cc_opt=$3; cc_sd=$4
        unset cc_self cc_link cc_root DIR
        set -a
        . "$cc_env" >/dev/null 2>&1 </dev/null
        set +a
        for cc_v in $cc_req $cc_opt; do
            eval "cc_val=\${$cc_v-}"
            if [ -z "$cc_val" ]; then
                case " $cc_req " in *" $cc_v "*) echo "missing $cc_v" ;; esac
                continue
            fi
            case "$cc_val" in
                *change-me*|*/path/to/*|*example.test*) echo "placeholder $cc_v"; continue ;;
            esac
            case "$cc_v" in
                *_DIR|*_REPO|*_FILE|*_SRC|*_SOURCE|*_DESTINATION)
                    cc_p=$cc_val
                    case "$cc_p" in "~/"*) cc_p="$HOME/${cc_p#"~/"}" ;; esac
                    [ -e "$cc_p" ] || echo "path $cc_v"
                    ;;
                *_SD_KEY)
                    if [ -z "$cc_sd" ]; then
                        echo "sdskip $cc_v"
                    elif ! "$cc_sd" config get "$cc_val" >/dev/null 2>&1 </dev/null; then
                        echo "sdkey $cc_v"
                    fi
                    ;;
            esac
        done
        echo done
    ) 2>/dev/null || true
}

# check_tool <folder>: print the tool's report; set cc_state to
# ok|broken|unconfigured|optional.
check_tool() {
    cc_folder=$1
    cc_tool=$(tool_name "$cc_folder")
    cc_cfg=$(st_config_dir "$cc_tool")
    cc_req=""; cc_opt=""; cc_has_env_example=0; cc_present=0
    cc_details=""
    if [ -f "$cc_root/$cc_folder/.env.example" ]; then
        cc_has_env_example=1
        cc_vars=$(example_vars "$cc_root/$cc_folder/.env.example")
        cc_req=$(printf '%s\n' "$cc_vars" | awk '$1 == "R" { print $2 }' | sort -u | tr '\n' ' ')
        cc_opt=$(printf '%s\n' "$cc_vars" | awk '$1 == "O" { print $2 }' | sort -u | tr '\n' ' ')
    fi
    cc_nreq=$(printf '%s' "$cc_req" | wc -w | tr -d ' ')
    cc_nopt=$(printf '%s' "$cc_opt" | wc -w | tr -d ' ')

    for cc_ex in $(examples "$cc_folder"); do
        [ "$cc_ex" = ".env.example" ] && continue
        cc_file=${cc_ex%.example}
        if [ -e "$cc_cfg/$cc_file" ]; then
            cc_present=1
            cc_details="$cc_details  conf: $cc_file present
"
        else
            cc_details="$cc_details  conf: $cc_file absent
"
        fi
    done

    cc_problems=0
    if [ "$cc_has_env_example" = 1 ]; then
        if [ -f "$cc_cfg/.env" ]; then
            cc_present=1
            cc_envnote=".env present; $cc_nreq required, $cc_nopt optional"
            cc_out=$(evaluate "$cc_cfg/.env" "$cc_req" "$cc_opt" "$cc_sd")
            case "$cc_out" in
                *done) ;;
                *) cc_details="  error: .env did not source
$cc_details"; cc_problems=1 ;;
            esac
            cc_found=$(printf '%s\n' "$cc_out" | grep -v '^done$' || true)
            if [ -n "$cc_found" ]; then
                cc_lines=$(printf '%s\n' "$cc_found" | awk '
                    $1 == "missing"     { print "  missing: " $2 }
                    $1 == "placeholder" { print "  placeholder: " $2 }
                    $1 == "path"        { print "  path missing: " $2 }
                    $1 == "sdkey"       { print "  sd key unreadable: " $2 }
                    $1 == "sdskip"      { print "  note: sd not on PATH; " $2 " not checked" }')
                cc_details="$cc_lines
$cc_details"
                printf '%s\n' "$cc_found" | grep -qv '^sdskip ' && cc_problems=1
            fi
        elif [ "$cc_nreq" -gt 0 ]; then
            cc_envnote=".env absent; $cc_nreq required"
        else
            cc_envnote="no .env; every variable optional"
        fi
    else
        cc_envnote="no .env.example"
    fi

    if [ "$cc_problems" = 1 ]; then
        cc_state=broken
    elif [ "$cc_present" = 1 ]; then
        cc_state=ok
    elif [ "$cc_has_env_example" = 1 ] && [ "$cc_nreq" -eq 0 ]; then
        cc_state=optional
    else
        cc_state=unconfigured
    fi
    printf '%s: %s (%s)\n' "$cc_tool" "$cc_state" "$cc_envnote"
    printf '%s' "$cc_details"
}

# Resolve `check` arguments to folders; fail on an unknown name.
select_folders() {
    cc_all=$(tool_folders)
    [ $# -eq 0 ] && { printf '%s\n' "$cc_all"; return 0; }
    for cc_want in "$@"; do
        cc_hit=""
        for cc_folder in $cc_all; do
            if [ "$cc_want" = "$cc_folder" ] || [ "$cc_want" = "$(tool_name "$cc_folder")" ]; then
                cc_hit=$cc_folder
            fi
        done
        if [ -z "$cc_hit" ]; then
            echo "config-check: no tool named $cc_want has a committed *.example" >&2
            return 1
        fi
        printf '%s\n' "$cc_hit"
    done
}

# run_checks <quiet> <folder>...: sets cc_n_ok, cc_n_broken, cc_n_unconf,
# cc_n_opt and cc_broken_names.
run_checks() {
    cc_quiet=$1; shift
    cc_sd=$(command -v sd 2>/dev/null || true)
    cc_n_ok=0; cc_n_broken=0; cc_n_unconf=0; cc_n_opt=0; cc_broken_names=""
    for cc_f in "$@"; do
        cc_report=$(check_tool "$cc_f")
        cc_state=$(printf '%s\n' "$cc_report" | head -1 | sed 's/^[^:]*: \([a-z]*\) .*/\1/')
        [ "$cc_quiet" = 1 ] || printf '%s\n' "$cc_report"
        case "$cc_state" in
            ok) cc_n_ok=$((cc_n_ok + 1)) ;;
            broken) cc_n_broken=$((cc_n_broken + 1))
                    cc_broken_names="${cc_broken_names:+$cc_broken_names, }$(tool_name "$cc_f")" ;;
            optional) cc_n_opt=$((cc_n_opt + 1)) ;;
            *) cc_n_unconf=$((cc_n_unconf + 1)) ;;
        esac
    done
}

case "${1:-}" in
    check)
        shift
        cc_sel=$(select_folders "$@") || exit 1
        # shellcheck disable=SC2086
        run_checks 0 $cc_sel
        cc_total=$((cc_n_ok + cc_n_broken + cc_n_unconf + cc_n_opt))
        echo "config-check: $cc_total tool(s): $cc_n_ok ok, $cc_n_broken broken, $cc_n_unconf unconfigured, $cc_n_opt optional"
        [ "$cc_n_broken" -eq 0 ] || exit 1
        ;;
    status)
        # shellcheck disable=SC2086
        run_checks 1 $(tool_folders)
        cc_conf=$((cc_n_ok + cc_n_broken))
        if [ "$cc_n_broken" -gt 0 ]; then
            echo "local-config-check: $cc_n_broken of $cc_conf configured tool(s) broken: $cc_broken_names; run config-check.sh check"
            exit 1
        elif [ "$cc_conf" -eq 0 ]; then
            echo "local-config-check: no tool configured under $SYSTEM_TOOLS_CONFIG"
            exit 3
        fi
        echo "local-config-check: $cc_conf configured tool(s) healthy"
        ;;
    list)
        for cc_folder in $(tool_folders); do
            cc_tool=$(tool_name "$cc_folder")
            for cc_ex in $(examples "$cc_folder"); do
                printf '%s\t%s/%s -> %s/%s\n' "$cc_tool" "$cc_folder" "$cc_ex" \
                    "$(st_config_dir "$cc_tool")" "${cc_ex%.example}"
            done
        done
        ;;
    test)
        shift
        exec "${PYTHON:-python3}" -m unittest discover -s "$DIR/tests" -t "$DIR" "$@"
        ;;
    -h|--help|help)
        usage
        exit 0
        ;;
    *)
        usage >&2
        exit 1
        ;;
esac

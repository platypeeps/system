#!/bin/sh
# Trigger the creation of a work item's planning documents.
# Usage: sd-plan.sh item ID|nightly|status|test|help
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
# The runner executes this with a bare PATH, where `python3` is Xcode's 3.9 --
# older than the `datetime.UTC` that `sd_db` imports, so the first real
# end-to-end run died on an ImportError before reaching any of this script's
# own logic. Pin the same provisioned interpreter `local-sd-runner` uses
# (source:local-sd-runner/runner.sh::runtime_python), fall back to PATH for CI
# and for machines without the pack, and check the version rather than
# leaving the next caller to rediscover this as a traceback. An explicitly set
# PYTHON is honoured, but still checked: refusing in a sentence beats failing
# three imports deep.
# The floor is `requires-python` in local-sd-db/pyproject.toml, the same one
# `local-sd-db/sd-db.sh` checks, and not the one import that first forced a
# floor: accepting less runs a package its own metadata refuses (sd:1612).
# Resolved only by the verbs that run Python, so `help`, usage and an
# unconfigured `status` still answer on a machine whose only Python is 3.9.
# Both Homebrew prefixes are searched, as `local-cron-jobs` puts either on
# PATH and the runner's bare PATH carries neither.
python_is_new_enough() {
    command -v "$1" >/dev/null 2>&1 || return 1
    "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 13) else 1)' 2>/dev/null
}

resolve_python() {
    if [ -n "${PYTHON:-}" ]; then
        if ! command -v "$PYTHON" >/dev/null 2>&1; then
            echo "sd-plan: PYTHON=$PYTHON cannot be run; name an installed interpreter" >&2
            return 1
        fi
        python_is_new_enough "$PYTHON" || {
            echo "sd-plan: $PYTHON is too old; sd_db needs Python 3.13 or newer" >&2
            return 1
        }
        return 0
    fi
    for candidate in \
        "${SD_PLAN_PYTHON:-}" \
        "${SD_PACK_ROOT:-$HOME/repos/platypeeps/sd-ai-command-pack}/.venv/bin/python" \
        /opt/homebrew/bin/python3 \
        /usr/local/bin/python3 \
        python3
    do
        [ -n "$candidate" ] || continue
        if python_is_new_enough "$candidate"; then
            PYTHON="$candidate"
            return 0
        fi
    done
    echo "sd-plan: no Python 3.13 or newer found; sd_db's pyproject.toml requires it" >&2
    return 1
}

# The runner is what actually executes a planning run: this folder enqueues,
# the runner clones, bounds and logs. Overridable so a test can answer for it
# without a service installed.
RUNNER="${SD_PLAN_RUNNER:-$DIR/../local-sd-runner/runner.sh}"

# Which machine this is, and so which participation list applies. The recorded
# profile beats guessing for the reason local-repo-sync documents at length:
# the directory heuristic calls any machine without a work checkout root
# "personal", which on a terra machine resolves someone else's fleet. No
# recorded profile is no list at all, not `personal`'s: participation is an
# opt-in, and a guess would opt a machine in to another's (sd:1189).
PROFILE_FILE="${MACHINE_SETUP_STATE:-$HOME/.config/machine-setup}/profile"
PROFILE=""
if [ -n "${SD_PLAN_PROFILE:-}" ]; then
    PROFILE="$SD_PLAN_PROFILE"
elif [ -r "$PROFILE_FILE" ] && [ -f "$PROFILE_FILE" ]; then
    PROFILE="$(cat "$PROFILE_FILE")"
fi
# The list is private config: `<config>/sd-plan/repos.<profile>.conf`, where
# <config> is $SYSTEM_TOOLS_CONFIG (default ~/.config/system).
. "$DIR/../lib/config.sh"
CONF_DIR="$(st_config_dir sd-plan)"
CONF=""
[ -z "$PROFILE" ] || CONF="$CONF_DIR/repos.$PROFILE.conf"

# Why the configuration cannot be read, or nothing. Something at a path that
# is not a readable file is a broken machine, not an unconfigured one.
config_problem() {
    if [ -z "$PROFILE" ] && [ -e "$PROFILE_FILE" ]; then
        echo "$PROFILE_FILE exists and cannot be read as the machine's profile"
    elif [ -n "$CONF" ] && [ -e "$CONF" ] && { [ ! -f "$CONF" ] || [ ! -r "$CONF" ]; }; then
        echo "$(basename "$CONF") exists and cannot be read as a participation list"
    fi
}

usage() {
    cat >&2 <<'USAGE'
sd-plan.sh — planning documents get written for work that is only a row.

Usage: sd-plan.sh <command>

  item ID     Write the planning documents for one row, standing in its
              checkout: `docs/work/<date>-<slug>/{prd,design,implement}.md`
              on a branch, committed, registered as the row that owns the
              folder's status, and pushed. Refuses a row belonging to another
              checkout (R10-D6) and a run that left any of the three
              documents unwritten. A folder that is committed and has its row
              is reported and left alone, or, when its planning commit
              alone failed to push, pushed; one without its row is registered,
              committed and pushed, with no second planning run. `--dry-run`
              says what it would do.
  nightly     Select one row per participating repository and enqueue each
              through the runner, the same queue the dashboard button uses.
              Gives each selected row a branch first, which the enqueue
              requires. Exits 0 when nothing participates, when no candidate
              is found, and when each row tried was queued or refused on its
              own -- an empty or absent participation list is the normal
              state, not a fault, and it is read before the runner is probed
              so that an unconfigured machine never fails on a runner it was
              never going to use.
              Exits non-zero when it could not ask (no runner, no database,
              no readable palette) or when a standing refusal skipped a
              repository, which is the one signal a skipped repository gets:
              cron-jobs pages on the exit and the dashboard's Jobs area shows
              it. `--dry-run` selects and reports, and enqueues nothing.
  status      Whether this machine is set up to plan anything, as an exit
              code: 0 configured and the runner is dispatching, 3 nothing to
              check (no profile or no participation list on this machine), 1
              configured and actually broken, or a list that cannot be read. local-health-check reads these.
  test        Run this folder's tests.
  help        This text.

Participation is a list, and an absent list means nothing participates. The
file is `<config>/sd-plan/repos.<profile>.conf` (<config> is
$SYSTEM_TOOLS_CONFIG, default ~/.config/system), one repository path per
line, `#` comments and blank lines ignored. Copy
`repos.personal.conf.example` there to start one. Opt a
repository in deliberately, because what it opts in to is an agent writing
branches there unattended.

`item` carries an optional judgment: the run writes prd.md first, asks
local-jev whether this item needs a design pass and an implement pass, and
writes only the passes it says are needed. It runs unless `JEV_SD_PLAN` is set
to 0, off, false, no or disabled -- unset means on -- and nothing depends on it: a switched-off
stage, a `jev off` or unkeyed machine, and a failing call all write all three
documents, as this tool always has. The prd's text is what leaves the machine,
so set `JEV_SD_PLAN=0` when planning something confidential. README.md says
exactly what is sent.

The nightly job that calls `nightly` is local-cron-jobs' sd-plan-nightly.
USAGE
}

# Every participating repository path, comments and blanks removed, each
# once, in file order. Reading it is how `status` tells "not configured" from "broken".
participants() {
    [ -n "$CONF" ] && [ -f "$CONF" ] && [ -r "$CONF" ] || return 0
    sed -e 's/#.*//' -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' "$CONF" \
        | awk 'NF && !seen[$0]++'
}

# Whether the runner would actually pick up what we enqueued. A queue nobody
# drains is the failure this exists to name: `nightly` would exit 0 having
# done nothing, every night, and say so nowhere.
#
# It stays a boolean. A stopped runner and a refusing one read the same here
# on purpose: the one consumer of this exit is `cron-jobs.sh`, which notifies
# on every non-zero code, and `reporting.py` marks every non-zero code for
# attention. A third code would document a distinction nothing downstream
# honours, so the night would still page and the page would still say
# `FAILED`. Splitting them is a change to those two consumers first.
#
# The runner's own exit is kept in RUNNER_RC beside the boolean, so `status`
# can name why. Its 3 ("agent not loaded") is sd-runner's nothing-to-check,
# not this tool's: launchd not holding the agent is also what a provisioned
# agent that was booted out or never came back looks like, and a machine with
# a participation list needs one. So it stays 1 here, and says so (sd:1387).
runner_dispatching() {
    RUNNER_RC=1
    [ -x "$RUNNER" ] || return 1
    RUNNER_RC=0
    out="$("$RUNNER" status 2>/dev/null)" || { RUNNER_RC=$?; return 1; }
    printf '%s' "$out" | "$PYTHON" -c '
import json, sys
try:
    state = json.load(sys.stdin)
except ValueError:
    sys.exit(1)
storage = state.get("storage") or {}
sys.exit(0 if state.get("healthy") and storage.get("dispatch_allowed") else 1)
' || return 1
}

case "${1:-}" in
    item)
        shift
        resolve_python || exit 1
        # `sd_db` is the one PYTHON has installed, and the folder's row is
        # made by the pack's `sd work register` under the same interpreter.
        exec "$PYTHON" "$DIR/sd_plan.py" item "$@"
        ;;
    nightly)
        shift
        # The two questions `status` already answers are answered here too,
        # by the same two helpers: parsing the conf or probing the runner a
        # second time in Python is how the two answers drift apart. The
        # participation list goes down the pipe; a runner that is not
        # dispatching stops the night before anything is queued.
        #
        # The list is read first, and that order is the whole of sd:1202. A
        # machine with no participation list enqueues nothing whatever the
        # runner is doing, so probing it first can only turn the quietest
        # night of the year into `FAILED rc=1` -- which is what 2026-09-20
        # was, on a machine whose `repos.personal.conf` has never existed.
        # Ask the runner only where the answer can change what happens.
        problem="$(config_problem)"
        if [ -n "$problem" ]; then
            echo "sd-plan: $problem; nothing enqueued" >&2
            exit 1
        fi
        resolve_python || exit 1
        list="$(participants)"
        if [ -n "$list" ] && ! runner_dispatching; then
            echo "sd-plan: the runner is not dispatching; nothing enqueued" >&2
            exit 1
        fi
        # Which `sd_db` imports is `sd-db.sh library`'s answer, the choice its
        # own database verbs make (sd:1765). The primary checkout can sit a
        # migration behind the live database, and putting it on PYTHONPATH
        # unasked refused the night while PYTHON held a copy that could open
        # it (sd:1812). `installed` runs `-I`, as sd-db.sh does, so nothing on
        # the path can stand in for the copy it probed. An answer it cannot
        # give, such as a PYTHON older than sd_db's floor, keeps the checkout.
        library="$(PYTHON="$PYTHON" "$DIR/../local-sd-db/sd-db.sh" library)" || library=checkout
        if [ "$library" = installed ]; then
            printf '%s\n' "$list" | "$PYTHON" -I "$DIR/sd_plan.py" nightly "$@"
        else
            printf '%s\n' "$list" | PYTHONPATH="$DIR/../local-sd-db${PYTHONPATH:+:$PYTHONPATH}" \
                "$PYTHON" "$DIR/sd_plan.py" nightly "$@"
        fi
        ;;
    status)
        problem="$(config_problem)"
        if [ -n "$problem" ]; then
            echo "local-sd-plan: FAIL — $problem"
            exit 1
        fi
        if [ -z "$PROFILE" ]; then
            echo "local-sd-plan: SKIP — no machine-setup profile at $PROFILE_FILE, so no participation list applies"
            exit 3
        fi
        count="$(participants | wc -l | tr -d ' ')"
        if [ ! -e "$CONF" ]; then
            echo "local-sd-plan: SKIP — no $(basename "$CONF") on this machine (copy local-sd-plan/repos.personal.conf.example to $CONF to opt repositories in)"
            exit 3
        fi
        if [ "$count" -eq 0 ]; then
            echo "local-sd-plan: SKIP — $(basename "$CONF") names no repository"
            exit 3
        fi
        if ! resolve_python; then
            echo "local-sd-plan: FAIL — $count repository(ies) participate and no Python 3.13 or newer was found"
            exit 1
        fi
        if ! runner_dispatching; then
            # Configured and broken, which is the one case worth a nightly
            # finding: the list says to plan and nothing would run it.
            if [ "$RUNNER_RC" -eq 3 ]; then
                # The sweep quotes line 1, so the cause goes there. `nightly`
                # refuses to enqueue in this state too; 3 here would silence
                # the check while the night still fails.
                echo "local-sd-plan: FAIL — $count repository(ies) participate and the runner's agent is not loaded"
                echo "  load it (local-sd-runner), or empty $(basename "$CONF") if this machine should not plan"
                exit 1
            fi
            echo "local-sd-plan: FAIL — $count repository(ies) participate and the runner is not dispatching"
            echo "  the runner is what executes a planning run; check: $RUNNER status"
            exit 1
        fi
        echo "local-sd-plan: $count repository(ies) participate, runner dispatching"
        exit 0
        ;;
    test)
        shift
        resolve_python || exit 1
        # The suite seeds a database from this repository's `sd_db`, as
        # `PYTHONPATH="$DIR:$DIR/../local-sd-db` in `local-sd-runner/runner.sh`
        # does. The `item` runs it drives get no such path: they see what
        # PYTHON installed, as a real run does.
        # Their registrations go through a pack, which CI names; a bare local
        # run finds the usual checkout, as `local-sd-db/sd-db.sh` does. Never
        # over an explicit SD_ACCEPTANCE_PACK.
        pack="$HOME/repos/platypeeps/sd-ai-command-pack"
        if [ -z "${SD_ACCEPTANCE_PACK:-}" ] && [ -f "$pack/bin/sd" ]; then
            SD_ACCEPTANCE_PACK="$pack"; export SD_ACCEPTANCE_PACK
        fi
        # The nightlies it drives seed from this checkout, so they import it
        # too, whatever copy PYTHON has installed.
        SD_DB_LIBRARY=checkout; export SD_DB_LIBRARY
        PYTHONPATH="$DIR:$DIR/../local-sd-db${PYTHONPATH:+:$PYTHONPATH}" \
            exec "$PYTHON" -m unittest discover -s "$DIR/tests" -t "$DIR" "$@"
        ;;
    -h|--help|help)
        usage
        exit 0
        ;;
    *)
        usage
        exit 1
        ;;
esac

#!/bin/sh
# Ask Jev one typed question from the shell, and print the answer.
# Usage: jev.sh ask|noul|choice|score|status|record|label|test
#
# Exit codes: 0 answered, 3 not configured on this machine, 1 a real error.
# local-health-check reads these codes.
set -e

# `local-bin-links` links this onto PATH as `jev`, so $0 is then the symlink in
# ~/bin/common and ../lib beside the real script is not beside $0.
# Walk $0 to where the file actually lives before resolving anything.
SELF="$0"
while [ -L "$SELF" ]; do
  target=$(readlink "$SELF")
  case "$target" in
    /*) SELF="$target" ;;
    *)  SELF="$(dirname "$SELF")/$target" ;;
  esac
done
DIR="$(cd "$(dirname "$SELF")" && pwd)"
. "$DIR/../lib/config.sh"

# <config>/jev/.env provides defaults only — values already in the environment win, so
# `JEV_MODEL=jev-1.13.0 jev noul ...` pins a model for one call as expected.
# The key normally comes from ~/.config/shell/env.sh and not from .env at all.
# The .env lives outside the checkout, in <config>/jev/ (<config> is
# $SYSTEM_TOOLS_CONFIG, default ~/.config/system).
ENV_TYPESAFE_API_KEY="${TYPESAFE_API_KEY:-}"
ENV_JEV_URL="${JEV_URL:-}"
ENV_JEV_MODEL="${JEV_MODEL:-}"
ENV_JEV_TIMEOUT="${JEV_TIMEOUT:-}"
ENV_JEV_RETRIES="${JEV_RETRIES:-}"
ENV_JEV_ENABLED="${JEV_ENABLED:-}"
ENV_JEV_FLAG_FILE="${JEV_FLAG_FILE:-}"
st_source_env jev
[ -n "$ENV_TYPESAFE_API_KEY" ] && TYPESAFE_API_KEY="$ENV_TYPESAFE_API_KEY"
[ -n "$ENV_JEV_URL" ]     && JEV_URL="$ENV_JEV_URL"
[ -n "$ENV_JEV_MODEL" ]   && JEV_MODEL="$ENV_JEV_MODEL"
[ -n "$ENV_JEV_TIMEOUT" ] && JEV_TIMEOUT="$ENV_JEV_TIMEOUT"
[ -n "$ENV_JEV_RETRIES" ] && JEV_RETRIES="$ENV_JEV_RETRIES"
[ -n "$ENV_JEV_ENABLED" ] && JEV_ENABLED="$ENV_JEV_ENABLED"
[ -n "$ENV_JEV_FLAG_FILE" ] && JEV_FLAG_FILE="$ENV_JEV_FLAG_FILE"
for var in TYPESAFE_API_KEY JEV_URL JEV_MODEL JEV_TIMEOUT JEV_RETRIES \
           JEV_ENABLED JEV_FLAG_FILE; do
  eval "value=\${$var:-}"
  [ -n "$value" ] && export "$var"
done

# The meter writes through `sd_db`, and a bare python3 answers but records
# nothing (sd:2087). Pick the interpreter the way sd-db.sh does: PYTHON, then
# SD_DB_PYTHON, then the command pack's venv, which is where the pack installs
# `sd_db`, then python3.
if [ -z "${PYTHON:-}" ]; then
  for candidate in "${SD_DB_PYTHON:-}" \
                   "$HOME/repos/platypeeps/sd-ai-command-pack/.venv/bin/python"; do
    if [ -n "$candidate" ] && [ -x "$candidate" ]; then
      PYTHON="$candidate"
      break
    fi
  done
fi

case "$1" in
  test)
    shift
    exec "${PYTHON:-python3}" -m unittest discover -s "$DIR/tests" -t "$DIR" "$@"
    ;;
  ask|noul|choice|score|status|enabled|on|off|record)
    exec "${PYTHON:-python3}" "$DIR/jev.py" "$@"
    ;;
  label)
    # Asks no model: it reads the ledger through sd-db.sh, the pull request
    # through gh and the default branch through git, and lives beside jev.py
    # rather than in it so jev.py stays standalone.
    shift
    exec "${PYTHON:-python3}" "$DIR/jev_label.py" "$@"
    ;;
  -h|--help|help)
    cat <<'HELPEOF'
usage: jev.sh ask|noul|choice|score|status|enabled|on|off|record|label|test

Jev is TypeSafe's System One model. It answers a narrow question about some
state with a type and a probability, in about the time a shell pipeline takes
to run. It does not write prose and does not explain itself. Code decides what
to do with the number.

  noul  INSTRUCTIONS      probability that the condition holds, 0 to 1
                          --gate P   print yes/no against P, for `if` in sh
  choice INSTRUCTIONS     pick one of a named set; prints the chosen name
                          --criteria name[=description],...  or @file.json
                          --unsure-below P  print `unsure` under that confidence
  score INSTRUCTIONS      position on ordered levels; prints the number
                          --levels "low,middle,high"  or @file.json
  ask                     several questions in one request, which is how they
                          run in parallel and stay cheap
                          --questions FILE|-   JSON object, the API's own shape
  status                  switch, key, endpoint and a one-question probe
                          exit 0 ok, 3 off or unkeyed here, 1 up and broken
                          local-health-check reads these codes
  enabled [STAGE]         can Jev answer on this machine? exit 0 yes, 3 no.
                          Costs nothing and calls nothing. --why says which.
                          STAGE names a caller's own variable, e.g.
                          JEV_ADVERSARIAL_GATE: exit 3 if that variable switched
                          the stage off. Unset means on, so one call answers
                          both halves and the vocabulary lives in one place.
  on | off                set the switch, fleet-wide for this machine
  record                  write one event for a decision this tool did not
                          make: what your own mechanism answered, how long it
                          took, and why Jev was not used. Sends nothing, needs
                          no key, prints nothing, always exits 0.
  label sd-review         write whether each recorded sd-review tier was
                          right, from what happened after the merge: a fix
                          to its files within 14 days means one tier too
                          shallow. Asks no model; reads sd-db.sh, gh and
                          git. Writes nothing without --apply.
  test                    run the unittest suite in tests/

Jev is experimental, so nothing may depend on it. Every caller keeps its old
path and uses one of two shapes:

  if jev enabled; then verdict=$(jev noul ... ); else verdict=$(old_way); fi

  verdict=$(jev noul 'Is this a real defect?' --state f --gate 0.8 --fallback yes)

`--fallback` prints what you gave it and exits 0 when Jev is switched off,
unkeyed, or failing, and says why on stderr. It is never silent: a lane that
quietly stops running is the bug this shape exists to avoid.

Every call is measured, and the measurement can never cost you anything: it is
written after your answer is printed, into `sd.db` when there is one, and a
machine with no database records nothing and behaves exactly as before. Name
yourself with --caller and --stage (or JEV_CALLER and JEV_STAGE) so the row
can be found again; JEV_METER=0 switches recording off.

Your own mechanism is the control arm and is measured too. `jev enabled STAGE
--record` writes the decline when the answer is no, and `jev record` writes
what your path then did. `--shadow ANSWER` asks anyway, records both answers
and prints yours, so a stage produces a paired sample without changing what it
does. It costs a real call, so it is off unless you pass it.

Each caller also has a variable of its own -- JEV_HEALTH_CHECK,
JEV_ADVERSARIAL_GATE and the like -- and it only switches that one stage off:
`enabled STAGE` reads it, and unset means on. They were opt-ins at first, and
a per-caller switch that defaults to off makes every integration added after
it silently never run. The words it accepts are the switch file's own: 0, off,
false, no, disabled. Anything else, including a word this does not know,
leaves the stage on.

The switch lives in a file, not only in a variable, because cron and launchd
read no shell profile. Absent means enabled; `jev off` writes it. A machine
with no key behaves exactly like a machine with the switch off -- and so does
one whose key is still the `.env.example` placeholder, and one whose
JEV_TIMEOUT or JEV_RETRIES does not parse. Unconfigured is not broken: all of
them exit 3, send nothing, and honour a fallback. `jev on` and `jev off`
consult none of it, so the remedy stays reachable on a machine that is
misconfigured.

Only one option may read stdin per call. `--state` defaults to `-`, so
`ask --questions -` needs a file for one of the two.

shared options:
  --state FILE|-          what the question is about; stdin by default
  --state-format text|json    json parses the file; text sends it as a string
  --id NAME               question id, which code sees and the model does not
  --subject NAME          what was judged, for the ledger only; never sent.
                          Recorded as the row's question id in place of --id
  --json                  print the whole answer, distribution included
  --model NAME            default jev-latest

examples:
  git log -1 --format=%s | jev choice 'Which change type is this?' \
      --criteria 'feat=new capability,fix=corrects behaviour,chore=housekeeping'
  jev noul 'Does this need a human tonight?' --state finding.txt --gate 0.8

Why a command and not an MCP server: a command costs a session nothing, and
cron and git hooks can call it too. Claude Code and Codex both reach it
through Bash. Claude Code's own skill for authoring the questions is
`typesafe-ai`; this is the thing the questions run on.

Why a command and not a `claude -p` prompt job: a judgment made this way does
not depend on an agent session. Thirteen agent-driven cron jobs died on
2026-09-19 when that session expired, and a `JOB_COMMAND` calling this one
would have survived it.

environment:
  TYPESAFE_API_KEY   required; normally exported from ~/.config/shell/env.sh
  JEV_URL            endpoint (default https://api.typesafe.ai/v1/systemone)
  JEV_MODEL          model (default jev-latest)
  JEV_TIMEOUT        seconds per attempt (default 30)
  JEV_RETRIES        retries for 429/529/5xx, doubling backoff (default 3)
  JEV_ENABLED        1/0 for one call or one session; beats the switch file
  JEV_FLAG_FILE      switch file (default ~/.config/jev/enabled)
Defaults for these may also sit in <config>/jev/.env (<config> is
$SYSTEM_TOOLS_CONFIG, default ~/.config/system; copy local-jev/.env.example);
an exported value wins over the file.
HELPEOF
    exit 0
    ;;
  *)
    echo "usage: $(basename "$0") ask|noul|choice|score|status|enabled|on|off|record|label|test" >&2
    exit 1
    ;;
esac

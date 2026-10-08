#!/bin/sh
# Ask Jev one typed question from the shell, and print the answer.
# Usage: jev.sh ask|noul|choice|score|status|record|test
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
ENV_JEV_SHADOW="${JEV_SHADOW:-}"
ENV_JEV_FLAG_FILE="${JEV_FLAG_FILE:-}"
ENV_JEV_TRACES_URL="${JEV_TRACES_URL:-}"
ENV_JEV_TRACES_TIMEOUT="${JEV_TRACES_TIMEOUT:-}"
# The comparison arms' settings (jev_compare.py), the corpus's
# (jev_corpus.py) and the meter's (jev_meter.py) follow the same rule: a
# suite's JEV_METER=0 must beat a JEV_METER=1 in the .env (sd:2799).
COMPARE_VARS="JEV_METER JEV_METER_DB JEV_CORPUS JEV_CORPUS_DIR JEV_CORPUS_DAYS JEV_COMPARE_STAGES JEV_COMPARE_KEV_URL JEV_COMPARE_KEV_MODEL KEV_API_KEY KEV_MODEL
  JEV_COMPARE_HAIKU_VIA JEV_COMPARE_HAIKU_MODEL JEV_COMPARE_HAIKU_USD_IN JEV_COMPARE_HAIKU_USD_OUT
  JEV_COMPARE_ANTHROPIC_KEY JEV_COMPARE_ANTHROPIC_URL JEV_COMPARE_OPENROUTER_KEY
  JEV_COMPARE_OPENROUTER_URL OPENROUTER_API_KEY JEV_COMPARE_BASETEN_KEY JEV_COMPARE_BASETEN_URL
  JEV_COMPARE_BASETEN_MODEL BASETEN_API_KEY JEV_COMPARE_CLAUDE JEV_COMPARE_TIMEOUT JEV_COMPARE_LOG
  JEV_BUDGET_DIR"
# A stage's budget variables (`<STAGE>_MAX_CALLS`, `<STAGE>_MAX_TOKENS`) have
# no fixed names, so the exported ones are read off the environment (sd:1239).
COMPARE_VARS="$COMPARE_VARS $(env | sed -n 's/^\([A-Za-z_][A-Za-z0-9_]*_MAX_CALLS\)=.*/\1/p; s/^\([A-Za-z_][A-Za-z0-9_]*_MAX_TOKENS\)=.*/\1/p')"
# Set-ness is kept apart from the value: an exported empty switch, such as
# `JEV_COMPARE_HAIKU_VIA= jev ...`, is an off arm and must beat an on-value
# in the .env, so it is restored even when empty.
for var in $COMPARE_VARS; do
  eval "SET_$var=\${$var+set}; ENV_$var=\"\${$var:-}\""
done
st_source_env jev
for var in $COMPARE_VARS; do
  eval "was=\"\${SET_$var:-}\"; value=\"\${ENV_$var:-}\""
  [ "$was" != set ] || eval "$var=\"\$value\"; export $var"
done

# The Kev arm asks the server local-kev runs, so it also reads that service's
# <config>/kev/.env: a checkpoint, port or key changed for `kev.sh serve` is
# the one the arm asks and records. Those values are defaults below everything
# above -- an exported value or <config>/jev/.env wins, and JEV_COMPARE_KEV_URL
# or JEV_COMPARE_KEV_MODEL beats them both. KEV_PORT only builds the URL.
KEV_ENV_FILE="$(st_config_dir kev)/.env"
kev_value() {
  [ -f "$KEV_ENV_FILE" ] || return 0
  ( unset "$1"; . "$KEV_ENV_FILE" >/dev/null 2>&1; eval "printf '%s' \"\${$1:-}\"" ) || true
}
# Set-ness decides the fallback, not the value: an exported empty KEV_API_KEY
# switches auth off, and the service key must not reach an overridden URL.
[ -n "${KEV_MODEL+set}" ] || KEV_MODEL="$(kev_value KEV_MODEL)"
[ -n "${KEV_API_KEY+set}" ] || KEV_API_KEY="$(kev_value KEV_API_KEY)"
case "${KEV_API_KEY:-}" in change-me|changeme) KEV_API_KEY="" ;; esac
if [ -z "${JEV_COMPARE_KEV_URL:-}" ]; then
  kev_port="${KEV_PORT:-$(kev_value KEV_PORT)}"
  [ -z "$kev_port" ] || JEV_COMPARE_KEV_URL="http://127.0.0.1:$kev_port/v1/systemone"
fi
for var in KEV_MODEL KEV_API_KEY JEV_COMPARE_KEV_URL; do
  eval "value=\${$var:-}"
  [ -z "$value" ] || export "$var"
done
[ -n "$ENV_TYPESAFE_API_KEY" ] && TYPESAFE_API_KEY="$ENV_TYPESAFE_API_KEY"
[ -n "$ENV_JEV_URL" ]     && JEV_URL="$ENV_JEV_URL"
[ -n "$ENV_JEV_MODEL" ]   && JEV_MODEL="$ENV_JEV_MODEL"
[ -n "$ENV_JEV_TIMEOUT" ] && JEV_TIMEOUT="$ENV_JEV_TIMEOUT"
[ -n "$ENV_JEV_RETRIES" ] && JEV_RETRIES="$ENV_JEV_RETRIES"
[ -n "$ENV_JEV_ENABLED" ] && JEV_ENABLED="$ENV_JEV_ENABLED"
[ -n "$ENV_JEV_SHADOW" ] && JEV_SHADOW="$ENV_JEV_SHADOW"
[ -n "$ENV_JEV_FLAG_FILE" ] && JEV_FLAG_FILE="$ENV_JEV_FLAG_FILE"
[ -n "$ENV_JEV_TRACES_URL" ] && JEV_TRACES_URL="$ENV_JEV_TRACES_URL"
[ -n "$ENV_JEV_TRACES_TIMEOUT" ] && JEV_TRACES_TIMEOUT="$ENV_JEV_TRACES_TIMEOUT"
for var in TYPESAFE_API_KEY JEV_URL JEV_MODEL JEV_TIMEOUT JEV_RETRIES \
           JEV_ENABLED JEV_SHADOW JEV_FLAG_FILE JEV_TRACES_URL JEV_TRACES_TIMEOUT; do
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
  ask|noul|choice|score|status|enabled|on|off|shadow|record)
    # A caller that names itself nowhere is named after the program that ran
    # this one (`jev.py` `parent_args`). Looked up only then: one `ps` per call.
    # Each argument on its own: a question's text may mention `--caller`.
    named=
    for arg in "$@"; do
      case "$arg" in --caller|--caller=*) named=1; break ;; esac
    done
    if [ -z "${JEV_CALLER:-}" ] && [ -z "$named" ]; then
      JEV_PARENT_ARGS="$(ps -o args= -p "$PPID" 2>/dev/null)"; export JEV_PARENT_ARGS
    fi
    exec "${PYTHON:-python3}" "$DIR/jev.py" "$@"
    ;;
  -h|--help|help)
    cat <<'HELPEOF'
usage: jev.sh ask|noul|choice|score|status|enabled|on|off|shadow|record|test

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
  shadow on|off           every call is still made and recorded, and the
                          caller gets its --fallback, or exit 3 without one,
                          so its old mechanism runs. Off unless switched on
  record                  write one event for a decision this tool did not
                          make: what your own mechanism answered, how long it
                          took, and why Jev was not used. Sends nothing, needs
                          no key, prints nothing, always exits 0.
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
does. It costs a real call, so it is off unless you pass it. `--baseline
ANSWER` is the live form: the judgment is printed and used as before, your
answer is recorded beside it as one pair, and `changed` compares the two.
--baseline-ms N times your own path. Not with --shadow.

--local-only sends the call to the local Kev alone (JEV_COMPARE_KEV_URL,
default http://127.0.0.1:8009/v1/systemone), never to Jev or a comparison arm.
A Kev URL that is not a loopback address is refused before anything is sent;
Kev down is a decline, like Jev down. `enabled STAGE --local-only` answers for
that path. The stages in LOCAL_ONLY_STAGES (jev.py) are local-only always.

Each caller also has a variable of its own -- JEV_HEALTH_CHECK,
JEV_ADVERSARIAL_GATE and the like -- and it only switches that one stage off:
`enabled STAGE` reads it, and unset means on. They were opt-ins at first, and
a per-caller switch that defaults to off makes every integration added after
it silently never run. The words it accepts are the switch file's own: 0, off,
false, no, disabled. Anything else, including a word this does not know,
leaves the stage on.

A stage may carry a ceiling per UTC day: <STAGE>_MAX_CALLS requests and
<STAGE>_MAX_TOKENS tokens, e.g. JEV_HEALTH_CHECK_MAX_CALLS=10. Spent, the
stage declines with cause `budget`, exactly as when Jev is off: `enabled
STAGE` exits 3 and --fallback is printed. Unset is no ceiling. The counter
is ~/.local/state/jev/budget.json, or under JEV_BUDGET_DIR.

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
  JEV_SHADOW         1/0 for one call or one session; beats the shadow file,
                     which sits beside the switch file
  JEV_TRACES_URL     OTLP/HTTP traces endpoint; one metadata-only span per call
                     (off when unset; local-genai-traces takes
                     http://127.0.0.1:4338/v1/traces)
  JEV_TRACES_TIMEOUT seconds for that post (default 0.5)
  JEV_CORPUS         0/off: store no trace corpus (unset means on)
  JEV_CORPUS_DIR     where it goes (default ~/.local/share/sd/jev-corpus);
                     one JSON line per call per arm, with the request and
                     the response, so a run can be redone from stored data
  JEV_CORPUS_DAYS    UTC days of corpus files kept before today's (default 30)
  JEV_COMPARE_STAGES stages that run the arms, separated by commas: the Kev
                     arm, and the Haiku arm when a transport is named
                     (unset means none)
  JEV_COMPARE_HAIKU_VIA  anthropic, openrouter, claude-cli or baseten: run
                     the second comparison arm (unset means off)
  JEV_COMPARE_*      keys, endpoints, prices and timeout of the arms;
                     see .env.example and the README
Defaults for these may also sit in <config>/jev/.env (<config> is
$SYSTEM_TOOLS_CONFIG, default ~/.config/system; copy local-jev/.env.example);
an exported value wins over the file.
HELPEOF
    exit 0
    ;;
  *)
    echo "usage: $(basename "$0") ask|noul|choice|score|status|enabled|on|off|shadow|record|test" >&2
    exit 1
    ;;
esac

#!/bin/sh
# Run the macOS-only suites that Linux cannot: every line of
# tests/macos-only-suites.txt, or the ones named on the command line.
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$DIR/.." && pwd)"
LIST="$DIR/macos-only-suites.txt"

usage() {
  cat <<'USAGE'
Usage: run-macos-only.sh all|<suite>...
       run-macos-only.sh list

  all      run every suite in tests/macos-only-suites.txt
  <suite>  run the named suites only (names are the first word of each line)
  list     print the suite names and commands

Linux cannot run these suites, so the legs of tests/ci-native.sh leave them
out; `make check` runs them on a Mac. Run this on a Mac before pushing a change
to a folder the list names. As in the legs, a suite fails when it reports a
skipped test or prints no unittest summary. Exits 1 when any suite fails.
USAGE
}

case "${1:-}" in
  -h|--help|help) usage; exit 0 ;;
  '') usage >&2; exit 1 ;;
  list) grep -Ev '^[[:space:]]*(#|$)' "$LIST"; exit 0 ;;
  all) shift; [ "$#" -eq 0 ] || { echo "run-macos-only.sh: 'all' takes no suite names" >&2; exit 1; } ;;
esac

if [ "$(uname -s)" != Darwin ]; then
  echo "run-macos-only.sh: these suites need macOS; this is $(uname -s)" >&2
  exit 1
fi

# As local-sd-runner/runner.sh does: the ship lifecycle needs the pack.
pack="$HOME/repos/platypeeps/sd-ai-command-pack"
if [ -z "${SD_ACCEPTANCE_PACK:-}" ] && [ -f "$pack/bin/sd-ship" ]; then
  SD_ACCEPTANCE_PACK="$pack"; export SD_ACCEPTANCE_PACK
fi

logs="$(mktemp -d "${TMPDIR:-/tmp}/run-macos-only.XXXXXX")"
# As the preflight does: the ship lifecycle runs the pack's sd-ship, which imports an
# installed sd_db, not source on PYTHONPATH. Suites run under this venv.
echo "== installing local-sd-db into a throwaway venv"
python3 -m venv "$logs/venv"  # no --copies: see tests/ci-native.sh
"$logs/venv/bin/python" -m pip install -q --no-index "$ROOT/local-sd-db" > "$logs/pip.log" 2>&1 || {
  cat "$logs/pip.log" >&2
  echo "run-macos-only.sh: could not install local-sd-db" >&2
  exit 1
}
PYTHON="$logs/venv/bin/python"; export PYTHON
PATH="$logs/venv/bin:$PATH"; export PATH
wanted=" $* "
failed=""
ran=0
set -f
cd "$ROOT"
while IFS= read -r line || [ -n "$line" ]; do
  case "$line" in ''|'#'*) continue ;; esac
  # shellcheck disable=SC2086 # the list holds literal words, split on purpose
  set -- $line
  suite="$1"
  shift
  if [ "$wanted" != "  " ]; then
    case "$wanted" in *" $suite "*) ;; *) continue ;; esac
  fi
  ran=$((ran + 1))
  echo "== $suite: $*"
  log="$logs/$suite.log"
  status=0
  # stdin is the list itself; a suite must not read the lines after it.
  "$@" < /dev/null > "$log" 2>&1 || status=$?
  cat "$log"
  if [ "$status" -ne 0 ]; then
    echo "$suite: exited $status" >&2
    failed="$failed $suite"
  elif grep -Eq 'skipped=[1-9][0-9]*' "$log"; then
    echo "$suite: skipped tests are not permitted" >&2
    failed="$failed $suite"
  elif ! grep -Eq '^Ran [1-9][0-9]* tests? in ' "$log"; then
    echo "$suite: no unittest execution summary" >&2
    failed="$failed $suite"
  fi
done < "$LIST"
set +f

if [ "$ran" -eq 0 ]; then
  echo "run-macos-only.sh: no suite matched:$wanted" >&2
  exit 1
fi
rm -rf "$logs"
if [ -n "$failed" ]; then
  echo "run-macos-only.sh: failed:$failed (logs in $logs)" >&2
  exit 1
fi
echo "run-macos-only.sh: $ran suite(s) passed"

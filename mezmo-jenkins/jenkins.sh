#!/bin/sh
# Read a pull request's Jenkins build: its console, or wait for its result.
# Usage: jenkins.sh log <owner/repo#N> [build] | wait <owner/repo#N> [--timeout MIN] | test
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
. "$DIR/../lib/config.sh"

usage() {
  cat <<'EOF'
usage: jenkins.sh log <owner/repo#N> [build]
       jenkins.sh wait <owner/repo#N> [--timeout MIN]
       jenkins.sh test [unittest args]

  log   print the console text of the PR's newest build, or of [build].
  wait  wait for the PR's newest build to finish (default 60 minutes),
        then print its result and its failing lines (FAIL, error).
        Exit 0 on SUCCESS, 1 on any other result or a timeout.
  test  run the unit tests.

Config: <config>/mezmo-jenkins/.env (copy mezmo-jenkins/.env.example):
  JENKINS_URL       the Jenkins base URL, a *.sdm.network name.
  JENKINS_JOB_PATH  a PR job's path, with {owner}, {repo} and {pr}.
When the name does not answer, the local port that `sdm status` lists for
it is used (JENKINS_SDM names the sdm binary).
Environment: JENKINS_POLL_SECS (30) between polls, JENKINS_CURL_SECS (30)
per request.
EOF
}

case "${1:-}" in
  -h|--help|help) usage; exit 0 ;;
  test) shift; exec "${PYTHON:-python3}" -m unittest discover -s "$DIR/tests" -t "$DIR" "$@" ;;
  log|wait) verb=$1; shift ;;
  *) usage >&2; exit 1 ;;
esac

spec="${1:-}"
case "$spec" in
  */*#*) ;;
  *) echo "jenkins.sh: give the PR as owner/repo#N, not '$spec'" >&2; exit 1 ;;
esac
shift
owner=${spec%%/*}
rest=${spec#*/}
repo=${rest%%#*}
pr=${rest#*#}
case "$pr" in ''|*[!0-9]*) echo "jenkins.sh: give the PR as owner/repo#N, not '$spec'" >&2; exit 1 ;; esac

st_source_env mezmo-jenkins
[ -n "${JENKINS_URL:-}" ] || { st_missing JENKINS_URL mezmo-jenkins .env mezmo-jenkins; exit 1; }
[ -n "${JENKINS_JOB_PATH:-}" ] || { st_missing JENKINS_JOB_PATH mezmo-jenkins .env mezmo-jenkins; exit 1; }

CURL_SECS="${JENKINS_CURL_SECS:-30}"
SDM="${JENKINS_SDM:-/Applications/SDM.app/Contents/Resources/sdm.darwin}"

jget() { curl -fsS --max-time "$CURL_SECS" "$base/$job/$1"; }

# The name answers on the tailnet when the SDM client resolves it; when it
# does not, the client still serves the site on a local port.
base=${JENKINS_URL%/}
if ! curl -fsS -o /dev/null --max-time 10 "$base/api/json" 2>/dev/null; then
  host=${base#*://}
  host=${host%%/*}
  port=$("$SDM" status 2>/dev/null | grep -F "://$host" | grep -o '127\.0\.0\.1:[0-9][0-9]*' | head -1) || port=""
  if [ -z "$port" ] || ! curl -fsS -o /dev/null --max-time 10 "http://$port/api/json" 2>/dev/null; then
    echo "jenkins.sh: $host does not answer, and sdm status lists no answering port for it; is the SDM client connected?" >&2
    exit 1
  fi
  base="http://$port"
fi

job=$(printf '%s\n' "$JENKINS_JOB_PATH" | sed -e "s|{owner}|$owner|g" -e "s|{repo}|$repo|g" -e "s|{pr}|$pr|g")
job=${job#/}

if [ "$verb" = log ]; then
  jget "${1:-lastBuild}/consoleText"
  exit 0
fi

timeout=60
if [ "${1:-}" = --timeout ]; then
  timeout=${2:?--timeout takes minutes}
fi
deadline=$(( $(date +%s) + timeout * 60 ))
while :; do
  # No build yet is a 404: keep waiting.
  if state=$(jget "lastBuild/api/json?tree=number,building,result" 2>/dev/null); then
    read -r number building result <<EOF
$(printf '%s' "$state" | python3 -c 'import json, sys
d = json.load(sys.stdin)
print(d["number"], str(d["building"]).lower(), d["result"] or "-")')
EOF
    [ "$building" = true ] || break
  fi
  if [ "$(date +%s)" -ge "$deadline" ]; then
    echo "jenkins.sh: timed out after $timeout minutes waiting for $spec" >&2
    exit 1
  fi
  sleep "${JENKINS_POLL_SECS:-30}"
done

echo "build $number: $result"
[ "$result" = SUCCESS ] && exit 0
jget "$number/consoleText" | grep -E 'FAIL|[Ee]rror' | head -50 || true
exit 1

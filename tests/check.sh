#!/bin/sh
# Every native suite on this machine, plus the macOS-only suites on a Mac.
# The root Makefile's `check` target runs this, and so does the local merge
# gate: `sd-ship merge` runs `sd-check`, which finds `make check`, in a
# clean detached worktree of the pull request's head.
#
# The environment is built here, not borrowed from the caller. The command
# pack is fetched into `.ci/pack` (gitignored) at the SHA in `.sd-pack-rev`,
# the one pin. `.ci/bin/python3` is the machine's python3.15. Each suite then
# runs under `env -i` with an isolated HOME and TMPDIR outside the tree and a
# PATH of that bin plus the system directories.
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$DIR/.." && pwd)"
PIN_FILE="$ROOT/.sd-pack-rev"
PACK_URL="${CI_PACK_URL:-https://github.com/platypeeps/sd-ai-command-pack.git}"
PACK_CLONE="${SD_PACK_ROOT:-$HOME/repos/platypeeps/sd-ai-command-pack}"
# Longest first, so that when the jobs run fewer at a time (below) the long
# ones start at once. repo-sync is a leg of its own (sd:3750): dealt into a
# tools shard it held that shard to 13 minutes. Its two suites run as two
# shards (sd:3962): whole, the leg took 450 s, the longest job of the gate.
LEGS="repo-sync tools shared"
TOOLS_SHARDS=4
REPO_SYNC_SHARDS=2

usage() {
  cat <<'USAGE'
Usage: check.sh run
       check.sh suite <name>
       check.sh legs

  run    the preflight, then the legs and (on macOS) each suite of
         tests/macos-only-suites.txt in parallel; the first failure stops
         the rest, and the run exits 1
  suite  the preflight, then the one suite named by a run_suite line in
         tests/ci-native.sh or a line of tests/macos-only-suites.txt, in the
         same environment; `make test SUITE=<name>` runs this
  legs   print the leg names

Environment:
  SD_PACK_ROOT       the machine's clone of the command pack; the pinned
                     commit is fetched from it when it holds that commit
                     (default: ~/repos/platypeeps/sd-ai-command-pack)
  CI_PACK_URL        where to fetch the pinned command pack from otherwise
                     (default: https://github.com/platypeeps/sd-ai-command-pack.git)
  SD_GATE_POOL_SIZE  the gate cap a slot holder exports; above 1, at most
                     CPUs / cap jobs run at once

`make check` runs `check.sh run`. Each needs python3.15, git, sqlite3, lsof,
ps, rsync, shellcheck and ruff on PATH, and network access when no local
clone holds the pin.
USAGE
}

case "${1:-}" in
  -h|--help|help) usage; exit 0 ;;
  legs) echo "$LEGS"; exit 0 ;;
  run) [ "$#" -eq 1 ] || { usage >&2; exit 1; } ;;
  suite) [ "$#" -eq 2 ] && [ -n "$2" ] || { usage >&2; exit 1; } ;;
  *) usage >&2; exit 1 ;;
esac

# The pack pin: one full commit sha in `.sd-pack-rev`; bump it deliberately.
# sd:1439: schema 14 stores repository paths as `~/` keys, and a pack older
# than pack #1166 writes the absolute path, so a nightly planner's registration
# failed its foreign key. Pack #1166 merged as 6d6214f6.
pin="$(cat "$PIN_FILE" 2>/dev/null || true)"
case "$pin" in
  *[!0-9a-f]*|"") echo "check.sh: no 40-hex pack sha in $PIN_FILE" >&2; exit 1 ;;
esac
[ "${#pin}" -eq 40 ] || { echo "check.sh: no 40-hex pack sha in $PIN_FILE" >&2; exit 1; }

python="$(command -v python3.15 || true)"
[ -n "$python" ] || { echo "check.sh: python3.15 is not on PATH; the suites need 3.15" >&2; exit 1; }
for tool in git sqlite3 lsof ps rsync; do
  command -v "$tool" > /dev/null || { echo "check.sh: $tool is not on PATH; the suites call it" >&2; exit 1; }
done
# The preflight's lint (sd:6742).
for tool in shellcheck ruff; do
  command -v "$tool" > /dev/null || { echo "check.sh: $tool is not on PATH; the lint needs it: brew install $tool" >&2; exit 1; }
done

env_dir="$ROOT/.ci"
mkdir -p "$env_dir/bin"
ln -sf "$python" "$env_dir/bin/python3"
# A suite must never reach the machine's launchd. Every suite that drives
# launchctl puts its own stub first on PATH; this one sits behind those and
# in front of /bin, so a call that misses its stub fails and is recorded
# instead of registering a job in the real gui domain. That happened once,
# when a stub directory's path held a `:` and PATH split it.
cat > "$env_dir/bin/launchctl" <<'GUARD'
#!/bin/sh
# HOME names the job; the parent chain names the suite that called.
{
  echo "launchctl $* (HOME=$HOME, cwd $PWD)"
  pid=$PPID
  while [ "${pid:-1}" -gt 1 ]; do
    echo "  from: $(ps -o command= -p "$pid" | cut -c1-200)"
    pid=$(ps -o ppid= -p "$pid" | tr -d ' ')
  done
} >> "${CI_WORK_ROOT:?}/launchctl-leaks"
echo "check.sh: a suite called the real launchctl: launchctl $*" >&2
exit 1
GUARD
chmod +x "$env_dir/bin/launchctl"

pack="$env_dir/pack"
if [ "$(git -C "$pack" rev-parse -q --verify HEAD 2>/dev/null)" != "$pin" ]; then
  # sd:2720: a gate runs in a fresh worktree, so this fetch ran in every
  # gate. The machine's own clone answers it when it holds the pin.
  source="$PACK_URL"
  if git -C "$PACK_CLONE" cat-file -e "$pin^{commit}" 2>/dev/null; then
    source="$PACK_CLONE"
  fi
  echo "check.sh: fetching the pack from $source"
  rm -rf "$pack"
  git init -q "$pack"
  git -C "$pack" fetch -q --depth 1 "$source" "$pin"
  git -C "$pack" -c advice.detachedHead=false checkout -q FETCH_HEAD
fi
echo "check.sh: pack $pin, $("$python" --version)"

# HOME and TMPDIR sit outside the tree: a fixture
# repository made under the checkout would find the checkout's own .git.
# /tmp and not $TMPDIR: macOS caps a socket path at 104 bytes, and a fixture
# socket under /var/folders/.../T/ plus the temp directories nested in it
# goes past that, so local-mirror-sync's socket tests failed there.
work="$(mktemp -d /tmp/system-check.XXXXXX)"

# sd:3749: the first failed job stops the run. A lane runs each job through
# own_group, whose leader takes a process group of its own, writes its id to
# $work/<job>.pid, and then, when $work/stop exists, marks the job stopped and
# exits 143 instead of starting it. signal_jobs marks each job named by a pid
# file stopped and signals its group. The stopping lane creates stop before it
# reads the files, so either it sees the leader's id or the leader sees stop.
# When a job ends, its lane kills the group, so nothing the job left running
# outlives it, then removes the pid file.
LEADER='import os, sys
os.setpgid(0, 0)
with open(sys.argv[1] + ".pid", "w") as pid_file:
    pid_file.write(str(os.getpid()))
if os.path.isdir(sys.argv[2]):
    open(sys.argv[1] + ".stopped", "w").close()
    sys.exit(143)
os.execvp(sys.argv[3], sys.argv[3:])'
own_group() { "$python" -I -c "$LEADER" "$work/$job" "$work/stop" "$@"; }
signal_group() {
  group="$(cat "$2" 2>/dev/null)" || return 0
  case "$group" in ''|*[!0-9]*) return 0 ;; esac
  kill -"$1" "-$group" 2>/dev/null || :
}
signal_jobs() {
  for pid_file in "$work"/*.pid; do
    [ -f "$pid_file" ] || continue
    : > "${pid_file%.pid}.stopped"
    signal_group "$1" "$pid_file"
  done
}
# TERM, then KILL for a job still running after about 5 seconds.
stop_jobs() {
  signal_jobs TERM
  wait_for=5
  while [ "$wait_for" -gt 0 ] && running_jobs; do
    sleep 1
    wait_for=$((wait_for - 1))
  done
  signal_jobs KILL
}
running_jobs() {
  for pid_file in "$work"/*.pid; do
    [ ! -f "$pid_file" ] || return 0
  done
  return 1
}
# A job's group no longer hears a signal sent to this script's group, so a
# signal ends the script, and its exit ends every job still running. It waits
# for the lanes, which then see stop and claim nothing, before it removes
# $work: a lane left running could claim a job once stop was gone.
trap 'mkdir "$work/stop" 2>/dev/null || :; signal_jobs TERM; signal_jobs KILL; wait; rm -rf "$work"' EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
tool_path="$env_dir/bin"
for tool in git sqlite3 lsof ps rsync make shellcheck ruff; do
  tool_path="$tool_path:$(dirname "$(command -v "$tool")")"
done
tool_path="$tool_path:/usr/bin:/bin:/usr/sbin:/sbin"

# Each job gets its own HOME and TMPDIR under $work/<job>: parallel suites must not share a home's state.
# A lane sets `lead=own_group`, so each job leads a process group of its own (sd:3749).
isolated() {
  mkdir -p "$work/$job/home" "$work/$job/tmp.noindex"
  ${lead:-} env -i PATH="$tool_path" HOME="$work/$job/home" TMPDIR="$work/$job/tmp.noindex" \
    CI_WORK_ROOT="$work" CI_SYSTEM_ROOT="$ROOT" \
    SD_ACCEPTANCE_PACK="$pack" SD_PACK_ROOT="$pack" \
    SD_PR_BODY="" \
    GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_OPTIONAL_LOCKS=0 \
    LANG=en_US.UTF-8 PYTHONDONTWRITEBYTECODE=1 NO_PROXY='*' no_proxy='*' \
    "$@" < /dev/null
}

echo "== preflight"
job=preflight
isolated /bin/bash --noprofile --norc "$DIR/ci-native.sh" preflight

# `suite` (sd:3534): one suite the way the gate runs it, against the pinned
# pack, where a hand run against SD_PACK_ROOT tested the live pack instead.
# A leg with SUITE_ONLY runs only the run_suite line of that name, so each
# leg is asked and the suite's log is the proof that one ran it.
if [ "$1" = suite ]; then
  job=suite
  if sh "$DIR/run-macos-only.sh" list | awk '{ print $1 }' | grep -qxF "$2"; then
    isolated sh "$DIR/run-macos-only.sh" "$2"
  else
    for leg in $LEGS; do
      isolated SUITE_ONLY="$2" /bin/bash --noprofile --norc "$DIR/ci-native.sh" leg "$leg"
    done
    # A suite in parts (repo-sync-1, repo-sync-2) leaves a log per part.
    ran=""
    for log in "$work/$2.log" "$work/$2"-[1-9].log; do
      [ ! -f "$log" ] || ran=1
    done
    if [ -z "$ran" ]; then
      echo "check.sh: no suite named $2; the run_suite lines in tests/ci-native.sh and the lines of tests/macos-only-suites.txt name them" >&2
      exit 1
    fi
  fi
  if [ -s "$work/launchctl-leaks" ]; then
    echo "== launchctl calls that missed their stub"
    cat "$work/launchctl-leaks"
    exit 1
  fi
  echo "check.sh: suite $2 passed"
  exit 0
fi

# Every leg and, on a Mac, every macOS-only suite run at once, unless a gate
# cap below lowers that. Run one after another they took 24 minutes here,
# and sd-check stops a check at 15. Each job
# writes its own log, printed whole once it ends, so the output does not
# interleave.
# The tools leg holds dozens of suites, one after another. It runs as
# TOOLS_SHARDS processes, each taking every n-th suite; the repo-sync leg
# runs as REPO_SYNC_SHARDS the same way.
# The macOS-only suites go first, so that when the jobs run fewer at a time
# (below) they start at once.
jobs=""
if [ "$(uname -s)" = Darwin ]; then
  for suite in $(sh "$DIR/run-macos-only.sh" list | awk '{ print $1 }'); do
    jobs="$jobs macos-$suite"
  done
fi
for leg in $LEGS; do
  case "$leg" in
    tools) shards=$TOOLS_SHARDS ;;
    repo-sync) shards=$REPO_SYNC_SHARDS ;;
    *) jobs="$jobs leg-$leg"; continue ;;
  esac
  shard=1
  while [ "$shard" -le "$shards" ]; do
    jobs="$jobs $leg-$shard"
    shard=$((shard + 1))
  done
done

# sd:2719: each job keeps about one CPU busy, and a slot holder such as
# `sd gate` runs up to SD_GATE_POOL_SIZE checks at once: 4 gates ran 32
# jobs and the load passed 200. Under a cap above 1 the jobs run in
# CPUs / cap lanes, so the pool's gates together keep one job per CPU.
# Each lane takes the next job no lane has claimed; mkdir is the claim.
set -- $jobs
lanes=$#
cores="$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 4)"
case "$cores" in ''|*[!0-9]*) cores=4 ;; esac
pool="${SD_GATE_POOL_SIZE:-0}"
case "$pool" in ''|*[!0-9]*) pool=0 ;; esac
if [ "$pool" -gt 1 ] && [ $((cores / pool)) -lt "$lanes" ]; then
  lanes=$((cores / pool))
  [ "$lanes" -ge 1 ] || lanes=1
fi
echo "check.sh: $# jobs, $lanes at a time (SD_GATE_POOL_SIZE=${SD_GATE_POOL_SIZE:-unset}, $cores CPUs)"
lead=own_group
lane=0
while [ "$lane" -lt "$lanes" ]; do
  for job in $jobs; do
    [ ! -d "$work/stop" ] || break
    mkdir "$work/$job.claim" 2>/dev/null || continue
    started=$(date +%s)
    status=0
    case "$job" in
      leg-*) isolated /bin/bash --noprofile --norc "$DIR/ci-native.sh" leg "${job#leg-}" ;;
      tools-*) isolated SUITE_SHARD="${job#tools-}/$TOOLS_SHARDS" \
                 /bin/bash --noprofile --norc "$DIR/ci-native.sh" leg tools ;;
      repo-sync-*) isolated SUITE_SHARD="${job#repo-sync-}/$REPO_SYNC_SHARDS" \
                     /bin/bash --noprofile --norc "$DIR/ci-native.sh" leg repo-sync ;;
      macos-*) isolated sh "$DIR/run-macos-only.sh" "${job#macos-}" ;;
    esac > "$work/$job.log" 2>&1 || status=$?
    signal_group KILL "$work/$job.pid"
    rm -f "$work/$job.pid"
    # The first failure stops the rest. A job the stop marked and that ended
    # nonzero was stopped; one that ended 0 as the stop reached it passed.
    if [ "$status" -ne 0 ]; then
      if mkdir "$work/stop" 2>/dev/null; then
        stop_jobs
      elif [ -f "$work/$job.stopped" ]; then
        status=stopped
      fi
    fi
    echo "$status $(($(date +%s) - started))" > "$work/$job.status"
  done &
  lane=$((lane + 1))
done
wait
failed="" stopped=""
for job in $jobs; do
  # A job with no status file never finished: count it failed, or stopped
  # when a failure stopped the run before a lane claimed it.
  status=1 seconds=0
  [ ! -d "$work/stop" ] || status=stopped
  [ ! -f "$work/$job.status" ] || read -r status seconds < "$work/$job.status"
  label=""
  [ "$status" != stopped ] || { label=", stopped"; stopped="$stopped $job"; }
  echo "== $job (${seconds}s$label)"
  [ ! -f "$work/$job.log" ] || cat "$work/$job.log"
  case "$status" in
    0|stopped) ;;
    *) echo "check.sh: $job exited $status" >&2; failed="$failed $job" ;;
  esac
done

if [ -s "$work/launchctl-leaks" ]; then
  echo "== launchctl calls that missed their stub"
  cat "$work/launchctl-leaks"
  failed="$failed launchctl-guard"
fi

if [ -n "$failed$stopped" ]; then
  # sd-check keeps only the end of the output, so the end names the failure.
  for job in $failed; do
    echo "== tail of $job"
    [ ! -f "$work/$job.log" ] || grep -E '^(FAIL|ERROR):|^[A-Za-z]*Error|: (skipped tests|no unittest)|FAILED' "$work/$job.log" | tail -n 20
  done
  # Each job's whole log is printed above, and sd-check keeps a failing
  # check's whole output, so the EXIT trap removes $work here too.
  [ -z "$stopped" ] || echo "check.sh: stopped:$stopped" >&2
  [ -z "$failed" ] || echo "check.sh: failed:$failed" >&2
  exit 1
fi
echo "check.sh: every suite passed"

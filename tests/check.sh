#!/bin/sh
# Every native suite on this machine, plus the macOS-only suites on a Mac.
# The root Makefile's `check` target runs this, and so does the local merge
# gate: `sd-ship merge` runs `sd-check`, which finds `make check`, in a
# clean detached worktree of the pull request's head.
#
# The environment is built here, not borrowed from the caller. The command
# pack is fetched into `.ci/pack` (gitignored) at the SHA in `.sd-pack-rev`,
# the one pin. `.ci/bin/python3` is the machine's python3.14. Each suite then
# runs under `env -i` with an isolated HOME and TMPDIR outside the tree and a
# PATH of that bin plus the system directories.
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$DIR/.." && pwd)"
PIN_FILE="$ROOT/.sd-pack-rev"
PACK_URL="${CI_PACK_URL:-https://github.com/platypeeps/sd-ai-command-pack.git}"
PACK_CLONE="${SD_PACK_ROOT:-$HOME/repos/platypeeps/sd-ai-command-pack}"
LEGS="shared dashboard tools"
TOOLS_SHARDS=3

usage() {
  cat <<'USAGE'
Usage: check.sh run
       check.sh legs

  run   the preflight, then the three legs and (on macOS) each suite of
        tests/macos-only-suites.txt in parallel; exits 1 when any fails
  legs  print the leg names

Environment:
  SD_PACK_ROOT       the machine's clone of the command pack; the pinned
                     commit is fetched from it when it holds that commit
                     (default: ~/repos/platypeeps/sd-ai-command-pack)
  CI_PACK_URL        where to fetch the pinned command pack from otherwise
                     (default: https://github.com/platypeeps/sd-ai-command-pack.git)
  SD_GATE_POOL_SIZE  the gate cap a slot holder exports; above 1, at most
                     CPUs / cap jobs run at once

`make check` runs `check.sh run`. It needs python3.14, git, sqlite3, lsof,
ps and rsync on PATH, and network access when no local clone holds the pin.
USAGE
}

case "${1:-}" in
  -h|--help|help) usage; exit 0 ;;
  legs) echo "$LEGS"; exit 0 ;;
  run) [ "$#" -eq 1 ] || { usage >&2; exit 1; } ;;
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

python="$(command -v python3.14 || true)"
[ -n "$python" ] || { echo "check.sh: python3.14 is not on PATH; the suites need 3.14" >&2; exit 1; }
for tool in git sqlite3 lsof ps rsync; do
  command -v "$tool" > /dev/null || { echo "check.sh: $tool is not on PATH; the suites call it" >&2; exit 1; }
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
trap 'rm -rf "$work"' EXIT
tool_path="$env_dir/bin"
for tool in git sqlite3 lsof ps rsync make; do
  tool_path="$tool_path:$(dirname "$(command -v "$tool")")"
done
tool_path="$tool_path:/usr/bin:/bin:/usr/sbin:/sbin"

# Each job gets its own HOME and TMPDIR under $work/<job>: parallel suites must not share a home's state.
isolated() {
  mkdir -p "$work/$job/home" "$work/$job/tmp.noindex"
  env -i PATH="$tool_path" HOME="$work/$job/home" TMPDIR="$work/$job/tmp.noindex" \
    CI_WORK_ROOT="$work" CI_SYSTEM_ROOT="$ROOT" \
    SD_ACCEPTANCE_PACK="$pack" \
    SD_WRITING_MANIFEST="$ROOT/.github/fixtures/writing-sd-plugin.json" \
    SD_PR_BODY="" \
    GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_OPTIONAL_LOCKS=0 \
    LANG=en_US.UTF-8 PYTHONDONTWRITEBYTECODE=1 NO_PROXY='*' no_proxy='*' \
    "$@" < /dev/null
}

echo "== preflight"
job=preflight
isolated /bin/bash --noprofile --norc "$DIR/ci-native.sh" preflight

# Every leg and, on a Mac, every macOS-only suite run at once, unless a gate
# cap below lowers that. Run one after another they took 24 minutes here,
# and sd-check stops a check at 15. Each job
# writes its own log, printed whole once it ends, so the output does not
# interleave.
# The tools leg is the longest: dozens of short suites, one after another.
# It runs as TOOLS_SHARDS processes, each taking every n-th suite.
# The macOS-only suites go first, so that when the jobs run fewer at a time
# (below) they start at once.
jobs=""
if [ "$(uname -s)" = Darwin ]; then
  for suite in $(sh "$DIR/run-macos-only.sh" list | awk '{ print $1 }'); do
    jobs="$jobs macos-$suite"
  done
fi
for leg in $LEGS; do
  if [ "$leg" = tools ]; then
    shard=1
    while [ "$shard" -le "$TOOLS_SHARDS" ]; do
      jobs="$jobs tools-$shard"
      shard=$((shard + 1))
    done
  else
    jobs="$jobs leg-$leg"
  fi
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
lane=0
while [ "$lane" -lt "$lanes" ]; do
  for job in $jobs; do
    mkdir "$work/$job.claim" 2>/dev/null || continue
    started=$(date +%s)
    status=0
    case "$job" in
      leg-*) isolated /bin/bash --noprofile --norc "$DIR/ci-native.sh" leg "${job#leg-}" ;;
      tools-*) isolated SUITE_SHARD="${job#tools-}/$TOOLS_SHARDS" \
                 /bin/bash --noprofile --norc "$DIR/ci-native.sh" leg tools ;;
      macos-*) isolated sh "$DIR/run-macos-only.sh" "${job#macos-}" ;;
    esac > "$work/$job.log" 2>&1 || status=$?
    echo "$status $(($(date +%s) - started))" > "$work/$job.status"
  done &
  lane=$((lane + 1))
done
wait
failed=""
for job in $jobs; do
  # A job with no status file never finished: count it failed.
  status=1 seconds=0
  [ ! -f "$work/$job.status" ] || read -r status seconds < "$work/$job.status"
  echo "== $job (${seconds}s)"
  [ ! -f "$work/$job.log" ] || cat "$work/$job.log"
  [ "$status" -eq 0 ] || { echo "check.sh: $job exited $status" >&2; failed="$failed $job"; }
done

if [ -s "$work/launchctl-leaks" ]; then
  echo "== launchctl calls that missed their stub"
  cat "$work/launchctl-leaks"
  failed="$failed launchctl-guard"
fi

if [ -n "$failed" ]; then
  # sd-check keeps only the end of the output, so the end names the failure.
  for job in $failed; do
    echo "== tail of $job"
    [ ! -f "$work/$job.log" ] || grep -E '^(FAIL|ERROR):|^[A-Za-z]*Error|: (skipped tests|no unittest)|FAILED' "$work/$job.log" | tail -n 20
  done
  # Each job's whole log is printed above, and sd-check keeps a failing
  # check's whole output, so the EXIT trap removes $work here too.
  echo "check.sh: failed:$failed" >&2
  exit 1
fi
echo "check.sh: every suite passed"

#!/bin/sh
# adversarial-gate — compose and run the hostile second-reader pass.
#
# The shared half of a gate two repos built separately: the writing pack ran it
# from `pack.py review adversarial`, local-research-kit from the Codex plugin,
# and both carried their own copy of the same careful prose about what the gate
# can and cannot decide. That prose is `core.md` now, once. What differs per
# document type is a lens in `lenses/`.
#
#   adversarial-gate render --lens NAME [--set KEY=VALUE ...]
#       compose core.md + lenses/NAME.md to stdout, substituting {KEY}
#
#   adversarial-gate run --lens NAME [--set KEY=VALUE ...]
#                        --repo DIR --out FILE [--model M] [--effort LEVEL]
#                        [--timeout S]
#       the same prompt, piped to `codex exec` in a read-only sandbox;
#       --effort is codex's `-c model_reasoning_effort=LEVEL`
#
#   adversarial-gate deep --lens NAME [--set KEY=VALUE ...]
#                         --repo DIR --out FILE [--model M] [--effort LEVEL]
#                         [--timeout S]
#       the same prompt, piped to headless `claude -p` in DIR, with only
#       the read-only tools in DEEP_TOOLS: no edit, write, shell, network
#       or MCP server. --effort is claude's --effort (low ... max)
#
#   adversarial-gate rank --in FILE   order an existing result by a Jev noul
#
#   adversarial-gate lenses           list the available lenses
#
#   adversarial-gate test [-v]        run the unittest suite under tests/
#
# `rank` runs unless `jev enabled JEV_ADVERSARIAL_GATE` declines -- unkeyed,
# the fleet switch off, or that variable switching this stage off -- and `run`
# calls it under exactly the same condition. It orders findings and
# labels them; it never drops, hides or filters one. Every failure leaves the
# result exactly as the reviewer wrote it, says why on stderr, and changes no
# exit code.
#
# `render` is the useful one for a caller that already owns its invocation:
# the writing pack composes through it and keeps its own `codex exec`, because
# the digest stamping and confidence counting around that call are its own.
# `run` is for the scripted path a caller does not otherwise have.
#
# Output is HYPOTHESES, not findings. Whoever calls this is responsible for
# saying so wherever the result lands.
set -eu

# The repo convention is ROOT="$(cd "$(dirname "$0")" && pwd)", but this script
# is invoked through the ~/bin/common/adversarial-gate symlink and reads core.md
# and lenses/ sitting next to it, so $0 has to be walked to its real location
# first -- the same walk repo-sync.sh and notify.sh do, for the same reason.
SELF="$0"
while [ -L "$SELF" ]; do
  link=$(readlink "$SELF")
  case "$link" in
    /*) SELF="$link" ;;
    *)  SELF="$(dirname "$SELF")/$link" ;;
  esac
done
ROOT="$(cd "$(dirname "$SELF")" && pwd)"
CORE="$ROOT/core.md"
LENS_DIR="$ROOT/lenses"
. "$ROOT/../lib/bounded.sh"
RANKER="$ROOT/rank.py"
# The sibling entrypoint by path from our own resolved directory, never by
# `jev` being on PATH: this script is reached through the ~/bin/common symlink,
# and through the symlink sibling files are missing. ADVERSARIAL_GATE_JEV is the
# seam the suite drives a stub through, so no test reaches the network.
JEV="${ADVERSARIAL_GATE_JEV:-$(dirname "$ROOT")/local-jev/jev.sh}"
# One id for every jev call this run makes, so the ledger groups them
# (sd:2953). A run that started this one keeps its own: it is the same run.
[ -n "${JEV_RUN:-}" ] || \
  JEV_RUN="adversarial-gate-$(date -u +%Y%m%dT%H%M%S)-$(od -An -N2 -tx1 /dev/urandom | tr -d ' \n')"
export JEV_RUN

die() { echo "adversarial-gate: $*" >&2; exit 1; }

# Jev is experimental, so nothing here depends on it: both halves must be true
# or the result is the one the reviewer wrote, unchanged. Every Jev call
# leaves the machine and this tool reads unpublished drafts, which is what
# `JEV_ADVERSARIAL_GATE=0` is for; `jev enabled` costs nothing and calls
# nothing.
jev_ordering_wanted() {
  [ -x "$JEV" ] || {
    echo "adversarial-gate: no jev at $JEV;" \
         "the order is the reviewer's own" >&2
    return 1
  }
  # `enabled JEV_ADVERSARIAL_GATE` is both halves in one call: Jev can answer
  # here, and the variable has not been used to switch this stage off. Unset
  # means on -- a per-caller switch that defaults to off makes every
  # integration added after it silently never run. Still loud when it
  # declines, for the same reason `--fallback` is: a lane that quietly stops
  # running is the defect this shape exists to avoid.
  # `--record` because this gate is where most runs end: without it the old
  # path runs and nothing anywhere says it did, and a stage whose callers only
  # ever decline has no number at all.
  "$JEV" enabled JEV_ADVERSARIAL_GATE --record --caller local-adversarial-gate \
      >/dev/null 2>&1 || {
    echo "adversarial-gate: jev is off or unkeyed here, or" \
         "JEV_ADVERSARIAL_GATE switched this stage off;" \
         "the order is the reviewer's own" >&2
    # No second row here: the gate above wrote one in the process this call
    # already started, and a `jev record` subprocess per decline would double
    # the cost of the path that declines on every run.
    return 1
  }
  return 0
}

# Ordering is a courtesy, never a gate: this returns 0 whatever happens, so a
# caller's exit code is the reviewer's result and nothing else.
jev_rank() {
  jev_ordering_wanted || return 0
  [ -s "$1" ] || {
    echo "adversarial-gate: rank: $1 is empty or absent; nothing to order" >&2
    return 0
  }
  "${PYTHON:-python3}" "$RANKER" --in "$1" --jev "$JEV" || \
    echo "adversarial-gate: rank: the ranker failed; the order is the reviewer's own" >&2
  return 0
}

usage() { sed -n '2,45p' "$SELF" | sed 's/^# \{0,1\}//'; }

# `deep`'s tools (sd:5100): the file readers and Agent, which starts the
# sub-agents; a sub-agent gets this set and no more. No Edit, Write, Bash,
# WebFetch or MCP tool, so the review cannot change the checkout, push or
# post. tests/test_deep.py fails a tool outside its read-only list.
DEEP_TOOLS="Read,Grep,Glob,Agent"

lens_path() {
  case "$1" in
    */*|*.md) echo "$1" ;;          # an explicit path, for a repo-local lens
    *)        echo "$LENS_DIR/$1.md" ;;
  esac
}

cmd_render() {
  lens="" ; sets=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --lens) lens="${2:?--lens needs a name}"; shift 2 ;;
      --set)  sets="$sets
${2:?--set needs KEY=VALUE}"; shift 2 ;;
      *) die "render: unexpected argument '$1'" ;;
    esac
  done
  [ -n "$lens" ] || die "render: --lens is required (see: adversarial-gate lenses)"
  lp="$(lens_path "$lens")"
  [ -f "$CORE" ] || die "missing core prompt: $CORE"
  [ -f "$lp" ] || die "no such lens: $lp"

  # Substitute {KEY} per --set, in one awk pass over the composed text.
  # awk on literal strings rather than sed: a value containing / or & would be
  # a delimiter or a backreference there, and paths contain /.
  { cat "$CORE"; printf '\n'; cat "$lp"; } | GATE_SETS="$sets" awk '
    BEGIN {
      n = split(ENVIRON["GATE_SETS"], lines, "\n")
      for (i = 1; i <= n; i++) {
        if (lines[i] == "") continue
        eq = index(lines[i], "=")
        if (eq == 0) { print "adversarial-gate: --set wants KEY=VALUE, got " lines[i] > "/dev/stderr"; exit 2 }
        k[++m] = "{" substr(lines[i], 1, eq-1) "}"
        v[m]   = substr(lines[i], eq+1)
      }
    }
    {
      for (i = 1; i <= m; i++) {
        p = index($0, k[i])
        while (p > 0) {
          $0 = substr($0, 1, p-1) v[i] substr($0, p + length(k[i]))
          p = index($0, k[i])
        }
      }
      print
    }'
}

# `cmd_run codex|claude ARGS`: `run` and `deep`, one parser and one bound.
cmd_run() {
  engine="$1"; shift
  verb=run; [ "$engine" = codex ] || verb=deep
  repo="" ; outfile="" ; model="" ; effort="" ; effort_given=0 ; timeout="1800"
  # The words for `render` (--lens, --set) are rotated to the end of "$@" as
  # they are met, in order; `n` counts the words not yet read. Until sd:5064
  # they went through an `eval` of single-quoted words, so a quote in a value
  # broke the words apart and the shell ran what followed it.
  n=$#
  while [ "$n" -gt 0 ]; do
    # `[ $# -ge 2 ]` and not `${2:?}`: that form also rejected an EMPTY value,
    # and did it with the shell's own `2: parameter null or not set`, so
    # `--out ""` never reached the `--out is required` line below (sd:790).
    # An empty value is the flag's fault to report; only the flag as the
    # last word is a missing value.
    [ "$n" -ge 2 ] || case "$1" in
      --repo|--out|--model|--effort|--timeout) die "$verb: $1 needs a value" ;;
    esac
    case "$1" in
      --repo)    repo="$2"; shift 2; n=$((n - 2)) ;;
      --out)     outfile="$2"; shift 2; n=$((n - 2)) ;;
      --model)   model="$2"; shift 2; n=$((n - 2)) ;;
      --effort)  effort="$2"; effort_given=1; shift 2; n=$((n - 2)) ;;
      --timeout) timeout="$2"; shift 2; n=$((n - 2)) ;;
      *) set -- "$@" "$1"; shift; n=$((n - 1)) ;;
    esac
  done
  [ -n "$repo" ] || die "$verb: --repo is required"
  [ -n "$outfile" ] || die "$verb: --out is required"
  case "$timeout" in
    ''|*[!0-9]*) die "$verb: --timeout wants seconds, a positive integer, not '$timeout'" ;;
  esac
  [ "$timeout" -gt 0 ] || die "$verb: --timeout wants seconds, a positive integer, not '$timeout'"
  # The level lands in a `-c key=value` override, which codex parses as TOML:
  # a quote, an `=` or a newline would make it another override (sd:5064).
  [ "$effort_given" = 0 ] || case "$effort" in
    ''|*[!a-z]*) die "$verb: --effort wants one lowercase word, such as high, not '$effort'" ;;
  esac
  # The model is one argv word after --model or -m: a leading dash would make
  # it a flag of the CLI's own, such as one that lifts the tool list.
  case "$model" in
    -*|*[!A-Za-z0-9._:/@-]*) die "$verb: --model wants a model id or alias, not '$model'" ;;
  esac
  command -v perl >/dev/null 2>&1 || die "perl not found on PATH (it bounds the run)"
  if [ "$engine" = codex ]; then
    command -v codex >/dev/null 2>&1 || die "codex CLI not found on PATH"
    prompt="$(cmd_render "$@")"
    set -- -s read-only -C "$repo" -o "$outfile" -
    [ -z "$effort" ] || set -- -c "model_reasoning_effort=$effort" "$@"
    [ -z "$model" ] || set -- -m "$model" "$@"
    set -- codex exec "$@"
    echo "# adversarial gate: read-only sandbox, $repo -> $outfile" >&2
  else
    [ -d "$repo" ] || die "deep: --repo $repo is not a directory"
    # Resolved before env.sh is sourced, so a PATH it sets cannot swap the
    # binary; CLAUDE_BIN is the seam the suites drive a stub through. The
    # native installer puts claude in ~/.local/bin, which launchd's PATH
    # lacks -- the same lookup as the dashboard's email_draft.py.
    claude="${CLAUDE_BIN:-$(command -v claude || echo "$HOME/.local/bin/claude")}"
    [ -x "$claude" ] || die "claude CLI not found on PATH or at $claude"
    prompt="$(cmd_render "$@")"
    case "$outfile" in /*) ;; *) outfile="$PWD/$outfile" ;; esac
    set -- -p --restricted --strict-mcp-config --tools "$DEEP_TOOLS" \
      --allowedTools "$DEEP_TOOLS" --permission-mode dontAsk \
      --no-session-persistence --disable-slash-commands
    [ -z "$effort" ] || set -- "$@" --effort "$effort"
    [ -z "$model" ] || set -- "$@" --model "$model"
    # The token for a headless run lives in the shell's env file, which a
    # launchd job never reads, and launchd's 256 open files are too few:
    # both are set in a shell of claude's own, so neither reaches this one.
    # lib/config.sh's st_shell_env sources that file and maps its
    # CUSTOM_CLAUDE_CODE_OAUTH_TOKEN to the name claude reads (sd:5791).
    set -- sh -c '. "$0"; st_shell_env
ulimit -S -n 65536 2>/dev/null
exec "$@"' "$ROOT/../lib/config.sh" "$claude" "$@"
    echo "# adversarial gate: claude -p with $DEEP_TOOLS only, $repo -> $outfile" >&2
    # claude prints its answer on stdout and reads the files under its cwd.
    exec 3>&1 >"$outfile"
    cd "$repo"
  fi
  # The bound, applied: lib/bounded.sh's st_bounded runs the reviewer in a
  # process group of its own, so codex's sandbox and reviewer end with it
  # (review of #396). At the bound, or on a TERM to the gate from a job's
  # limit, the group gets TERM, then KILL after the grace (sd:8814). A run
  # cut off at the bound exits 124, timeout(1)'s status; a denied group
  # runs nothing and exits 125 (sd:7901).
  status=0
  printf '%s' "$prompt" | st_bounded "$timeout" "$@" || status=$?
  case $status in
    124) echo "adversarial-gate: $verb: $engine did not answer within $timeout s; its process group was killed (exit 124)" >&2 ;;
    125) echo "adversarial-gate: $verb: $engine not run: the bound needs its own process group" >&2 ;;
  esac
  [ "$engine" = codex ] || exec 1>&3 3>&-
  # Only a run that answered has findings to order, and the exit code is the
  # reviewer's either way: a cut-off run still exits 124.
  [ "$status" -eq 0 ] && jev_rank "$outfile"
  return "$status"
}

case "${1:-}" in
  render) shift; cmd_render "$@" ;;
  run)    shift; cmd_run codex "$@" ;;
  deep)   shift; cmd_run claude "$@" ;;
  rank)   shift
          [ $# -eq 2 ] && [ "$1" = "--in" ] || die "usage: adversarial-gate rank --in FILE"
          [ -n "$2" ] || die "rank: --in needs a file"
          jev_rank "$2" ;;
  lenses) ls "$LENS_DIR" | sed 's/\.md$//' ;;
  test)   shift
          exec "${PYTHON:-python3}" -m unittest discover -s "$ROOT/tests" -t "$ROOT" "$@" ;;
  -h|--help|help) usage ;;
  "")
    usage >&2
    exit 1
    ;;
  *) die "unknown command '$1' (render | run | deep | rank | lenses | test)" ;;
esac

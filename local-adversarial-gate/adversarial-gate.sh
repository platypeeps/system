#!/bin/sh
# adversarial-gate — compose and run the hostile second-reader pass.
#
# The shared half of a gate two repos built separately: sd-writing-pack ran it
# from `pack.py review adversarial`, local-research-kit from the Codex plugin,
# and both carried their own copy of the same careful prose about what the gate
# can and cannot decide. That prose is `core.md` now, once. What differs per
# document type is a lens in `lenses/`.
#
#   adversarial-gate render --lens NAME [--set KEY=VALUE ...]
#       compose core.md + lenses/NAME.md to stdout, substituting {KEY}
#
#   adversarial-gate run --lens NAME [--set KEY=VALUE ...]
#                        --repo DIR --out FILE [--model M] [--timeout S]
#       the same prompt, piped to `codex exec` in a read-only sandbox
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
# sd-writing-pack composes through it and keeps its own `codex exec`, because
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
RANKER="$ROOT/rank.py"
# The sibling entrypoint by path from our own resolved directory, never by
# `jev` being on PATH: this script is reached through the ~/bin/common symlink,
# and through the symlink sibling files are missing. ADVERSARIAL_GATE_JEV is the
# seam the suite drives a stub through, so no test reaches the network.
JEV="${ADVERSARIAL_GATE_JEV:-$(dirname "$ROOT")/local-jev/jev.sh}"

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

usage() { sed -n '2,35p' "$0" | sed 's/^# \{0,1\}//'; }

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

cmd_run() {
  repo="" ; outfile="" ; model="" ; timeout="1800" ; pass=""
  while [ $# -gt 0 ]; do
    # `[ $# -ge 2 ]` and not `${2:?}`: that form also rejected an EMPTY value,
    # and did it with the shell's own `2: parameter null or not set`, so
    # `--out ""` never reached the `--out is required` line below (sd:790).
    # An empty value is the flag's fault to report; only the flag as the
    # last word is a missing value.
    [ "$#" -ge 2 ] || case "$1" in
      --repo|--out|--model|--timeout) die "run: $1 needs a value" ;;
    esac
    case "$1" in
      --repo)    repo="$2"; shift 2 ;;
      --out)     outfile="$2"; shift 2 ;;
      --model)   model="$2"; shift 2 ;;
      --timeout) timeout="$2"; shift 2 ;;
      *) pass="$pass '$1'"; shift ;;
    esac
  done
  [ -n "$repo" ] || die "run: --repo is required"
  [ -n "$outfile" ] || die "run: --out is required"
  case "$timeout" in
    ''|*[!0-9]*) die "run: --timeout wants seconds, a positive integer, not '$timeout'" ;;
  esac
  [ "$timeout" -gt 0 ] || die "run: --timeout wants seconds, a positive integer, not '$timeout'"
  command -v codex >/dev/null 2>&1 || die "codex CLI not found on PATH"
  command -v perl >/dev/null 2>&1 || die "perl not found on PATH (it bounds the run)"
  prompt="$(eval cmd_render $pass)"
  set -- codex exec -s read-only -C "$repo" -o "$outfile" -
  [ -n "$model" ] && set -- codex exec -m "$model" -s read-only -C "$repo" -o "$outfile" -
  echo "# adversarial gate: read-only sandbox, $repo -> $outfile" >&2
  # The bound, applied. Until sd:790 `--timeout` was parsed into a variable
  # nothing read, and a reviewer that never answered held the caller for as
  # long as it liked. macOS ships no timeout(1) -- the one on this machine
  # is Homebrew's coreutils, which a fresh Mac does not have -- but it does
  # ship /usr/bin/perl, and perl can do what timeout(1) does: fork, put the
  # child in a process group of its own, exec codex there, and at the bound
  # kill the GROUP, TERM then KILL. The group and not the one process,
  # because codex is a launcher that spawns its sandbox and the reviewer
  # under it (review of #396): a signal to codex alone returns to the
  # caller while the reviewer keeps running and keeps the pipe open. The
  # same shape as the two timeout paths this repository already has,
  # local-project-dashboard/collectors.py (start_new_session + killpg) and
  # local-sd-db/sd_db/runner_exec.py. A run cut off at the bound exits 124,
  # timeout(1)'s own status, so a caller can tell it from the reviewer's
  # own failure; a child that dies of a signal otherwise exits 128+signal.
  status=0
  printf '%s' "$prompt" | perl -e '
    use POSIX ":sys_wait_h";
    my $secs = shift;
    my $pid = fork;
    defined $pid or die "adversarial-gate: fork: $!\n";
    if ($pid == 0) { setpgrp(0, 0); exec @ARGV or die "adversarial-gate: exec: $!\n"; }
    $SIG{ALRM} = sub {
      kill TERM => -$pid;
      my $gone = 0;
      for (1 .. 20) {
        if (waitpid($pid, WNOHANG) != 0) { $gone = 1; last; }
        select(undef, undef, undef, 0.1);
      }
      kill KILL => -$pid;
      waitpid($pid, 0) unless $gone;
      print STDERR "adversarial-gate: run: codex did not answer within $secs s; its process group was killed (exit 124)\n";
      exit 124;
    };
    alarm $secs;
    waitpid($pid, 0);
    my $status = $?;
    exit(($status & 127) ? 128 + ($status & 127) : $status >> 8);
  ' "$timeout" "$@" || status=$?
  # Only a run that answered has findings to order, and the exit code is the
  # reviewer's either way: a cut-off run still exits 124.
  [ "$status" -eq 0 ] && jev_rank "$outfile"
  return "$status"
}

case "${1:-}" in
  render) shift; cmd_render "$@" ;;
  run)    shift; cmd_run "$@" ;;
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
  *) die "unknown command '$1' (render | run | lenses | test)" ;;
esac

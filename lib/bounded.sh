# shellcheck shell=sh
# Bounded steps for unattended jobs. Source it; it defines functions and two
# variables and runs nothing else.
#
#   . "$DIR/../lib/bounded.sh"
#   st_step 1800 brew upgrade
#
# On some nights macOS stops answering permission checks for launchd jobs, and
# a brew or mas call then hangs. It used to run until the job's limit killed
# the whole job, with nothing in the log to say which call it was (sd:2660).

# Perl, not timeout(1): macOS ships none, and Homebrew's coreutils is not on
# launchd's PATH. The command runs in a process group of its own, so the bound
# ends all of it. On expiry: one line on stderr naming the command, TERM to
# the group, KILL after ST_BOUNDED_GRACE seconds (default
# ST_BOUNDED_DEFAULT_GRACE), and exit 124, the code GNU timeout uses.
#
# An INT, TERM or HUP to this process ends the command the same way: the
# signal goes on to the group, KILL follows after the grace, and the exit is
# 128 plus the signal. Passing the signal on alone was not enough: cron-jobs
# TERMs a job's group at its limit and KILLs it after its own grace (10 s by
# default), and that KILL never reaches a group of its own, so a command that
# ignored the TERM outlived the job. The default grace stays well under that
# 10 s; lib/tests/test_bounded.py reads cron-jobs' default to hold it there.
ST_BOUNDED_DEFAULT_GRACE=5
# A sandbox can deny setpgid (sd:7901). The command would then stay in the
# caller's group, and no signal would reach what it forks: the child refuses
# before the exec, by name, and the bound exits 125 (operator, 2026-11-06).
# Only the child sets its group: a parent setpgrp($pid, $pid) raced it into
# EPERM. The bound is armed at once, as an exec can stall. Until its setpgrp
# the child has no group and has forked nothing, so a signal that finds no
# group goes to the child itself.
# The source starts with its use line: a stall fixture finds this perl by
# that argv prefix (fixture_config.STALL).
# shellcheck disable=SC2016 # perl source, expanded by perl
ST_BOUNDED='use strict; use POSIX (); use Time::HiRes ();
my ($limit, $grace, @cmd) = @ARGV;
my $pid = fork;
defined $pid or do { print STDERR "cannot start $cmd[0]: $!\n"; exit 1 };
if (!$pid) {
  setpgrp(0, 0) or do {
    print STDERR "st_bounded: process group denied ($!); a bound needs its own process group, nothing was run: @cmd\n";
    POSIX::_exit(125);
  };
  exec { $cmd[0] } @cmd;
  print STDERR "cannot run $cmd[0]: $!\n";
  POSIX::_exit(127);
}
# Once reaped, the PID may be another process: only the group is signalled.
my $reaped = 0;
sub reap { my $got = waitpid($pid, $_[0]); $reaped = 1 if $got == $pid; $got }
sub hit { kill($_[0], -$pid) or $reaped or kill($_[0], $pid) }
# Reap first: an unreaped zombie leader answers the group probe on Linux.
sub alive { my $running = reap(POSIX::WNOHANG()) == 0; kill(0, -$pid) or $running }
my %number = (INT => POSIX::SIGINT(), TERM => POSIX::SIGTERM(), HUP => POSIX::SIGHUP());
my ($status, $caught);
# perl retries a waitpid a signal interrupts, so each handler dies to end it.
eval {
  local $SIG{ALRM} = sub { die "limit\n" };
  local @SIG{keys %number} = map {
    my $sig = $_;
    sub { $caught = $sig; hit($sig); die "signal\n" }
  } keys %number;
  alarm $limit;
  $status = $? if reap(0) == $pid;
  alarm 0;
};
alarm 0;
if (defined $status) { exit($status & 127 ? 128 + ($status & 127) : $status >> 8) }
# Ending the group is not interrupted: a second TERM must not leave it half done.
$SIG{$_} = "IGNORE" for keys %number;
if (!defined $caught) {
  print STDERR "timed out after ${limit}s: @cmd\n";
  hit("TERM");
}
my $end = Time::HiRes::time() + $grace;
while (alive()) {
  if (Time::HiRes::time() >= $end) { hit("KILL"); last }
  Time::HiRes::sleep(0.1);
}
reap(0);
exit(defined $caught ? 128 + $number{$caught} : 124);'

# st_bounded SECONDS COMMAND...: run COMMAND; stop it after SECONDS.
# Exits with COMMAND's code, 124 when the bound expired, or 125 when a
# denied process group kept COMMAND from running.
st_bounded() {
    _st_seconds=$1
    shift
    perl -e "$ST_BOUNDED" "$_st_seconds" "${ST_BOUNDED_GRACE:-$ST_BOUNDED_DEFAULT_GRACE}" "$@"
}

# st_step SECONDS COMMAND...: log COMMAND, then run it under st_bounded.
# The line is written before COMMAND starts, so a step that never ends is the
# last one the log names. It goes to stderr. A caller that captures stderr
# into a report opens fd 3 on the job log and sets ST_STEP_FD3=1; the line
# then goes there instead. A failure goes to both.
st_step() {
    _st_seconds=$1
    shift
    _st_say "$* (bound ${_st_seconds}s)"
    _st_rc=0
    st_bounded "$_st_seconds" "$@" || _st_rc=$?
    [ "$_st_rc" -eq 0 ] || _st_say "$* exited $_st_rc" both
    return "$_st_rc"
}

_st_say() { # line [both]
    _st_text="[step $(date '+%H:%M:%S')] $1"
    if [ "${ST_STEP_FD3:-}" = 1 ]; then
        echo "$_st_text" >&3
        [ -z "${2:-}" ] || echo "$_st_text" >&2
    else
        echo "$_st_text" >&2
    fi
}

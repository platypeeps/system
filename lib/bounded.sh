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

# Perl subs for every bound that ends a command's process group: this one,
# cron-jobs' job limit and the adversarial gate's reviewer bound. A sandbox
# can deny setpgid (sd:7901): the command then stays in its caller's group,
# a signal to "its" group reaches nothing, and the bound waited on it for as
# long as it ran. st_prepare(PID, WHY) reads, once the bound is reached,
# whether PID leads a group of its own; when it does not, it lists PID and
# its descendants by parent links, and st_kill and st_alive work on that
# list instead of the group. It returns a line naming the denial, or "".
# WHY is the parent's own setpgrp error, if any.
# shellcheck disable=SC2016 # perl source, expanded by perl
ST_GROUP_PERL='my @st_tree;
sub st_prepare {
  my ($pid, $why) = @_;
  @st_tree = ();
  return "" if getpgrp($pid) == $pid;
  my %kids;
  if (open(my $ps, "-|", "ps", "-A", "-o", "pid=,ppid=")) {
    while (<$ps>) { my ($p, $pp) = split; push @{$kids{$pp}}, $p if defined $pp }
    close $ps;
  }
  @st_tree = ($pid);
  for (my $i = 0; $i < @st_tree; $i++) { push @st_tree, @{$kids{$st_tree[$i]} || []} }
  return "process group denied (" . ($why || "pid $pid leads no group") . "); signalling its "
    . scalar(@st_tree) . " process(es) by parent links\n";
}
sub st_kill { my ($sig, $pid) = @_; @st_tree ? kill($sig, @st_tree) : kill($sig, -$pid) }
sub st_alive { my ($pid) = @_; @st_tree ? scalar(grep { kill 0, $_ } @st_tree) : kill(0, -$pid) }
'
# shellcheck disable=SC2016 # perl source, expanded by perl
ST_BOUNDED="$ST_GROUP_PERL"'use strict; use POSIX (); use Time::HiRes ();
my ($limit, $grace, @cmd) = @ARGV;
my $pid = fork;
defined $pid or do { print STDERR "cannot start $cmd[0]: $!\n"; exit 1 };
if (!$pid) {
  setpgrp(0, 0);
  exec { $cmd[0] } @cmd;
  print STDERR "cannot run $cmd[0]: $!\n";
  POSIX::_exit(127);
}
my $why = setpgrp($pid, $pid) || !$!{EPERM} ? "" : "$!";
my %number = (INT => POSIX::SIGINT(), TERM => POSIX::SIGTERM(), HUP => POSIX::SIGHUP());
my ($status, $caught);
# perl retries a waitpid a signal interrupts, so each handler dies to end it.
eval {
  local $SIG{ALRM} = sub { die "limit\n" };
  local @SIG{keys %number} = map {
    my $sig = $_;
    sub { $caught = $sig; print STDERR st_prepare($pid, $why); st_kill($sig, $pid); die "signal\n" }
  } keys %number;
  alarm $limit;
  $status = $? if waitpid($pid, 0) == $pid;
  alarm 0;
};
alarm 0;
if (defined $status) { exit($status & 127 ? 128 + ($status & 127) : $status >> 8) }
# Ending the group is not interrupted: a second TERM must not leave it half done.
$SIG{$_} = "IGNORE" for keys %number;
if (!defined $caught) {
  print STDERR "timed out after ${limit}s: @cmd\n", st_prepare($pid, $why);
  st_kill("TERM", $pid);
}
my $end = Time::HiRes::time() + $grace;
while (st_alive($pid)) {
  waitpid($pid, POSIX::WNOHANG());
  if (Time::HiRes::time() >= $end) { st_kill("KILL", $pid); last }
  Time::HiRes::sleep(0.1);
}
waitpid($pid, 0);
exit(defined $caught ? 128 + $number{$caught} : 124);'

# st_bounded SECONDS COMMAND...: run COMMAND; stop it after SECONDS.
# Exits with COMMAND's code, or 124 when the bound expired.
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

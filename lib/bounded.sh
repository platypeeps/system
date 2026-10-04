# Bounded steps for unattended jobs. Source it; it defines functions and one
# variable and runs nothing else.
#
#   . "$DIR/../lib/bounded.sh"
#   st_step 1800 brew upgrade
#
# On some nights macOS stops answering permission checks for launchd jobs, and
# a brew or mas call then hangs. It used to run until the job's limit killed
# the whole job, with nothing in the log to say which call it was (sd:2660).

# Perl, not timeout(1): macOS ships none, and Homebrew's coreutils is not on
# launchd's PATH. The command runs in a process group of its own, so the bound
# ends all of it; INT, TERM and HUP are passed on to that group, so a job's own
# limit still reaches it. On expiry: one line on stderr naming the command,
# TERM, then KILL after ST_BOUNDED_GRACE seconds (default 10), and exit 124,
# the code GNU timeout uses.
# shellcheck disable=SC2016 # perl source, expanded by perl
ST_BOUNDED='use strict; use POSIX (); use Time::HiRes ();
my ($limit, $grace, @cmd) = @ARGV;
my $pid = fork;
defined $pid or do { print STDERR "cannot start $cmd[0]: $!\n"; exit 1 };
if (!$pid) {
  setpgrp(0, 0);
  exec { $cmd[0] } @cmd;
  print STDERR "cannot run $cmd[0]: $!\n";
  POSIX::_exit(127);
}
setpgrp($pid, $pid);
for my $sig (qw(INT TERM HUP)) { $SIG{$sig} = sub { kill $sig, -$pid } }
my $status;
eval {
  local $SIG{ALRM} = sub { die "limit\n" };
  alarm $limit;
  while (1) {
    my $done = waitpid($pid, 0);
    if ($done == $pid) { $status = $?; last }
    last unless $!{EINTR};
  }
  alarm 0;
};
if (defined $status) { exit($status & 127 ? 128 + ($status & 127) : $status >> 8) }
print STDERR "timed out after ${limit}s: @cmd\n";
kill "TERM", -$pid;
my $end = Time::HiRes::time() + $grace;
while (kill 0, -$pid) {
  waitpid($pid, POSIX::WNOHANG());
  if (Time::HiRes::time() >= $end) { kill "KILL", -$pid; last }
  Time::HiRes::sleep(0.1);
}
waitpid($pid, 0);
exit 124;'

# st_bounded SECONDS COMMAND...: run COMMAND; stop it after SECONDS.
# Exits with COMMAND's code, or 124 when the bound expired.
st_bounded() {
    _st_seconds=$1
    shift
    perl -e "$ST_BOUNDED" "$_st_seconds" "${ST_BOUNDED_GRACE:-10}" "$@"
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

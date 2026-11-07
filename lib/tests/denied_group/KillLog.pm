# Test seam (sd:7901): in a perl started with
#   PERL5LIB=<this folder> PERL5OPT=-MKillLog KILL_LOG=<file>
# each kill with a signal appends "<signal> <target>" to <file>, then
# runs. With KILL_LOG_GROUP_ALIVE=1, kill(0, -PID) answers yes, as for a
# group member that is gone by the KILL that follows.
package KillLog;
BEGIN {
  *CORE::GLOBAL::kill = sub {
    my ($sig, @to) = @_;
    if ($sig eq "0") {
      return 1 if $ENV{KILL_LOG_GROUP_ALIVE} && @to == 1 && $to[0] < 0;
      return CORE::kill(0, @to);
    }
    if (open(my $log, ">>", $ENV{KILL_LOG})) { print $log "$sig $_\n" for @to; close $log }
    return CORE::kill($sig, @to);
  };
}
1;

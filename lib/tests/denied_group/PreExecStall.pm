# Test seam (sd:7901): in a perl started with
#   PERL5LIB=<this folder> PERL5OPT=-MPreExecStall PRE_EXEC_STALL=<when>:<file>
# the first child setpgrp(0, 0) to create <file> sleeps 4 s before
# (<when> = before) or after (<when> = after) its real call, as an exec
# on a stalled share does. Later calls, such as a report's bound, run as is.
package PreExecStall;
use Fcntl ();
BEGIN {
  *CORE::GLOBAL::setpgrp = sub {
    my ($pid, $pgrp) = (@_, 0, 0);
    my ($when, $file) = split /:/, $ENV{PRE_EXEC_STALL} || "", 2;
    my $stall = !$pid && $file
      && sysopen(my $fh, $file, Fcntl::O_WRONLY() | Fcntl::O_CREAT() | Fcntl::O_EXCL());
    sleep 4 if $stall && $when eq "before";
    my $ok = CORE::setpgrp($pid, $pgrp);
    sleep 4 if $stall && $when eq "after";
    return $ok;
  };
}
1;

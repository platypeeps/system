# Test seam (sd:7901): in a perl started with
#   PERL5LIB=<this folder> PERL5OPT=-MParentGroup
# a setpgrp that names another process prints a line to stderr, then runs.
# A parent's setpgrp($pid, $pid) raced the child's own call into EPERM.
package ParentGroup;
BEGIN {
  *CORE::GLOBAL::setpgrp = sub {
    my ($pid, $pgrp) = (@_, 0, 0);
    print STDERR "ParentGroup: setpgrp($pid, $pgrp) from $$\n" if $pid && $pid != $$;
    return CORE::setpgrp($pid, $pgrp);
  };
}
1;

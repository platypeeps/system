# Test seam (sd:7901): every setpgrp in a perl started with
#   PERL5LIB=<this folder> PERL5OPT=-MDeniedGroup
# fails with EPERM, as a sandbox that denies setpgid does. Only tests set it.
package DeniedGroup;
BEGIN { *CORE::GLOBAL::setpgrp = sub { $! = 1; return 0 } }
1;

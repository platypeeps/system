# Test seam (sd:7901): in a perl started with
#   PERL5LIB=<this folder> PERL5OPT=-MZombieGroup
# kill(0, -PID) also answers yes while process PID exists, an unreaped
# zombie included, as Linux answers for a group whose leader is a zombie.
package ZombieGroup;
BEGIN {
  *CORE::GLOBAL::kill = sub {
    my ($sig, @to) = @_;
    return CORE::kill($sig, @to) unless $sig eq "0" && @to == 1 && $to[0] < 0;
    return CORE::kill(0, $to[0]) || CORE::kill(0, -$to[0]);
  };
}
1;

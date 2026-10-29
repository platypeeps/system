# shellcheck shell=sh
# The cask claude's Gatekeeper quarantine, cleared only after its checks
# pass. Source it; it defines one function and one variable and runs nothing
# else.
#
#   cq_check() { st_step "$@"; }                       # SECONDS COMMAND...
#   cq_clear() { xattr -d com.apple.quarantine "$1"; } # PATH
#   . "$DIR/../lib/claude_quarantine.sh"
#   st_claude_quarantine "$(command -v claude)"
#
# Gatekeeper asks before the first run of a quarantined binary, and under
# launchd with the screen locked no one answers: a new claude-code@latest
# cask version held every agent job for 4.5 h (sd:4787). The operator's
# ruling: the flag goes only when the signature verifies, Gatekeeper calls the
# binary notarized and its team is Anthropic's, each checked on the path whose
# flag goes. A check that fails or does not answer keeps the flag and
# returns 1, so the caller reports it. A `claude` outside the cask is not ours.
#
# machine-setup runs it after each cask upgrade and in every brew stage; the
# cron runner runs it before it starts an agent, since a cask installed by
# hand stays quarantined until the next nightly (sd:4815). The caller defines
# cq_check, which runs one read-only check under its bound, and cq_clear,
# which removes the flag or, in a dry run, says it would. Findings go to
# stdout in machine-setup's status words, `ok` and `DIFFERS`.
CLAUDE_TEAM=Q6L2SF6YDW
st_claude_quarantine() { # binary
  _cq=$(readlink "$1" 2>/dev/null) || _cq=$1
  case $_cq in */Caskroom/claude-code@latest/*) ;; *) return 0 ;; esac
  if ! _cq_names=$(xattr "$_cq" 2>/dev/null); then
    echo "  DIFFERS claude quarantine unknown: xattr could not read $_cq"
    return 1
  fi
  if ! printf '%s\n' "$_cq_names" | grep -qx com.apple.quarantine; then
    echo "  ok      claude not quarantined"
    return 0
  fi
  if ! cq_check 120 codesign --verify --strict "$_cq" >/dev/null 2>&1; then
    _cq_why="the signature does not verify"
  elif ! _cq_out=$(cq_check 120 spctl --assess --type open --context context:primary-signature -v "$_cq" 2>&1) \
      || ! printf '%s\n' "$_cq_out" | grep -qx 'source=Notarized Developer ID'; then
    _cq_why="Gatekeeper did not say it is notarized"
  elif ! _cq_out=$(cq_check 120 codesign -dv "$_cq" 2>&1) \
      || ! printf '%s\n' "$_cq_out" | grep -qx "TeamIdentifier=$CLAUDE_TEAM"; then
    _cq_why="its team is not $CLAUDE_TEAM"
  else
    echo "  DIFFERS claude quarantined; signed, notarized, team $CLAUDE_TEAM: clearing"
    cq_clear "$_cq" && return 0
    # Jobs that share a slot race to clear it; a loser finds it gone (sd:6482).
    if _cq_names=$(xattr "$_cq" 2>/dev/null) \
        && ! printf '%s\n' "$_cq_names" | grep -qx com.apple.quarantine; then
      echo "  ok      claude quarantine cleared by another run"
      return 0
    fi
    _cq_why="xattr could not clear it"
  fi
  echo "  DIFFERS claude quarantine kept on $_cq: $_cq_why"
  return 1
}

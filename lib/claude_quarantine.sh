# shellcheck shell=sh
# The Gatekeeper quarantine of the claude and codex casks' binaries,
# cleared only after their checks pass. Source it; it defines functions and
# variables and runs nothing else.
#
#   cq_check() { st_step "$@"; }                       # SECONDS COMMAND...
#   cq_clear() { xattr -d com.apple.quarantine "$1"; } # PATH
#   . "$DIR/../lib/claude_quarantine.sh"
#   st_claude_quarantine "$(command -v claude)"
#   st_codex_quarantine "$(command -v codex)"
#
# Gatekeeper asks before the first run of a quarantined binary, and under
# launchd with the screen locked no one answers: a new claude-code@latest
# cask version held every agent job for 4.5 h (sd:4787). The operator's
# ruling: the flag goes only when the signature verifies, Gatekeeper calls the
# binary notarized and its team is Anthropic's, each checked on the path whose
# flag goes. A check that fails or does not answer keeps the flag and
# returns 1, so the caller reports it. A `claude` outside the cask is not ours.
# The codex cask's bin/ files carry the flag after each upgrade too, and
# Gatekeeper blocked codex-code-mode-host (sd:7095): the same rule clears each
# file in the bin/ that `codex` links into, with OpenAI's team. The file name
# keeps claude's, since cron-jobs.sh sources it by that name.
#
# machine-setup runs it after each cask upgrade and in every brew stage; the
# cron runner runs it before it starts an agent, since a cask installed by
# hand stays quarantined until the next nightly (sd:4815). The caller defines
# cq_check, which runs one read-only check under its bound, and cq_clear,
# which removes the flag or, in a dry run, says it would. Findings go to
# stdout in machine-setup's status words, `ok` and `DIFFERS`, each led by
# the binary's file name.
CLAUDE_TEAM=Q6L2SF6YDW
CODEX_TEAM=2DC432GLL2
st_claude_quarantine() { # binary
  st_cask_quarantine claude-code@latest "$CLAUDE_TEAM" "$1"
}
st_codex_quarantine() { # binary
  _cx=$(readlink "$1" 2>/dev/null) || return 0
  case $_cx in */Caskroom/codex/*/bin/*) ;; *) return 0 ;; esac
  _cx_rc=0
  for _cx_f in "${_cx%/*}"/*; do
    [ -f "$_cx_f" ] || continue
    st_cask_quarantine codex "$CODEX_TEAM" "$_cx_f" || _cx_rc=1
  done
  return $_cx_rc
}
st_cask_quarantine() { # cask team binary
  _cq=$(readlink "$3" 2>/dev/null) || _cq=$3
  case $_cq in */Caskroom/"$1"/*) ;; *) return 0 ;; esac
  _cq_team=$2
  _cq_name=${_cq##*/}
  if ! _cq_names=$(xattr "$_cq" 2>/dev/null); then
    echo "  DIFFERS $_cq_name quarantine unknown: xattr could not read $_cq"
    return 1
  fi
  if ! printf '%s\n' "$_cq_names" | grep -qx com.apple.quarantine; then
    echo "  ok      $_cq_name not quarantined"
    return 0
  fi
  if ! cq_check 120 codesign --verify --strict "$_cq" >/dev/null 2>&1; then
    _cq_why="the signature does not verify"
  elif ! _cq_out=$(cq_check 120 spctl --assess --type open --context context:primary-signature -v "$_cq" 2>&1) \
      || ! printf '%s\n' "$_cq_out" | grep -qx 'source=Notarized Developer ID'; then
    _cq_why="Gatekeeper did not say it is notarized"
  elif ! _cq_out=$(cq_check 120 codesign -dv "$_cq" 2>&1) \
      || ! printf '%s\n' "$_cq_out" | grep -qx "TeamIdentifier=$_cq_team"; then
    _cq_why="its team is not $_cq_team"
  else
    echo "  DIFFERS $_cq_name quarantined; signed, notarized, team $_cq_team: clearing"
    cq_clear "$_cq" && return 0
    # Jobs that share a slot race to clear it; a loser finds it gone (sd:6482).
    if _cq_names=$(xattr "$_cq" 2>/dev/null) \
        && ! printf '%s\n' "$_cq_names" | grep -qx com.apple.quarantine; then
      echo "  ok      $_cq_name quarantine cleared by another run"
      return 0
    fi
    _cq_why="xattr could not clear it"
  fi
  echo "  DIFFERS $_cq_name quarantine kept on $_cq: $_cq_why"
  return 1
}

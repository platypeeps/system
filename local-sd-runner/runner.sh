#!/bin/sh
# One public entrypoint; launcher credentials never flow wholesale to providers.
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
case "${1:-}" in
  -h|--help|help)
    echo 'runner.sh serve|once|status|preflight|install-plan|prune|prune-apply|retained-remove|discard-plan|archive-plan|archive-refresh|recovery-plan|recovery-reconcile|recovery-quarantine|recovery-unlink|commands|cancel|resume|restore|restore-status|test [--config FILE]'
    echo 'prune and discard-plan are read-only plans. prune-apply --fingerprint FP --who NAME removes only the retained clones that plan lists.'
    echo 'retained-remove --assignment N --who NAME removes one released assignment'"'"'s retained copy, early, with the operator'"'"'s name.'
    echo 'retained-remove --clone-only removes only each attempt'"'"'s clone, and keeps kept.tar, archives/, ignored/ and the directories.'
    echo 'status exits 0 healthy, 3 when the <prefix>.sd-runner agent (prefix SYSTEM_TOOLS_LABEL_PREFIX, default local.system-tools) is not loaded (nothing to check, even without a runtime), 1 stale, unhealthy, or loaded without a runtime; local-health-check reads these codes'
    exit 0 ;;
  '') echo 'usage: runner.sh serve|once|status|preflight|install-plan|prune|test' >&2; exit 1 ;;
  test|check)
    shift
    # As `local-sd-db/sd-db.sh` does: the ship lifecycle test skips without a
    # pack, and CI names its own. Never over an explicit SD_ACCEPTANCE_PACK.
    pack="$HOME/repos/platypeeps/sd-ai-command-pack"
    if [ -z "${SD_ACCEPTANCE_PACK:-}" ] && [ -f "$pack/bin/sd-ship" ]; then
      SD_ACCEPTANCE_PACK="$pack"; export SD_ACCEPTANCE_PACK
    fi
    PYTHONPATH="$DIR:$DIR/../local-sd-db${PYTHONPATH:+:$PYTHONPATH}" exec "${PYTHON:-python3}" -m unittest discover -s "$DIR/tests" -t "$DIR" "$@" ;;
  status)
    # A missing interpreter is answered here, before the runtime guard below,
    # which exits 1 for every verb. Which code depends on whether launchd
    # holds the agent, the same question `cli.agent_loaded` asks: not loaded
    # is 3, nothing to check, because a machine that never provisioned the
    # pack raised a nightly finding nobody could act on (sd:1237); loaded is
    # 1, because an installed runner whose virtualenv was rebuilt or whose
    # Python was upgraded cannot start, and that is the finding. The body is
    # one line on stdout, the shape `cli.py` prints, so the sweep's `head -1`
    # names the reason. env.sh is read first, as `serve` reads it, so both
    # verbs answer for the same interpreter.
    [ ! -f "$HOME/.config/shell/env.sh" ] || . "$HOME/.config/shell/env.sh"
    runtime_python="${SD_RUNNER_PYTHON:-$HOME/repos/platypeeps/sd-ai-command-pack/.venv/bin/python}"
    if [ ! -x "$runtime_python" ]; then
      echo "{\"ok\": false, \"reason\": \"runner runtime is not provisioned: $runtime_python\"}"
      LABEL_PREFIX="${SYSTEM_TOOLS_LABEL_PREFIX:-local.system-tools}"
      if launchctl print "gui/$(id -u)/$LABEL_PREFIX.sd-runner" >/dev/null 2>&1; then
        exit 1
      fi
      exit 3
    fi ;;
  serve|once)
    # env.sh may set SD_RUNNER_PYTHON, so the interpreter resolves after it.
    [ ! -f "$HOME/.config/shell/env.sh" ] || . "$HOME/.config/shell/env.sh" ;;
esac
runtime_python="${SD_RUNNER_PYTHON:-$HOME/repos/platypeeps/sd-ai-command-pack/.venv/bin/python}"
if [ ! -x "$runtime_python" ]; then
  echo "runner: missing installed runtime $runtime_python; provision the command pack first" >&2
  exit 1
fi
exec "$runtime_python" -I "$DIR/sd_runner/bootstrap.py" "$@"

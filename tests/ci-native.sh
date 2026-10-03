#!/bin/bash
# The native suites: a preflight, then the run_suite lines of one leg.
#
# tests/check.sh, which the root Makefile's `check` target runs and the local
# merge gate runs through sd-check, calls it for every leg on this machine.
# The caller starts it under `env -i` with an isolated HOME and TMPDIR and
# with CI_WORK_ROOT, CI_SYSTEM_ROOT, SD_ACCEPTANCE_PACK, SD_WRITING_MANIFEST
# and SD_PR_BODY set. Bash, not POSIX sh: `pipefail` is what fails a suite
# whose output goes through tee.
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: ci-native.sh preflight
       ci-native.sh leg shared|dashboard|runner|tools

  preflight  the fixture digest, sd-docs-lint, the citation, Jev contract
             and product-name gates, the unwired-suite guard, and the venv at
             $CI_WORK_ROOT/venv with local-sd-db installed
  leg NAME   every run_suite line of one leg, under the preflight's venv

Run it through tests/check.sh (or `make check`) on a workstation; it expects
the isolated environment that script builds.
USAGE
}

case "${1:-}" in
  -h|--help|help) usage; exit 0 ;;
  preflight) [ "$#" -eq 1 ] || { usage >&2; exit 1; } ;;
  leg) [ "$#" -eq 2 ] || { usage >&2; exit 1; }; SUITE_LEG="$2" ;;
  *) usage >&2; exit 1 ;;
esac

cd "$CI_SYSTEM_ROOT"
python3 -c 'import sys; assert sys.version_info[:2] == (3, 14), sys.version'
test -f "$SD_ACCEPTANCE_PACK/bin/sd-ship"
test -f "$SD_ACCEPTANCE_PACK/bin/sd-docs-lint"
test -f "$SD_WRITING_MANIFEST"

if [ "$1" = leg ]; then
  # The preflight built it; a leg never builds its own.
  export PYTHON="$CI_WORK_ROOT/venv/bin/python"
  test -x "$PYTHON"
else
# The preflight runs to the `fi` above
# run_suite.

python3 - <<'PY_VERIFY'
import hashlib
import json
import os
from pathlib import Path
fixture = Path(os.environ["SD_WRITING_MANIFEST"])
source = json.loads(fixture.with_suffix(".source.json").read_text())
if hashlib.sha256(fixture.read_bytes()).hexdigest() != source["sha256"]:
    raise SystemExit("writing manifest fixture differs from its pinned source digest")
json.loads(fixture.read_text())
print("Verified writing manifest from " + source["repository"] + "@" + source["commit"])
PY_VERIFY

# The docs/work gate, which nothing ran before #238. After #230 main
# carried 165 rule 6 failures for five days with every check green;
# a human running this by hand is what found them. It takes no
# flags on purpose: since R10-D6 the working directory selects the
# repository, and --work-dir defaults to docs/work and is
# repo-relative. Passing an absolute path here would leave rule 7
# matching nothing and reporting a pass (sd-ai-command-pack#809).
# It runs before the suites because it takes under a second and the
# suites take minutes. It cannot go through run_suite, which
# requires a unittest summary; `set -e` is the gate.
#
# --pr-body is the one flag it does take, and only on a pull
# request. It is rule 5: exactly one `Work:` line, naming either a
# docs/work item or a database row as `sd:<positive integer>`. A
# body with no Work: line is a note and not a failure, so this can
# be true of every PR before it is true of every author.
#
# The lint refuses a repository without docs/work, and this one keeps
# planning documents only when a change needs them. So it runs when
# the folder exists and is skipped, with a note, when it does not.
if [ ! -d docs/work ]; then
  echo "sd-docs-lint: no docs/work in this repository; not run"
elif [ -n "${SD_PR_BODY:-}" ]; then
  "$SD_ACCEPTANCE_PACK/bin/sd-docs-lint" --pr-body "$SD_PR_BODY"
else
  "$SD_ACCEPTANCE_PACK/bin/sd-docs-lint"
fi

# The other half of the citation gate (sd:828): a `path:line` into
# code, in a tracked .md, .py or .sh outside docs/work/archive/,
# fails unless it is carried in the ratchet; an anchor is cited
# instead. Stdlib and
# git only, so it runs here before the venv exists, and it is at
# the repository root, so the unwired-suite guard below demands a
# python3 line for it -- delete this one and the guard fails.
python3 tests/test_citations.py

# Who calls Jev, and whether each one degrades. Two layers: the
# folder inventory is read from the filesystem, so a twelfth caller
# cannot be added silently; the rules then ban a fallback that reads
# as a judgment -- the shape that put `--fallback unsure` next to
# what `--unsure-below` itself prints. Stdlib and git only, so it
# runs here before the venv, and the guard below demands this line.
python3 tests/test_jev_contract.py

# Only the vendor helper folders, mezmo-*, name the product they help
# with (sd:2535): a tracked line outside them that does fails here
# naming its path and line, unless the file's ALLOWED entry gives a
# reason. Stdlib and git only, so it runs here before the venv, and
# the guard below demands this line.
python3 tests/test_product_name.py

# The run_suite lines at the bottom are a hand-maintained list, and
# a folder that grows a suite without a line here is never run --
# it stays green, which is the failure mode this repository already
# knows (sd:404). So enumerate the filesystem instead of trusting
# the list: every folder holding tests/test_*.py must be named by a
# run_suite line, and this fails naming each one that is not. It
# keys on the tests directory and not on a subcommand, because no
# verb is common to all of them: local-sd-db answers `check` and
# local-repo-sync answers `test` because `check` was already taken.
# Like the lint, it belongs in the preflight and not behind
# run_suite, which asserts a unittest summary this prints none of.
#
# A macOS-only suite cannot run here, so tests/macos-only-suites.txt
# names it instead, in the same `<folder>/` form, and
# tests/run-macos-only.sh runs it on a Mac. That list is the one
# other place a folder counts as wired. A tests/macos/ folder of
# test_*.py is never discovered by its folder's suite, so it must be
# named there too.
script=tests/ci-native.sh
macos_only=tests/macos-only-suites.txt
# A folder or file name is a literal inside the patterns below, so
# escape what an extended regular expression would read: `c+d/`
# would otherwise miss its own line, and `a.b/` match `axb/`.
ere_literal() { printf '%s' "$1" | sed 's/[][\\.*^$+?(){}|]/\\&/g'; }
seen=" "
unwired=""
for test_file in */tests/test_*.py; do
  [ -e "$test_file" ] || continue
  folder="${test_file%%/*}"
  case "$seen" in *" $folder "*) continue ;; esac
  seen="$seen$folder "
  if ! grep -Eq "^[[:space:]]*run_suite .*[[:space:]]$(ere_literal "$folder")/" "$script" &&
     ! grep -Eq "^[^#[:space:]]+ .*[[:space:]]$(ere_literal "$folder")/" "$macos_only"; then
    unwired="$unwired $folder"
  fi
done
for test_file in */tests/macos/test_*.py; do
  [ -e "$test_file" ] || continue
  folder="${test_file%%/*}"
  case "$seen" in *" $folder/macos "*) continue ;; esac
  seen="$seen$folder/macos "
  if ! grep -Eq "^[^#[:space:]]+ .*[[:space:]]$(ere_literal "$folder")/" "$macos_only"; then
    unwired="$unwired $folder/tests/macos"
  fi
done
# A stale line would count a folder as wired that runs nothing, so
# every path a macOS-only command names must exist.
while read -r suite command; do
  case "$suite" in ''|'#'*) continue ;; esac
  for word in $command; do
    case "$word" in
      */*) [ -e "$word" ] || { echo "$macos_only: $suite names $word, which does not exist" >&2; exit 1; } ;;
    esac
  done
done < "$macos_only"
# A suite at the repository root has no folder and no run_suite
# line: it runs here in the preflight as a bare `python3 <file>`
# line, so demand that line for each one, or the file above could
# be deleted from this step and its own wiring test would go with
# it.
for test_file in tests/test_*.py; do
  [ -e "$test_file" ] || continue
  if ! grep -Eq "^[[:space:]]*python3 $(ere_literal "$test_file")([[:space:]]|$)" "$script"; then
    unwired="$unwired $test_file"
  fi
done
if [ -n "$unwired" ]; then
  for entry in $unwired; do
    case "$entry" in
      tests/*) echo "$entry: a root suite but no python3 line in $script runs it" >&2 ;;
      */tests/macos) echo "$entry: holds test_*.py but no line in $macos_only names its folder" >&2 ;;
      *) echo "$entry: holds tests/test_*.py but no run_suite line in $script or line in $macos_only names it" >&2 ;;
    esac
  done
  exit 1
fi

python3 -m venv --copies "$CI_WORK_ROOT/venv"
export PYTHON="$CI_WORK_ROOT/venv/bin/python"
"$PYTHON" -m pip install --no-index ./local-sd-db
# Ship acceptance needs an installed copy, not source on PYTHONPATH.
"$PYTHON" -I -c 'import pathlib, sd_db, sys; assert pathlib.Path(sd_db.__file__).is_relative_to(pathlib.Path(sys.prefix))'
exit 0
fi

# SUITE_SHARD=i/n runs only every n-th suite of the leg, starting at the
# i-th, so tests/check.sh can split the long tools leg across processes.
# Unset, every suite runs.
shard_index="${SUITE_SHARD:-}"; shard_index="${shard_index%/*}"
shard_count="${SUITE_SHARD:-}"; shard_count="${shard_count#*/}"
case "${SUITE_SHARD:-}" in
  '') shard_index=1; shard_count=1 ;;
  [1-9]/[1-9]) [ "$shard_index" -le "$shard_count" ] || { echo "SUITE_SHARD=$SUITE_SHARD: i must not exceed n" >&2; exit 1; } ;;
  *) echo "SUITE_SHARD=$SUITE_SHARD: want i/n, single digits" >&2; exit 1 ;;
esac
suite_ordinal=0
mine() {
  suite_ordinal=$((suite_ordinal + 1))
  [ $(( (suite_ordinal - 1) % shard_count + 1 )) -eq "$shard_index" ]
}

run_suite() {
  mine || return 0
  suite="$1"
  shift
  "$@" 2>&1 | tee "$CI_WORK_ROOT/$suite.log"
  if grep -Eq 'skipped=[1-9][0-9]*' "$CI_WORK_ROOT/$suite.log"; then
    echo "$suite: skipped tests are not permitted in native CI" >&2
    return 1
  fi
  if ! grep -Eq '^Ran [1-9][0-9]* tests? in ' "$CI_WORK_ROOT/$suite.log"; then
    echo "$suite: no unittest execution summary" >&2
    return 1
  fi
}

# The run_suite lines stay literal, one per folder, inside a case on
# the leg: the unwired-suite guard above greps for them, and a suite
# named in a variable or a loop would be one it cannot see. A leg
# that names nothing is a typo in the matrix, and fails as one.
case "$SUITE_LEG" in
  shared)
    # One leg, not four: the manifest is the same in every leg. The cron
    # job that runs `plugin lock --check` gates the `print` plugin, not
    # this repository's own manifest, so nothing else reads this lock.
    python3 "$SD_ACCEPTANCE_PACK/bin/sd" plugin lock --check .
    run_suite shared sh local-sd-db/sd-db.sh check
    ;;
  dashboard)
    run_suite dashboard sh local-project-dashboard/dashboard.sh test
    ;;
  runner)
    run_suite runner sh local-sd-runner/runner.sh test -v
    ;;
  tools)
    run_suite repo-sync sh local-repo-sync/repo-sync.sh test -v
    run_suite bin-links sh local-bin-links/bin-links.sh test -v
    run_suite agent-prompt sh local-agent-prompt/agent-prompt.sh test -v
    run_suite sd-plan sh local-sd-plan/sd-plan.sh test -v
    run_suite health-check sh local-health-check/health-check.sh test -v
    run_suite cron-jobs sh local-cron-jobs/cron-jobs.sh test -v
    run_suite dependabot sh local-dependabot/dependabot.sh test -v
    run_suite herdr sh local-herdr/herdr.sh test -v
    run_suite adversarial-gate sh local-adversarial-gate/adversarial-gate.sh test -v
    run_suite aws-setup sh local-aws-setup/aws-setup.sh test -v
    run_suite ai-apps sh local-ai-apps/ai-apps.sh test -v
    run_suite jev sh local-jev/jev.sh test -v
    run_suite kev sh local-kev/kev.sh test -v
    run_suite leak-guard sh local-leak-guard/leak-guard.sh test -v
    run_suite claude sh local-claude/claude.sh test -v
    run_suite drive-intake sh local-drive-intake/drive-intake.sh test -v
    run_suite mail-intake sh local-mail-intake/mail-intake.sh test -v
    run_suite maintenance sh local-maintenance/maintenance.sh test -v
    run_suite opentelemetry-collector sh local-opentelemetry-collector/opentelemetry-collector.sh test -v
    run_suite genai-traces sh local-genai-traces/genai-traces.sh test -v
    run_suite aura sh local-aura/aura.sh test -v
    # local-mirror-sync has no test verb of its own: mirror-sync.sh
    # takes sync|plan|list and adding a fourth would change what a
    # pair list means. The suite is named directly, like notify
    # above, and the unwired-suite guard only asks that a run_suite
    # line name the folder -- which this does.
    run_suite mirror-sync "$PYTHON" local-mirror-sync/tests/test_mirror_sync.py -v
    # scan-for-secrets.sh reads its first word as a scan mode, so a
    # `test` verb would be one more mode; the suite is named directly.
    run_suite scan-for-secrets "$PYTHON" local-scan-for-secrets/tests/test_scan_for_secrets.py -v
    # notify.sh takes a message, not a subcommand: adding a
    # `test` verb would change what `notify.sh test` does today,
    # which is send a notification whose body reads "test". So this
    # one line names the suite directly instead of an entrypoint.
    # The guard above only asks that a run_suite line name the
    # folder, which this does.
    run_suite notify "$PYTHON" local-notify/tests/test_notify.py -v
    run_suite notify-email "$PYTHON" local-notify/tests/test_email_retry.py -v
    # statusline.sh takes render|install and is what Claude Code
    # calls on every refresh; the suite is named directly rather than
    # adding a verb to it. It stubs bun and sysctl, so Linux runs it.
    run_suite statusline "$PYTHON" local-statusline/tests/test_statusline.py -v
    # cswap.sh has no test verb; the suite is named directly, like
    # statusline above. It stubs launchctl, so Linux runs it.
    run_suite cswap "$PYTHON" local-cswap/tests/test_cswap.py -v
    run_suite obsidian-tasks sh local-obsidian-tasks/obsidian-tasks.sh test
    run_suite obsidian-review sh local-obsidian-review/obsidian-review.sh test
    run_suite task-actions sh local-task-actions/task-actions.sh test -v
    run_suite weekly-digest sh local-weekly-digest/weekly-digest.sh test -v
    run_suite workspace-mcp sh local-workspace-mcp/workspace-mcp.sh test -v
    run_suite network-testing sh network-testing/network-testing.sh test -v
    run_suite lib "$PYTHON" lib/tests/test_config.py -v
    run_suite machine-setup sh local-machine-setup/machine-setup.sh test -v
    run_suite mock-mcp sh local-mock-mcp/mock-mcp.sh test -v
    run_suite fluentbit sh local-fluentbit/fluentbit.sh test -v
    run_suite opentelemetry-demo sh local-opentelemetry-demo/opentelemetry-demo.sh test -v
    run_suite worldmonitor sh local-worldmonitor/worldmonitor.sh test -v
    run_suite ai-songs-backup sh local-ai-songs-backup/ai-songs-backup.sh test -v
    run_suite github-rulesets sh github-rulesets/github-rulesets.sh test -v
    run_suite config-check sh local-config-check/config-check.sh test -v
    # mezmo-pipeline's suite is shell with its own tally, not
    # unittest, so run_suite's summary check cannot read it. It
    # stubs curl and makes no network call; the gate is a nonzero
    # pass count and zero failures on its last line.
    if mine; then
      sh mezmo-pipeline/pipeline.sh test 2>&1 | tee "$CI_WORK_ROOT/mezmo-pipeline.log"
      grep -Eq '^[1-9][0-9]* passed, 0 failed$' "$CI_WORK_ROOT/mezmo-pipeline.log"
    fi
    ;;
  *)
    echo "ci-native.sh: no suites are wired for leg '$SUITE_LEG'" >&2
    exit 1
    ;;
esac

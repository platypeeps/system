"""A brew or mas step that hangs names itself and ends at its bound (sd:2660).

On some nights macOS stops answering permission checks for launchd jobs, and
a brew call then hangs. upgrade-report captures the sweep's output for its
mail, so the job log said nothing until the job's two-hour limit killed it.

The script runs from a copy of this folder under a temporary root, beside a
copy of lib/ and a stub notify.sh that only logs, so no mail leaves. `brew`
and `mas` are stubs on PATH.
"""

import json
import os
import pathlib
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import time
import unittest

from tests import fixture_config
from tests.fixture_config import STALL, STALL_BOUND

HERE = pathlib.Path(__file__).resolve().parent
FOLDER = HERE.parent
LIB = FOLDER.parent / "lib"

# One formula is outdated, so upgrade-report runs the sweep. With BREW_HANG
# set, `brew upgrade` never finishes; with BREW_QUIET, nothing is outdated.
# BREW_GREEDY_CASK names a cask with `auto_updates true`: brew lists it as
# outdated only when asked with --greedy; with BREW_CASKS_DONE, an
# `upgrade --cask --greedy` clears it (sd:7058 names the casks it upgrades). STUB_PINNED names an outdated
# formula pinned by hand, which machine-setup no longer reads (sd:6433).
# BREW_OUTDATED renames the outdated formula; BREW_UPGRADES_ALL makes
# `brew upgrade` clear it; BREW_CLEANUP_FAIL fails `brew cleanup`.
# CLAUDE_LINK and CLAUDE_NEW: `brew upgrade --cask --greedy` relinks the
# claude cask's binary to its new Caskroom path (sd:4738).
# BREW_FAIL names one call, its arguments as logged, that exits 1. BREW_SUDO
# names one call that prints what sudo prints with no terminal and exits 1
# (sd:5046); with STUB_SUDO_HANG it then stalls. MAS_SUDO is mas's. A stall
# ends its bound at once (fixture_config.STALL); with BREW_WAIT, `brew
# upgrade` waits for a signal instead. BREW_SLOW names one call that answers
# 4 s late, past the 2 s bound these cases had: a loaded machine (sd:6253).
# BREW_PKG names a cask whose installer calls sudo (sd:6312): upgrading it
# needs SUDO_ASKPASS to print STUB_PASSWORD, or it refuses as sudo does with
# no terminal. Each upgrade call made with SUDO_ASKPASS set is logged in
# $BREW_LOG.askpass; an upgraded BREW_PKG leaves the outdated list.
# BREW_INFO is what `brew info --json=v2 --installed --cask` prints.
# BREW_BUILD_FAIL names one call that prints BREW_BUILD_TEXT on stderr and
# exits 1, as a formula whose build fails does (sd:7056).
SUDO_REFUSED = r"""
if [ "$*" = "$STUB_SUDO" ]; then
  echo "sudo: a terminal is required to read the password; either use the -S option to read from standard input or configure an askpass helper" >&2
  echo "sudo: a password is required" >&2
  [ -z "$STUB_SUDO_HANG" ] || stall
  exit 1
fi
"""

BREW_STUB = "#!/bin/sh\n" + STALL + r"""echo "$*" >> "$BREW_LOG"
[ "$*" != "$BREW_SLOW" ] || sleep 4
[ "$*" != "$BREW_FAIL" ] || exit 1
[ "$*" != "$BREW_BUILD_FAIL" ] || { printf '%s\n' "$BREW_BUILD_TEXT" >&2; exit 1; }
""" + SUDO_REFUSED.replace("STUB_SUDO\"", "BREW_SUDO\"") + r"""
case "$*" in
  "upgrade --cask"*) [ -z "$SUDO_ASKPASS" ] || echo "$*" >> "$BREW_LOG.askpass" ;;
esac
case "$*" in
  "upgrade --cask --greedy"|"upgrade --cask --greedy $BREW_PKG")
    if [ -n "$BREW_PKG" ] && [ ! -e "$BREW_LOG.pkg" ]; then
      if [ -n "$SUDO_ASKPASS" ] && [ "$("$SUDO_ASKPASS" "Password:")" = "$STUB_PASSWORD" ]; then
        : > "$BREW_LOG.pkg"
      else
        echo "sudo: a terminal is required to read the password; either use the -S option to read from standard input or configure an askpass helper" >&2
        echo "sudo: a password is required" >&2
        exit 1
      fi
    fi ;;
esac
case "$*" in
  "outdated --formula --quiet") [ -n "$BREW_QUIET" ] || [ -e "$BREW_LOG.upgraded" ] || echo "${BREW_OUTDATED:-example-formula}"
                                [ -z "$STUB_PINNED" ] || echo "$STUB_PINNED" ;;
  "list --pinned") [ -z "$STUB_PINNED" ] || echo "$STUB_PINNED" ;;
  "outdated --cask --greedy --quiet") [ -z "$BREW_GREEDY_CASK" ] || [ -e "$BREW_LOG.casks" ] || echo "$BREW_GREEDY_CASK"
                                     [ -z "$BREW_PKG" ] || [ -e "$BREW_LOG.pkg" ] || echo "$BREW_PKG" ;;
  "upgrade --cask --greedy"*) [ -z "$CLAUDE_NEW" ] || ln -sf "$CLAUDE_NEW" "$CLAUDE_LINK"
                              [ -z "$BREW_CASKS_DONE" ] || : > "$BREW_LOG.casks" ;;
  --cellar) [ -z "$BREW_CELLAR" ] || echo "$BREW_CELLAR" ;;
  --prefix) echo "${BREW_PREFIX:-/opt/homebrew}" ;;
  "info --json=v2 --installed --cask") if [ -n "$BREW_INFO" ]; then echo "$BREW_INFO"; else echo '{"casks": []}'; fi ;;
  "cleanup --prune=all") [ -z "$BREW_CLEANUP_FAIL" ] || exit 1 ;;
  upgrade) [ -z "$BREW_STARTED" ] || : > "$BREW_STARTED"
           [ -z "$BREW_UPGRADES_ALL" ] || : > "$BREW_LOG.upgraded"
           [ -z "$BREW_NEW_PYTHON" ] || mkdir -p "$BREW_NEW_PYTHON"
           [ -z "$BREW_WAIT" ] || exec sleep 60
           [ -z "$BREW_HANG" ] || stall ;;
esac
exit 0
"""

# MAS_OUTDATED is what `mas outdated` prints.
MAS_STUB = r"""#!/bin/sh
echo "$*" >> "$MAS_LOG"
[ "$*" != "$MAS_FAIL" ] || exit 1
""" + SUDO_REFUSED.replace("STUB_SUDO\"", "MAS_SUDO\"") + r"""
[ "$*" != outdated ] || [ -z "$MAS_OUTDATED" ] || echo "$MAS_OUTDATED"
exit 0
"""

# One block per call, each ended by a "--" line. NOTIFY_FAIL fails every send.
NOTIFY_STUB = r"""#!/bin/sh
printf '%s\n' "$@" -- >> "$NOTIFY_LOG"
[ -z "$NOTIFY_FAIL" ]
"""

# The vault-grant probe. GRANTS_OUT is what it prints, GRANTS_RC its exit.
DASHBOARD_STUB = r"""#!/bin/sh
[ "$1" = grants ] || exit 2
echo "grants" >> "$BREW_LOG"
printf '%s\n' "${GRANTS_OUT:-DASHBOARD_PYTHON python3: ok, lists the vault}"
exit "${GRANTS_RC:-0}"
"""

# The AI-app inventory capture. AI_APPS_OUT is what it prints, AI_APPS_RC its
# exit; each call is logged into brew's log, so one list holds the order.
AI_APPS_STUB = r"""#!/bin/sh
echo "ai-apps $*" >> "$BREW_LOG"
printf '%s\n' "${AI_APPS_OUT:-  none}"
exit "${AI_APPS_RC:-0}"
"""

# `security` with the askpass item when STUB_KEYCHAIN holds its password;
# without it, exit 44 (no item), or SECURITY_RC. `-w` prints the password.
SECURITY_STUB = r"""#!/bin/sh
echo "$*" >> "$SECURITY_LOG"
[ -z "$SECURITY_RC" ] || exit "$SECURITY_RC"
[ -n "$STUB_KEYCHAIN" ] || exit 44
case " $* " in *" -w "*) echo "$STUB_KEYCHAIN" ;; esac
exit 0
"""

REFUSED = ("SD_DASHBOARD_PYTHON /opt/homebrew/bin/python3: cannot read /vault — macOS is asking for "
           "Documents access and nothing under launchd can answer the prompt. Grant Full Disk "
           "Access to /opt/homebrew/bin/python3 in System Settings > Privacy & Security, then "
           "restart the agent.")

OMLX_LINE = "Error: An exception occurred within a child process: SystemExit: exit"
OMLX_TEXT = "==> Upgrading omlx\nError: An exception occurred within a child process:\n  SystemExit: exit\n"
SUDO_TEXT = ("sudo: a terminal is required to read the password; either use the -S option to read from "
             "standard input or configure an askpass helper\nsudo: a password is required\n"
             "Error: Failure while executing; `/usr/bin/sudo -E -- /bin/rm -f` exited with 1.\n")

STEP = r"\[step \d\d:\d\d:\d\d\] {} \(bound {}s\)"


def write_exec(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class UpgradeStepTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = pathlib.Path(self.tmp.name)
        self.tmp_path = base
        repo = base / "repo"
        self.folder = repo / "local-machine-setup"
        shutil.copytree(FOLDER, self.folder, ignore=shutil.ignore_patterns("tests", "__pycache__"))
        shutil.copytree(LIB, repo / "lib", ignore=shutil.ignore_patterns("tests", "__pycache__"))
        write_exec(repo / "local-notify/notify.sh", NOTIFY_STUB)
        # The dashboard is the pack's (pack sd:5987), at SD_PACK_ROOT's default.
        write_exec(base / "home/repos/platypeeps/sd-ai-command-pack/dashboard/dashboard.sh", DASHBOARD_STUB)
        write_exec(repo / "local-ai-apps/ai-apps.sh", AI_APPS_STUB)
        self.home = base / "home"
        self.state = self.home / ".config/machine-setup"
        self.state.mkdir(parents=True)
        self.brew_log = base / "brew.log"
        self.mas_log = base / "mas.log"
        self.notify_log = base / "notify.log"
        self.stubs = base / "stubs"
        write_exec(self.stubs / "brew", BREW_STUB)
        write_exec(self.stubs / "mas", MAS_STUB)
        fixture_config.seal(self, self.stubs)
        self.cask_move = {}

    def env(self, **extra):
        return {
            "HOME": str(self.home),
            "MACHINE_SETUP_STATE": str(self.state),
            "PATH": f"{self.stubs}:/usr/bin:/bin:/usr/sbin:/sbin",
            "LANG": "en_US.UTF-8",
            **fixture_config.env(),
            "BREW_LOG": str(self.brew_log),
            "MAS_LOG": str(self.mas_log),
            "NOTIFY_LOG": str(self.notify_log),
            "ST_BOUNDED_GRACE": "1",
            **self.cask_move,
            **extra,
        }

    def run_verb(self, *args, **extra):
        return subprocess.run([str(self.folder / "machine-setup.sh"), *args],
                              env=self.env(**extra), capture_output=True, text=True,
                              cwd=self.tmp.name, stdin=subprocess.DEVNULL, timeout=90)

    def sends(self):
        """Each notify call's arguments, one list per call."""
        if not self.notify_log.exists():
            return []
        calls, call = [], []
        for line in self.notify_log.read_text().splitlines():
            if line == "--":
                calls.append(call)
                call = []
            else:
                call.append(line)
        return calls

    def attention(self, result):
        """The SD_REPORT_ATTENTION reasons the run printed for its report (sd:3454)."""
        return [line.split(": ", 1)[1] for line in result.stdout.splitlines()
                if line.startswith("SD_REPORT_ATTENTION: ")]

    def findings(self, result):
        """Each attention line's finding: one line per finding, each `upgrade on <host>: ` (sd:4664)."""
        lines = self.attention(result)
        for line in lines:
            self.assertRegex(line, r"^upgrade on \S+: \S")
        return [line.split(": ", 1)[1] for line in lines]

    def claude_moves_to(self, old, new):
        """The claude-code@latest cask: `claude` on PATH links into the Caskroom
        version folder OLD, and `brew upgrade --cask --greedy` relinks it to NEW
        (sd:4738)."""
        caskroom = self.tmp_path / "Caskroom/claude-code@latest"
        for version in (old, new):
            write_exec(caskroom / version / "claude", "#!/bin/sh\nexit 0\n")
        link = self.stubs / "claude"
        link.symlink_to(caskroom / old / "claude")
        self.cask_move = {"CLAUDE_LINK": str(link), "CLAUDE_NEW": str(caskroom / new / "claude")}
        return caskroom

    def brew_calls(self):
        return self.brew_log.read_text().splitlines() if self.brew_log.exists() else []

    def test_a_hung_step_is_logged_stopped_and_the_sweep_goes_on(self):
        result = self.run_verb("upgrade", "--apply",
                               MACHINE_SETUP_STEP_TIMEOUT=str(STALL_BOUND), BREW_HANG="1")

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertRegex(result.stderr, STEP.format("brew upgrade", STALL_BOUND))
        self.assertIn(f"timed out after {STALL_BOUND}s: brew upgrade", result.stderr)
        self.assertEqual(self.brew_calls(),
                         ["update", "list --pinned", "upgrade", "outdated --cask --greedy --quiet",
                          "cleanup --prune=all"])
        self.assertEqual(self.mas_log.read_text(), "upgrade\n")
        self.assertIn("failed steps: brew upgrade", result.stdout)

    def test_a_slow_step_that_answers_is_no_stall(self):
        """A loaded machine slows a step that answers: only the hung step
        fails, never one cut off at the bound (sd:6253)."""
        result = self.run_verb("upgrade", "--apply", MACHINE_SETUP_STEP_TIMEOUT=str(STALL_BOUND),
                               BREW_HANG="1", BREW_SLOW="update")

        self.assertIn("failed steps: brew upgrade\n", result.stdout)
        self.assertNotIn("brew update exited", result.stderr)

    def test_the_sweep_runs_no_native_claude_update(self):
        """sd:4738: Claude Code is the claude-code@latest cask, which
        `brew upgrade --cask --greedy` moves; a native install left behind is
        not updated by the sweep."""
        write_exec(self.home / ".local/bin/claude", f'#!/bin/sh\necho "claude $*" >> {self.brew_log}\n')
        result = self.run_verb("upgrade", "--apply")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.brew_calls(),
                         ["update", "list --pinned", "upgrade", "outdated --cask --greedy --quiet",
                          "cleanup --prune=all"])
        self.assertNotIn("claude update", result.stderr)

    def test_a_quiet_week_runs_no_native_claude_update(self):
        """sd:4738: upgrade-report returns before the sweep when nothing is
        outdated, and runs no update of its own."""
        write_exec(self.home / ".local/bin/claude", f'#!/bin/sh\necho "claude $*" >> {self.brew_log}\n')
        result = self.run_verb("upgrade-report", BREW_QUIET="1")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("nothing outdated", result.stdout)
        self.assertEqual(self.brew_calls().count("claude update"), 0, self.brew_calls())
        self.assertFalse(self.notify_log.exists())

    def test_each_step_has_its_own_default_bound(self):
        result = self.run_verb("upgrade", "--apply", BREW_GREEDY_CASK="example-cask")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for command, bound in (("brew update", 600), ("brew upgrade", 1800),
                               ("brew upgrade --cask --greedy example-cask", 1800),
                               ("brew cleanup --prune=all", 600), ("mas upgrade", 1200)):
            self.assertRegex(result.stderr, STEP.format(re.escape(command), bound))

    def test_a_self_updating_cask_is_outdated_and_upgraded(self):
        """sd:3062: casks with `auto_updates true` move in the weekly run too,
        so the outdated lists and the quiet-week check ask with --greedy."""
        result = self.run_verb("upgrade-report", BREW_QUIET="1", BREW_GREEDY_CASK="example-cask")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("nothing outdated", result.stdout)
        self.assertIn("outdated --cask --greedy --quiet", self.brew_calls())
        self.assertIn("upgrade --cask --greedy example-cask", self.brew_calls())
        self.assertNotIn("outdated --cask --quiet", self.brew_calls())

    def test_the_report_names_a_hung_step_and_needs_attention(self):
        result = self.run_verb("upgrade-report", MACHINE_SETUP_STEP_TIMEOUT=str(STALL_BOUND), BREW_HANG="1")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # stderr is the job log; the sweep's own output goes into the report.
        self.assertRegex(result.stderr, STEP.format("brew upgrade", STALL_BOUND))
        self.assertIn(f"timed out after {STALL_BOUND}s: brew upgrade", result.stdout)
        found = self.findings(result)
        self.assertTrue(any(f.startswith("still outdated (") for f in found), found)
        self.assertEqual([f for f in found if not f.startswith("still outdated (")], [found[-1]])
        self.assertRegex(found[-1], r"^upgrade sweep failed \[\d+\]$")
        self.assertEqual(self.sends(), [])

    def test_the_report_carries_the_grants_result(self):
        """sd:3062: the weekly report says whether the vault grants held."""
        cellar = pathlib.Path(self.tmp.name) / "Cellar"
        (cellar / "python@3.14/3.14.0").mkdir(parents=True)
        result = self.run_verb("upgrade-report", BREW_CELLAR=str(cellar),
                               BREW_NEW_PYTHON=str(cellar / "python@3.14/3.14.1"))

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.sends(), [])
        self.assertIn("TCC grants (dashboard.sh grants, exit 0)", result.stdout)
        self.assertIn("DASHBOARD_PYTHON python3: ok, lists the vault", result.stdout)
        self.assertIn(f"new python Cellar path: {cellar}/python@3.14/3.14.1", result.stdout)

    def test_a_missing_grant_names_what_to_regrant_and_needs_attention(self):
        """sd:3062: the report names the binary and the permission, and
        sd:3454 sends it to Reports and Today instead of a push; the job
        still exits 0."""
        caskroom = self.claude_moves_to("1.0.0", "1.0.1")
        result = self.run_verb("upgrade-report", GRANTS_OUT=REFUSED, GRANTS_RC="1", NOTIFY_FAIL="1",
                               BREW_GREEDY_CASK="claude-code@latest", BREW_CASKS_DONE="1")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.sends(), [])
        self.assertIn("  Full Disk Access: /opt/homebrew/bin/python3", result.stdout)
        self.assertIn(f"Claude Code: {caskroom / '1.0.0/claude'} -> {caskroom / '1.0.1/claude'}", result.stdout)
        self.assertIn("TCC grants (dashboard.sh grants, exit 1)", result.stdout)
        self.assertEqual(self.findings(result), ["still outdated (formula): example-formula",
                                                 "TCC re-grant needed: Full Disk Access: /opt/homebrew/bin/python3"])

    def test_a_quiet_week_with_a_missing_grant_needs_attention_and_exits_0(self):
        """The grants probe runs in a quiet week too."""
        result = self.run_verb("upgrade-report", BREW_QUIET="1", GRANTS_OUT=REFUSED, GRANTS_RC="1")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("nothing outdated", result.stdout)
        self.assertEqual(self.sends(), [])
        self.assertIn("  Full Disk Access: /opt/homebrew/bin/python3", result.stdout)
        self.assertEqual(len(self.attention(result)), 1, result.stdout)

    def test_a_grant_still_to_redo_after_the_update_night_keeps_the_reason(self):
        """sd:4182: the upgrade that dropped the grant shows only on its own
        night; the grant still to redo the next night is the same finding.
        The cask moves in the sweep, and the stub's upgrade clears the
        outdated list, so the next night is quiet (sd:4738)."""
        caskroom = self.claude_moves_to("1.0.0", "1.0.1")
        update_night = self.run_verb("upgrade-report", BREW_UPGRADES_ALL="1", GRANTS_OUT=REFUSED, GRANTS_RC="1",
                                    BREW_GREEDY_CASK="claude-code@latest", BREW_CASKS_DONE="1")
        next_night = self.run_verb("upgrade-report", GRANTS_OUT=REFUSED, GRANTS_RC="1")
        self.assertIn(f"Claude Code: {caskroom / '1.0.0/claude'} -> {caskroom / '1.0.1/claude'}", update_night.stdout)
        self.assertNotIn(f"-> {caskroom / '1.0.1/claude'}", next_night.stdout)
        self.assertIn("nothing outdated", next_night.stdout)
        self.assertEqual(self.findings(update_night), ["TCC re-grant needed: Full Disk Access: /opt/homebrew/bin/python3"])
        self.assertEqual(self.attention(next_night), self.attention(update_night))

    def test_another_grant_to_redo_gives_another_reason(self):
        """sd:4182 review round 1, class pass: each producer's checksum must
        read inputs that exist and move with their content at the same
        count. One grant to redo for another binary is another finding."""
        python = self.run_verb("upgrade-report", BREW_QUIET="1", GRANTS_OUT=REFUSED, GRANTS_RC="1")
        node = self.run_verb("upgrade-report", BREW_QUIET="1", GRANTS_RC="1",
                             GRANTS_OUT=REFUSED.replace("/opt/homebrew/bin/python3", "/opt/homebrew/bin/node"))
        for run in (python, node):
            self.assertNotIn("No such file", run.stderr)
            [finding] = self.findings(run)
            self.assertRegex(finding, r"^TCC re-grant needed: Full Disk Access: /opt/homebrew/bin/\S+$")
        self.assertNotEqual(self.attention(node), self.attention(python))

    def test_a_failed_sweep_step_needs_attention_with_nothing_left(self):
        """Review of sd:3537: a failed `brew cleanup` with every package
        upgraded exited 0 and asked nothing."""
        result = self.run_verb("upgrade-report", BREW_UPGRADES_ALL="1", BREW_CLEANUP_FAIL="1")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("upgrade sweep exited nonzero", result.stdout)
        [finding] = self.findings(result)
        self.assertRegex(finding, r"^upgrade sweep failed \[\d+\]$")

    def test_a_clean_sweep_needs_no_attention(self):
        result = self.run_verb("upgrade-report", BREW_UPGRADES_ALL="1")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.attention(result), [])

    def test_the_reason_follows_the_findings_not_the_day(self):
        """sd:4182: the ingest folds a run into the open report with its reason.
        The same failed step on two nights gives one reason, so one open
        report; another failed step gives another (sd:3537: a checksum of
        "upgrade sweep failed" made a failed cleanup and a failed mas
        upgrade one report)."""
        write_exec(self.stubs / "date", '#!/bin/sh\n[ -n "$FAKE_DAY" ] && [ "$1" = "+%Y-%m-%d" ] && '
                                        '{ echo "$FAKE_DAY"; exit 0; }\nexec /bin/date "$@"\n')
        upgraded = self.brew_log.with_name(self.brew_log.name + ".upgraded")
        first = self.attention(self.run_verb("upgrade-report", BREW_UPGRADES_ALL="1", BREW_CLEANUP_FAIL="1",
                                             FAKE_DAY="2026-10-12"))
        upgraded.unlink()
        again = self.attention(self.run_verb("upgrade-report", BREW_UPGRADES_ALL="1", BREW_CLEANUP_FAIL="1",
                                             FAKE_DAY="2026-10-13"))
        upgraded.unlink()
        other = self.attention(self.run_verb("upgrade-report", BREW_UPGRADES_ALL="1", MAS_FAIL="upgrade",
                                             FAKE_DAY="2026-10-13"))
        self.assertRegex(" ".join(first), r"^upgrade on \S+: upgrade sweep failed \[\d+\]$")
        self.assertEqual(again, first)
        self.assertRegex(" ".join(other), r"^upgrade on \S+: upgrade sweep failed \[\d+\]$")
        self.assertNotEqual(other, first)

    def test_a_formula_and_a_cask_of_one_name_are_two_findings(self):
        """sd:4182 review: the lists went into the checksum unlabelled, so a
        formula and a cask of one name gave one reason."""
        formula = self.attention(self.run_verb("upgrade-report", BREW_OUTDATED="example-tool"))
        cask = self.attention(self.run_verb("upgrade-report", BREW_QUIET="1", BREW_GREEDY_CASK="example-tool"))
        self.assertRegex(" ".join(formula), r"^upgrade on \S+: still outdated \(formula\): example-tool$")
        self.assertRegex(" ".join(cask), r"^upgrade on \S+: still outdated \(cask\): example-tool$")
        self.assertNotEqual(formula, cask)
        # sd:4664: one line per package, so one of them upgraded leaves a subset (pack sd:4658 folds it).
        both = self.attention(self.run_verb("upgrade-report", BREW_OUTDATED="example-tool", BREW_GREEDY_CASK="example-tool"))
        self.assertEqual(sorted(both), sorted(formula + cask))

    def test_an_inconclusive_probe_is_reported_and_needs_no_attention(self):
        """Exit 3 is not a missing grant: the report says so and asks nothing."""
        result = self.run_verb("upgrade-report", BREW_QUIET="1",
                               GRANTS_OUT="inconclusive: /bin/ls lists the vault too", GRANTS_RC="3")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("inconclusive: /bin/ls lists the vault too", result.stdout)
        self.assertEqual(self.attention(result), [])

    def test_the_inventory_capture_runs_after_the_upgrades_and_before_the_grants(self):
        """sd:3062: the weekly run takes over the retired ai-apps-nightly, and
        its mail carries the capture's diff."""
        result = self.run_verb("upgrade-report", AI_APPS_OUT="  + claude-code|skill|example")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = self.brew_calls()
        self.assertLess(calls.index("cleanup --prune=all"), calls.index("ai-apps capture"))
        self.assertLess(calls.index("ai-apps capture"), calls.index("grants"))
        self.assertIn("ai-apps inventory:", result.stdout)
        self.assertIn("+ claude-code|skill|example", result.stdout)

    def test_a_failed_capture_needs_attention_and_the_grants_still_run(self):
        result = self.run_verb("upgrade-report", AI_APPS_OUT="opencode.json does not parse", AI_APPS_RC="1")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("grants", self.brew_calls())
        self.assertRegex(self.findings(result)[-1], r"^inventory capture failed \[\d+\]$")
        self.assertIn("ai-apps inventory capture FAILED (exit 1)", result.stdout)

    def test_a_quiet_week_captures_and_a_failed_capture_fails_the_job(self):
        """A quiet week prints no summary, so the exit reports a failed capture."""
        result = self.run_verb("upgrade-report", BREW_QUIET="1", AI_APPS_RC="1")

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("ai-apps capture", self.brew_calls())
        self.assertIn("grants", self.brew_calls())
        self.assertIn("ai-apps inventory capture FAILED (exit 1)", result.stdout)

    def test_a_hand_pinned_formula_is_left_over_not_held(self):
        """sd:6433: machine-setup keeps no pins, so the sweep runs one bare
        `brew upgrade`; a pin made by hand that no older brew stage made is
        brew's to skip, and the report lists it as still outdated."""
        result = self.run_verb("upgrade-report", STUB_PINNED="example-pinned")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("unpin example-pinned", self.brew_calls())
        self.assertIn("upgrade", self.brew_calls())
        self.assertNotIn("held (pinned)", result.stdout)
        self.assertIn("still outdated (formula) — held back or failed:\n  example-formula\n  example-pinned",
                      result.stdout)

    def test_the_sweep_unpins_an_older_stage_pin_before_brew_upgrade(self):
        """sd:6448: a python pin an older brew stage made fails a bare `brew
        upgrade` on a dependent that needs a newer python. The weekly upgrade
        ran before the nightly `update --apply` removed it (sd:6433)."""
        for verb in ("upgrade", "upgrade-report"):
            with self.subTest(verb=verb):
                self.brew_log.unlink(missing_ok=True)
                args = (verb, "--apply") if verb == "upgrade" else (verb,)
                result = self.run_verb(*args, STUB_PINNED="python@3.14")

                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                calls = self.brew_calls()
                self.assertIn("unpin python@3.14", calls)
                self.assertLess(calls.index("unpin python@3.14"), calls.index("upgrade"))
                self.assertIn("  DIFFERS python@3.14 pinned\n", result.stdout)

    def test_a_dry_run_names_the_unpin_and_makes_none(self):
        result = self.run_verb("upgrade", STUB_PINNED="python@3.14")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("  [dry-run] brew unpin python@3.14\n", result.stdout)
        self.assertNotIn("unpin python@3.14", self.brew_calls())

    def test_a_pin_list_or_unpin_that_fails_is_a_failed_step_not_none(self):
        """A pin list brew did not give is unknown (sd:3536); the sweep goes
        on and names the step."""
        for fail, step in (("list --pinned", "brew pin list"),
                           ("unpin python@3.14", "brew unpin python@3.14")):
            with self.subTest(fail=fail):
                self.brew_log.unlink(missing_ok=True)
                result = self.run_verb("upgrade", "--apply", STUB_PINNED="python@3.14", BREW_FAIL=fail)

                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("upgrade", self.brew_calls())
                self.assertRegex(result.stdout, rf"  failed steps: (.*, )?{step}(,|\n)")

    def test_an_outdated_list_that_did_not_answer_is_not_a_quiet_week(self):
        """sd:3536: a failed mas outdated read as nothing outdated."""
        result = self.run_verb("upgrade-report", BREW_QUIET="1", MAS_FAIL="outdated")

        self.assertNotIn("nothing outdated", result.stdout)
        self.assertIn("outdated (mas): unknown, the list did not answer", result.stdout)
        self.assertIn("outdated list failed (mas)", self.attention(result)[0])

    def test_a_term_mid_step_exits_without_writing_into_the_removed_temp_dir(self):
        """The job's limit ends the run; the TERM trap used to remove the
        temporary folder and return, and the run then read and wrote into it."""
        started = pathlib.Path(self.tmp.name) / "brew-started"
        # A group of its own, as cron-jobs' run_bounded gives a job: its limit
        # sends TERM to the whole group.
        process = subprocess.Popen(
            [str(self.folder / "machine-setup.sh"), "upgrade-report"],
            env=self.env(BREW_WAIT="1", BREW_STARTED=str(started)), cwd=self.tmp.name,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True)
        self.addCleanup(lambda: process.poll() is None and os.killpg(process.pid, signal.SIGKILL))
        deadline = time.monotonic() + 20
        while not started.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertTrue(started.exists(), "brew upgrade never started")

        os.killpg(process.pid, signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=30)

        self.assertEqual(process.returncode, 143, stdout + stderr)
        self.assertNotIn("No such file or directory", stdout + stderr)
        self.assertFalse(self.notify_log.exists(), "a killed run mailed a report")

    def test_a_cask_that_needs_sudo_is_an_action_not_a_failed_sweep(self):
        """sd:5046: an unattended run has no terminal for sudo, so a cask with
        a pkg installer fails `brew upgrade --cask --greedy`. The report asks
        the operator to run the cask's upgrade in a terminal."""
        result = self.run_verb("upgrade-report", BREW_UPGRADES_ALL="1", BREW_GREEDY_CASK="example-cask",
                               BREW_SUDO="upgrade --cask --greedy example-cask")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("upgrade sweep exited nonzero", result.stdout)
        self.assertIn("needs sudo (cask) — run in a terminal:\n  brew upgrade --cask example-cask\n", result.stdout)
        self.assertEqual(self.findings(result), ["needs sudo, run in a terminal: brew upgrade --cask example-cask"])

    def askpass_run(self, casks, keychain="example-password", **extra):
        """upgrade-report with example-pkg outdated, its installer calling
        sudo; CASKS is the allowlist, KEYCHAIN the item's password or None."""
        self.security_log = self.tmp_path / "security.log"
        write_exec(self.stubs / "security-stub", SECURITY_STUB)
        return self.run_verb("upgrade-report", BREW_UPGRADES_ALL="1", BREW_PKG="example-pkg",
                             STUB_PASSWORD="example-password", STUB_KEYCHAIN=keychain or "",
                             MACHINE_SETUP_SUDO_CASKS=casks,
                             MACHINE_SETUP_SECURITY=str(self.stubs / "security-stub"),
                             SECURITY_LOG=str(self.security_log), **extra)

    def askpass_calls(self):
        log = pathlib.Path(f"{self.brew_log}.askpass")
        return log.read_text().splitlines() if log.exists() else []

    def test_an_allowed_cask_upgrades_with_the_keychain_password(self):
        """sd:6312: a cask on the allowlist upgrades unattended through
        SUDO_ASKPASS, which reads the login-Keychain item; the probe reads
        the item's attributes, never its password, and nothing is left
        for the greedy sweep after it."""
        result = self.askpass_run("other-cask example-pkg")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.askpass_calls(), ["upgrade --cask --greedy example-pkg"])
        self.assertNotIn("upgrade --cask --greedy", self.brew_calls())
        self.assertIn("upgraded (cask):\n  example-pkg\n", result.stdout)
        self.assertEqual(self.findings(result), [])
        probe, read = self.security_log.read_text().splitlines()
        self.assertNotIn("-w", probe.split())
        self.assertEqual(read.split()[-1], "-w")
        self.assertIn("-s machine-setup-sudo", read)
        self.assertNotIn("example-password", result.stdout + result.stderr)

    def test_without_the_keychain_item_or_the_allowlist_sudo_stays_a_finding(self):
        """sd:6312: no item, a probe that fails, or a cask off the allowlist
        keeps today's needs-sudo finding, and no askpass runs."""
        for casks, keychain, extra in (("example-pkg", None, {}),
                                       ("example-pkg", "example-password", {"SECURITY_RC": "1"}),
                                       ("other-cask", "example-password", {}),
                                       ("", "example-password", {})):
            with self.subTest(casks=casks, keychain=keychain, extra=extra):
                for log in (self.brew_log, pathlib.Path(f"{self.brew_log}.askpass")):
                    log.unlink(missing_ok=True)
                result = self.askpass_run(casks, keychain, **extra)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.askpass_calls(), [])
                self.assertEqual(self.findings(result),
                                 ["needs sudo, run in a terminal: brew upgrade --cask example-pkg"])
                if self.security_log.exists():
                    self.assertNotIn("-w", self.security_log.read_text().split())

    def test_the_casks_that_need_sudo_come_from_brew_info(self):
        """sd:6927: with no hand list, the askpass upgrade takes the casks
        whose artifacts hold a pkg installer or a pkgutil uninstall, or put
        a file outside a folder this user writes (/usr/local/bin on Apple
        silicon); an app in /Applications or a binary in the brew prefix
        needs no sudo. MACHINE_SETUP_SUDO_CASKS adds to the derived set."""
        info = json.dumps({"casks": [
            {"token": "example-pkg", "artifacts": [{"pkg": ["Example.pkg"]}]},
            {"token": "example-pkgutil", "artifacts": [
                {"uninstall": [{"pkgutil": "com.example.pkg", "quit": "com.example"}]},
                {"app": ["Util.app"], "target": "/Applications/Util.app"}]},
            {"token": "example-root", "artifacts": [
                {"app": ["Root.app"], "target": "/Applications/Root.app"},
                {"binary": ["/Applications/Root.app/bin/root", {"target": "/usr/local/bin/root"}],
                 "target": "/usr/local/bin/root"}]},
            {"token": "example-app", "artifacts": [
                {"app": ["Plain.app"], "target": "/Applications/Plain.app"},
                {"binary": ["/Applications/Plain.app/bin/plain"], "target": "/opt/homebrew/bin/plain"},
                {"zap": [{"trash": "/Library/Example"}]}]},
            {"token": "example-extra", "artifacts": [{"app": ["Extra.app"], "target": "/Applications/Extra.app"}]},
        ]})
        outdated = "example-root\nexample-app\nexample-pkgutil\nexample-extra"
        for casks, want in (("", ["example-root", "example-pkgutil", "example-pkg"]),
                            ("change-me", ["example-root", "example-pkgutil", "example-pkg"]),
                            ("example-extra", ["example-root", "example-pkgutil", "example-extra", "example-pkg"])):
            with self.subTest(casks=casks):
                pathlib.Path(f"{self.brew_log}.askpass").unlink(missing_ok=True)
                self.brew_log.unlink(missing_ok=True)
                result = self.askpass_run(casks, BREW_INFO=info, BREW_GREEDY_CASK=outdated)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                [call] = self.askpass_calls()
                self.assertEqual(call, "upgrade --cask --greedy " + " ".join(want))

    def test_an_app_store_upgrade_that_needs_sudo_names_each_app(self):
        result = self.run_verb("upgrade-report", BREW_UPGRADES_ALL="1", MAS_SUDO="upgrade",
                               MAS_OUTDATED="497799835 Example App (1.0 -> 1.1)")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.mas_log.read_text().splitlines().count("upgrade"), 1)
        self.assertEqual(self.findings(result),
                         ["needs sudo, run in a terminal: mas upgrade 497799835  # Example App (1.0 -> 1.1)"])

    def test_a_sweep_that_needs_sudo_defers_the_step_and_exits_0(self):
        result = self.run_verb("upgrade", "--apply", BREW_GREEDY_CASK="example-cask",
                               BREW_SUDO="upgrade --cask --greedy example-cask")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("  DEFER   needs sudo: brew upgrade --cask --greedy example-cask", result.stdout)
        self.assertIn("deferred — 1 action(s) still need sudo. Run these, then re-run:\n"
                      "    brew upgrade --cask --greedy example-cask\n"
                      "    machine-setup.sh upgrade --apply\n", result.stdout)
        self.assertNotIn("failed steps", result.stdout)
        # The step's own output still reaches the report.
        self.assertIn("sudo: a password is required", result.stdout + result.stderr)
        self.assertEqual(self.mas_log.read_text(), "upgrade\n", "the steps after it still run")

    def test_a_failure_beside_a_refused_sudo_still_fails_the_sweep(self):
        result = self.run_verb("upgrade-report", BREW_UPGRADES_ALL="1", BREW_GREEDY_CASK="example-cask",
                               BREW_SUDO="upgrade --cask --greedy example-cask", BREW_CLEANUP_FAIL="1")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        found = self.findings(result)
        self.assertEqual(found[0], "needs sudo, run in a terminal: brew upgrade --cask example-cask")
        self.assertRegex(found[1], r"^upgrade sweep failed \[\d+\]$")
        self.assertEqual(len(found), 2, found)

    def test_a_cask_step_that_fails_without_sudo_is_a_failed_sweep(self):
        """sd:5046: only sudo's own refusal is an action; any other failure of
        the same step stays a failed sweep with its leftovers."""
        result = self.run_verb("upgrade-report", BREW_UPGRADES_ALL="1", BREW_GREEDY_CASK="example-cask",
                               BREW_FAIL="upgrade --cask --greedy example-cask")

        found = self.findings(result)
        self.assertEqual(found[0], "still outdated (cask): example-cask")
        self.assertRegex(found[1], r"^upgrade sweep failed \[\d+\]$")
        self.assertNotIn("DEFER", result.stdout)

    def test_a_step_that_hangs_after_a_refused_sudo_is_a_failure(self):
        """A step stopped at its bound is unknown, not a deferral."""
        result = self.run_verb("upgrade", "--apply", MACHINE_SETUP_STEP_TIMEOUT=str(STALL_BOUND),
                               BREW_SUDO="upgrade", STUB_SUDO_HANG="1")

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("failed steps: brew upgrade", result.stdout)
        self.assertNotIn("DEFER", result.stdout)

    def test_a_build_failure_is_not_a_sudo_deferral(self):
        """sd:7056: a formula whose build fails is a failure with its error
        line, not a command for the operator's terminal."""
        result = self.run_verb("upgrade-report", BREW_UPGRADES_ALL="1", BREW_BUILD_FAIL="upgrade",
                               BREW_BUILD_TEXT=OMLX_TEXT)

        self.assertNotIn("needs sudo", result.stdout)
        self.assertNotIn("DEFER", result.stdout)
        found = self.findings(result)
        self.assertEqual(found[0], f"build failure (formula): example-formula: {OMLX_LINE}")
        self.assertEqual(len(found), 2, found)
        self.assertIn(f"build failure (formula) — {OMLX_LINE}:\n  example-formula\n", result.stdout)

    def test_a_build_failure_beside_a_refused_sudo_is_not_a_sudo_deferral(self):
        """sd:7056: `brew upgrade` also upgrades casks, so one run can hold a
        sudo refusal and a failed build. The formula left over is not a
        sudo command."""
        result = self.run_verb("upgrade-report", BREW_UPGRADES_ALL="1", BREW_BUILD_FAIL="upgrade",
                               BREW_BUILD_TEXT=SUDO_TEXT + OMLX_TEXT)

        self.assertNotIn("needs sudo, run in a terminal: brew upgrade example-formula", result.stdout)
        self.assertEqual(self.findings(result)[0], f"build failure (formula): example-formula: {OMLX_LINE}")

    def test_a_refused_sudo_in_a_formula_upgrade_still_needs_sudo(self):
        result = self.run_verb("upgrade-report", BREW_UPGRADES_ALL="1", BREW_BUILD_FAIL="upgrade",
                               BREW_BUILD_TEXT=SUDO_TEXT)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("needs sudo (formula) — run in a terminal:\n  brew upgrade example-formula\n", result.stdout)
        self.assertEqual(self.findings(result), ["needs sudo, run in a terminal: brew upgrade example-formula"])

    def test_the_upgrade_plan_leaves_chrome_and_slack_to_their_own_updaters(self):
        """sd:7058: the two casks stay root-owned, so brew's upgrade needs
        sudo for them; their own updaters move them. Other self-updating
        casks still upgrade."""
        result = self.run_verb("upgrade", BREW_GREEDY_CASK="google-chrome\nexample-cask\nslack")

        self.assertIn("  [dry-run] brew upgrade --cask --greedy example-cask\n", result.stdout)
        self.assertNotIn("google-chrome", result.stdout)
        self.assertNotIn("slack", result.stdout)

    def test_a_week_with_only_chrome_and_slack_outdated_is_quiet(self):
        result = self.run_verb("upgrade-report", BREW_QUIET="1", BREW_GREEDY_CASK="google-chrome\nslack")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("nothing outdated", result.stdout)
        self.assertEqual([c for c in self.brew_calls() if c.startswith("upgrade")], [])

    def test_the_report_leaves_chrome_and_slack_out_of_the_cask_leftovers(self):
        result = self.run_verb("upgrade-report", BREW_UPGRADES_ALL="1", BREW_SUDO="upgrade --cask --greedy example-cask",
                               BREW_GREEDY_CASK="google-chrome\nexample-cask\nslack")

        self.assertEqual(self.findings(result), ["needs sudo, run in a terminal: brew upgrade --cask example-cask"])
        self.assertIn("upgrade --cask --greedy example-cask", self.brew_calls())

    def test_the_askpass_sweep_leaves_chrome_and_slack_out(self):
        result = self.askpass_run("google-chrome slack", BREW_GREEDY_CASK="google-chrome\nslack")

        self.assertEqual(self.askpass_calls(), [], result.stdout + result.stderr)

    def test_a_dry_run_runs_no_step(self):
        result = self.run_verb("upgrade", BREW_GREEDY_CASK="example-cask")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("  [dry-run] brew upgrade --cask --greedy example-cask", result.stdout)
        # The pin list and the outdated casks are reads, bounded as reads
        # are: the dry run names what it would unpin (sd:6448) and upgrade
        # (sd:7058). Nothing else runs.
        self.assertEqual(self.brew_calls(), ["list --pinned", "outdated --cask --greedy --quiet"])
        self.assertEqual(re.findall(r"\[step [^]]+\] (.+?) \(bound", result.stdout + result.stderr),
                         ["brew list --pinned", "brew outdated --cask --greedy --quiet"])


if __name__ == "__main__":
    unittest.main()

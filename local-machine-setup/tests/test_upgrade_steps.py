"""A brew or mas step that hangs names itself and ends at its bound (sd:2660).

On some nights macOS stops answering permission checks for launchd jobs, and
a brew call then hangs. upgrade-report captures the sweep's output for its
mail, so the job log said nothing until the job's two-hour limit killed it.

The script runs from a copy of this folder under a temporary root, beside a
copy of lib/ and a stub notify.sh that only logs, so no mail leaves. `brew`
and `mas` are stubs on PATH.
"""

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

HERE = pathlib.Path(__file__).resolve().parent
FOLDER = HERE.parent
LIB = FOLDER.parent / "lib"

# One formula is outdated, so upgrade-report runs the sweep. With BREW_HANG
# set, `brew upgrade` never finishes; with BREW_QUIET, nothing is outdated.
# BREW_GREEDY_CASK names a cask with `auto_updates true`: brew lists it as
# outdated only when asked with --greedy.
BREW_STUB = r"""#!/bin/sh
echo "$*" >> "$BREW_LOG"
case "$*" in
  "outdated --formula --quiet") [ -n "$BREW_QUIET" ] || echo example-formula
                                [ -z "$BREW_PINNED" ] || echo "$BREW_PINNED" ;;
  "list --pinned") [ -z "$BREW_PINNED" ] || echo "$BREW_PINNED" ;;
  "outdated --cask --greedy --quiet") [ -z "$BREW_GREEDY_CASK" ] || echo "$BREW_GREEDY_CASK" ;;
  --cellar) [ -z "$BREW_CELLAR" ] || echo "$BREW_CELLAR" ;;
  upgrade) [ -z "$BREW_STARTED" ] || : > "$BREW_STARTED"
           [ -z "$BREW_NEW_PYTHON" ] || mkdir -p "$BREW_NEW_PYTHON"
           [ -z "$BREW_HANG" ] || exec sleep 60 ;;
esac
exit 0
"""

MAS_STUB = r"""#!/bin/sh
echo "$*" >> "$MAS_LOG"
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

REFUSED = ("SD_DASHBOARD_PYTHON /opt/homebrew/bin/python3: cannot read /vault — macOS is asking for "
           "Documents access and nothing under launchd can answer the prompt. Grant Full Disk "
           "Access to /opt/homebrew/bin/python3 in System Settings > Privacy & Security, then "
           "restart the agent.")

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
        repo = base / "repo"
        self.folder = repo / "local-machine-setup"
        shutil.copytree(FOLDER, self.folder, ignore=shutil.ignore_patterns("tests", "__pycache__"))
        shutil.copytree(LIB, repo / "lib", ignore=shutil.ignore_patterns("tests", "__pycache__"))
        write_exec(repo / "local-notify/notify.sh", NOTIFY_STUB)
        write_exec(repo / "local-project-dashboard/dashboard.sh", DASHBOARD_STUB)
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
            **extra,
        }

    def run_verb(self, *args, **extra):
        return subprocess.run([str(self.folder / "machine-setup.sh"), *args],
                              env=self.env(**extra), capture_output=True, text=True,
                              cwd=self.tmp.name, stdin=subprocess.DEVNULL, timeout=30)

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

    def claude_moves_to(self, old, new):
        """~/.local/bin/claude links to version OLD; `claude update` relinks it to NEW."""
        versions = self.home / ".local/share/claude/versions"
        for version in (old, new):
            write_exec(versions / version, "#!/bin/sh\nexit 0\n")
        write_exec(versions / old, f'#!/bin/sh\nln -sf {versions / new} {self.home / ".local/bin/claude"}\n')
        (self.home / ".local/bin").mkdir(parents=True, exist_ok=True)
        (self.home / ".local/bin/claude").symlink_to(versions / old)
        return versions

    def brew_calls(self):
        return self.brew_log.read_text().splitlines() if self.brew_log.exists() else []

    def test_a_hung_step_is_logged_stopped_and_the_sweep_goes_on(self):
        result = self.run_verb("upgrade", "--apply",
                               MACHINE_SETUP_STEP_TIMEOUT="2", BREW_HANG="1")

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertRegex(result.stderr, STEP.format("brew upgrade", 2))
        self.assertIn("timed out after 2s: brew upgrade", result.stderr)
        self.assertEqual(self.brew_calls(),
                         ["update", "upgrade", "upgrade --cask --greedy", "cleanup --prune=all"])
        self.assertEqual(self.mas_log.read_text(), "upgrade\n")
        self.assertIn("failed steps: brew upgrade", result.stdout)

    def test_the_sweep_updates_claude_code_before_brew_cleanup(self):
        """sd:3033: Claude Code updates in the weekly upgrade only."""
        # The stub logs into brew's log, so one list holds the order.
        write_exec(self.home / ".local/bin/claude", f'#!/bin/sh\necho "claude $*" >> {self.brew_log}\n')
        result = self.run_verb("upgrade", "--apply")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.brew_calls(),
                         ["update", "upgrade", "upgrade --cask --greedy", "claude update", "cleanup --prune=all"])
        self.assertRegex(result.stderr, STEP.format(re.escape(str(self.home / ".local/bin/claude")) + " update", 600))

    def test_a_quiet_week_still_updates_claude_code(self):
        """sd:3033: upgrade-report returns before the sweep when nothing is
        outdated, and Claude Code updates outside brew."""
        write_exec(self.home / ".local/bin/claude", f'#!/bin/sh\necho "claude $*" >> {self.brew_log}\n')
        result = self.run_verb("upgrade-report", BREW_QUIET="1")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("nothing outdated", result.stdout)
        self.assertEqual(self.brew_calls().count("claude update"), 1, self.brew_calls())
        self.assertFalse(self.notify_log.exists())

    def test_a_failed_quiet_week_claude_update_fails_the_job(self):
        """No mail goes in a quiet week, so the exit is what reports it."""
        write_exec(self.home / ".local/bin/claude", "#!/bin/sh\nexit 1\n")
        result = self.run_verb("upgrade-report", BREW_QUIET="1")

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertFalse(self.notify_log.exists())

    def test_each_step_has_its_own_default_bound(self):
        result = self.run_verb("upgrade", "--apply")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for command, bound in (("brew update", 600), ("brew upgrade", 1800),
                               ("brew upgrade --cask --greedy", 1800),
                               ("brew cleanup --prune=all", 600), ("mas upgrade", 1200)):
            self.assertRegex(result.stderr, STEP.format(re.escape(command), bound))

    def test_a_self_updating_cask_is_outdated_and_upgraded(self):
        """sd:3062: casks with `auto_updates true` move in the weekly run too,
        so the outdated lists and the quiet-week check ask with --greedy."""
        result = self.run_verb("upgrade-report", BREW_QUIET="1", BREW_GREEDY_CASK="example-cask")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("nothing outdated", result.stdout)
        self.assertIn("outdated --cask --greedy --quiet", self.brew_calls())
        self.assertIn("upgrade --cask --greedy", self.brew_calls())
        self.assertNotIn("outdated --cask --quiet", self.brew_calls())

    def test_the_report_names_a_hung_step_in_the_job_log_and_mails_it(self):
        result = self.run_verb("upgrade-report", MACHINE_SETUP_STEP_TIMEOUT="2", BREW_HANG="1")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # stderr is the job log; the sweep's own output goes into the mail.
        self.assertRegex(result.stderr, STEP.format("brew upgrade", 2))
        mail = self.notify_log.read_text().splitlines()
        self.assertIn("-F", mail)
        self.assertIn("timed out after 2s: brew upgrade", "\n".join(mail))

    def test_the_report_mail_carries_the_grants_result(self):
        """sd:3062: the weekly mail says whether the vault grants held."""
        cellar = pathlib.Path(self.tmp.name) / "Cellar"
        (cellar / "python@3.14/3.14.0").mkdir(parents=True)
        result = self.run_verb("upgrade-report", BREW_CELLAR=str(cellar),
                               BREW_NEW_PYTHON=str(cellar / "python@3.14/3.14.1"))

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        sends = self.sends()
        self.assertEqual(len(sends), 1, sends)
        body = "\n".join(sends[0])
        self.assertIn("TCC grants (dashboard.sh grants, exit 0)", body)
        self.assertIn("DASHBOARD_PYTHON python3: ok, lists the vault", body)
        self.assertIn(f"new python Cellar path: {cellar}/python@3.14/3.14.1", body)

    def test_a_missing_grant_pushes_what_to_regrant(self):
        """sd:3062: one push names the binary and the permission; the job
        still exits 0, since the report went out."""
        versions = self.claude_moves_to("1.0.0", "1.0.1")
        result = self.run_verb("upgrade-report", GRANTS_OUT=REFUSED, GRANTS_RC="1")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        sends = self.sends()
        self.assertEqual(len(sends), 2, sends)
        push = sends[0]
        self.assertIn("-F", push)
        self.assertIn("ntfy,email", push)
        self.assertIn("  Full Disk Access: /opt/homebrew/bin/python3", "\n".join(push))
        self.assertIn(f"Claude Code: {versions / '1.0.0'} -> {versions / '1.0.1'}", "\n".join(push))
        self.assertIn("TCC grants (dashboard.sh grants, exit 1)", "\n".join(sends[1]))

    def test_a_quiet_week_with_a_missing_grant_pushes_and_exits_0(self):
        """`claude update` runs in a quiet week too, and can drop a grant."""
        self.claude_moves_to("1.0.0", "1.0.1")
        result = self.run_verb("upgrade-report", BREW_QUIET="1", GRANTS_OUT=REFUSED, GRANTS_RC="1")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("nothing outdated", result.stdout)
        sends = self.sends()
        self.assertEqual(len(sends), 1, sends)
        self.assertIn("  Full Disk Access: /opt/homebrew/bin/python3", "\n".join(sends[0]))

    def test_an_inconclusive_probe_is_reported_and_pushes_nothing(self):
        """Exit 3 is not a missing grant: the mail says so, no push goes."""
        result = self.run_verb("upgrade-report", GRANTS_OUT="inconclusive: /bin/ls lists the vault too",
                               GRANTS_RC="3")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        sends = self.sends()
        self.assertEqual(len(sends), 1, sends)
        self.assertIn("inconclusive: /bin/ls lists the vault too", "\n".join(sends[0]))

    def test_a_lost_grant_push_fails_the_job(self):
        """The push is the quiet week's only report, so losing it is exit 1."""
        result = self.run_verb("upgrade-report", BREW_QUIET="1", GRANTS_OUT=REFUSED, GRANTS_RC="1",
                               NOTIFY_FAIL="1")

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("grant push FAILED", result.stderr)

    def test_the_inventory_capture_runs_after_the_upgrades_and_before_the_grants(self):
        """sd:3062: the weekly run takes over the retired ai-apps-nightly, and
        its mail carries the capture's diff."""
        result = self.run_verb("upgrade-report", AI_APPS_OUT="  + claude-code|skill|example")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = self.brew_calls()
        self.assertLess(calls.index("cleanup --prune=all"), calls.index("ai-apps capture"))
        self.assertLess(calls.index("ai-apps capture"), calls.index("grants"))
        body = "\n".join(self.sends()[0])
        self.assertIn("ai-apps inventory:", body)
        self.assertIn("+ claude-code|skill|example", body)

    def test_a_failed_capture_is_mailed_and_the_grants_still_run(self):
        result = self.run_verb("upgrade-report", AI_APPS_OUT="opencode.json does not parse", AI_APPS_RC="1")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("grants", self.brew_calls())
        mail = self.sends()[0]
        self.assertIn("-F", mail)
        self.assertIn("ai-apps inventory capture FAILED (exit 1)", "\n".join(mail))

    def test_a_quiet_week_captures_and_a_failed_capture_fails_the_job(self):
        """No mail goes in a quiet week, so the exit reports a failed capture."""
        result = self.run_verb("upgrade-report", BREW_QUIET="1", AI_APPS_RC="1")

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("ai-apps capture", self.brew_calls())
        self.assertIn("grants", self.brew_calls())
        self.assertIn("ai-apps inventory capture FAILED (exit 1)", result.stdout)

    def test_a_pinned_formula_is_held_not_failed(self):
        """sd:3062: python@3.14 is pinned, so a newer version is held on
        purpose; the report says so, and does not count it as left over."""
        result = self.run_verb("upgrade-report", BREW_PINNED="python@3.14")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        mail = self.sends()[0]
        body = "\n".join(mail)
        self.assertIn("held (pinned):\n  python@3.14", body)
        self.assertNotIn("still outdated (formula) — held back or failed:\n  example-formula\n  python@3.14", body)
        self.assertIn("upgrade: 0 upgraded, 1 still outdated", mail[1])

    def test_a_week_with_only_a_pinned_formula_outdated_is_quiet(self):
        result = self.run_verb("upgrade-report", BREW_QUIET="1", BREW_PINNED="python@3.14")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("nothing outdated", result.stdout)
        self.assertIn("held (pinned):\n  python@3.14", result.stdout)
        self.assertEqual(self.sends(), [])

    def test_a_term_mid_step_exits_without_writing_into_the_removed_temp_dir(self):
        """The job's limit ends the run; the TERM trap used to remove the
        temporary folder and return, and the run then read and wrote into it."""
        started = pathlib.Path(self.tmp.name) / "brew-started"
        # A group of its own, as cron-jobs' run_bounded gives a job: its limit
        # sends TERM to the whole group.
        process = subprocess.Popen(
            [str(self.folder / "machine-setup.sh"), "upgrade-report"],
            env=self.env(BREW_HANG="1", BREW_STARTED=str(started)), cwd=self.tmp.name,
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

    def test_a_dry_run_runs_no_step(self):
        result = self.run_verb("upgrade")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("  [dry-run] brew upgrade --cask --greedy", result.stdout)
        self.assertEqual(self.brew_calls(), [])
        self.assertNotIn("[step", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()

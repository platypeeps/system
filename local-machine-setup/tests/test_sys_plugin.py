"""The repos stage registers this checkout as the `sys` sd plugin (sd:7352).

local-cswap's sync reads `sys.cswap_account` through `sd config get`, which
refuses a prefix no plugin registered. Registration lives in each machine's
own config, so a fresh hub or satellite failed every sync until someone ran
`sd plugin add` there by hand. The repos stage runs on every machine and adds
the main checkout when `sd plugin list` lacks it.

The pack's `bin/sd` is a stub that answers `plugin list` from a rows file and
appends to it on `plugin add`; `bin/sd-db` fails, so the stage's other steps
skip.
"""

import os
import pathlib
import subprocess

from tests import fixture_config
from tests import test_claude_settings as base

MANIFEST = base.FOLDER.parent / "sd-plugin.json"

# `plugin list` prints $SD_ROWS, or exits $SD_LIST_RC when it is set;
# `plugin add <path>` appends `sys  <path>`. Every call is logged.
SD = r"""#!/bin/sh
printf '%s\n' "$*" >> "$SD_LOG"
case "$1 $2" in
  "plugin list")
    [ -z "${SD_LIST_RC:-}" ] || exit "$SD_LIST_RC"
    cat "$SD_ROWS"
    ;;
  "plugin add")
    printf 'sys  %s  config: cswap_account\n' "$3" >> "$SD_ROWS"
    echo "registered sys from $3"
    ;;
  *) exit 9 ;;
esac
"""


class SysPlugin(base.SyntheticHome):
    def setUp(self):
        super().setUp()
        (self.repo / "sd-plugin.json").write_text(MANIFEST.read_text())
        self.pack = self.home / "pack"
        base.write_exec(self.pack / "bin/sd", SD)
        base.write_exec(self.pack / "bin/sd-db", "#!/bin/sh\nexit 1\n")
        self.rows = self.home / "sd-rows"
        self.rows.write_text("hoa  /srv/example.test/hoa  config: google_account\n")
        self.log = self.home / "sd.log"
        self.list_rc = ""

    def env(self):
        return {"HOME": str(self.home), "PATH": f"{self.stubs}:/usr/bin:/bin:/usr/sbin:/sbin",
                "LANG": "en_US.UTF-8", "MACHINE_SETUP_STATE": str(self.state),
                **fixture_config.env(self.config_root),
                "SYSTEM_TOOLS_LABEL_PREFIX": fixture_config.LABEL_PREFIX,
                "SD_PACK_ROOT": str(self.pack), "SD_LOG": str(self.log),
                "SD_ROWS": str(self.rows), "SD_LIST_RC": self.list_rc}

    def run_verb(self, *args, script=None):
        script = script or self.folder / "machine-setup.sh"
        result = subprocess.run([str(script), *args], env=self.env(), capture_output=True,
                                text=True, cwd=self.cwd, stdin=subprocess.DEVNULL, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def run_repos(self, *flags, script=None):
        return self.run_verb("update", "repos", *flags, script=script)

    def adds(self):
        calls = self.log.read_text().splitlines() if self.log.exists() else []
        return [c for c in calls if c.startswith("plugin add")]

    def test_a_dry_run_names_the_missing_plugin_and_adds_nothing(self):
        out = self.run_repos()

        self.assertIn(f"MISSING sys sd plugin: {self.repo} is not registered", out)
        self.assertIn(f"[dry-run] {self.pack}/bin/sd plugin add {self.repo}", out)
        self.assertEqual(self.adds(), [])

    def test_apply_registers_the_checkout_once_and_a_second_run_is_ok(self):
        out = self.run_repos("--apply")

        self.assertIn("MISSING sys sd plugin", out)
        self.assertEqual(self.adds(), [f"plugin add {self.repo}"])

        again = self.run_repos("--apply")

        self.assertIn(f"ok      sys sd plugin from {self.repo}", again)
        self.assertNotIn("MISSING", again)
        self.assertEqual(len(self.adds()), 1)

    def test_a_registration_through_a_symlink_is_ok(self):
        # sd stores the resolved path; ROOT may be the unresolved one.
        real = pathlib.Path(os.path.realpath(self.repo))
        self.rows.write_text(f"sys  {real}  config: cswap_account\n")

        out = self.run_repos("--apply")

        self.assertIn("ok      sys sd plugin", out)
        self.assertEqual(self.adds(), [])

    def test_sys_from_another_checkout_is_differs_and_left_alone(self):
        self.rows.write_text("sys  /srv/example.test/old-system  config: cswap_account\n")

        out = self.run_repos("--apply")

        self.assertIn(f"DIFFERS sys sd plugin is registered from /srv/example.test/old-system, not {self.repo}",
                      out)
        self.assertNotIn("MISSING sys sd plugin", out)
        self.assertEqual(self.adds(), [])

    def test_this_checkout_read_as_another_prefix_is_differs(self):
        self.rows.write_text(f"?  {self.repo}  UNREADABLE: no sd-plugin.json\n")

        out = self.run_repos("--apply")

        self.assertIn(f"DIFFERS sys sd plugin: sd plugin list reads {self.repo} as '?'", out)
        self.assertEqual(self.adds(), [])

    def test_a_failed_list_is_unknown_not_missing(self):
        self.list_rc = "4"

        out = self.run_repos("--apply")

        self.assertIn("SKIP    sys sd plugin: sd plugin list failed (exit 4)", out)
        self.assertNotIn("MISSING sys sd plugin", out)
        self.assertEqual(self.adds(), [])

    def test_a_failed_add_is_reported_as_a_failure(self):
        base.write_exec(self.pack / "bin/sd", SD.replace('"plugin add")', '"plugin add")\n    exit 5'))

        result = subprocess.run([str(self.folder / "machine-setup.sh"), "update", "repos", "--apply"],
                                env=self.env(), capture_output=True, text=True, cwd=self.cwd,
                                stdin=subprocess.DEVNULL, timeout=120)

        self.assertIn(f"FAILED  {self.pack}/bin/sd plugin add {self.repo} (exit 5)", result.stdout)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)

    def test_a_checkout_without_the_manifest_skips(self):
        (self.repo / "sd-plugin.json").unlink()

        out = self.run_repos("--apply")

        self.assertIn(f"SKIP    sys sd plugin: {self.repo} has no sd-plugin.json", out)
        self.assertEqual(self.log.exists(), False)

    def test_a_linked_worktree_registers_the_main_checkout(self):
        # A worktree's path dies with the worktree (sd:3390).
        git = {"HOME": str(self.home), "PATH": "/usr/bin:/bin", "GIT_CONFIG_GLOBAL": os.devnull,
               "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.test",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.test"}
        tree = self.repo.parent / "tree"
        for cmd in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "fixture"],
                    ["worktree", "add", "-q", str(tree)]):
            subprocess.run(["git", "-C", str(self.repo), *cmd], check=True, env=git,
                           capture_output=True, stdin=subprocess.DEVNULL)

        self.run_repos("--apply", script=tree / "local-machine-setup/machine-setup.sh")

        self.assertEqual(self.adds(), [f"plugin add {os.path.realpath(self.repo)}"])

    def test_status_counts_the_missing_plugin_and_not_once_it_is_added(self):
        # status runs every stage; these answer "cannot read" the same way
        # before and after, so only the repos stage moves the count.
        for name in ("sudo", "launchctl", "defaults", "mas"):
            base.write_exec(self.stubs / name, "#!/bin/sh\nexit 1\n")
        before = self.run_verb("status")
        self.run_repos("--apply")
        after = self.run_verb("status")

        drift_before = int(before.split("drift   : ")[1].split()[0])
        drift_after = int(after.split("drift   : ")[1].split()[0])
        self.assertIn("MISSING sys sd plugin", before)
        self.assertIn("ok      sys sd plugin", after)
        self.assertEqual(drift_before - drift_after, 1, before + after)

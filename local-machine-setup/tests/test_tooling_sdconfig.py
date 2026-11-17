"""The tooling stage sets the pack settings a `.sdconfig` profile file lists.

`common.sdconfig` and `<profile>.sdconfig` hold one `key value` line each,
such as `sd.bundle_max 5` (sd:9142). The stage reads each with
`$SD_PACK_ROOT/bin/sd config get` and runs `sd config set` for one this
machine lacks or holds differently. A stub `sd` keeps the settings in a file.
"""

import pathlib
import shutil
import stat
import subprocess
import tempfile
import unittest

from tests import fixture_config

HERE = pathlib.Path(__file__).resolve().parent
FOLDER = HERE.parent
LIB = FOLDER.parent / "lib"

# `config get` prints the stored value or exits 1; `config set` stores it.
SD_STUB = r"""#!/bin/sh
[ "$1" = config ] || exit 2
case "$2" in
  get) v=$(sed -n "s/^$3=//p" "$SD_STORE" 2>/dev/null); [ -n "$v" ] || exit 1; echo "$v" ;;
  set) grep -v "^$3=" "$SD_STORE" > "$SD_STORE.new" 2>/dev/null || :
       echo "$3=$4" >> "$SD_STORE.new"; mv "$SD_STORE.new" "$SD_STORE" ;;
  *) exit 2 ;;
esac
"""


def write_exec(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class ToolingSdconfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = pathlib.Path(self.tmp.name)
        repo = base / "repo"
        self.folder = repo / "local-machine-setup"
        shutil.copytree(FOLDER, self.folder, ignore=shutil.ignore_patterns("tests", "__pycache__"))
        shutil.copytree(LIB, repo / "lib", ignore=shutil.ignore_patterns("tests", "__pycache__"))
        self.config_root = fixture_config.copy_config(base)
        self.profiles = self.config_root / "machine-setup/profiles"
        self.home = base / "home"
        self.state = self.home / ".config/machine-setup"
        self.state.mkdir(parents=True)
        (self.state / "profile").write_text("personal\n")
        self.pack = base / "pack"
        write_exec(self.pack / "bin/sd", SD_STUB)
        self.store = base / "sd-settings"
        self.store.write_text("sd.gate_slots=5\n")
        self.stubs = base / "stubs"
        write_exec(self.stubs / "brew", "#!/bin/sh\nexit 0\n")
        write_exec(self.stubs / "sudo", "#!/bin/sh\nexit 1\n")
        fixture_config.seal(self, self.stubs)

    def tooling(self, *flags):
        env = {
            "HOME": str(self.home),
            "MACHINE_SETUP_STATE": str(self.state),
            "PATH": f"{self.stubs}:/usr/bin:/bin:/usr/sbin:/sbin",
            "LANG": "en_US.UTF-8",
            **fixture_config.env(self.config_root),
            "SYSTEM_TOOLS_LABEL_PREFIX": fixture_config.LABEL_PREFIX,
            "SD_PACK_ROOT": str(self.pack),
            "SD_STORE": str(self.store),
        }
        r = subprocess.run([str(self.folder / "machine-setup.sh"), "update", "tooling", *flags],
                           env=env, capture_output=True, text=True, cwd=self.tmp.name,
                           stdin=subprocess.DEVNULL, timeout=60)
        return r.stdout + r.stderr

    def test_a_dry_run_names_the_unset_setting_and_writes_nothing(self):
        (self.profiles / "personal.sdconfig").write_text("sd.gate_slots 5\nsd.bundle_max 5  # trial\n")

        out = self.tooling()

        self.assertIn("  ok      sd setting sd.gate_slots=5", out)
        self.assertIn("  DIFFERS sd setting sd.bundle_max is unset, want 5", out)
        self.assertIn("[dry-run]", out)
        self.assertNotIn("bundle_max", self.store.read_text())

    def test_apply_sets_a_differing_value_and_a_rerun_reads_ok(self):
        (self.profiles / "personal.sdconfig").write_text("sd.gate_slots 3\n")

        out = self.tooling("--apply")

        self.assertIn("  DIFFERS sd setting sd.gate_slots is 5, want 3", out)
        self.assertEqual(self.store.read_text(), "sd.gate_slots=3\n", out)
        self.assertIn("  ok      sd setting sd.gate_slots=3", self.tooling())

    def test_no_sdconfig_file_says_nothing(self):
        self.assertNotIn("sd setting", self.tooling("--apply"))


if __name__ == "__main__":
    unittest.main()

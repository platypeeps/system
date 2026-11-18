"""The tooling stage sets the pack settings a `.sdconfig` profile file lists.

`common.sdconfig` and `<profile>.sdconfig` hold one `key value` line each,
such as `sd.bundle_max 5` (sd:9142). The stage reads each with
`$SD_PACK_ROOT/bin/sd config get` and runs `sd config set` for one this
machine lacks or holds differently. A stub `sd` keeps the settings in a file.
"""

import json
import pathlib
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

from tests import fixture_config

HERE = pathlib.Path(__file__).resolve().parent
FOLDER = HERE.parent
LIB = FOLDER.parent / "lib"

# As the real one: `config get` prints the value, or says "is not set" and
# exits 1, an empty stored value too; with $SD_GET_FAILS it fails as a
# locked database would. Keys match whole, not as a pattern (sd:9226).
SD_STUB = r"""#!/bin/sh
[ "$1" = config ] || exit 2
case "$2" in
  get) [ -z "$SD_GET_FAILS" ] || { echo "sd: database is locked" >&2; exit 2; }
       v=$(awk -v k="$3" 'index($0, k "=") == 1 { print substr($0, length(k) + 2) }' "$SD_STORE" 2>/dev/null)
       [ -n "$v" ] || { echo "sd: $3 is not set (help). Run:" >&2; exit 1; }
       [ -z "$SD_GET_WARN" ] || echo "sd: warning: stale pin" >&2
       echo "$v" ;;
  set) awk -v k="$3" 'index($0, k "=") != 1' "$SD_STORE" > "$SD_STORE.new" 2>/dev/null || :
       echo "$3=$4" >> "$SD_STORE.new"; mv "$SD_STORE.new" "$SD_STORE" ;;
  unset) awk -v k="$3" 'index($0, k "=") != 1' "$SD_STORE" > "$SD_STORE.new" 2>/dev/null || :
       mv "$SD_STORE.new" "$SD_STORE" ;;
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
        self.get_fails = ""
        self.get_warn = ""
        self.env = {}
        self.write_sd_lib()

    def write_sd_lib(self, body=None):
        """The pack's key lists, as sd_lib declares them, and its machine
        config, `{"config": {"sd": {...}}}`, read from the stub's store."""
        (self.pack / "bin/sd_lib.py").write_text(body or (
            'import os\n'
            'CORE_CONFIG = dict.fromkeys(("gate_slots", "bundle_max", "bulk_storage_root",'
            ' "hub_name", "copilot_review", "fleet_owners"))\n'
            'FLEET_KEYS = ("hub_name", "copilot_review", "fleet_owners")\n'
            'def machine_config():\n'
            '    lines = open(os.environ["SD_STORE"]).read().splitlines()\n'
            '    return {"config": {"sd": {k.removeprefix("sd."): v for k, _, v in'
            ' (line.partition("=") for line in lines)}}}\n'))

    def tooling(self, *flags, verb=("update", "tooling")):
        env = {
            "HOME": str(self.home),
            "MACHINE_SETUP_STATE": str(self.state),
            "PATH": f"{self.stubs}:/usr/bin:/bin:/usr/sbin:/sbin",
            "LANG": "en_US.UTF-8",
            **fixture_config.env(self.config_root),
            "SYSTEM_TOOLS_LABEL_PREFIX": fixture_config.LABEL_PREFIX,
            "SD_PACK_ROOT": str(self.pack),
            "SD_STORE": str(self.store),
            "SD_GET_FAILS": self.get_fails,
            "SD_GET_WARN": self.get_warn,
            **self.env,
        }
        r = subprocess.run([str(self.folder / "machine-setup.sh"), *verb, *flags],
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

    def test_a_failed_read_is_a_failure_and_writes_nothing(self):
        (self.profiles / "personal.sdconfig").write_text("sd.gate_slots 3\n")
        self.get_fails = "1"

        out = self.tooling("--apply")

        self.assertIn("  FAILED  sd config get sd.gate_slots", out)
        self.assertNotIn("is unset", out)
        self.assertEqual(self.store.read_text(), "sd.gate_slots=5\n", out)

    def test_a_warning_on_a_good_read_is_not_part_of_the_value(self):
        (self.profiles / "personal.sdconfig").write_text("sd.gate_slots 5\n")
        self.get_warn = "1"

        out = self.tooling("--apply")

        self.assertIn("  ok      sd setting sd.gate_slots=5", out)
        self.assertNotIn("DIFFERS", out)

    def test_the_profile_line_wins_over_common(self):
        (self.profiles / "common.sdconfig").write_text("sd.gate_slots 9\n")
        (self.profiles / "personal.sdconfig").write_text("sd.gate_slots 3\n")

        out = self.tooling("--apply")

        self.assertEqual(self.store.read_text(), "sd.gate_slots=3\n", out)
        self.assertNotIn("want 9", out)

    def test_a_line_with_no_value_fails_and_writes_nothing(self):
        (self.profiles / "personal.sdconfig").write_text("sd.gate_slots #3\n")

        out = self.tooling("--apply")

        self.assertIn("  FAILED  personal.sdconfig: sd.gate_slots has no value", out)
        self.assertEqual(self.store.read_text(), "sd.gate_slots=5\n", out)

    def test_a_key_the_bulkmove_stage_owns_is_refused(self):
        self.store.write_text("sd.bulk_storage_root=/nas\n")
        (self.profiles / "personal.sdconfig").write_text("sd.bulk_storage_root /local\n")

        out = self.tooling("--apply")

        self.assertIn("  FAILED  personal.sdconfig: sd.bulk_storage_root is the bulkmove stage's", out)
        self.assertEqual(self.store.read_text(), "sd.bulk_storage_root=/nas\n", out)

    def test_keys_cswap_and_the_hub_role_own_are_refused(self):
        (self.profiles / "personal.sdconfig").write_text("sys.cswap_account 2\nsd.hub_name hub.example.test\n")

        out = self.tooling("--apply")

        self.assertIn("  FAILED  personal.sdconfig: sys.cswap_account is local-cswap's", out)
        self.assertIn("  FAILED  personal.sdconfig: sd.hub_name is the hub role's", out)
        self.assertEqual(self.store.read_text(), "sd.gate_slots=5\n", out)

    def test_report_keeps_one_setting_per_line(self):
        (self.profiles / "personal.sdconfig").write_text("sd.bundle_max 5\nsd.gate_slots 3\n")

        out = self.tooling(verb=("report", "personal"))

        self.assertIn("      sd.bundle_max 5\n      sd.gate_slots 3\n", out)

    def test_report_keeps_every_multi_field_kind_one_entry_per_line(self):
        kinds = {
            "mas": "497799835 Xcode\n409183694 Keynote\n",
            "service": "local-trellis install\nlocal-redis start\n",
            "node": "24\n26 link\n",
            "release": "aa example/aa aa.tar.gz\nbb example/bb bb.tar.gz\n",
            "app": "Visual Studio Code\nExample App\n",
        }
        for kind, lines in kinds.items():
            with self.subTest(kind=kind):
                (self.profiles / f"personal.{kind}").write_text(lines)
                out = self.tooling(verb=("report", "personal"))
                want = "".join(f"      {line}\n" for line in lines.splitlines())
                self.assertIn(want, out)
                (self.profiles / f"personal.{kind}").unlink()

    # sd:9218: an `unset <key>` line unsets the key here; `!key` keeps its old
    # meaning, drop common's line only (lane review, 2026-11-17).
    # Review round 5 of sd:9218, class: a line's key read from the wrong field.
    def test_a_bang_line_drops_commons_unset_line_too(self):
        self.store.write_text("sd.bundle_max=5\n")
        (self.profiles / "common.sdconfig").write_text("unset sd.bundle_max\n")
        (self.profiles / "personal.sdconfig").write_text("!sd.bundle_max\n")

        out = self.tooling("--apply")

        self.assertEqual(self.store.read_text(), "sd.bundle_max=5\n", out)
        self.assertNotIn("config unset", out)
        self.assertNotIn("FAILED", out)

    def test_a_fail_line_names_the_file_with_the_bad_line(self):
        self.store.write_text("sd.gate_slots=5\n")
        (self.profiles / "common.sdconfig").write_text("sd.gate_slots 4\nunset sd.gate_slots\n")
        (self.profiles / "personal.sdconfig").write_text("sd.gate_slots 8\n")

        out = self.tooling("--apply")

        self.assertIn("  FAILED  common.sdconfig: sd.gate_slots is both set and unset", out)
        self.assertEqual(self.store.read_text(), "sd.gate_slots=5\n", out)

    def test_an_unset_line_unsets_the_key(self):
        self.store.write_text("sd.gate_slots=5\nsd.bundle_max=5\n")
        (self.profiles / "personal.sdconfig").write_text("unset sd.bundle_max  # trial over\n")

        dry = self.tooling()
        self.assertIn("  DIFFERS sd setting sd.bundle_max is 5, want unset", dry)
        self.assertIn("bundle_max", self.store.read_text())

        out = self.tooling("--apply")
        self.assertEqual(self.store.read_text(), "sd.gate_slots=5\n", out)
        self.assertIn("  ok      sd setting sd.bundle_max unset", self.tooling())

    def test_a_bang_line_only_drops_common_and_keeps_this_machines_value(self):
        self.store.write_text("sd.gate_slots=5\nsd.bundle_max=5\n")
        (self.profiles / "common.sdconfig").write_text("sd.bundle_max 4\n")
        (self.profiles / "personal.sdconfig").write_text("!sd.bundle_max\n")

        out = self.tooling("--apply")

        self.assertEqual(self.store.read_text(), "sd.gate_slots=5\nsd.bundle_max=5\n", out)
        self.assertNotIn("want 4", out)
        self.assertNotIn("config unset", out)

    def test_a_profile_unset_line_wins_over_commons_value(self):
        self.store.write_text("sd.gate_slots=5\nsd.bundle_max=4\n")
        (self.profiles / "common.sdconfig").write_text("sd.bundle_max 4\n")
        (self.profiles / "personal.sdconfig").write_text("unset sd.bundle_max\n")

        out = self.tooling("--apply")

        self.assertEqual(self.store.read_text(), "sd.gate_slots=5\n", out)

    def test_a_key_both_set_and_unset_fails_and_writes_nothing(self):
        self.store.write_text("sd.bundle_max=5\n")
        (self.profiles / "personal.sdconfig").write_text("sd.bundle_max 4\nunset sd.bundle_max\n")

        out = self.tooling("--apply")

        self.assertIn("  FAILED  personal.sdconfig: sd.bundle_max is both set and unset", out)
        self.assertEqual(self.store.read_text(), "sd.bundle_max=5\n", out)

    def test_an_owned_key_is_never_unset(self):
        self.store.write_text("sd.bulk_storage_root=/nas\n")
        (self.profiles / "personal.sdconfig").write_text("unset sd.bulk_storage_root\n")

        out = self.tooling("--apply")

        self.assertIn("  FAILED  personal.sdconfig: sd.bulk_storage_root is the bulkmove stage's", out)
        self.assertEqual(self.store.read_text(), "sd.bulk_storage_root=/nas\n", out)

    def test_a_failed_read_never_unsets(self):
        self.store.write_text("sd.bundle_max=5\n")
        (self.profiles / "personal.sdconfig").write_text("unset sd.bundle_max\n")
        self.get_fails = "1"

        out = self.tooling("--apply")

        self.assertIn("  FAILED  sd config get sd.bundle_max", out)
        self.assertEqual(self.store.read_text(), "sd.bundle_max=5\n", out)

    def test_a_fleet_plugin_or_undeclared_key_is_never_unset(self):
        self.store.write_text("sd.copilot_review=never\n")
        (self.profiles / "personal.sdconfig").write_text(
            "unset sd.copilot_review\nunset plugin.thing\nunset sd.no_such_key\n")

        out = self.tooling("--apply")

        for key in ("sd.copilot_review", "plugin.thing", "sd.no_such_key"):
            self.assertIn(f"  FAILED  personal.sdconfig: {key} is no per-machine sd setting", out)
        self.assertNotIn("config unset", out)
        self.assertEqual(self.store.read_text(), "sd.copilot_review=never\n", out)

    def test_an_unloadable_key_list_fails_unset_lines_and_still_sets(self):
        """Lane review of sd:9218: the key list gates unset lines only, not the whole file."""
        self.write_sd_lib("raise ImportError('example')\n")
        self.store.write_text("sd.gate_slots=5\nsd.bundle_max=5\n")
        (self.profiles / "personal.sdconfig").write_text("sd.gate_slots 3\nunset sd.bundle_max\n")

        out = self.tooling("--apply")

        self.assertIn("  FAILED  sd settings: the pack's key list did not load: ImportError: example", out)
        self.assertNotIn("Traceback", out)
        self.assertEqual(sorted(self.store.read_text().splitlines()), ["sd.bundle_max=5", "sd.gate_slots=3"], out)

    def test_an_unset_line_with_a_value_or_no_key_fails(self):
        self.store.write_text("sd.bundle_max=5\n")
        (self.profiles / "personal.sdconfig").write_text("unset sd.bundle_max 4\nunset\n")

        out = self.tooling("--apply")

        self.assertIn("  FAILED  personal.sdconfig: unset sd.bundle_max takes no value", out)
        self.assertIn("  FAILED  personal.sdconfig: unset needs a key", out)
        self.assertEqual(self.store.read_text(), "sd.bundle_max=5\n", out)

    def test_one_bad_unset_line_stops_every_write_for_its_key(self):
        self.store.write_text("sd.bundle_max=5\n")
        (self.profiles / "personal.sdconfig").write_text("unset sd.bundle_max 4\nunset sd.bundle_max\n")

        out = self.tooling("--apply")

        self.assertIn("  FAILED  personal.sdconfig: unset sd.bundle_max takes no value", out)
        self.assertNotIn("config unset", out)
        self.assertEqual(self.store.read_text(), "sd.bundle_max=5\n", out)

    # sd:9226: review advisories of sd:9218.
    def test_an_empty_stored_value_is_unset_too(self):
        # `config get` reads an empty value as not set, so the stage said
        # ok while config.json still held the key.
        self.store.write_text("sd.gate_slots=5\nsd.bundle_max=\n")
        (self.profiles / "personal.sdconfig").write_text("unset sd.bundle_max\n")

        dry = self.tooling()
        self.assertIn("  DIFFERS sd setting sd.bundle_max holds an empty value, want unset", dry)
        self.assertIn("bundle_max", self.store.read_text())

        out = self.tooling("--apply")
        self.assertEqual(self.store.read_text(), "sd.gate_slots=5\n", out)
        self.assertIn("  ok      sd setting sd.bundle_max unset", self.tooling())

    def test_report_and_compare_name_what_a_bang_line_drops(self):
        # A `!` line is no entry and no count holds it (sd:4916), yet a
        # profile that drops common's key read as one that keeps it.
        (self.profiles / "personal.sdconfig").write_text("! sd.bundle_max  # off here\nsd.gate_slots 3\n")
        (self.profiles / "personal.cask").write_text("!example-cask\n")

        report = self.tooling(verb=("report", "personal"))
        self.assertIn("    sdconfig (1)\n      sd.gate_slots 3\n      drops common's sd.bundle_max\n", report)
        self.assertIn("    cask (0)\n      drops common's example-cask\n", report)
        counts = self.tooling(verb=("report",))
        self.assertRegex(counts, r"\n    sdconfig +0 +1 ")
        compare = self.tooling(verb=("compare", "personal", "work"))
        self.assertIn("== sdconfig (match: 0, only personal: 1, only work: 0)\n", compare)
        self.assertIn("  <personal    drops common's sd.bundle_max\n", compare)

    def test_a_bang_line_drops_only_its_whole_key(self):
        # A malformed `!sd.bundle_max,typo` must not drop common's line.
        self.store.write_text("sd.bundle_max=5\n")
        (self.profiles / "common.sdconfig").write_text("sd.bundle_max 4\n")
        (self.profiles / "personal.sdconfig").write_text("!sd.bundle_max,typo\n")

        out = self.tooling("--apply")

        self.assertIn("  DIFFERS sd setting sd.bundle_max is 5, want 4", out)
        self.assertEqual(self.store.read_text(), "sd.bundle_max=4\n", out)

    def test_a_key_list_that_times_out_says_so(self):
        self.write_sd_lib("import time\ntime.sleep(10)\n")
        self.env = {"MACHINE_SETUP_STEP_TIMEOUT": "1"}
        (self.profiles / "personal.sdconfig").write_text("unset sd.bundle_max\n")

        out = self.tooling("--apply")

        self.assertIn("  FAILED  sd settings: the pack's key list did not load: timed out (exit 1)\n", out)

    def test_a_key_is_never_read_as_a_pattern(self):
        self.store.write_text("sdXbundle_max=7\n")
        (self.profiles / "personal.sdconfig").write_text("sd.bundle_max 5\n")

        out = self.tooling()

        self.assertIn("  DIFFERS sd setting sd.bundle_max is unset, want 5", out)

    def test_the_key_list_and_unset_against_the_pinned_pack(self):
        # The stage's sd_lib probe and `sd config` bound to the real pack,
        # not a stub: a per-machine key with an empty value is unset, and a
        # fleet key refused.
        from tests.test_backbone import PACK
        write_exec(self.stubs / "python3", f"#!/bin/sh\nexec {sys.executable} \"$@\"\n")
        config = self.home / ".config/sd-ai-command-pack/config.json"
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({"config": {"sd": {"gate_slots": "5", "review_slots": ""}}}))
        (self.profiles / "personal.sdconfig").write_text("unset sd.review_slots\nunset sd.copilot_review\n")
        self.env = {"SD_PACK_ROOT": str(PACK)}

        out = self.tooling("--apply")

        self.assertIn("  DIFFERS sd setting sd.review_slots holds an empty value, want unset", out)
        self.assertIn("  FAILED  personal.sdconfig: sd.copilot_review is no per-machine sd setting", out)
        self.assertEqual(json.loads(config.read_text()), {"config": {"sd": {"gate_slots": "5"}}}, out)
        self.assertIn("  ok      sd setting sd.review_slots unset", self.tooling())

    def test_a_config_json_that_does_not_load_is_named(self):
        # sd:9282 round 1: a malformed config.json read as a key list that
        # did not load.
        from tests.test_backbone import PACK
        write_exec(self.stubs / "python3", f"#!/bin/sh\nexec {sys.executable} \"$@\"\n")
        config = self.home / ".config/sd-ai-command-pack/config.json"
        config.parent.mkdir(parents=True)
        config.write_text("{not json")
        (self.profiles / "personal.sdconfig").write_text("unset sd.review_slots\n")
        self.env = {"SD_PACK_ROOT": str(PACK)}

        out = self.tooling("--apply")

        self.assertRegex(out, r"  FAILED  sd settings: the machine config did not load: \S*config.json is not valid JSON")

    def test_no_sdconfig_file_says_nothing(self):
        self.assertNotIn("sd setting", self.tooling("--apply"))


if __name__ == "__main__":
    unittest.main()

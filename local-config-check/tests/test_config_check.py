"""config-check.sh against fake tool folders and a temp config dir.

Every test but the last builds its own tree under a temp dir, points
CONFIG_CHECK_ROOT at it and SYSTEM_TOOLS_CONFIG at a temp config dir, so
nothing reads the real tools or the real config. The last runs `list` over
the real tree, which reads only committed example files.
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

FOLDER = Path(__file__).resolve().parent.parent
ENTRYPOINT = FOLDER / "config-check.sh"
SENTINEL = "sentinel-value-never-printed"


class ConfigCheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.root = self.tmp / "tree"
        self.config = self.tmp / "config"
        self.bin = self.tmp / "bin"
        for d in (self.root, self.config, self.bin):
            d.mkdir()

    def folder(self, name, files):
        d = self.root / name
        d.mkdir()
        for fname, text in files.items():
            (d / fname).write_text(text)
        return d

    def conf(self, tool, fname, text):
        d = self.config / tool
        d.mkdir(exist_ok=True)
        (d / fname).write_text(text)

    def fake_sd(self, known):
        script = self.bin / "sd"
        cases = "|".join(known) if known else "__none__"
        script.write_text(
            "#!/bin/sh\n"
            '[ "$1 $2" = "config get" ] || exit 2\n'
            f'case "$3" in {cases}) echo ok; exit 0 ;; esac\n'
            "exit 1\n")
        script.chmod(0o755)

    def run_cc(self, *args, sd=False):
        path = "/usr/bin:/bin"
        if sd:
            path = f"{self.bin}:{path}"
        env = {"PATH": path, "HOME": str(self.tmp), "CONFIG_CHECK_ROOT": str(self.root),
               "SYSTEM_TOOLS_CONFIG": str(self.config)}
        return subprocess.run(["sh", str(ENTRYPOINT), *args], env=env, capture_output=True,
                              text=True, timeout=60)

    def assertNoSecret(self, proc):
        self.assertNotIn(SENTINEL, proc.stdout + proc.stderr)

    # --- entrypoint conventions -------------------------------------------

    def test_no_args_prints_usage_to_stderr_and_exits_1(self):
        proc = self.run_cc()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("Usage:", proc.stderr)
        self.assertEqual(proc.stdout, "")

    def test_help_exits_0_and_declares_the_status_contract(self):
        for arg in ("help", "-h", "--help"):
            proc = self.run_cc(arg)
            self.assertEqual(proc.returncode, 0, arg)
            self.assertIn("local-health-check reads these codes", proc.stdout)

    # --- check ------------------------------------------------------------

    def test_required_var_missing_and_placeholder_are_named(self):
        self.folder("local-alpha", {".env.example": "ALPHA_USER=change-me\nALPHA_PASS=change-me\n# ALPHA_OPT=x\n"})
        self.conf("alpha", ".env", f"ALPHA_USER=change-me\nALPHA_OTHER={SENTINEL}\n")
        proc = self.run_cc("check")
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("alpha: broken (.env present; 2 required, 1 optional)", proc.stdout)
        self.assertIn("  missing: ALPHA_PASS", proc.stdout)
        self.assertIn("  placeholder: ALPHA_USER", proc.stdout)
        self.assertNotIn("ALPHA_OPT", proc.stdout)
        self.assertNoSecret(proc)

    def test_complete_env_is_ok_and_values_never_print(self):
        self.folder("local-alpha", {".env.example": "ALPHA_USER=change-me\nexport ALPHA_PASS=change-me\n"})
        self.conf("alpha", ".env", f"ALPHA_USER=admin\nexport ALPHA_PASS={SENTINEL}\n")
        proc = self.run_cc("check")
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("alpha: ok (.env present; 2 required, 0 optional)", proc.stdout)
        self.assertNotIn("admin", proc.stdout)
        self.assertNoSecret(proc)

    def test_other_placeholders_count(self):
        self.folder("local-alpha", {".env.example": "A_URL=x\nB_HOST=x\n"})
        self.conf("alpha", ".env", "A_URL=/path/to/thing\nB_HOST=box.example.test\n")
        proc = self.run_cc("check")
        self.assertIn("  placeholder: A_URL", proc.stdout)
        self.assertIn("  placeholder: B_HOST", proc.stdout)

    def test_path_suffixes_are_checked(self):
        real = self.tmp / "exists"
        real.mkdir()
        self.folder("local-alpha", {".env.example":
                                    "GOOD_DIR=x\nBAD_DIR=x\nBAD_FILE=x\nBAD_REPO=x\nBAD_SRC=x\n"
                                    "BAD_SOURCE=x\nBAD_DESTINATION=x\nTILDE_DIR=x\n# OPT_FILE=x\n"})
        self.conf("alpha", ".env",
                  f"GOOD_DIR={real}\nBAD_DIR=/nonexistent/a\nBAD_FILE=/nonexistent/b\n"
                  "BAD_REPO=/nonexistent/c\nBAD_SRC=/nonexistent/d\nBAD_SOURCE=/nonexistent/e\n"
                  "BAD_DESTINATION=/nonexistent/f\nTILDE_DIR='~/exists'\nOPT_FILE=/nonexistent/g\n")
        proc = self.run_cc("check")
        self.assertEqual(proc.returncode, 1)
        for name in ("BAD_DIR", "BAD_FILE", "BAD_REPO", "BAD_SRC", "BAD_SOURCE", "BAD_DESTINATION",
                     "OPT_FILE"):
            self.assertIn(f"  path missing: {name}\n", proc.stdout)
        self.assertNotIn("GOOD_DIR", proc.stdout)
        self.assertNotIn("TILDE_DIR", proc.stdout)
        self.assertNotIn("/nonexistent", proc.stdout)

    def test_marker_dir_may_be_absent(self):
        # A *_MARKER_DIR names a directory whose existence tells machines apart.
        real = self.tmp / "exists"
        real.mkdir()
        self.folder("local-alpha", {".env.example": "# WORK_MARKER_DIR=x\n# HOME_MARKER_DIR=x\n"})
        self.conf("alpha", ".env", f"WORK_MARKER_DIR=/nonexistent/a\nHOME_MARKER_DIR={real}\n")
        proc = self.run_cc("check")
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("alpha: ok", proc.stdout)
        self.assertNotIn("MARKER_DIR", proc.stdout)

    def test_marker_dir_placeholder_still_counts(self):
        self.folder("local-alpha", {".env.example": "# WORK_MARKER_DIR=x\n"})
        self.conf("alpha", ".env", "WORK_MARKER_DIR=/path/to/marker\n")
        proc = self.run_cc("check")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("  placeholder: WORK_MARKER_DIR", proc.stdout)

    def test_sd_key_checked_through_sd_config_get(self):
        self.folder("local-alpha", {".env.example": "GOOD_SD_KEY=x\nBAD_SD_KEY=x\n"})
        self.conf("alpha", ".env", "GOOD_SD_KEY=known.key\nBAD_SD_KEY=unknown.key\n")
        self.fake_sd(["known.key"])
        proc = self.run_cc("check", sd=True)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("  sd key unreadable: BAD_SD_KEY", proc.stdout)
        self.assertNotIn("GOOD_SD_KEY", proc.stdout)
        self.assertNotIn("unknown.key", proc.stdout)

    def test_sd_key_skipped_with_note_when_sd_absent(self):
        self.folder("local-alpha", {".env.example": "SOME_SD_KEY=x\n"})
        self.conf("alpha", ".env", "SOME_SD_KEY=any.key\n")
        proc = self.run_cc("check")
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("alpha: ok", proc.stdout)
        self.assertIn("  note: sd not on PATH; SOME_SD_KEY not checked", proc.stdout)

    def test_all_optional_without_env_is_optional_not_broken(self):
        self.folder("local-beta", {".env.example": "# BETA_A=x\n#BETA_B=y\n"})
        proc = self.run_cc("check")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("beta: optional (no .env; every variable optional)", proc.stdout)

    def test_required_without_env_is_unconfigured(self):
        self.folder("local-gamma", {".env.example": "GAMMA_TOKEN=change-me\n"})
        proc = self.run_cc("check")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("gamma: unconfigured (.env absent; 1 required)", proc.stdout)

    def test_env_that_does_not_source_is_broken(self):
        self.folder("local-alpha", {".env.example": "A=x\n"})
        self.conf("alpha", ".env", "A=1\nif then fi (\n")
        proc = self.run_cc("check")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("  error: .env did not source", proc.stdout)

    def test_other_examples_map_to_config_files(self):
        self.folder("local-delta", {"repos.one.conf.example": "x\n", "hosts.conf.example": "y\n"})
        self.conf("delta", "hosts.conf", "real\n")
        proc = self.run_cc("check")
        self.assertIn("delta: ok (no .env.example)", proc.stdout)
        self.assertIn("  conf: hosts.conf present", proc.stdout)
        self.assertIn("  conf: repos.one.conf absent", proc.stdout)

    def test_privacy_patterns_map_to_the_config_root(self):
        # leak-guard reads <config>/privacy-patterns, not <config>/leak-guard/.
        self.folder("local-leak-guard", {"privacy-patterns.example": "# x\n"})
        (self.config / "privacy-patterns").write_text(f"{SENTINEL}\n")
        proc = self.run_cc("check")
        self.assertIn("leak-guard: ok (no .env.example)", proc.stdout)
        self.assertIn("  conf: privacy-patterns present", proc.stdout)
        self.assertNoSecret(proc)
        proc = self.run_cc("list")
        self.assertIn(f"leak-guard\tlocal-leak-guard/privacy-patterns.example -> "
                      f"{self.config}/privacy-patterns", proc.stdout.splitlines())

    def test_tool_names_strip_local_and_keep_other_prefixes(self):
        self.folder("vendor-thing", {".env.example": "# X=1\n"})
        self.folder("network-testing", {"hosts.conf.example": "x\n"})
        self.folder("no-examples", {"README.md": "x\n"})
        proc = self.run_cc("check")
        self.assertIn("vendor-thing: optional", proc.stdout)
        self.assertIn("network-testing: unconfigured", proc.stdout)
        self.assertNotIn("no-examples", proc.stdout)
        self.assertIn("config-check: 2 tool(s): 0 ok, 0 broken, 1 unconfigured, 1 optional", proc.stdout)

    def test_check_named_tools_only_and_rejects_unknown(self):
        self.folder("local-alpha", {".env.example": "A=x\n"})
        self.folder("local-beta", {".env.example": "# B=x\n"})
        proc = self.run_cc("check", "beta")
        self.assertIn("beta:", proc.stdout)
        self.assertNotIn("alpha:", proc.stdout)
        proc = self.run_cc("check", "local-alpha")
        self.assertIn("alpha:", proc.stdout)
        proc = self.run_cc("check", "nosuch")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("no tool named nosuch", proc.stderr)

    def test_exported_value_satisfies_required_var(self):
        self.folder("local-alpha", {".env.example": "A_TOKEN=x\nB_TOKEN=x\n"})
        self.conf("alpha", ".env", "A_TOKEN=1\n")
        env_extra = {"B_TOKEN": "exported"}
        env = {"PATH": "/usr/bin:/bin", "HOME": str(self.tmp), "CONFIG_CHECK_ROOT": str(self.root),
               "SYSTEM_TOOLS_CONFIG": str(self.config), **env_extra}
        proc = subprocess.run(["sh", str(ENTRYPOINT), "check"], env=env, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("alpha: ok", proc.stdout)

    # --- status -----------------------------------------------------------

    def test_status_3_when_nothing_configured(self):
        self.folder("local-alpha", {".env.example": "A=x\n"})
        self.folder("local-beta", {".env.example": "# B=x\n"})
        proc = self.run_cc("status")
        self.assertEqual(proc.returncode, 3, proc.stdout)
        self.assertTrue(proc.stdout.startswith("local-config-check: no tool configured"))

    def test_status_0_when_configured_tools_healthy(self):
        self.folder("local-alpha", {".env.example": "A=x\n"})
        self.folder("local-beta", {".env.example": "B=x\n"})
        self.conf("alpha", ".env", "A=1\n")
        proc = self.run_cc("status")
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(proc.stdout.strip(), "local-config-check: 1 configured tool(s) healthy")

    def test_status_1_names_broken_tools_only(self):
        self.folder("local-alpha", {".env.example": "A=x\n"})
        self.folder("local-beta", {".env.example": "B=x\n"})
        self.conf("alpha", ".env", f"A=change-me-{SENTINEL}\n")
        self.conf("beta", ".env", "B=1\n")
        proc = self.run_cc("status")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("1 of 2 configured tool(s) broken: alpha;", proc.stdout)
        self.assertNoSecret(proc)

    # --- list -------------------------------------------------------------

    def test_list_maps_each_example_to_its_config_path(self):
        self.folder("local-alpha", {".env.example": "A=x\n", "notify.conf.example": "y\n"})
        proc = self.run_cc("list")
        self.assertEqual(proc.returncode, 0)
        lines = proc.stdout.splitlines()
        self.assertIn(f"alpha\tlocal-alpha/.env.example -> {self.config}/alpha/.env", lines)
        self.assertIn(f"alpha\tlocal-alpha/notify.conf.example -> {self.config}/alpha/notify.conf", lines)

    def test_list_over_the_real_tree(self):
        env = {"PATH": "/usr/bin:/bin", "HOME": str(self.tmp), "SYSTEM_TOOLS_CONFIG": str(self.config)}
        proc = subprocess.run(["sh", str(ENTRYPOINT), "list"], env=env, capture_output=True, text=True,
                              timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        tree = FOLDER.parent
        expected = sorted(str(p.relative_to(tree)) for p in tree.glob("*/*.example")) + \
            sorted(str(p.relative_to(tree)) for p in tree.glob("*/.*.example"))
        listed = [line.split("\t")[1].split(" -> ")[0] for line in proc.stdout.splitlines()]
        self.assertEqual(sorted(listed), sorted(set(expected)))
        self.assertTrue(listed, "the tree commits at least one example")
        self.assertIn(f"leak-guard\tlocal-leak-guard/privacy-patterns.example -> "
                      f"{self.config}/privacy-patterns", proc.stdout.splitlines())


if __name__ == "__main__":
    unittest.main()

"""shell-env.example names the variables this repository's tools read (sd:3105).

The example is the generic list doctor checks a machine's env.sh against when
the operator keeps no list of their own. It lacked TYPESAFE_API_KEY and every
Jev stage switch, so a machine built from it had Jev unkeyed and no record of
which switches exist.
"""

import pathlib
import re
import subprocess
import unittest

HERE = pathlib.Path(__file__).resolve().parent
FOLDER = HERE.parent
ROOT = FOLDER.parent
EXAMPLE = FOLDER / "shell-env.example"
# 0 is one of the off-words local-jev/jev.py keeps.
OFF = "0"


def example_names():
    names = {}
    for line in EXAMPLE.read_text().splitlines():
        m = re.match(r"([A-Za-z_][A-Za-z0-9_]*)=(.*)", line)
        if m:
            names[m.group(1)] = m.group(2)
    return names


class ShellEnvExampleTest(unittest.TestCase):
    def test_it_names_the_jev_key_and_the_label_prefix(self):
        names = example_names()
        self.assertEqual(names.get("TYPESAFE_API_KEY"), "change-me")
        self.assertIn("SYSTEM_TOOLS_LABEL_PREFIX", names)

    def test_each_jev_stage_switch_is_off_and_still_read_by_a_tool(self):
        stages = {n: v for n, v in example_names().items()
                  if n.startswith("JEV_")}
        self.assertTrue(stages, "no Jev stage switch in shell-env.example")
        for name, value in sorted(stages.items()):
            self.assertEqual(value, OFF, f"{name} must default to off")
            # A tracked file outside docs, tests and this example names it:
            # a stage no caller reads any more is a stale line.
            hits = subprocess.run(
                ["git", "-C", str(ROOT), "grep", "-l", "-w", name, "--",
                 ":!docs", ":!*/tests/*", ":!tests", ":!*.md",
                 ":!local-machine-setup/shell-env.example"],
                capture_output=True, text=True).stdout
            self.assertTrue(hits.strip(), f"{name}: no tool reads it")


if __name__ == "__main__":
    unittest.main()

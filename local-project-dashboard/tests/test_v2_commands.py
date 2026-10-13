"""One declaration per command (sd:3524).

`static/commands.js` holds the commands more than one page registers: Fix, Retry, Show log, Print, Show assignment, Open
on GitHub for a pull request, and Re-run collector. A page registers them through `window.commands`; it writes none itself. Before this,
Fix was declared three times under three ids, Retry four times, Show assignment three times, and a text change landed on
one page and not the others (review 2026-10-12, A2 F6).
"""

from __future__ import annotations

import re
import unittest
from collections import defaultdict
from pathlib import Path

from sd_dashboard import v2

V2 = Path(v2.__file__).resolve().parent
STATIC = V2 / "static"
SHARED = "commands.js"
SCRIPTS = {p.name: p.read_text(encoding="utf-8") for p in sorted(STATIC.glob("*.js"))}
# A declaration names its id and then the type it is on: `{ id: 'jobs.retry', on: 'job', …`.
DECLARED = re.compile(r"\bid: '([^'$`]+)',\s*on:")
# What only the shared declaration may write: each command's route or CLI line, and the answer wording.
SHARED_TEXT = ("/fix`", "`sd jobs retry ", "cron-jobs.sh", "`launchctl print ", "`sd assignments get ", "gh pr view",
               "sd shadow sync --max-seconds", "/api/shadow/state", "function answered(")


class OneDeclaration(unittest.TestCase):
    def test_no_command_id_is_declared_in_two_scripts(self):
        where = defaultdict(list)
        for name, text in SCRIPTS.items():
            for found in sorted(set(DECLARED.findall(text))):
                where[found].append(name)
        twice = {found: names for found, names in where.items() if len(names) > 1}
        self.assertEqual(twice, {}, "declare a shared command once, in commands.js")

    def test_only_the_shared_module_writes_a_shared_command(self):
        for text in SHARED_TEXT:
            with self.subTest(text=text):
                self.assertEqual([name for name, body in SCRIPTS.items() if text in body], [SHARED])

    def test_a_page_that_registers_a_shared_command_loads_the_module_before_its_script(self):
        users = sorted(name for name, body in SCRIPTS.items() if re.search(r"window\.commands\b", body) and name != SHARED)
        self.assertEqual(users, ["activity.js", "contributions.js", "health.js", "management.js", "reports.js", "tasks.js",
                                 "today.js"])
        for name in users:
            page = (V2 / name.replace(".js", ".html")).read_text(encoding="utf-8")
            scripts = re.findall(r'<script src="/ui/([^"]+)"', page)
            with self.subTest(page=name):
                self.assertIn(SHARED, scripts)
                self.assertLess(scripts.index(SHARED), scripts.index(name))


if __name__ == "__main__":
    unittest.main()

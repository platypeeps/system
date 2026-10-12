"""The v2 Settings page (sd:3438): the theme control, moved from the rail, and the sections width (sd:3445).

What this slice promises: `/settings` is a registered page with no API route; it holds the one theme control (the rail
draws none now), and its width slider sets, shows and resets the width through `shell.railWidth`. `settings.js` runs under
JavaScriptCore (osascript) against the stand-in page `test_v2_tasks` uses, with a stand-in `shell.railWidth`.
"""

from __future__ import annotations

import json
import re
import subprocess
import unittest
from pathlib import Path

from sd_dashboard import v2

from test_v2_registry import GROUPS, Registers
from test_v2_tasks import STAND_IN
from test_v2_today import OSASCRIPT

V2 = Path(v2.__file__).resolve().parent
HTML = (V2 / "settings.html").read_text(encoding="utf-8") if (V2 / "settings.html").exists() else ""
SHELL_JS = (V2 / "static" / "shell.js").read_text(encoding="utf-8")
PAGE_JS = (V2 / "static" / "settings.js").read_text(encoding="utf-8") if (V2 / "static" / "settings.js").exists() else ""


class TheRegistration(Registers, unittest.TestCase):
    page, section, route, api = "settings", "Settings", "/settings", ()


class TheThemeMoved(unittest.TestCase):
    def test_settings_holds_the_one_theme_control_and_the_rail_draws_none(self):
        self.assertEqual(re.findall(r'data-theme-choice="(\w+)"', HTML), ["dark", "light", "system"])
        self.assertNotIn("data-theme-choice", SHELL_JS)
        self.assertIn("Settings", GROUPS)


class TheWidthControl(unittest.TestCase):
    def run_page(self, body, kept="null"):
        script = (STAND_IN + "\nvar KEPT = " + kept + ", CALLS = [];\n"
                  "window.shell = { railWidth: (...a) => { CALLS.push(a); if (a.length) KEPT = a[0] === null ? null : Math.min(384, Math.max(176, Math.round(+a[0]))); return KEPT; } };\n"
                  + PAGE_JS + "\n(DOC_LISTENERS.DOMContentLoaded || []).forEach(f => f());\nvar R = {};\n" + body
                  + "\nfunction run() { R.calls = CALLS; R.value = ELS['rail-width'].value; R.out = ELS['rail-width-out'].textContent;"
                  " R.att = window.PAGE_ATTENTION; return JSON.stringify(R); }\n")
        result = subprocess.run([OSASCRIPT, "-l", "JavaScript", "-e", script], capture_output=True, text=True, timeout=60, check=False)
        if result.returncode:
            raise AssertionError(result.stderr)
        return json.loads(result.stdout)

    def test_the_slider_shows_the_kept_width_or_says_default(self):
        self.assertEqual(self.run_page("", kept="264")["out"], "264 px")
        out = self.run_page("")
        self.assertEqual((out["value"], out["out"], out["calls"]), ("216", "default", [[]]))
        self.assertEqual(out["att"]["state"], "ok")

    def test_moving_the_slider_sets_the_width_and_default_resets_it(self):
        out = self.run_page("ELS['rail-width'].value = '300'; ELS['rail-width'].listeners.input[0]();"
                            " R.moved = ELS['rail-width-out'].textContent; ELS['rail-reset'].listeners.click[0]();")
        self.assertEqual(out["moved"], "300 px")
        self.assertEqual(out["calls"], [[], ["300"], [None]])
        self.assertEqual((out["value"], out["out"]), ("216", "default"))


if __name__ == "__main__":
    unittest.main()

"""The v2 Settings page (sd:3438): the theme control, moved from the rail, and the sections width (sd:3445).

What this slice promises: `/settings` is a registered page with no API route; it holds the one theme control (the rail
draws none now), and its width control sets, shows and resets the width through `shell.railWidth`. `settings.js` runs under
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
    """The width is the shell's .seg radio control (foundation patterns.md has no slider), and a change says Saved (§ Forms)."""

    def run_page(self, body, kept="null"):
        script = (STAND_IN + "\nvar KEPT = " + kept + ", CALLS = [];\n"
                  "window.shell = { railWidth: (...a) => { CALLS.push(a); if (a.length) KEPT = a[0] === null ? null : Math.min(384, Math.max(176, Math.round(+a[0]))); return KEPT; } };\n"
                  "var RADIOS = ['176', '', '288', '384'].map(v => ({ value: v, checked: false }));\n"
                  "document.getElementById('rail-width').querySelectorAll = () => RADIOS;\n"
                  + PAGE_JS + "\n(DOC_LISTENERS.DOMContentLoaded || []).forEach(f => f());\nvar R = {};\n" + body
                  + "\nfunction run() { R.calls = CALLS; R.checked = RADIOS.filter(r => r.checked).map(r => r.value);"
                  " R.out = ELS['rail-width-out'].textContent; R.theme = (ELS['theme-saved'] || {}).textContent || ''; R.att = window.PAGE_ATTENTION; return JSON.stringify(R); }\n")
        result = subprocess.run([OSASCRIPT, "-l", "JavaScript", "-e", script], capture_output=True, text=True, timeout=60, check=False)
        if result.returncode:
            raise AssertionError(result.stderr)
        return json.loads(result.stdout)

    def test_the_page_draws_a_seg_radio_control_and_no_slider(self):
        self.assertNotIn('type="range"', HTML)
        self.assertRegex(HTML, r'<fieldset class="seg" id="rail-width"')
        self.assertEqual(re.findall(r'name="rail-width" value="(\d*)"', HTML), ["176", "", "288", "384"])

    def test_the_control_checks_the_kept_width_or_default(self):
        out = self.run_page("", kept="288")
        self.assertEqual((out["checked"], out["out"]), (["288"], "288 px"))
        out = self.run_page("")
        self.assertEqual((out["checked"], out["out"], out["calls"]), ([""], "default, 216 px", [[]]))
        self.assertEqual(out["att"]["state"], "ok")

    def test_a_dragged_width_checks_no_preset(self):
        out = self.run_page("", kept="300")
        self.assertEqual((out["checked"], out["out"]), ([], "300 px"))

    def test_a_change_sets_the_width_and_says_saved(self):
        out = self.run_page("ELS['rail-width'].listeners.change[0]({ target: { value: '384' } }); R.wide = ELS['rail-width-out'].textContent;"
                            " ELS['rail-width'].listeners.change[0]({ target: { value: '' } });")
        self.assertEqual(out["wide"], "384 px · Saved")
        self.assertEqual(out["calls"], [[], ["384"], [None]])
        self.assertEqual((out["checked"], out["out"]), ([""], "default, 216 px · Saved"))

    def test_a_width_set_on_the_rail_edge_shows_here(self):
        out = self.run_page("(DOC_LISTENERS['shell:rail-width'] || [])[0]({ detail: 263 });")
        self.assertEqual((out["checked"], out["out"]), ([], "263 px · Saved"))

    def test_a_theme_choice_says_saved(self):
        out = self.run_page("ELS['theme'].listeners.click[0]({ target: { closest: s => s === '[data-theme-choice]' ? {} : null } });")
        self.assertEqual(out["theme"], "Saved")
        self.assertEqual(self.run_page("ELS['theme'].listeners.click[0]({ target: { closest: () => null } });")["theme"], "")


if __name__ == "__main__":
    unittest.main()

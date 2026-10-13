"""The v2 Skills page (sd:2123).

What this slice promises: `/skills` answers under the shared policy and loads the reader and its script before `shell.js`,
and the old screen is retired (sd:3402). `/api/skills` is the catalog the old screen read,
each skill's use in the last three Monday-start weeks from `skill_use`, the rows that name a path instead of a skill, the
rows by surface and the skills outside the pack that recorded use; it sends no local path and writes nothing. The page
registers the design's Skills commands: Try, Review, Promote and Demote ask first and post the old screen's routes with the
skill's revision; Run, Schedule and Scan are copy only and post nothing.

`skills.js` runs under JavaScriptCore (osascript) against the stand-in page and shell `test_v2_tasks` uses, with the real
reader (`read.js`). The browser half -- the look at 1440 and 375 px, the confirm dialog -- is a manual check.
"""

from __future__ import annotations

import html
import inspect
import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sd_db import skills_catalog
from sd_db.writes import record_skill_use

from sd_dashboard import server, skill_intake, skills_screen, v2

from support import ScreenCase
from test_v2_read import READ_SHELL
from test_v2_registry import Registers
from test_v2_tasks import SHELL, STAND_IN
from test_v2_today import OSASCRIPT, Refused
from test_v2_shell_shared import cell_grammar
from test_v2_shell_shared import cell_grammar
from test_workflow_actions import BrowserSession

V2 = Path(v2.__file__).resolve().parent
PAGE_JS = (V2 / "static" / "skills.js").read_text(encoding="utf-8")
MARKUP_JS = (V2 / "static" / "markup.js").read_text(encoding="utf-8")

#: Wednesday; its week starts Monday 2026-09-21, so the three weeks start 09-07, 09-14 and 09-21.
NOW = "2026-09-23T12:00:00+00:00"

#: The design's Skills commands (design source products/system/designs/pages/skills.js): id, object type, label, key, risk.
#: Try, Review and Demote are `confirm` here, not the design's `undo`: no verb withdraws a trial or a queued request.
COMMANDS = [
    ["skill.try", "skill", "Try", "t", "confirm"],
    ["skill.review", "skill", "Review", "v", "confirm"],
    ["skill.promote", "skill", "Promote", "p", "safe"],
    ["skill.demote", "skill", "Demote", "d", "confirm"],
    ["skill.reimport", "skill", "Re-import", "i", "safe"],
    ["skill.schedule", "skill", "Schedule", "s", "safe"],
    ["skill.apply", "skill", "Apply", "a", "confirm"],
    ["skill.scan", "use surfaces", "Scan", "s", "safe"],
]

SKILL = "---\nname: {name}\ndescription: The {name} fixture.\n---\n## When to use\nFor a bounded fixture.\n"


class Pack:
    """A pack checkout: `sd-used` and `sd-idle` on the `build` path, `sd-extra` in contrib."""

    def make_pack(self):
        self.root = Path(tempfile.mkdtemp(prefix="pack-")).resolve()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.root)], check=False))
        for name, source in (("sd-used", "skills"), ("sd-idle", "skills"), ("sd-extra", "contrib")):
            (self.root / source / name).mkdir(parents=True)
            (self.root / source / name / "SKILL.md").write_text(SKILL.format(name=name))
        (self.root / "skills/paths.json").write_text(json.dumps({"paths": {"build": {"summary": "plan to ship", "skills": ["sd-used", "sd-idle"]}}}))
        patcher = mock.patch.object(skills_catalog, "location", return_value=(self.root, {}))
        patcher.start()
        self.addCleanup(patcher.stop)

    def seed_use(self):
        for stamp, skill, surface in (("2026-09-11T10:00:00Z", "sd-used", "claude"), ("2026-09-15T10:00:00Z", "sd-used", "claude"),
                                      ("2026-09-22T10:00:00Z", "sd-used", "claude"), ("2026-09-22T11:00:00Z", "sd-used", "codex"),
                                      ("2026-08-01T10:00:00Z", "sd-idle", "claude"), ("2026-09-22T12:00:00Z", "/Volumes/example/Models", "claude"),
                                      ("2026-09-16T12:00:00Z", "outside-skill", "claude"), ("2026-09-17T12:00:00Z", "outside-skill", "claude")):
            record_skill_use(self.connection, skill, surface=surface, mode="direct", timestamp=stamp)


class TheDocument(Pack, ScreenCase):
    def setUp(self):
        super().setUp()
        self.make_pack()
        self.seed_use()

    def test_use_is_counted_per_monday_week_and_by_what_it_names(self):
        doc = skills_screen.document(self.connection, now=NOW)
        self.assertEqual(doc["weeks"], ["2026-09-07", "2026-09-14", "2026-09-21"])
        by = {s["name"]: s for s in doc["skills"]}
        self.assertEqual(by["sd-used"]["weeks"], [1, 1, 2])
        # A use before the first week counts toward nothing drawn, and the last use still says when it was.
        self.assertEqual((by["sd-idle"]["weeks"], by["sd-idle"]["last"]), ([0, 0, 0], "2026-08-01T10:00:00+00:00"))
        self.assertEqual(doc["totals"], [1, 3, 3])
        self.assertEqual(doc["junk"], 1)
        self.assertEqual(doc["other"], [["outside-skill", 2]])
        self.assertEqual(doc["surfaces"], {"claude": 7, "codex": 1})
        self.assertEqual(doc["use"], {"first": "2026-08-01T10:00:00+00:00", "last": "2026-09-22T12:00:00+00:00", "rows": 8})
        self.assertEqual(by["sd-used"]["modes"], {"direct": 4})
        # sd:3446: the rail's badge, the idle path skill and the use-record fault, from the read every page's rail runs.
        self.assertEqual(doc["attention"], {"state": "caution", "n": 2, "what": "unused path skills and use-record faults"})
        self.assertEqual({s["name"]: s["status"] for s in doc["skills"]}, {"sd-used": "path", "sd-idle": "path", "sd-extra": "contrib"})
        self.assertEqual(doc["paths"], {"build": "plan to ship"})

    def test_no_field_holds_a_local_path_or_a_file_digest(self):
        text = json.dumps(skills_screen.document(self.connection, now=NOW))
        self.assertNotIn(str(self.root), text)
        self.assertNotIn('"files"', text)
        self.assertNotIn('"root"', text)
        self.assertIn('"source": "skills/sd-used/SKILL.md"', text)

    def test_the_revision_is_the_one_the_write_routes_check(self):
        doc = skills_screen.document(self.connection, now=NOW)
        catalog = {s["name"]: s["revision"] for s in skills_catalog.catalog(self.connection, now=NOW)["skills"]}
        self.assertEqual({s["name"]: s["revision"] for s in doc["skills"]}, catalog)

    def test_a_skill_installed_by_intake_names_its_source(self):
        """sd:3992: the Re-import control is on for a skill whose trailing Provenance holds the intake's line, and off for the rest."""
        line = f"Adopted by `sd skill intake` on 2026-10-13 from `{URL}` at revision `{'c' * 40} (sha256:{'d' * 16})`."
        (self.root / "contrib/sd-extra/SKILL.md").write_text(SKILL.format(name="sd-extra") + f"\n## Provenance\n\n{line}\n")
        # sd-skill-adopt's own line, and the intake's line planted in the body above Provenance, name no intake source.
        (self.root / "skills/sd-idle/SKILL.md").write_text(SKILL.format(name="sd-idle") + f"\n{line}\n\n## Provenance\n\n"
                                                           "Adopted by `sd-skill-adopt` on 2026-10-13 from `/tmp/x` at revision `abc`.\n")
        by = {s["name"]: s["imported"] for s in skills_screen.document(self.connection, now=NOW)["skills"]}
        self.assertEqual(by, {"sd-extra": {"url": URL, "commit": "c" * 40}, "sd-idle": None, "sd-used": None})

    def test_the_document_writes_nothing(self):
        before = self.connection.total_changes
        skills_screen.document(self.connection, now=NOW)
        self.assertEqual(self.connection.total_changes, before)


class ThePage(Pack, BrowserSession):
    def setUp(self):
        super().setUp()
        self.make_pack()

    def test_the_route_answers_under_the_shared_policy(self):
        status, headers, body = self.request("/skills")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Security-Policy"], server.CSP)
        self.assertIn("<title>Skills · system</title>", body)
        self.assertEqual(Refused(body).found, [])
        scripts = re.findall(r'<script src="/ui/([^"?]+)', body)
        self.assertEqual(scripts, ["theme.js", "markup.js", "annunciator-strip.js", "icons.js", "sections.js", "read.js", "skills.js", "shell.js"])
        for path in re.findall(r'(?:src|href)="(/ui/[^"]+)"', body):
            self.assertEqual(self.request(path)[0], 200, path)

    def test_the_rail_opens_the_new_page_and_the_old_one_is_retired(self):
        self.assertEqual(v2.SECTIONS.get("Skills"), "/skills")
        self.assertNotIn("Skills", v2.CLASSIC)
        # sd:3402 retired the classic screen; `test_classic_retired` holds the 404.
        self.assertNotIn("Skills (classic)", v2.SCREENS)
        self.assertEqual(self.request("/classic/skills")[0], 404)

    def test_the_data_route_needs_a_session_takes_no_query_and_says_why_it_failed(self):
        self.assertEqual(self.request("/api/skills")[0], 403)
        cookie = {"Cookie": self.cookie}
        self.assertEqual(self.request("/api/skills?facet=path", headers=cookie)[0], 400)
        status, _, body = self.request("/api/skills", headers=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(sorted(s["name"] for s in json.loads(body)["skills"]), ["sd-extra", "sd-idle", "sd-used"])
        (self.root / "skills/paths.json").write_text("{}")
        status, _, body = self.request("/api/skills", headers=cookie)
        self.assertEqual(status, 503)
        self.assertIn("paths.json", json.loads(body)["error"])

    def test_a_skill_the_page_reads_is_one_the_try_route_accepts(self):
        cookie = {"Cookie": self.cookie}
        skill = next(s for s in json.loads(self.request("/api/skills", headers=cookie)[2])["skills"] if s["name"] == "sd-extra")
        self.assertEqual(self.post("/api/skills/sd-extra/try", {"revision": skill["revision"]})[0], 200)
        again = next(s for s in json.loads(self.request("/api/skills", headers=cookie)[2])["skills"] if s["name"] == "sd-extra")
        self.assertEqual(again["status"], "trial")


URL = "https://github.com/example/skills/tree/main/sd-example"
REVISION = "0123456789abcdef"
#: What `sd skill intake --json` prints (pack bin/sd_skill_intake.py `intake`): a re-import with one warning and a diff.
REPORT = {"url": URL, "commit": "a" * 40, "scope": "pack", "skill": "sd-example", "destination": "/pack/contrib/sd-example",
          "mode": "re-import", "installed_commit": "b" * 40, "files": {"SKILL.md": "100644"},
          "risk": [{"severity": "warn", "rule": "ships-script", "file": "run.sh", "line": 0}],
          "match": [{"skill": "sd-used", "overlap": 0.12}], "value": {"instruction_lines": 9, "novelty": 0.88},
          "verdict": {"action": "install", "reason": "no refusal, enough instructions, no close match"},
          "diff": ["--- installed/SKILL.md", "+++ upstream/SKILL.md", "@@ -1 +1 @@", "-old line", "+new line"],
          "revision": REVISION, "recovered": [], "installed": False}
#: A stand-in `sd`: it records its arguments, one per line, then prints INTAKE_OUT on stdout or stderr and exits INTAKE_EXIT.
STUB_SD = """#!/bin/sh
for arg in "$@"; do printf '%s\\n' "$arg"; done > "$INTAKE_ARGS"
[ -z "$INTAKE_SLEEP" ] || exec sleep "$INTAKE_SLEEP"
if [ "${INTAKE_EXIT:-0}" = 0 ]; then printf '%s' "$INTAKE_OUT"; else printf '%s\\n' "$INTAKE_OUT" >&2; fi
exit "${INTAKE_EXIT:-0}"
"""


#: A source URL that runs commands if a shell reads it unquoted: `$( )`, backticks and `;`, then a `'`.
#: Each command touches a file in `folder`. `tick=False` drops the backtick part, which the route refuses on its own;
#: `quote=False` drops the `'`, whose unbalanced quote would stop sh before the other parts run.
def hostile(folder, tick=True, quote=True):
    touch = "touch${IFS}%s/" % folder
    url = f"https://github.com/o/r/tree/main/x$({touch}p1)" + (f"`{touch}p2`" if tick else "") + f";{touch}p3"
    return url + (";'q" if quote else "")


def shell_argv(case, line, folder):
    """The arguments `sd` or `claude` gets when the operator pastes `line` into sh; nothing else may run."""
    stubs = 'sd() { printf "%s\\0" "$@"; }; claude() { printf "%s\\0" "$@"; }; '
    done = subprocess.run(["sh", "-c", stubs + line], cwd=folder, capture_output=True, text=True, timeout=10, check=False)
    case.assertEqual(sorted(os.listdir(folder)), [], f"pasting {line!r} ran a command")
    case.assertEqual(done.returncode, 0, done.stderr)
    return done.stdout.split("\0")[:-1]


def scratch(case):
    folder = Path(tempfile.mkdtemp(prefix="intake-")).resolve()
    case.addCleanup(lambda: subprocess.run(["rm", "-rf", str(folder)], check=False))
    return folder


class TheIntakeRoute(Pack, BrowserSession):
    """POST /api/skills/intake runs `sd skill intake` once, as an argv list, and installs only with --approve (sd:3992)."""

    def setUp(self):
        super().setUp()
        self.make_pack()
        folder = Path(tempfile.mkdtemp(prefix="intake-")).resolve()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(folder)], check=False))
        self.args = folder / "args"
        stub = folder / "sd"
        stub.write_text(STUB_SD)
        stub.chmod(0o755)
        patcher = mock.patch.object(skill_intake, "command", lambda name: str(stub) if name == "sd" else name)
        patcher.start()
        self.addCleanup(patcher.stop)

    def intake(self, payload, *, out=None, code=0):
        environment = {"INTAKE_ARGS": str(self.args), "INTAKE_OUT": json.dumps(REPORT) if out is None else out, "INTAKE_EXIT": str(code)}
        with mock.patch.dict(os.environ, environment):
            status, _, body = self.post("/api/skills/intake", payload)
        argv = self.args.read_text().splitlines() if self.args.exists() else None
        if self.args.exists():
            self.args.unlink()
        return status, body, argv

    def test_reimport_runs_intake_on_the_skill_name_and_answers_its_report(self):
        status, body, argv = self.intake({"source": "sd-extra"})
        self.assertEqual(argv, ["skill", "intake", "--json", "--", "sd-extra"])
        self.assertEqual((status, body["revision"], body["verdict"]["action"]), (200, REVISION, "install"))

    def test_a_github_url_runs_with_the_scope_the_form_chose(self):
        status, _, argv = self.intake({"source": URL, "scope": "user"})
        self.assertEqual((status, argv), (200, ["skill", "intake", "--json", "--scope", "user", "--", URL]))

    def test_approve_passes_the_exact_revision_the_report_printed(self):
        status, _, argv = self.intake({"source": URL, "scope": "pack", "approve": REVISION}, out=json.dumps(dict(REPORT, installed=True)))
        self.assertEqual(status, 200)
        self.assertEqual(argv, ["skill", "intake", "--json", "--scope", "pack", "--approve", REVISION, "--", URL])

    def test_a_source_that_is_not_a_github_url_or_a_skill_name_is_refused_and_runs_nothing(self):
        for source in ("http://github.com/example/skills", "https://github.com.example.test/example/skills",
                       "https://example.test/example/skills", "https://github.com/example/sk ills", "https://github.com/a`b",
                       "https://github.com/", "--approve", "file:///etc/passwd", "git@github.com:example/skills.git",
                       "../sd-extra", "", 7, None):
            with self.subTest(source=source):
                status, body, argv = self.intake({"source": source})
                self.assertEqual(status, 400)
                self.assertIn("https://github.com/", body["error"])
                self.assertIsNone(argv, "a refused source still ran sd")

    def test_nothing_installs_without_an_approve_that_names_a_revision(self):
        for payload in ({"source": "sd-extra"}, {"source": URL}, {"source": URL, "scope": "pack"}):
            with self.subTest(payload=payload):
                self.assertNotIn("--approve", self.intake(payload)[2])
        for payload in ({"source": URL, "scope": "pack", "approve": ""}, {"source": URL, "scope": "pack", "approve": "yes"},
                        {"source": URL, "scope": "pack", "approve": REVISION + "0"}, {"source": URL, "approve": REVISION},
                        {"source": "sd-extra", "scope": "pack", "approve": REVISION}, {"source": URL, "scope": "all"},
                        {"source": URL, "install": True}, {"revision": REVISION}):
            with self.subTest(payload=payload):
                status, _, argv = self.intake(payload)
                self.assertEqual((status, argv), (400, None))

    def test_a_refused_or_failed_run_says_what_is_known(self):
        status, body, _ = self.intake({"source": "sd-extra"}, out="sd: no installed skill sd-extra records a GitHub source; give its URL", code=1)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "Not evaluated: no installed skill sd-extra records a GitHub source; give its URL")
        stale = f"sd: approved revision {REVISION} is not this report's fedcba9876543210: the source, the target or the installed copy changed; nothing written"
        status, body, _ = self.intake({"source": URL, "scope": "pack", "approve": REVISION}, out=stale, code=1)
        self.assertEqual((status, body.get("reload")), (409, True))
        refused = "sd: a refusal blocks the install (ships-hook-config); nothing written"
        status, body, _ = self.intake({"source": URL, "scope": "pack", "approve": REVISION}, out=refused, code=1)
        self.assertEqual((status, body["error"]), (400, "Not installed: a refusal blocks the install (ships-hook-config); nothing written"))
        # An approve that failed any other way, or printed no report, may have moved files: not known, not "not installed".
        for out, code in (("sd: [Errno 28] No space left on device", 1), ("not json", 0)):
            with self.subTest(out=out):
                status, body, _ = self.intake({"source": URL, "scope": "pack", "approve": REVISION}, out=out, code=code)
                self.assertEqual((status, body["outcome"]), (502, "unknown"))
                self.assertIn("Evaluate again", body["error"])

    def test_the_run_is_sd_skill_intake_alone_and_the_screen_still_calls_no_process(self):
        """test_skill_states keeps `skills_screen` free of process calls; the one run lives here and names no git or gh."""
        source = inspect.getsource(skill_intake)
        self.assertIsNone(re.search(r'--method|git push|pr create|"gh"|"git"|shell=True', source))
        self.assertEqual(source.count("subprocess.run("), 1)
        self.assertIn('[command("sd"), "skill", "intake", "--json"', source)

    def test_a_url_with_shell_syntax_reaches_sd_as_one_literal_argument(self):
        folder = scratch(self)
        status, _, argv = self.intake({"source": hostile(folder), "scope": "pack"})
        self.assertEqual((status, argv), (400, None))
        url = hostile(folder, tick=False)
        status, _, argv = self.intake({"source": url, "scope": "pack", "approve": REVISION}, out=json.dumps(dict(REPORT, installed=True)))
        self.assertEqual((status, argv), (200, ["skill", "intake", "--json", "--scope", "pack", "--approve", REVISION, "--", url]))
        self.assertEqual(os.listdir(folder), [], "the route ran the URL through a shell")

    def test_a_run_past_its_bound_is_unknown_for_an_approve(self):
        with mock.patch.object(skill_intake, "INTAKE_SECONDS", 0.5), mock.patch.dict(os.environ, {"INTAKE_SLEEP": "5"}):
            status, body, _ = self.intake({"source": URL, "scope": "pack", "approve": REVISION})
        self.assertEqual((status, body["outcome"]), (502, "unknown"))


SHELL_MORE = r"""
window.shell.row = () => null; window.shell.closePane = () => {}; OUT.urls = []; window.shell.url = q => OUT.urls.push(q);
window.shell.setContext = () => {}; OUT.picks = []; C.pick = id => OUT.picks.push(id);
var pageQuery = document.querySelector;
document.querySelector = sel => sel === '.ledger thead' ? document.getElementById('thead') : pageQuery(sel);
"""


class TheScript(Pack, ScreenCase):
    """skills.js against the document `skills_screen` builds."""

    def setUp(self):
        super().setUp()
        self.make_pack()
        self.seed_use()
        self.doc = skills_screen.document(self.connection, now=NOW)

    def run_page(self, body, answer=None, search=""):
        answer = answer or "(path, body) => path === '/api/skills' ? [200, DOC] : [200, { item: { id: 42 } }]"
        script = (STAND_IN + f"location.search = {json.dumps(search)};\nvar DOC = {json.dumps(self.doc)};\n" + MARKUP_JS
                  + "\nconst mk = window.markup.html;\n" + SHELL + SHELL_MORE + READ_SHELL + f"\nANSWER = {answer};\n" + PAGE_JS
                  + "\nvar R = {};\n(async () => { try {\n(WIN_LISTENERS.DOMContentLoaded || []).forEach(f => f());\nawait flush();\n"
                  + body + "\n} catch (e) { OUT.error = String(e) + ' ' + e.stack; } })();\n"
                  + "function run() { OUT.R = R; OUT.attention = window.PAGE_ATTENTION; OUT.html = Object.fromEntries(Object.entries(ELS).map(([k, e]) => [k, e.html]));"
                  + " OUT.toasts = OUT.toasts.map(t => [t.msg, !!t.undo]); return JSON.stringify(OUT); }\n")
        result = subprocess.run([OSASCRIPT, "-l", "JavaScript", "-e", script], capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertIsNone(out["error"])
        return out

    # The list grammar from shell.list (sd:2682, as Reports in sd:2527-2529). Each click goes to the listener the page put on its holder.
    CLICK = "ELS['%s'].listeners.click[0]({ target: { closest: s => s === %s ? { dataset: %s } : null } });"
    URL = "String(OUT.urls[OUT.urls.length - 1])"

    def many(self, n):
        """The document with n path skills named sd-000 onward, none used."""
        base = next(s for s in self.doc["skills"] if s["name"] == "sd-idle")
        self.doc["skills"] = [dict(base, name=f"sd-{i:03d}") for i in range(n)]

    def test_every_column_with_an_order_sorts_through_the_shared_header(self):
        """sd:2682: the header is shell.list's; aria-sort is on the sorted th alone, and the sort is in the URL."""
        click = self.CLICK % ("rows-head", "'button[data-sort]'", "{ sort: 'n' }")
        out = self.run_page(f"R.head = ELS['rows-head'].html; {click} R.head2 = ELS['rows-head'].html; R.rows = ELS.rows.html; R.url = {self.URL};"
                            f" {click} R.rows2 = ELS.rows.html; R.url2 = {self.URL};")
        sorted_th = r'aria-sort="(\w+)"[^>]*><button[^>]*data-sort="(\w+)"'
        self.assertEqual(re.findall(r'data-sort="(\w+)"', out["R"]["head"]), ["n", "st", "u"])
        self.assertEqual(re.findall(sorted_th, out["R"]["head"]), [("descending", "u")])
        self.assertEqual(re.findall(sorted_th, out["R"]["head2"]), [("ascending", "n")])
        self.assertEqual(re.findall(r'<tr data-id="([^"]+)"', out["R"]["rows"]), ["sd-extra", "sd-idle", "sd-used"])
        self.assertEqual(re.findall(r'<tr data-id="([^"]+)"', out["R"]["rows2"]), ["sd-used", "sd-idle", "sd-extra"])
        self.assertEqual((out["R"]["url"], out["R"]["url2"]), ("sort=n&dir=asc", "sort=n&dir=desc"))

    def test_the_pager_numbers_pages_and_keeps_page_and_size_in_the_url(self):
        self.many(60)
        out = self.run_page("R.pager = ELS.pager.html;" + self.CLICK % ("pager", "'.list-pager [data-page]'", "{ page: '3' }")
                            + f" R.pager2 = ELS.pager.html; R.rows2 = ELS.rows.html; R.url2 = {self.URL};"
                            + self.CLICK % ("pager", "'.list-pager [data-size]'", "{ size: '50' }")
                            + f" R.pager3 = ELS.pager.html; R.url3 = {self.URL};", search="?page=2&sort=n")
        self.assertIn('<span class="range">26–50 of 60</span>', out["R"]["pager"])
        self.assertEqual(re.findall(r'data-page="(\d+)"', out["R"]["pager"]), ["1", "2", "3"])
        self.assertEqual(re.findall(r'data-size="(\d+)"', out["R"]["pager"]), ["25", "50", "100", "200"])
        self.assertIn('<span class="range">51–60 of 60</span>', out["R"]["pager2"])
        self.assertEqual(re.findall(r'<tr data-id="([^"]+)"', out["R"]["rows2"])[0], "sd-050")
        self.assertEqual(out["R"]["url2"], "page=3&sort=n&dir=asc")
        self.assertIn('<span class="range">1–50 of 60</span>', out["R"]["pager3"])
        self.assertEqual(out["R"]["url3"], "size=50&sort=n&dir=asc")

    def test_active_filters_show_as_chips_that_remove_one_or_clear_all(self):
        out = self.run_page("R.chips = ELS.chips.html;" + self.CLICK % ("chips", "'[data-unfilter]'", "{ unfilter: 'facet' }")
                            + f" R.chips2 = ELS.chips.html; R.url2 = {self.URL};"
                            + " ELS.chips.listeners.click[0]({ target: { closest: s => s === '[data-unfilter-all]' ? {} : null } });"
                            + f" R.chips3 = ELS.chips.html; R.url3 = {self.URL}; R.rows = ELS.rows.html;", search="?facet=path&q=sd")
        self.assertEqual(re.findall(r'data-unfilter="(\w+)"', out["R"]["chips"]), ["facet", "q"])
        self.assertIn("2 filters · 2 of 3", out["R"]["chips"])
        self.assertEqual(re.findall(r'data-unfilter="(\w+)"', out["R"]["chips2"]), ["q"])
        self.assertEqual(out["R"]["url2"], "q=sd")
        self.assertEqual((out["R"]["chips3"], out["R"]["url3"]), ("", ""))
        self.assertEqual(len(re.findall(r'<tr data-id=', out["R"]["rows"])), 3)

    def test_the_design_commands_are_registered_with_their_ids_labels_keys_and_risks(self):
        out = self.run_page("R.reg = REG.map(c => [c.id, c.on, c.label, c.key, c.risk]); R.bulk = REG.filter(c => c.bulk).map(c => c.id);")
        self.assertEqual(out["R"]["reg"], COMMANDS)
        self.assertEqual(out["R"]["bulk"], ["skill.try", "skill.review"])

    def test_try_asks_first_posts_the_skills_revision_and_rereads(self):
        revision = next(s["revision"] for s in self.doc["skills"] if s["name"] == "sd-extra")
        out = self.run_page("shellRun(cmd('skill.try'), C.get('sd-extra')); await flush();")
        self.assertEqual(out["confirms"], ["skill.try"])
        self.assertEqual(out["posts"], [["/api/skills/sd-extra/try", {"revision": revision}, 64]])
        self.assertEqual(out["gets"], ["/api/skills", "/api/skills"], "the page did not reread after the write")
        self.assertEqual(out["toasts"][-1], ["Trial of sd-extra starts at the next install", False])

    def test_review_and_demote_post_their_routes_and_name_the_queued_item(self):
        out = self.run_page("shellRun(cmd('skill.review'), C.get('sd-used')); await flush(); shellRun(cmd('skill.demote'), C.get('sd-idle')); await flush();")
        self.assertEqual([p[0] for p in out["posts"]], ["/api/skills/sd-used/review", "/api/skills/sd-idle/demote"])
        self.assertEqual([t[0] for t in out["toasts"]], ["Review of sd-used queued as item #42", "Removal of sd-idle from paths queued as item #42"])

    def test_a_refused_write_says_why_and_reads_again(self):
        answer = "(path) => path === '/api/skills' ? [200, DOC] : [409, { error: 'The skill changed', reload: true }]"
        out = self.run_page("shellRun(cmd('skill.try'), C.get('sd-extra')); await flush();", answer=answer)
        self.assertEqual(out["toasts"][-1], ["sd-extra not changed: The skill changed", False])
        self.assertEqual(len(out["gets"]), 2)
        self.assertEqual(out["states"][-2]["kind"], "loading", "a refused write reads again with load()")

    def test_off_commands_name_their_reason(self):
        out = self.run_page("""const w = (id, key) => cmd(id).when(C.get(key));
R.off = [w('skill.try', 'sd-used'), w('skill.promote', 'sd-used'), w('skill.demote', 'sd-extra'), w('skill.apply', 'sd-used'), w('skill.try', 'sd-extra')];""")
        self.assertEqual(out["R"]["off"], ["already installed · path", "on build already", "not on a path",
                                           "apply review notes from the review item", True])

    def test_the_copy_only_commands_post_nothing(self):
        out = self.run_page("""R.exec = ['skill.schedule', 'skill.scan'].map(id => cmd(id).executes);
shellRun(cmd('skill.schedule'), C.get('sd-used')); shellRun(cmd('skill.scan'), C.get('surfaces')); await flush();""")
        self.assertEqual(out["R"]["exec"], [False, False])
        self.assertEqual((out["posts"], out["confirms"]), ([], []))

    def test_the_page_draws_lamps_rows_and_the_rail_badge_from_the_document(self):
        out = self.run_page("R.sub = ELS.sub.textContent; R.tally = ELS.tally.textContent;")
        rows, lamps = out["html"]["rows"], out["html"]["annunciator"]
        # Sorted by use this week, most first; an unused path skill carries the caution glyph.
        self.assertEqual(re.findall(r'<tr data-id="([^"]+)"', rows), ["sd-used", "sd-extra", "sd-idle"])
        self.assertIn('<td class="g g-caution" title="on a path, no recorded use">', rows)
        self.assertEqual(re.findall(r'data-facet="(\w+)" data-state="(\w+)"', lamps), [("path", "ok"), ("contrib", "ok"), ("unused", "caution")])
        self.assertIn('data-open="hygiene" data-state="caution"', lamps)
        self.assertIn('data-open="surfaces" data-state="ok"', lamps)
        self.assertEqual([tag for tag, _ in cell_grammar(self, lamps)], ["button", "div", "button", "div", "button", "a", "a", "button"])
        self.assertEqual([tag for tag, _ in cell_grammar(self, lamps)], ["button", "div", "button", "div", "button", "a", "a", "button"])
        self.assertEqual(out["R"]["sub"], "The sd pack catalog: 3 skills. 2 on paths, 0 on trial, 1 in contrib.")
        self.assertEqual(out["R"]["tally"], "3 uses this week · 1 catalog skills used")
        self.assertEqual(out["attention"], {"state": "caution", "n": 2, "what": "unused path skills and use-record faults"})
        self.assertIn("outside-skill (2)", out["html"]["foot"])

    def test_a_facet_filters_the_ledger_and_the_url_keeps_it(self):
        out = self.run_page("document.dispatchEvent(new CustomEvent('skills:facet', { detail: 'unused' })); R.rows = ELS.rows.html; R.url = String(OUT.urls[OUT.urls.length - 1]);")
        self.assertEqual(re.findall(r'<tr data-id="([^"]+)"', out["R"]["rows"]), ["sd-idle"])
        self.assertEqual(str(out["R"]["url"]), "facet=unused")
        out = self.run_page("R.rows = ELS.rows.html;", search="?facet=contrib")
        self.assertEqual(re.findall(r'<tr data-id="([^"]+)"', out["R"]["rows"]), ["sd-extra"])

    def test_a_failed_read_clears_the_page_and_says_so(self):
        out = self.run_page("R.rows = ELS.rows.html;", answer="() => [503, { error: 'skills/paths.json has no valid path inventory' }]")
        self.assertEqual(out["states"][-1]["kind"], "error")
        self.assertIn("The skills were not read: skills/paths.json has no valid path inventory", out["states"][-1]["text"])
        self.assertIn("Not read: skills/paths.json", out["R"]["rows"])
        self.assertEqual(out["attention"], {"state": "unknown", "n": 0, "what": "skills not read"})

    def test_details_and_the_use_record_notes_are_read_not_drawn(self):
        out = self.run_page("open('sd-used'); R.used = ELS.details.html; open('hygiene'); R.hygiene = ELS.details.html; open('surfaces'); R.surfaces = ELS.details.html;")
        self.assertIn("week of 09-21, 2", out["R"]["used"])
        self.assertIn("skills/sd-used/SKILL.md", out["R"]["used"])
        self.assertIn("1 of 8", out["R"]["hygiene"])
        self.assertIn("claude 7 · codex 1", out["R"]["surfaces"])
        self.assertNotIn("3DPrinter", out["R"]["hygiene"])

    # sd:3992: Re-import runs `sd skill intake` on an imported skill, shows its report, and offers Approve only after one.
    INTAKE = ("(path, body) => path === '/api/skills' ? [200, DOC] : path === '/api/skills/intake' ? [200, Object.assign({}, %s,"
              " { installed: !!(body && body.approve) })] : [200, {}]")
    CLICK_APPROVE = "ELS.details.listeners.click.forEach(f => f({ target: { closest: s => s === '[data-approve]' ? { dataset: {} } : null } }));"

    def imported(self, report=None):
        """sd-extra installed by intake; the stand-in server's answer, with the report it gives."""
        next(s for s in self.doc["skills"] if s["name"] == "sd-extra")["imported"] = {"url": URL, "commit": "b" * 40}
        return self.INTAKE % json.dumps(report or REPORT)

    def test_reimport_is_on_only_for_a_skill_intake_installed(self):
        self.imported()
        out = self.run_page("R.off = [cmd('skill.reimport').when(C.get('sd-extra')), cmd('skill.reimport').when(C.get('sd-used'))];"
                            " R.cli = cmd('skill.reimport').cli(C.get('sd-extra'));")
        self.assertEqual(out["R"]["off"], [True, "not installed by sd skill intake"])
        self.assertEqual(out["R"]["cli"], "sd skill intake 'sd-extra'")

    def test_reimport_shows_the_report_and_approve_posts_the_exact_revision(self):
        answer = self.imported()
        out = self.run_page("open('sd-extra'); R.before = ELS.details.html; shellRun(cmd('skill.reimport'), C.get('sd-extra')); await flush();"
                            " R.slot = ELS['intake-slot'].html; R.posts = OUT.posts.length; " + self.CLICK_APPROVE + " await flush();"
                            " R.after = ELS['intake-slot'].html;", answer=answer)
        self.assertNotIn("data-approve", out["R"]["before"], "Approve showed before any report")
        self.assertEqual(out["posts"][0], ["/api/skills/intake", {"source": "sd-extra"}, 64])
        slot = out["R"]["slot"]
        for shown in ("ships-script", "run.sh", "sd-used 0.12", "9 instruction lines", "novelty 0.88", "install",
                      "no refusal, enough instructions, no close match", "-old line", "+new line", "re-import"):
            self.assertIn(shown, slot)
        self.assertEqual(re.findall(r"<button[^>]*data-approve[^>]*>([^<]*)", slot), [f"Approve revision {REVISION}"])
        self.assertEqual(out["confirms"], ["skill.intake.approve"])
        self.assertEqual(out["posts"][1], ["/api/skills/intake", {"source": URL, "scope": "pack", "approve": REVISION}, 64])
        self.assertEqual(out["R"]["posts"], 1, "the report posted an approve on its own")
        self.assertNotIn("data-approve", out["R"]["after"], "Approve stayed after the install")
        self.assertEqual(out["toasts"][-1], [f"Installed sd-example at revision {REVISION}", False])

    def test_no_approve_without_a_report_that_can_install(self):
        refused = dict(REPORT, risk=[{"severity": "refuse", "rule": "ships-hook-config", "file": "hooks.json", "line": 0}],
                       verdict={"action": "decline", "reason": "refused: ships-hook-config"})
        for name, report in (("refused", refused), ("unchanged", dict(REPORT, mode="unchanged", diff=[])),
                             ("blocked", dict(REPORT, mode="blocked", diff=[]))):
            with self.subTest(report=name):
                out = self.run_page("shellRun(cmd('skill.reimport'), C.get('sd-extra')); await flush();"
                                    " R.slot = ELS['intake-slot'].html; " + self.CLICK_APPROVE + " await flush();", answer=self.imported(report))
                self.assertIn(report["verdict"]["reason"], out["R"]["slot"])
                self.assertNotIn("data-approve", out["R"]["slot"])
                self.assertEqual(len(out["posts"]), 1)
        self.imported()
        answer = "(path) => path === '/api/skills' ? [200, DOC] : [400, { error: 'Not evaluated: git fetch failed' }]"
        out = self.run_page("shellRun(cmd('skill.reimport'), C.get('sd-extra')); await flush(); R.slot = ELS['intake-slot'].html;", answer=answer)
        self.assertIn("Not evaluated: git fetch failed", out["R"]["slot"])
        self.assertNotIn("data-approve", out["R"]["slot"])

    # The lines the operator copies quote every value (sd:3992 review): pasted into sh, each runs sd with the URL as one argument.
    OPEN_ADOPT = ("ELS.details.querySelector = s => s[0] === '#' ? document.getElementById(s.slice(1)) : El('x');"
                  " ELS['adopt-btn'].listeners.click[0](); ELS['ad-src'].value = %s; ELS['ad-scope'].value = 'user';"
                  " ELS['ad-src'].listeners.input[0]();")

    def test_the_adopt_panel_lines_quote_the_url(self):
        folder = scratch(self)
        for url in (hostile(folder, quote=False), hostile(folder)):
            self.adopt_lines(url, folder)

    def adopt_lines(self, url, folder):
        out = self.run_page(self.OPEN_ADOPT % json.dumps(url) + " R.intake = ELS['ad-intake'].textContent;"
                            " R.copy = ELS['ad-intake-copy'].dataset.copy; R.adopt = ELS['ad-cli'].textContent;")
        self.assertEqual(out["R"]["copy"], out["R"]["intake"])
        self.assertEqual(shell_argv(self, out["R"]["intake"], folder), ["skill", "intake", url, "--scope", "user"])
        self.assertEqual(shell_argv(self, out["R"]["adopt"], folder), ["-p", "/sd-skill-adopt o/r/" + url.split("/main/", 1)[1]])

    def test_the_report_line_and_the_approve_confirm_quote_the_url(self):
        for quote in (False, True):
            for name in ("approvable", "unchanged"):
                with self.subTest(report=name, quote=quote):
                    folder = scratch(self)
                    self.report_lines(name, hostile(folder, quote=quote), folder)

    def report_lines(self, name, url, folder):
        if name == "approvable":
            report, want = dict(REPORT, url=url), ["skill", "intake", url, "--scope", "pack", "--approve", REVISION]
        else:
            report, want = dict(REPORT, url=url, mode="unchanged", diff=[]), ["skill", "intake", url, "--scope", "pack"]
        body = (self.OPEN_ADOPT % json.dumps(url) + " ELS['ad-scope'].value = 'pack';"
                " ELS['adopt-form'].listeners.submit[0]({ preventDefault() {} }); await flush(); R.slot = ELS['intake-slot'].html;"
                " const run = C.run; C.run = (c, o) => { R.confirm = c.cli(o); return run(c, o); }; " + self.CLICK_APPROVE + " await flush();")
        out = self.run_page(body, answer=self.INTAKE % json.dumps(report))
        self.assertEqual(out["posts"][0], ["/api/skills/intake", {"source": url, "scope": "pack"}, 64])
        copied = [html.unescape(c) for c in re.findall(r'data-copy="([^"]*)"', out["R"]["slot"])]
        self.assertEqual([shell_argv(self, line, folder) for line in copied], [want])
        if name == "approvable":
            self.assertEqual(shell_argv(self, out["R"]["confirm"], folder), want)
        else:
            self.assertNotIn("confirm", out["R"])

    def test_the_reimport_line_quotes_the_name(self):
        folder = scratch(self)
        for name in (hostile(folder, quote=False), hostile(folder)):
            out = self.run_page("R.cli = cmd('skill.reimport').cli({ id: %s });" % json.dumps(name))
            self.assertEqual(shell_argv(self, out["R"]["cli"], folder), ["skill", "intake", name])

    def test_the_script_adds_no_sink_no_inline_style_and_no_reader_of_its_own(self):
        self.assertNotIn("innerHTML", PAGE_JS)
        self.assertNotRegex(PAGE_JS, r"style=\\?\"")
        self.assertNotIn("history.replaceState", PAGE_JS)
        self.assertNotIn("SKILLS_DATA", PAGE_JS)
        self.assertIn("window.shell.read(", PAGE_JS)
        self.assertNotIn("++generation", PAGE_JS)
        self.assertIn("window.PAGE_LIST", PAGE_JS)


class TheRegistration(Registers, unittest.TestCase):
    page, section, route, api = "skills", "Skills", "/skills", ("/api/skills", "/api/skills/intake")


if __name__ == "__main__":
    unittest.main()

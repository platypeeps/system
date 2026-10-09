"""The v2 shell and its Today page (sd:2110).

What this slice promises: `/today` answers under the same policy as every
other response; the shell's files are served from the package with their
types; the page loads its data and script before `shell.js`, as the design's
page contract requires; Today's rows are `now_screen.document`'s, read by the
same `/api/now` v1 reads; the v1 files are as they were (sd:2163 moved the
old Today to `/classic/today`; `test_default_routes` holds the moves); and
`markup.js`, run under JavaScriptCore, turns no value into markup and help
text into bare <b> and <code> only. The browser half -- no console error, no policy refusal, no horizontal
scroll at 375 px -- is a manual check recorded on the pull request.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
import unittest
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

from sd_dashboard import now_screen, server, v2

from test_now_screen import JobsBackend, fleet_document, repo
from test_workflow_actions import BrowserSession
from test_v2_registry import Registers

V2 = Path(v2.__file__).resolve().parent
TODAY_JS = (V2 / "static" / "today.js").read_text(encoding="utf-8")
SHELL_JS = (V2 / "static" / "shell.js").read_text(encoding="utf-8")
MARKUP_JS = (V2 / "static" / "markup.js").read_text(encoding="utf-8")


class Refused(HTMLParser):
    """What the page policy (default-src 'self') refuses, read the way a browser reads the markup:
    a <style> element, a style attribute, an inline handler, a <script> without src or with text,
    and a src or href on another origin."""

    def __init__(self, markup):
        super().__init__(convert_charrefs=True)
        self.found, self.script = [], None
        self.feed(markup)
        self.close()
        self.end_script()

    def handle_starttag(self, tag, attrs):
        self.end_script()
        for name, value in attrs:
            if name == "style":
                self.found.append(f"style attribute on <{tag}>")
            elif name.startswith("on"):
                self.found.append(f"handler {name} on <{tag}>")
            elif name in ("src", "href") and re.match(r"(?:https?:)?//", value or ""):
                self.found.append(f"another origin: {value}")
        if tag == "style":
            self.found.append("<style>")
        elif tag == "script":
            self.script = []
            if not dict(attrs).get("src"):
                self.found.append("script without src")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_data(self, data):
        if self.script is not None:
            self.script.append(data)

    def handle_endtag(self, tag):
        if tag == "script":
            self.end_script()

    def end_script(self):
        if self.script is not None and "".join(self.script).strip():
            if "script without src" in self.found[-1:]:
                self.found.pop()
            self.found.append(f"inline script: {''.join(self.script).strip()!r}")
        self.script = None


class ThePage(BrowserSession):
    fleet_backend = staticmethod(lambda area: fleet_document(area, repos=[repo("pushy", ahead=1)]))

    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.backend = JobsBackend(root.name, jobs=[("nightly-sync", "failed", 7, None)])
        super().setUp()

    def test_the_route_answers_under_the_shared_policy_with_a_session(self):
        status, headers, body = self.request("/today")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/html; charset=utf-8")
        self.assertEqual(headers["Content-Security-Policy"], server.CSP)
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertIn("sd_session=", headers["Set-Cookie"])
        self.assertRegex(body, r'<meta name="sd-csrf" content="[a-f0-9]{64}"></head>')
        self.assertIn("<title>Today · system</title>", body)

    def test_the_page_holds_nothing_the_policy_refuses(self):
        _, _, body = self.request("/today")
        self.assertEqual(Refused(body).found, [])

    def test_the_policy_scan_reads_markup_as_a_browser_does(self):
        # Each of these is what a regex over the text missed or could miss; the parser reads tags as a browser does.
        self.assertEqual(Refused('<script\n>x</script\t\n bar>').found, ["inline script: 'x'"])
        self.assertEqual(Refused('<SCRIPT >alert(1)</SCRIPT >').found, ["inline script: 'alert(1)'"])
        self.assertEqual(Refused('<script src="/ui/a.js"></script>').found, [])
        self.assertEqual(Refused('<Style>p{}</Style><p STYLE="x" OnClick="y">').found,
                         ["<style>", "style attribute on <p>", "handler onclick on <p>"])
        self.assertEqual(Refused('<link href="//fonts.example/x.css"><img src="https://x.example/i.png">').found,
                         ["another origin: //fonts.example/x.css", "another origin: https://x.example/i.png"])

    def test_every_file_the_page_names_is_served(self):
        _, _, body = self.request("/today")
        named = re.findall(r'(?:src|href)="(/ui/[^"]+)"', body)
        self.assertEqual(len(named), 12)  # four stylesheets, eight scripts
        for path in named:
            status, headers, _ = self.request(path)
            self.assertEqual(status, 200, path)
            self.assertEqual(headers["Content-Security-Policy"], server.CSP, path)

    def test_data_and_page_script_load_before_the_shell_and_the_shell_loads_last(self):
        _, _, body = self.request("/today")
        scripts = re.findall(r'<script src="/ui/([^"?]+)', body)
        self.assertEqual(scripts, ["theme.js", "markup.js", "icons.js", "sections.js", "read.js", "today.js", "decisions.js", "shell.js"])
        self.assertLess(body.index('src="/ui/markup.js?v='), body.index("</head>"))
        self.assertGreater(body.index('src="/ui/icons.js?v='), body.index("<body"))

    def test_the_session_the_page_opens_reads_now(self):
        """The rows Today paints are `now_screen.document`'s, through the route v1 uses."""
        status, headers, _ = self.request("/today")
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        status, _, body = self.request("/api/now", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        rows = json.loads(body)["rows"]
        self.assertEqual([(row["id"], row["band"], row["source"]) for row in rows],
                         [("job:nightly-sync:7", "broken", "jobs"), ("ahead:pushy:1", "look", "repos")])
        self.assertIn("source: '/api/now'", TODAY_JS)

    def test_a_path_under_ui_that_is_not_a_file_is_a_404(self):
        for path in ("/ui/", "/ui/nowhere.js", "/ui/../__init__.py", "/ui/%2e%2e/__init__.py", "/ui/today.html",
                     "/today.html"):
            status, headers, _ = self.request(path)
            self.assertEqual(status, 404, path)
            self.assertEqual(headers["Content-Security-Policy"], server.CSP, path)


class TheAssets(BrowserSession):
    def test_each_file_is_served_with_its_type(self):
        expected = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
                    ".woff2": "font/woff2", ".txt": "text/plain; charset=utf-8"}
        on_disk = sorted(path.relative_to(V2 / "static").as_posix()
                         for path in (V2 / "static").rglob("*") if path.is_file())
        self.assertEqual(sorted(v2.ASSETS), sorted(on_disk + list(v2.GENERATED)))
        for name in v2.ASSETS:
            with urllib.request.urlopen(f"{self.base}/ui/{name}", timeout=5) as answer:
                status, headers, body = answer.status, answer.headers, answer.read()
            self.assertEqual(status, 200, name)
            # A stylesheet's references carry their digests (sd:2141); with those taken out it is the file.
            self.assertEqual(re.sub(rb"\?v=[0-9a-f]{16}", b"", body) if name.endswith(".css") else body,
                             v2.GENERATED.get(name) or (V2 / "static" / name).read_bytes(), name)
            self.assertEqual(headers["Content-Type"], expected[Path(name).suffix], name)
            self.assertEqual(headers["X-Content-Type-Options"], "nosniff", name)
        self.assertIn("fonts/ibm-plex-sans-var.woff2", on_disk)

    def test_every_font_face_is_a_served_file_and_nothing_names_another_origin(self):
        """The foundation's fonts.css lists a Google copy after each local file.

        Chromium checks each source against the policy and logs a refusal for
        the Google one even when the local file loads, so the port drops them.
        """
        css = (V2 / "static" / "fonts.css").read_text(encoding="utf-8")
        faces = re.findall(r"src:\s*url\(([^)]+)\)", css)
        self.assertEqual(len(faces), 6)
        for first in faces:
            self.assertIn(first, v2.ASSETS)
        for name in v2.ASSETS:
            if name.endswith((".css", ".js")) and name not in v2.GENERATED:
                text = (V2 / "static" / name).read_text(encoding="utf-8")
                self.assertNotRegex(text, r"url\(\s*['\"]?(https?:)?//", name)


class TheShellPort(BrowserSession):
    def test_the_shell_builds_nothing_the_policy_refuses(self):
        for source in (SHELL_JS, TODAY_JS):
            self.assertNotIn("createElement('style')", source)
            self.assertNotIn("setAttribute('style'", source)
            self.assertNotRegex(source, r"style=\\?\"")
        # The favicon count draws a data: URL; the build leaves it to the mockups.
        self.assertIn("if (!window.SHELL_PAGES) icon.href = c.toDataURL", SHELL_JS)

    def test_today_maps_every_band_source_and_kind_now_can_send(self):
        state = re.search(r"const STATE = \{([^}]*)\}", TODAY_JS).group(1)
        self.assertEqual(set(re.findall(r"(\w+):", state)), set(now_screen.BANDS))
        sources = re.search(r"const SRC = \{(.*?)\};", TODAY_JS).group(1)
        self.assertEqual(set(re.findall(r"(\w+): \[", sources)), set(now_screen.SOURCES))
        kinds = set(re.findall(r'"kind": "(\w+)"', Path(now_screen.__file__).read_text(encoding="utf-8")))
        types = re.search(r"const TYPE = \{([^}]*)\}", TODAY_JS).group(1)
        self.assertEqual(set(re.findall(r"(\w+):", types)), kinds)

    def test_the_v1_files_are_unchanged_and_no_page_names_a_v2_address(self):
        self.assertEqual(server.STATIC_FILES, ("dashboard.css", "dashboard.js"))
        for path in ("/", "/today", "/classic/today", "/static/dashboard.js", "/static/dashboard.css"):
            status, headers, body = self.request(path)
            self.assertEqual(status, 200, path)
            self.assertNotRegex(body, r"""["'(]/v2/""", path)


# markup.js runs under JavaScriptCore through osascript: the gate's PATH has no node and allows no skipped test.
# The page is a stand-in: a <template> records the text it is asked to parse, and a target records where it went.
OSASCRIPT = "/usr/bin/osascript"
STAND_IN = """
var window = globalThis, sunk = [];
var document = { createElement: function (tag) {
  if (tag !== 'template') throw new Error('a sink other than <template>: ' + tag);
  var t = {}; Object.defineProperty(t, 'innerHTML', { set: function (v) { t.content = { parsed: v }; } }); return t; } };
var target = {}; ['replaceChildren', 'append', 'prepend', 'before'].forEach(function (w) { target[w] = function (f) { sunk.push([w, f.parsed]); }; });
"""
HELP = re.search(r"^  // help:start\n(.*?)^  // help:end$", SHELL_JS, re.S | re.M).group(1)


def run_js(test, script):
    """Run markup.js and then `script` in the stand-in page; the script's last expression is JSON."""
    result = subprocess.run([OSASCRIPT, "-l", "JavaScript", "-e", STAND_IN + MARKUP_JS + script],
                            capture_output=True, text=True, timeout=60, check=False)
    test.assertEqual(result.returncode, 0, result.stderr)
    return json.loads(result.stdout)


class Parsed(HTMLParser):
    """What a browser builds from markup: its tags with their attributes, and its text."""

    def __init__(self, markup):
        super().__init__(convert_charrefs=True)
        self.tags, self.text = [], []
        self.feed(markup)
        self.close()

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, attrs))

    def handle_data(self, data):
        self.text.append(data)


class TheMarkup(unittest.TestCase):
    """markup.js is the v2 scripts' one HTML sink; test_markup's grep exempts it and nothing else."""

    def test_the_one_sink_is_a_template_fed_only_by_the_tag(self):
        self.assertEqual(MARKUP_JS.count("innerHTML"), 1)
        self.assertIn("const t = document.createElement('template');\n    t.innerHTML = m.text;", MARKUP_JS)
        self.assertIn("if (!made.has(m)) throw new TypeError", MARKUP_JS)
        self.assertEqual(MARKUP_JS.count("made.add("), 1)
        for source in (SHELL_JS, TODAY_JS):
            self.assertNotIn("esc(", source)
            self.assertIn("window.markup", source)
        # A template's strings carry `raw`; a script that names it could forge one. Only markup.js reads it.
        others = sorted(p.name for p in (V2 / "static").glob("*.js") if p.name != "markup.js")
        self.assertIn("shell.js", others)
        self.assertEqual([n for n in others if re.search(r"\braw\b", (V2 / "static" / n).read_text(encoding="utf-8"))], [])
        # No other script can hand html a forged strings array: it names html only as a tag (html`), and reads markup.js
        # only by destructuring it. In-page code could build a template object's shape; this is what rules that out.
        for name in others:
            source = (V2 / "static" / name).read_text(encoding="utf-8")
            reads = re.sub(r"const \{ [\w, ]+ \} = window\.markup;", "", source)
            self.assertEqual(re.findall(r"window\.markup\b", reads), [], name)
            self.assertEqual(re.findall(r"(?<![\w.$])html\b(?!`)", reads), [], name)

    def test_a_hole_holding_markup_renders_as_text(self):
        attack = "<img src=x onerror=alert(1)>"
        quote = '"><img src=x onerror=alert(1)>'
        sunk = run_js(self, f"""const {{ html, put }} = window.markup;
put(target, html`<p title="${{{json.dumps(quote)}}}">${{{json.dumps(attack)}}}</p>`); JSON.stringify(sunk)""")
        self.assertEqual(len(sunk), 1)
        where, markup = sunk[0]
        self.assertEqual(where, "replaceChildren")
        parsed = Parsed(markup)
        self.assertEqual(parsed.tags, [("p", [("title", quote)])])
        self.assertEqual("".join(parsed.text), attack)

    def test_only_the_tag_makes_markup_and_only_put_parses_it(self):
        got = run_js(self, """const { html, put } = window.markup;
const refused = f => { try { f(); return false; } catch (e) { return e instanceof TypeError; } };
const forged = Object.assign(['<img src=x onerror=alert(1)>'], { raw: ['<img src=x onerror=alert(1)>'] });
const m = html`<b>${'<i>'}</b>`;
put(target, html`<ul>${['a', '<b>'].map(x => html`<li>${x}</li>`)}</ul>`);
put(target, html`${m}`, 'append'); put(target, html`${null}${undefined}${false}${0}`, 'prepend'); put(target, html`<hr>`, 'before');
JSON.stringify({ exports: Object.keys(window.markup), frozen: Object.isFrozen(window.markup) && Object.isFrozen(m), sunk,
  refused: [refused(() => html(['<img>'])), refused(() => html(forged)), refused(() => put(target, '<img>')),
            refused(() => put(target, { text: '<img>' })), refused(() => put(target, m, 'outerHTML'))],
  kept: (() => { try { m.text = '<img>'; } catch (e) { /* strict mode throws; sloppy mode ignores the write */ } return m.text; })(),
  frozenForgery: refused(() => html(Object.freeze(Object.assign(['<img src=x onerror=alert(1)>'], { raw: Object.freeze(['<img src=x onerror=alert(1)>']) })))),
  jsonForgery: refused(() => html(Object.freeze(JSON.parse('["<img>"]')))) })""")
        self.assertEqual(got["exports"], ["html", "put", "plural"])
        self.assertTrue(got["frozen"])
        self.assertEqual(got["sunk"], [["replaceChildren", "<ul><li>a</li><li>&lt;b&gt;</li></ul>"], ["append", "<b>&lt;i&gt;</b>"],
                                       ["prepend", "0"], ["before", "<hr>"]])
        self.assertEqual(got["refused"], [True] * 5)
        self.assertEqual(got["kept"], "<b>&lt;i&gt;</b>")
        self.assertTrue(got["frozenForgery"], "a frozen array with a frozen, enumerable raw passed as a template")
        self.assertTrue(got["jsonForgery"])

    def test_a_value_inside_a_tag_must_be_markup_the_tag_made(self):
        got = run_js(self, """const { html, put } = window.markup;
const refused = f => { try { f(); return false; } catch (e) { return e instanceof TypeError; } };
const on = 'onmouseover=alert(1)';
const bareFragment = html` ${on}`;
JSON.stringify({
  refused: [refused(() => html`<div ${on}>`), refused(() => html`<a href=${'x onclick=alert(1)'}>`), refused(() => html`<${'img src=x onerror=alert(1)'}>`),
            refused(() => html`<div${bareFragment}>`), refused(() => html`<div ${[html` a="1"`, on]}>`), refused(() => html`<p data-x=${'1'}>`)],
  allowed: [html`<div ${''}${null}${false}${html` class="x"`}${[html` a="1"`]}>`.text, html`<div${html` title="${'" onclick=alert(1)'}"`}>`.text,
            html`<p title='${"it's"}'>${on}</p>`.text, html`<p>${bareFragment}</p>`.text] })""")
        self.assertEqual(got["refused"], [True] * 6)
        self.assertEqual(got["allowed"], ['<div  class="x" a="1">', '<div title="&quot; onclick=alert(1)">',
                                          "<p title='it&#39;s'>onmouseover=alert(1)</p>", "<p> onmouseover=alert(1)</p>"])

    def test_help_renders_bare_b_and_code_and_drops_every_other_tag_and_attribute(self):
        text = ('Rank <b>broken</b> first; run <code>sd jobs</code>. <B class="x" onclick="alert(1)">loud</B> '
                '<img src=x onerror=alert(1)><a href="javascript:alert(1)">link</a> <script>alert(2)</script><i>it</i> '
                '1 < 2 & 3 > 2 <code>a<b>c</code> <b>open')
        sunk = run_js(self, "const { html, put } = window.markup;\n" + HELP + f"put(target, helpText({json.dumps(text)})); JSON.stringify(sunk)")
        markup = sunk[0][1]
        self.assertEqual(markup, "Rank <b>broken</b> first; run <code>sd jobs</code>. <b>loud</b> link alert(2)it "
                                 "1 &lt; 2 &amp; 3 &gt; 2 <code>a<b>c</b></code> <b>open</b>")
        parsed = Parsed(markup)
        self.assertEqual({tag for tag, _ in parsed.tags}, {"b", "code"})
        self.assertEqual([attrs for _, attrs in parsed.tags if attrs], [])
        self.assertEqual("".join(parsed.text), "Rank broken first; run sd jobs. loud link alert(2)it 1 < 2 & 3 > 2 ac open")


class TheRegistration(Registers, unittest.TestCase):
    page, section, route, api = "today", "Today", "/today", ("/api/decisions", "/api/decisions/answer")

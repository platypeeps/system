"""What every v2 page now takes from the shell (sd:2588, sd:2527, sd:2528, sd:2529, sd:2530).

`shell.js` holds the page's reads and writes (`shell.post`, `shell.getJSON`) and the list grammar (`shell.list`: sort header,
numbered pager, filter chips, and the URL keys they keep). `shell.css` holds the annunciator and ledger rules that were the
same on every page, the list grammar's styles, and the lamp hover: a ring in the lamp's ink token, not a brightness filter.

The JavaScript runs under JavaScriptCore (osascript) on the blocks the tests cut out of `shell.js`, so a test reads the
shipped code, not a copy. Reports wires the list grammar end to end; `test_v2_reports` drives that page.

sd:2680: one annunciator and one ledger layout. shell.css holds the ledger's desktop rules, with the browser's auto table
layout and no colgroup. `markup.cells(spec)` draws every page's lamps; `cell_grammar` is the markup test each page's own
suite runs on what it drew. sd:3588: the lamps sit in the foundation's <annunciator-strip>, and its stylesheet, a pinned
copy (test_v2_foundation), is the only one that styles a lamp, but for the sd:2530 hover ring in shell.css.
"""

from __future__ import annotations

import json
import re
import subprocess
import unittest

from test_v2_tasks import FETCH_JS, LIST_JS, MARKUP_JS, SHELL_JS, STAND_IN, V2
from test_v2_today import OSASCRIPT

STATIC = V2 / "static"
SHELL_CSS = (STATIC / "shell.css").read_text(encoding="utf-8")
# The foundation's own files (test_v2_foundation pins them) are neither page nor shell code.
FOUNDATION = ("tokens.css", "theme.js", "annunciator-strip.css", "annunciator-strip.js")
PAGE_CSS = {p.name: p.read_text(encoding="utf-8") for p in STATIC.glob("*.css") if p.name not in ("shell.css", *FOUNDATION)}
PAGE_JS = {p.name: p.read_text(encoding="utf-8") for p in STATIC.glob("*.js")
           if p.name not in ("shell.js", "markup.js", "sections.js", "read.js", *FOUNDATION)}
# The ledger rules every page that draws one shares; shell.css holds the one copy (sd:2588, sd:2680).
SHARED = (
    ".ledger { width: 100%; border-collapse: collapse; font-size: var(--text-sm); }",
    ".ledger th { text-align: left; font: 600 var(--text-2xs)/1 var(--font-label); letter-spacing: 0.09em; text-transform: uppercase;"
    " color: var(--color-muted); padding: var(--space-xs); border-bottom: var(--rule-hair) solid var(--color-rule); white-space: nowrap; }",
    ".ledger td { padding: var(--space-2xs) var(--space-xs); height: var(--row); border-bottom: var(--rule-hair) solid var(--color-rule-2); vertical-align: middle; }",
    ".ledger .g { text-align: center; font-family: var(--font-data); }",
)
# The lamp selectors only annunciator-strip.css may style, and the ledger selectors only shell.css may. A page keeps what is
# its own: a cell's extra class (`.cell.clock`), what sits inside a cell (`.cell kbd`) and its narrow-screen ledger cards.
LAMP_ONLY = re.compile(r'(?m)^\s*((\.annunciator\s+)?\.cell(\s*[{,]|:hover|:focus-visible|:disabled|\s+\.(lbl|ph|val)\b|\[data-state|\[aria-pressed)'
                       r'|(a|button)\.cell\b|\.annunciator(\s+li)?\s*[{,])')
SHELL_ONLY = re.compile(LAMP_ONLY.pattern + r'|^\s*\.ledger(\s+(th|td)\s*[{,]|\s*[{,]|\s+col\b|\s+tbody tr(:hover td|\[aria-selected="true"\] td)?\s*[{,])')
# The pages whose script draws an annunciator; each draws it through markup.cells. Today's lamps are server HTML that
# today.js fills in place, so its test reads today.html against the same grammar.
LAMP_PAGES = ("activity", "briefs", "contributions", "designs", "health", "hoa", "metrics", "reports", "skills")
# One lamp as markup.cells draws it: a link, a button or a div; the page's attributes; then the label, the value and words
# for a screen reader. A page that draws a lamp by hand fails here.
CELL = re.compile(r'<li><(button|a|div) class="cell(?: [\w-]+)?"(?: type="button"| href="[^"]*")?((?: [\w-]+="[^"]*")*)>'
                  r'<span class="lbl">(?:(?!</?li>).)*?</span><span class="val">(?:(?!</?li>).)*</span>(?:<span class="sr">[^<]*</span>)?</\1></li>', re.S)


def wide(css):
    """`css` without its comments and its @media (max-width: …) blocks, where a page lays its ledger out as cards."""
    css, out, i = re.sub(r"/\*.*?\*/", "", css, flags=re.S), [], 0
    for m in re.finditer(r"@media \(max-width:[^{]*\{", css):
        if m.start() < i:
            continue
        depth, j = 1, m.end()
        while depth:
            depth += {"{": 1, "}": -1}.get(css[j], 0)
            j += 1
        out.append(css[i:m.start()])
        i = j
    return "".join(out) + css[i:]


def cell_grammar(case, markup):
    """Fail `case` unless every <li> in `markup` is a lamp markup.cells drew; return each lamp's (tag, data-state)."""
    items = re.findall(r"<li>.*?</li>(?=<li>|$)", markup.strip(), re.S)
    case.assertTrue(items, "no lamp drawn")
    out = []
    for item in items:
        m = CELL.fullmatch(item)
        case.assertIsNotNone(m, f"not a markup.cells lamp: {item[:160]}")
        state = re.search(r' data-state="(\w+)"', m.group(2))
        out.append((m.group(1), state.group(1) if state else None))
    return out


def run_js(body):
    """The list block with markup.js, as the shell has it; body sets R."""
    script = (STAND_IN + MARKUP_JS + "\nconst { html, plural } = window.markup;\n"
              + "const ICON = n => html`<svg class=\"i\" aria-hidden=\"true\"><use href=\"#i-${n}\"/></svg>`;\n"
              + FETCH_JS + LIST_JS + "\nvar R = {};\n(async () => { try {\n" + body
              + "\n} catch (e) { OUT.error = String(e) + ' ' + e.stack; } })();\n"
              + "function run() { OUT.R = R; return JSON.stringify(OUT); }\n")
    result = subprocess.run([OSASCRIPT, "-l", "JavaScript", "-e", script], capture_output=True, text=True, timeout=60, check=False)
    if result.returncode:
        raise AssertionError(result.stderr)
    out = json.loads(result.stdout)
    if out["error"]:
        raise AssertionError(out["error"])
    return out["R"]


class TheFetchHelpers(unittest.TestCase):
    """sd:2588: one post and one getJSON, in the shell; no page keeps its own."""

    def test_no_page_defines_its_own_csrf_post_or_getjson(self):
        for name, js in PAGE_JS.items():
            with self.subTest(name):
                self.assertNotRegex(js, r"(?m)^\s*(const|let|var) csrf\b|^\s*(async )?function (post|getJSON)\b|sd-csrf")

    def test_post_sends_the_token_and_marks_a_409_stale(self):
        out = run_js("""ANSWER = path => path === '/stale' ? [409, { error: 'changed since' }] : path === '/bad' ? [400, {}] : [200, { ok: 1 }];
  R.ok = await post('/fine', { a: 1 });
  R.stale = await post('/stale', {}).catch(e => [e.message, e.stale]);
  R.bad = await post('/bad', {}).catch(e => [e.message, e.stale]);
  R.read = await getJSON('/stale').catch(e => e.message);
  R.posts = OUT.posts;""")
        self.assertEqual(out["ok"], {"ok": 1})
        self.assertEqual(out["stale"], ["changed since", True])
        self.assertEqual(out["bad"], ["HTTP 400", False])
        self.assertEqual(out["read"], "changed since")
        self.assertEqual(out["posts"][0], ["/fine", {"a": 1}, 64])


# sd:3520: the shell files every capture. A page hook only adds to it; with none, the dialog still files a row.
CAPTURE = re.search(r"^  // capture:start\n(.*?)^  // capture:end$", SHELL_JS, re.S | re.M)


def run_capture(case, body):
    case.assertIsNotNone(CAPTURE, "shell.js has no capture:start block, so a page without SHELL_CAPTURE files nothing")
    return run_js(CAPTURE.group(1) + body)


class TheDefaultCapture(unittest.TestCase):
    """sd:3520: a capture on a page without its own hook files a row, and a toast says Captured only with the new id."""

    def test_a_task_or_followup_posts_to_api_items_and_names_the_new_id(self):
        out = run_capture(self, """ANSWER = (path, body) => [201, { item: { id: 77, title: body.title } }];
  R.task = await fileCapture({ kind: 'task', title: 'Read it' });
  R.followup = await fileCapture({ kind: 'followup', title: 'Then this', item: 12 });
  R.p = await fileCapture({ kind: 'task', title: 'Ship it p1 today' });
  R.posts = OUT.posts;""")
        self.assertEqual(out["task"], "Captured sd:77: Read it")
        self.assertEqual(out["followup"], "Captured sd:77: Then this")
        self.assertEqual(out["posts"], [["/api/items", {"title": "Read it"}, 64],
                                        ["/api/items", {"title": "Then this", "kind": "followup", "followup_of": 12}, 64],
                                        ["/api/items", {"title": "Ship it today", "priority": 1}, 64]])
        self.assertEqual(out["p"], "Captured sd:77: Ship it today")

    def test_a_note_reads_the_items_revision_then_posts_the_note(self):
        out = run_capture(self, """ANSWER = path => path === '/api/items/12/capture-context' ? [200, { item: { id: 12 }, revision: 'r'.repeat(64) }] : [200, {}];
  R.note = await fileCapture({ kind: 'note', title: 'Seen it', item: 12 });
  R.gets = OUT.gets; R.posts = OUT.posts;""")
        self.assertEqual(out["note"], "Note added to sd:12")
        self.assertEqual(out["gets"], ["/api/items/12/capture-context"])
        self.assertEqual(out["posts"], [["/api/items/12/notes", {"body": "Seen it", "kind": "comment", "revision": "r" * 64}, 64]])

    def test_a_refusal_says_not_filed_and_no_answer_says_not_known(self):
        out = run_capture(self, """ANSWER = path => [400, { error: 'A title is required.' }];
  R.refused = await fileCapture({ kind: 'task', title: 'x' }).catch(e => e.message);
  ANSWER = path => { throw new TypeError('Load failed'); };
  R.lost = await fileCapture({ kind: 'task', title: 'x' }).catch(e => e.message);
  ANSWER = path => [201, {}];
  R.empty = await fileCapture({ kind: 'task', title: 'x' }).catch(e => e.message);
  R.noRow = await fileCapture({ kind: 'note', title: 'x', item: null }).catch(e => e.message);""")
        self.assertEqual(out["refused"], "Not filed: A title is required.")
        self.assertEqual(out["lost"], "Not known if it filed: Load failed. Look in Tasks before you capture it again.")
        self.assertEqual(out["empty"], "Not known if it filed: the server named no item. Look in Tasks before you capture it again.")
        self.assertEqual(out["noRow"], "Not filed: select a row that has an item first")

    def test_a_refused_capture_reopens_with_the_kind_priority_and_repo_the_operator_picked(self):
        # Review 2026-10-12 class pass: the reopened dialog kept the text but reset Kind, Priority and Repo to the defaults.
        reopen = re.search(r"if \(m\.startsWith\('Not filed'\)\) openCapture\(o, v(, [^)]*)?\)", SHELL_JS)
        self.assertIsNotNone(reopen)
        self.assertEqual(reopen.group(1), ", picks", "a refusal reopens without the operator's picks")
        self.assertIn("function openCapture(target, text = '', picks = null) {", SHELL_JS)
        self.assertIn("if (picks) { if (!f.querySelector(`[name=\"kind\"][value=\"${picks.kind}\"]`)?.disabled) f.kind.value = picks.kind; f.priority.value = String(picks.priority); }", SHELL_JS)
        self.assertIn("sel.value = captureRepo(list, picks ? picks.repo : hint);", SHELL_JS)
        # A repo the operator picked resolves from the list as any hint does; No repo stays No repo.
        out = run_capture(self, """const LIST = [{ path: '~/repos/one', name: 'one' }];
  R.got = [captureRepo(LIST, '~/repos/one'), captureRepo(LIST, '')];""")
        self.assertEqual(out["got"], ["~/repos/one", ""])

    def test_enter_in_the_capture_box_files_it_and_does_not_cancel(self):
        # Review 2026-10-12 edit-during-refresh check: the form's first button is Cancel, so the browser's implicit submit on
        # Enter closed the box with "no" and dropped the typed text. Enter in a field submits with the Capture button, as the
        # confirm dialog does; the required text still blocks an empty capture.
        box = SHELL_JS[SHELL_JS.index("function openCapture("):SHELL_JS.index("modal(cap, inp);")]
        self.assertLess(box.index('value="no" formnovalidate>Cancel'), box.index('value="yes">Capture'))
        enter = ("if (e.key === 'Enter' && !e.isComposing && e.target.tagName === 'INPUT') { e.preventDefault(); "
                 "f.requestSubmit(f.querySelector('[value=\"yes\"]')); }")
        self.assertTrue(enter in box, "Enter in the capture box cancels it and drops the text")

    def test_the_repo_and_priority_go_with_the_capture_and_a_typed_p_wins(self):
        # sd:3521: the box's picks (the page's repo, P3 unless changed) are fields of the new item; p1 to p4 typed in the
        # title still sets the priority.
        out = run_capture(self, """ANSWER = (path, body) => [201, { item: { id: 78, title: body.title } }];
  await fileCapture({ kind: 'task', title: 'Fix it', priority: 3, repo: '~/repos/one' });
  await fileCapture({ kind: 'followup', title: 'Fix it p1', item: 5, priority: 3, repo: '' });
  await fileCapture({ kind: 'note', title: 'Seen', item: 5, priority: 3, repo: '~/repos/one' }).catch(() => null);
  R.posts = OUT.posts.map(p => p[1]);""")
        self.assertEqual(out["posts"][:2], [{"title": "Fix it", "priority": 3, "repo": "~/repos/one"},
                                            {"title": "Fix it", "priority": 1, "kind": "followup", "followup_of": 5}])

    def test_the_page_repo_resolves_by_path_or_name_and_an_unknown_one_is_no_default(self):
        out = run_capture(self, """const LIST = [{ path: '~/repos/one', name: 'one' }, { path: '~/repos/two', name: 'two' }];
  R.byPath = captureRepo(LIST, '~/repos/two'); R.byName = captureRepo(LIST, 'one'); R.byOwner = captureRepo(LIST, 'someone/two');
  R.unknown = captureRepo(LIST, 'three'); R.none = captureRepo(LIST, null); R.empty = captureRepo([], '~/repos/one');""")
        self.assertEqual([out[k] for k in ("byPath", "byName", "byOwner", "unknown", "none", "empty")],
                         ["~/repos/two", "~/repos/one", "~/repos/two", "", "", ""])

    def test_the_command_line_names_the_repo_and_priority_it_files(self):
        out = run_capture(self, """R.lines = [captureCli({ kind: 'task', title: "it's", priority: 3, repo: '~/repos/one' }),
  captureCli({ kind: 'followup', title: 'x', item: 5, priority: 2, repo: '' }),
  captureCli({ kind: 'note', title: 'x', item: 5, priority: 3, repo: '~/repos/one' }),
  captureCli({ kind: 'task', title: 'x', priority: 4, repo: '/tmp/a b' })];""")
        self.assertEqual(out["lines"], ["cd ~/repos/one && sd task add --priority 3 'it'\\''s'",
                                        "sd task add --no-repo --kind followup --priority 2 'x'",
                                        "sd task note 5 'x' --kind comment",
                                        "cd '/tmp/a b' && sd task add --priority 4 'x'"])

    def test_the_box_offers_the_pickers_with_p3_and_the_pages_repo_as_defaults(self):
        self.assertIn("window.PAGE_REPO", SHELL_JS)
        box = re.search(r"function openCapture\(.*?\n  \}\n", SHELL_JS, re.S).group(0)
        self.assertRegex(box, r'name="priority" value="\$\{n\}"\$\{n === 3 \? html` checked` : ..\}')
        self.assertIn('id="cap-repo"', box)
        self.assertIn('aria-keyshortcuts="Alt+R"', box)
        for page in ("management.js", "tasks.js", "today.js"):
            with self.subTest(page):
                self.assertIn("window.PAGE_REPO =", PAGE_JS[page])

    def test_the_dialog_files_through_the_page_hook_or_the_default_and_never_toasts_captured_itself(self):
        close = re.search(r"cap\.onclose = .*?\n    modal\(cap, inp\);", SHELL_JS, re.S)
        self.assertIsNotNone(close)
        self.assertIn("(window.SHELL_CAPTURE || fileCapture)(", close.group(0))
        self.assertNotIn("'Captured'", close.group(0))
        self.assertNotIn("Note added", close.group(0))

    def test_only_tasks_and_review_keep_a_page_hook_and_review_files_tasks_through_the_shell(self):
        # Today's and Briefs' hooks filed the same POST the shell now files; Tasks keeps its own to reread its board.
        # Review (sd:3709, merged from main) keeps one for a note on a question, a comment on its batch; a task or a
        # followup goes through the shell's fileCapture, so the box's priority and repo (sd:3521) reach the item.
        hooks = sorted(name for name, js in PAGE_JS.items() if re.search(r"window\.SHELL_CAPTURE\s*=", js))
        self.assertEqual(hooks, ["review.js", "tasks.js"])
        self.assertTrue("return window.shell.fileCapture({ kind, title, item, priority, repo });" in PAGE_JS["review.js"],
                        "Review's capture drops the priority and repo the operator picked")


class TheActBar(unittest.TestCase):
    """sd:3464, sd:3444: the Details Act area holds the buttons; the CLI lines and the off and copy-only notes fold away."""

    def test_the_cli_section_is_a_details_element_closed_by_default(self):
        bar = re.search(r"  function bar\(id\) \{.*?\n  \}\n", SHELL_JS, re.S).group(0)
        self.assertIn('<details class="cli-all"><summary>Same thing from the CLI</summary>', bar)
        self.assertNotIn("<details class=\"cli-all\" open", bar)
        self.assertNotIn("<h3>Same thing from the CLI</h3>", bar)
        # Nothing explains a command between the buttons and the fold: the off reasons are inside it.
        self.assertRegex(bar, r'</div>\s*<details class="cli-all">')

    def test_a_help_icon_with_a_command_copies_it_when_pressed(self):
        click = re.search(r"document\.addEventListener\('click', e => \{\n(?:    .*\n)?    const b = e\.target\.closest\('\.help'\);.*?\}, true\);", SHELL_JS, re.S)
        self.assertIsNotNone(click)
        self.assertIn("if (b.dataset.copy) copy(b.dataset.copy);", click.group(0))


class TheSharedRules(unittest.TestCase):
    """sd:2588: shell.css holds each shared annunciator and ledger rule once; no page repeats one."""

    def test_shell_css_holds_each_shared_rule_and_no_page_repeats_it(self):
        for rule in SHARED:
            self.assertEqual(SHELL_CSS.count(rule), 1, rule)
            for name, css in PAGE_CSS.items():
                with self.subTest(name, rule=rule[:30]):
                    self.assertNotIn(rule, css)

    def test_no_page_restyles_the_lamp_or_the_ledger(self):
        for name, css in PAGE_CSS.items():
            if name == "home.css":  # Home's lamp grid is its own component (home.js), not the annunciator
                continue
            with self.subTest(name):
                self.assertEqual([m.group(0).strip() for m in SHELL_ONLY.finditer(wide(css))], [])

    def test_the_shell_leaves_the_lamp_to_the_foundation_strip(self):
        self.assertEqual([m.group(0).strip() for m in LAMP_ONLY.finditer(wide(SHELL_CSS))], [])
        self.assertTrue(LAMP_ONLY.search(wide((STATIC / "annunciator-strip.css").read_text(encoding="utf-8"))),
                        "the pattern finds the strip's own lamp rules")

    def test_one_ledger_layout_auto_with_no_colgroup(self):
        self.assertNotIn("table-layout", SHELL_CSS)
        for name, text in {**PAGE_CSS, **PAGE_JS, **{p.name: p.read_text(encoding="utf-8") for p in V2.glob("*.html")}}.items():
            with self.subTest(name):
                self.assertFalse("table-layout" in text or "<colgroup" in text)


class TheHallmarkStamp(unittest.TestCase):
    """sd:1333: every v2 stylesheet names the Hallmark run it came from; fonts.css is a font list, not a design."""

    def test_every_stylesheet_but_fonts_carries_a_hallmark_stamp(self):
        for path in sorted(STATIC.glob("*.css")):
            if path.name == "fonts.css":
                continue
            with self.subTest(path.name):
                self.assertRegex(path.read_text(encoding="utf-8"), r"(?m)^(/\*| \*) Hallmark · ")


class TheCells(unittest.TestCase):
    """sd:2680: markup.cells(spec) draws every annunciator's lamps; no page writes a lamp's markup by hand."""

    def test_every_lamp_page_draws_through_cells_and_writes_no_cell_markup(self):
        for page in LAMP_PAGES:
            with self.subTest(page):
                js = PAGE_JS[f"{page}.js"]
                self.assertTrue("cells(" in js and 'class="cell' not in js)

    def test_todays_server_drawn_lamps_follow_the_same_grammar(self):
        today = (V2 / "today.html").read_text(encoding="utf-8")
        lamps = re.search(r'<ul class="annunciator" id="annunciator">(.*?)</ul>', today, re.S).group(1)
        self.assertEqual(cell_grammar(self, re.sub(r">\s+<li>", "><li>", lamps)), [("button", "unknown")] * 4 + [("button", None)])
        self.assertNotIn('class="cell', PAGE_JS["today.js"])

    def test_a_spec_becomes_a_button_a_link_or_a_div(self):
        out = run_js("""const { cells } = window.markup;
  R.button = cells([{ button: true, state: 'caution', pressed: true, attrs: html` data-area="wt"`, label: 'Worktrees', mark: ICON('folder'),
    val: html`<b>2</b> gone`, sr: 'state caution' }]).text;
  R.link = cells([{ href: '?row=a&b', state: 'ok', label: 'Use', mark: html`<span class="g" aria-hidden="true">●</span>`, val: '1 row', small: 'from claude' }]).text;
  R.div = cells([null, { state: 'unknown', cls: 'clock', label: '<x>', val: 'not read' }, false]).text;
  R.plain = cells([{ button: true, attrs: html` id="refresh"`, label: 'Observed', val: 'now' }]).text;
  try { cells([{ attrs: ' onclick="x()"', label: 'a', val: 'b' }]); R.refused = false; } catch (e) { R.refused = e instanceof TypeError; }""")
        self.assertEqual(out["button"], '<li><button class="cell" type="button" data-area="wt" data-state="caution" aria-pressed="true">'
                         '<span class="lbl">Worktrees<svg class="i" aria-hidden="true"><use href="#i-folder"/></svg></span>'
                         '<span class="val"><b>2</b> gone</span><span class="sr">state caution</span></button></li>')
        self.assertEqual(out["link"], '<li><a class="cell" href="?row=a&amp;b" data-state="ok"><span class="lbl">Use<span class="g" aria-hidden="true">●</span></span>'
                         '<span class="val">1 row<small>from claude</small></span></a></li>')
        self.assertEqual(out["div"], '<li><div class="cell clock" data-state="unknown"><span class="lbl">&lt;x&gt;</span><span class="val">not read</span></div></li>')
        self.assertEqual(out["plain"], '<li><button class="cell" type="button" id="refresh"><span class="lbl">Observed</span><span class="val">now</span></button></li>')
        self.assertTrue(out["refused"])
        self.assertEqual(cell_grammar(self, out["button"] + out["link"] + out["div"] + out["plain"]),
                         [("button", "caution"), ("a", "ok"), ("div", "unknown"), ("button", None)])


class TheLampHover(unittest.TestCase):
    """sd:2530: a lit lamp keeps its state colour under the pointer. The operator ruled for a ring (2026-10-13); the
    foundation strip draws it since ui-design sd:3743 (sd:4750), so no stylesheet here dims or rings a lamp."""

    STRIP = (STATIC / "annunciator-strip.css").read_text(encoding="utf-8")

    def test_no_state_hover_shifts_brightness(self):
        for name, css in {**PAGE_CSS, "shell.css": SHELL_CSS, "annunciator-strip.css": self.STRIP}.items():
            with self.subTest(name):
                self.assertNotRegex(css, r'data-state="(caution|warning|unknown)"\][^{]*:hover[^{]*\{[^}]*filter:(?!\s*none\b)')

    def test_the_strip_no_longer_dims(self):
        self.assertNotIn("brightness(", self.STRIP)

    def test_the_strip_draws_the_hover_ring_in_the_lamps_ink_token(self):
        for state, ink in (("caution", "--color-caution-lamp-ink"), ("warning", "--color-warning-ink"), ("unknown", "--color-ink-2")):
            self.assertIn(f'.cell[data-state="{state}"]:not([aria-pressed="true"]):is(:hover, .is-hover) '
                          f'{{ box-shadow: inset 0 0 0 1px var({ink}); }}', self.STRIP)

    def test_the_shell_neither_cancels_the_dim_nor_draws_a_ring(self):
        self.assertNotIn("filter:", SHELL_CSS.split("Annunciator and ledger")[1].split("The ledger")[0])
        self.assertNotRegex(SHELL_CSS, r'\.cell\[data-state="(caution|warning|unknown)"\][^{]*\{[^}]*box-shadow')


class TheSortHeader(unittest.TestCase):
    """sd:2527: each column with an order sorts through a button; only the sorted th carries aria-sort."""

    def test_only_the_sorted_th_carries_aria_sort_and_each_ordered_column_has_a_button(self):
        out = run_js("""const cols = [['a', 'Alpha'], ['b', 'Beta'], [null, 'Actions']];
  const st = { sort: 'b', dir: -1, page: 3 };
  R.head = sortHead(cols, st).text;
  R.flip = [sortBy({ target: { closest: () => ({ dataset: { sort: 'b' } }) } }, st), st.sort, st.dir, st.page];
  R.other = (sortBy({ target: { closest: () => ({ dataset: { sort: 'a' } }) } }, st), [st.sort, st.dir]);
  R.none = sortBy({ target: { closest: () => null } }, st);""")
        self.assertEqual(re.findall(r'aria-sort="(\w+)"', out["head"]), ["descending"])
        self.assertRegex(out["head"], r'<th scope="col" class="" aria-sort="descending"><button class="sorter" type="button" data-sort="b"[^>]*>Beta<svg[^>]*><use href="#i-arrow-down"/>')
        self.assertIn('data-sort="a" title="Sort ascending">Alpha<svg class="i" aria-hidden="true"><use href="#i-arrow-up-down"/>', out["head"])
        self.assertIn('<th scope="col" class="">Actions</th>', out["head"])
        self.assertEqual(out["flip"], [True, "b", 1, 1])
        self.assertEqual(out["other"], ["a", 1])
        self.assertFalse(out["none"])


class ThePager(unittest.TestCase):
    """sd:2528: a range line, numbered pages and the sizes 25, 50, 100 and 200."""

    def test_the_range_line_reads_like_51_to_100_of_1284(self):
        out = run_js("""R.mid = rangeText(1284, { page: 2, size: 50 }); R.last = rangeText(1284, { page: 26, size: 50 });
  R.none = rangeText(0, { page: 1, size: 50 }); R.html = pager(1284, { page: 2, size: 50 }, 'reports').text;""")
        self.assertEqual(out["mid"], "51–100 of 1,284")
        self.assertEqual(out["last"], "1,251–1,284 of 1,284")
        self.assertEqual(out["none"], "0 of 0")
        self.assertIn('<span class="range">51–100 of 1,284</span>', out["html"])
        self.assertEqual(re.findall(r'data-size="(\d+)" aria-pressed="(\w+)"', out["html"]),
                         [("25", "false"), ("50", "true"), ("100", "false"), ("200", "false")])
        self.assertEqual(re.findall(r'data-page="(\d+)"[^>]*aria-current="page"', out["html"]), ["2"])

    def test_pages_show_the_ends_and_two_either_side_with_one_gap_for_a_skipped_run(self):
        out = run_js("R.a = pageList(26, 2); R.b = pageList(26, 13); R.c = pageList(3, 1); R.d = pageList(1, 1);")
        self.assertEqual(out["a"], [1, 2, 3, 4, "…", 26])
        self.assertEqual(out["b"], [1, "…", 11, 12, 13, 14, 15, "…", 26])
        self.assertEqual(out["c"], [1, 2, 3])
        self.assertEqual(out["d"], [1])

    def test_a_click_moves_the_page_and_a_size_starts_again_at_page_one(self):
        out = run_js("""const st = { page: 4, size: 50 }, at = (sel, ds) => ({ target: { closest: s => s === sel ? { dataset: ds } : null } });
  R.page = [paging(at('.list-pager [data-page]', { page: '7' }), st), st.page];
  R.size = [paging(at('.list-pager [data-size]', { size: '200' }), st), st.page, st.size];
  const rows = Array.from({ length: 120 }, (_, i) => i), late = { page: 9, size: 50 };
  R.slice = [pageOf(rows, late).length, late.page];""")
        self.assertEqual(out["page"], [True, 7])
        self.assertEqual(out["size"], [True, 1, 200])
        self.assertEqual(out["slice"], [20, 3])


class TheFocusAfterARedraw(unittest.TestCase):
    """sd:2682 review: a redraw replaces the header and the pager, so focus moves to the new copy of the clicked button."""

    def test_focus_goes_to_the_redrawn_copy_of_the_clicked_control(self):
        out = run_js("""const click = ds => {
    const e = { target: { closest: () => ({ dataset: ds }) }, currentTarget: { querySelector: s => ({ focus: () => R.seen.push([s, R.drawn]) }) } };
    keepFocus(e, () => R.drawn++);
  };
  R.seen = []; R.drawn = 0;
  click({ sort: 'mod' }); click({ page: '3' }); click({ size: '100' });
  keepFocus({ target: { closest: () => null }, currentTarget: null }, () => R.drawn++);""")
        self.assertEqual(out["seen"], [['button[data-sort="mod"]', 1], ['button[data-page="3"]', 2], ['button[data-size="100"]', 3]])
        self.assertEqual(out["drawn"], 4)

    def test_every_page_redraws_a_sort_or_pager_click_through_keep_focus(self):
        for name, js in PAGE_JS.items():
            for line in re.findall(r"(?m)^.*\b(?:sortBy|paging)\(e, .*$", js):
                with self.subTest(name, line=line.strip()[:80]):
                    self.assertIn("keepFocus(e, ", line)

    def test_page_size_and_sort_round_trip_through_the_url_and_bad_values_are_ignored(self):
        out = run_js("""const opts = { sorts: ['at', 'what'], size: 50 }, defaults = { sort: 'at', dir: -1, size: 50 };
  const st = listParams(new URLSearchParams('?page=3&size=100&sort=what&dir=desc'), {}, opts);
  R.read = st; R.back = listQuery(new URLSearchParams(), st, defaults).toString();
  R.bad = listParams(new URLSearchParams('?page=1.5&size=30&sort=nope'), { page: 1, sort: 'at', dir: -1 }, opts);
  R.hex = listParams(new URLSearchParams('?page=0x2'), { page: 1 }, opts).page;
  R.plain = listQuery(new URLSearchParams(), { page: 1, size: 50, sort: 'at', dir: -1 }, defaults).toString();""")
        self.assertEqual(out["read"], {"page": 3, "size": 100, "sort": "what", "dir": -1})
        self.assertEqual(out["back"], "page=3&size=100&sort=what&dir=desc")
        self.assertEqual(out["bad"], {"page": 1, "sort": "at", "dir": -1, "size": 50})
        self.assertEqual(out["hex"], 1)
        self.assertEqual(out["plain"], "")


class TheFilterChips(unittest.TestCase):
    """sd:2529: one removable chip per active filter, a count, and one Clear all."""

    def test_chips_name_each_filter_the_count_and_clear_all(self):
        out = run_js("""R.two = chips([{ key: 'job', label: 'Job: nightly' }, { key: 'act', label: 'Needs action' }], 3, 1284).text;
  R.none = chips([], 10, 10).text;""")
        self.assertEqual(re.findall(r'data-unfilter="(\w+)"', out["two"]), ["job", "act"])
        self.assertIn('<span class="n">2 filters · 3 of 1,284</span>', out["two"])
        self.assertEqual(out["two"].count("data-unfilter-all"), 1)
        self.assertIn('aria-label="Remove filter: Job: nightly"', out["two"])
        self.assertEqual(out["none"], "")


SHELL_JS = (STATIC / "shell.js").read_text(encoding="utf-8")
#: The rail's markup as shell.js draws it: its head row (the mark and its controls) and its foot.
RAIL = re.search(r"put\(rail, html`(.*?)`\);", SHELL_JS, re.S).group(1)
RAIL_HEAD = re.search(r'<div class="rail-head">(.*?)\n    </div>', RAIL, re.S)
RAIL_FOOT = re.search(r'<div class="foot">(.*?)</div>\s*$', RAIL, re.S).group(1)


class TheRailHead(unittest.TestCase):
    """sd:3442, sd:3443: the rail's fold and Reload are small icon buttons beside the title, not rows in the foot."""

    def test_the_fold_sits_in_the_head_beside_the_mark_and_not_in_the_foot(self):
        self.assertIsNotNone(RAIL_HEAD, "the rail has no head row")
        head = RAIL_HEAD.group(1)
        self.assertLess(head.index('class="mark"'), head.index('id="fold-rail"'))
        self.assertRegex(head, r'<button class="icon-btn" type="button" id="fold-rail"[^>]* aria-label="Collapse sections \(\[\)"')
        self.assertNotIn("fold-rail", RAIL_FOOT)

    def test_reload_sits_in_the_head_between_the_mark_and_the_fold(self):
        # sd:3443: Reload moves up beside the fold; the shell still binds it by id within the rail.
        self.assertIsNotNone(RAIL_HEAD, "the rail has no head row")
        head = RAIL_HEAD.group(1)
        self.assertLess(head.index('class="mark"'), head.index('id="reload"'))
        self.assertLess(head.index('id="reload"'), head.index('id="fold-rail"'))
        self.assertRegex(head, r'<button class="icon-btn" type="button" id="reload"[^>]* aria-label="Reload this page \(r\)"')
        self.assertNotIn('id="reload"', RAIL_FOOT)
        self.assertIn("rail.querySelector('#reload').addEventListener('click', () => location.reload());", SHELL_JS)


WIDTH = re.search(r"^  // width:start\n(.*?)^  // width:end$", SHELL_JS, re.S | re.M)


class TheRailWidth(unittest.TestCase):
    """sd:3445: the rail's width is set on its edge, clamped, kept per browser as sd.rail.width, and reset to the default."""

    def run_width(self, body, stored=None):
        self.assertIsNotNone(WIDTH, "shell.js has no width block")
        script = ("var OUT = {}, STORE = " + json.dumps({} if stored is None else {"sd.rail.width": stored}) + ", ATTR = {}, PROPS = {};\n"
                  "var EVENTS = []; function CustomEvent(type, o) { this.type = type; this.detail = o.detail; }\n"
                  "var document = { documentElement: { style: { setProperty: (k, v) => { PROPS[k] = v; }, removeProperty: k => { delete PROPS[k]; } } },\n"
                  "  dispatchEvent: e => EVENTS.push([e.type, e.detail]) };\n"
                  "const store = (k, v) => { if (v === undefined) return k in STORE ? STORE[k] : null; STORE[k] = v; return v; };\n"
                  "const rail = { getBoundingClientRect: () => ({ width: 216 }) }, grip = { setAttribute: (k, v) => { ATTR[k] = v; } };\n"
                  + WIDTH.group(1) + "\nvar R = {};\n" + body
                  + "\nfunction run() { return JSON.stringify({ R, STORE, ATTR, PROPS, EVENTS }); }\n")
        result = subprocess.run([OSASCRIPT, "-l", "JavaScript", "-e", script], capture_output=True, text=True, timeout=60, check=False)
        if result.returncode:
            raise AssertionError(result.stderr)
        return json.loads(result.stdout)

    def test_a_width_is_clamped_set_on_the_root_and_kept(self):
        out = self.run_width("R.set = setWidth(250.4); R.low = railWidth(20); R.high = railWidth('900'); R.junk = railWidth('wide');")
        self.assertEqual(out["R"], {"set": 250, "low": 176, "high": 384, "junk": None})
        self.assertEqual(out["PROPS"], {"--rail": "250px"})
        self.assertEqual(out["STORE"], {"sd.rail.width": "250"})
        self.assertEqual((out["ATTR"]["role"], out["ATTR"]["aria-valuenow"], out["ATTR"]["aria-valuemin"], out["ATTR"]["aria-valuemax"]),
                         ("separator", "250", "176", "384"))

    def test_a_drag_moves_without_storing_and_a_reset_clears_the_kept_width(self):
        out = self.run_width("setWidth(300, false); R.moved = Object.assign({}, PROPS); setWidth(null);", stored="240")
        self.assertEqual(out["R"]["moved"], {"--rail": "300px"})
        self.assertEqual(out["PROPS"], {})
        self.assertEqual(out["STORE"], {"sd.rail.width": ""})
        self.assertEqual(out["ATTR"]["aria-valuenow"], "216")

    def test_a_kept_width_tells_the_page_and_a_drag_in_progress_does_not(self):
        # Settings shows the width the edge set; without the event its control kept the old preset checked.
        out = self.run_width("setWidth(300, false); setWidth(250); setWidth(null);")
        self.assertEqual(out["EVENTS"], [["shell:rail-width", 250], ["shell:rail-width", None]])

    def test_the_shell_applies_the_kept_width_on_load_and_hands_pages_the_setter(self):
        self.assertIn("setWidth(store('sd.rail.width'), false);", SHELL_JS)
        self.assertIn("railWidth: (...a) => a.length ? setWidth(a[0]) : railWidth(store('sd.rail.width'))", SHELL_JS)

    def test_the_grip_sizes_come_from_tokens(self):
        # Foundation tokens.css covers space and rules, so the grip's own rules carry no raw px (the 2px focus ring aside: no token).
        rules = re.findall(r"^[^\n]*\.rail-grip[^{\n]*\{([^}]*)\}", SHELL_CSS, re.M)
        self.assertTrue(rules)
        self.assertEqual([r for r in rules if re.search(r"\d+px", r)], [])



# The view switch's stand-in: elements that keep their attributes, a rail with one current page, and an address whose
# pushState writes the hash. Events on window (hashchange, popstate) and document (DOMContentLoaded, keydown, shell:view)
# are recorded, so a test fires them as the browser would.
VIEW_STAND_IN = r"""
var window = globalThis, OUT = { events: [], pushed: [], error: null };
var WIN = {}, DOC = {};
function addEventListener(t, f) { (WIN[t] = WIN[t] || []).push(f); }
const fireWin = t => (WIN[t] || []).forEach(f => f({ type: t }));
function El(tag) { const e = { tagName: tag, id: '', attrs: {}, hidden: false, dataset: {}, children: [], listeners: {}, textContent: '', className: '', tabIndex: 0,
  setAttribute(k, v) { e.attrs[k] = String(v); if (k === 'id') e.id = String(v); }, getAttribute(k) { return k in e.attrs ? e.attrs[k] : null; },
  removeAttribute(k) { delete e.attrs[k]; }, append(...cs) { e.children.push(...cs); }, before(c) { OUT.before = [c.getAttribute('role'), e.id]; },
  addEventListener(t, f) { (e.listeners[t] = e.listeners[t] || []).push(f); }, fire(t, ev) { (e.listeners[t] || []).forEach(f => f(Object.assign({ type: t, target: e, preventDefault() {} }, ev))); },
  focus() { OUT.focused = e.id; }, closest() { return null; } };
  return e; }
var PANELS = {}, MADE = [], HOST = WITH_HOST ? El('div') : null;
['reports', 'cadence'].forEach(id => { const p = El('section'); p.id = 'view-' + id; PANELS[p.id] = p; });
var document = { readyState: 'loading', activeElement: null,
  getElementById: id => (id === 'viewswitch' ? HOST : PANELS[id]) || null, createElement: tag => { const e = El(tag); MADE.push(e); return e; },
  querySelector: () => null,
  addEventListener(t, f) { (DOC[t] = DOC[t] || []).push(f); },
  dispatchEvent(e) { if (e.type === 'shell:view') OUT.events.push(e.detail); (DOC[e.type] || []).forEach(f => f(e)); } };
function CustomEvent(type, o) { this.type = type; this.detail = o && o.detail; }
var location = { hash: HASH, pathname: '/reports', search: '?job=nightly' };
var history = { state: null, pushState(s, t, u) { OUT.pushed.push(u); location.hash = u.slice(u.indexOf('#')); } };
var LI = El('li'), LINK = El('a');
LINK.closest = sel => sel === 'li' ? LI : null;
var rail = { querySelector: sel => sel === '[data-section="Reports"]' ? LINK : null };
var page = 'Reports', hrefOf = name => name === 'Reports' ? '/reports' : undefined;
window.PAGE_KEYS = [['/', 'Filter reports']];
window.PAGE_VIEWS = [{ id: 'reports', label: 'Reports' }, { id: 'cadence', label: 'Cadence', api: '/api/reports/cadence' }, { id: 'gone', label: 'No panel' }];
"""


class TheViewSwitch(unittest.TestCase):
    """The view switch, cut out of shell.js: one declaration, window.PAGE_VIEWS, draws the tablist and the rail's sub-entries."""

    def run_view(self, body, hash="", host=False):
        block = re.search(r"^  // view:start\n(.*?)^  // view:end$", SHELL_JS, re.S | re.M)
        self.assertIsNotNone(block, "shell.js has no view:start block")
        script = (VIEW_STAND_IN.replace("HASH", json.dumps(hash)).replace("WITH_HOST", json.dumps(host)) + block.group(1) + r"""
var R = {};
const shown = () => Object.values(PANELS).filter(p => !p.hidden).map(p => p.id);
const SWITCH_SEL = MADE.find(e => e.getAttribute('role') === 'tablist'), SWITCH_TABS = SWITCH_SEL ? SWITCH_SEL.children : [];
try {
(DOC.DOMContentLoaded || []).forEach(f => f({ type: 'DOMContentLoaded' }));
""" + body + """
} catch (e) { OUT.error = String(e) + ' ' + e.stack; }
function run() { OUT.R = R; return JSON.stringify(OUT); }
""")
        result = subprocess.run([OSASCRIPT, "-l", "JavaScript", "-e", script], capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertIsNone(out["error"])
        return out

    def test_the_first_view_is_the_default_and_only_its_panel_renders(self):
        out = self.run_view("R.cur = view.current(); R.shown = shown(); R.tabs = SWITCH_TABS.map(t => [t.getAttribute('aria-controls'), t.getAttribute('aria-selected'), t.tabIndex]);"
                            " R.roles = Object.values(PANELS).map(p => [p.getAttribute('role'), p.getAttribute('aria-labelledby')]);")
        self.assertEqual(out["R"]["cur"], "reports")
        self.assertEqual(out["R"]["shown"], ["view-reports"])
        # A view whose page has no panel is not drawn.
        self.assertEqual(out["R"]["tabs"], [["view-reports", "true", 0], ["view-cadence", "false", -1]])
        self.assertEqual(out["R"]["roles"], [["tabpanel", "vt-reports"], ["tabpanel", "vt-cadence"]])
        # The tablist goes in once, before the first panel, and the page hears the first view once, at DOMContentLoaded.
        self.assertEqual(out["before"], ["tablist", "view-reports"])
        self.assertEqual(out["events"], [{"id": "reports", "first": True, "api": None}])
        self.assertEqual(out["pushed"], [])

    def test_the_hash_round_trips(self):
        out = self.run_view("""R.start = [view.current(), shown()];
view.show('reports'); R.shown = [location.hash, shown()];
location.hash = '#view=cadence'; fireWin('hashchange'); R.back = [view.current(), shown()];
location.hash = '#main'; fireWin('hashchange'); R.other = view.current();
location.hash = ''; fireWin('popstate'); R.none = view.current();
R.unknown = view.show('nope');""", hash="#view=cadence")
        self.assertEqual(out["R"]["start"], ["cadence", ["view-cadence"]])
        self.assertEqual(out["R"]["shown"], ["#view=reports", ["view-reports"]])
        self.assertEqual(out["R"]["back"], ["cadence", ["view-cadence"]])
        # Another hash (the skip link's #main) leaves the view alone; no hash at all is the first view.
        self.assertEqual((out["R"]["other"], out["R"]["none"], out["R"]["unknown"]), ("cadence", "reports", False))
        self.assertEqual(out["pushed"], ["#view=reports"])
        self.assertEqual(out["events"], [{"id": "cadence", "first": True, "api": "/api/reports/cadence"},
                                         {"id": "reports", "first": True, "api": None},
                                         {"id": "cadence", "first": False, "api": "/api/reports/cadence"},
                                         {"id": "reports", "first": False, "api": None}])

    def test_a_segment_arrow_keys_and_a_v_chord_switch_the_view(self):
        out = self.run_view("""SWITCH_TABS[1].fire('click'); R.click = view.current();
document.activeElement = SWITCH_TABS[1]; SWITCH_SEL.fire('keydown', { key: 'ArrowRight', target: SWITCH_TABS[1] }); R.wrap = [view.current(), OUT.focused];
SWITCH_SEL.fire('keydown', { key: 'End', target: SWITCH_TABS[0] }); R.end = view.current();
const key = k => (DOC.keydown || []).forEach(f => f({ type: 'keydown', key: k, target: { matches: () => false }, preventDefault() {}, stopImmediatePropagation() {} }));
key('v'); key('r'); R.chord = view.current(); R.keys = window.PAGE_KEYS;""")
        self.assertEqual(out["R"]["click"], "cadence")
        self.assertEqual(out["R"]["wrap"], ["reports", "vt-reports"])
        self.assertEqual(out["R"]["end"], "cadence")
        self.assertEqual(out["R"]["chord"], "reports")
        self.assertEqual(out["R"]["keys"], [["/", "Filter reports"], ["v r / v c", "Reports or Cadence view"]])

    def test_the_rail_lists_the_views_under_the_page_and_marks_the_current_one(self):
        out = self.run_view("""const links = () => LI.children[0].children.map(li => li.children[0]);
R.links = links().map(a => [a.getAttribute('href'), a.textContent, a.getAttribute('aria-current')]);
let stopped = false; links()[1].fire('click', { preventDefault() { stopped = true; } }); R.after = [view.current(), stopped, location.hash];
R.marked = links().map(a => a.getAttribute('aria-current'));""")
        self.assertEqual(out["R"]["links"], [["/reports#view=reports", "Reports", "true"], ["/reports#view=cadence", "Cadence", None]])
        # On the page a sub-entry switches in place, so the page keeps its filters (?job=); the href opens a new tab there.
        self.assertEqual(out["R"]["after"], ["cadence", True, "#view=cadence"])
        self.assertEqual(out["R"]["marked"], [None, "true"])

    def test_a_count_shows_on_its_segment_and_null_hides_it(self):
        out = self.run_view("""view.count('cadence', 7); const n = SWITCH_TABS[1].children[1]; R.on = [n.textContent, n.hidden];
view.count('cadence', null); R.off = n.hidden; R.label = SWITCH_TABS[1].children[0].textContent;""")
        self.assertEqual(out["R"]["on"], ["7", False])
        self.assertEqual((out["R"]["off"], out["R"]["label"]), (True, "Cadence"))

    def test_a_page_with_a_host_gets_the_switch_there_and_keeps_views_without_a_panel(self):
        out = self.run_view("""R.inHost = HOST.children[0] === SWITCH_SEL;
R.tabs = SWITCH_TABS.map(t => [t.id, t.getAttribute('aria-controls')]);
view.show('gone'); R.gone = [view.current(), shown(), location.hash];
view.show('cadence'); R.back = shown();""", host=True)
        # Tasks filters one board, matrix and list: its views have no panels, so the page names where the tablist goes.
        self.assertTrue(out["R"]["inHost"], "the tablist did not go in the page's #viewswitch")
        self.assertNotIn("before", out)
        self.assertEqual(out["R"]["tabs"], [["vt-reports", "view-reports"], ["vt-cadence", "view-cadence"], ["vt-gone", None]])
        self.assertEqual(out["R"]["gone"], ["gone", [], "#view=gone"])
        self.assertEqual(out["R"]["back"], ["view-cadence"])
        self.assertEqual([e["id"] for e in out["events"]], ["reports", "gone", "cadence"])

    def test_the_shell_hands_pages_the_view_api_and_styles_the_switch(self):
        self.assertRegex(SHELL_JS, r"window\.shell = \{[^\n]*\bview\b")
        self.assertIn(".viewswitch", SHELL_CSS)
        self.assertRegex(SHELL_CSS, r"\.viewswitch \[role=\"tab\"\][^{]*\{[^}]*white-space: nowrap")


if __name__ == "__main__":
    unittest.main()

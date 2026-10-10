"""What every v2 page now takes from the shell (sd:2588, sd:2527, sd:2528, sd:2529, sd:2530).

`shell.js` holds the page's reads and writes (`shell.post`, `shell.getJSON`) and the list grammar (`shell.list`: sort header,
numbered pager, filter chips, and the URL keys they keep). `shell.css` holds the annunciator and ledger rules that were the
same on every page, the list grammar's styles, and the lamp hover: a ring in the lamp's ink token, not a brightness filter.

The JavaScript runs under JavaScriptCore (osascript) on the blocks the tests cut out of `shell.js`, so a test reads the
shipped code, not a copy. Reports wires the list grammar end to end; `test_v2_reports` drives that page.

sd:2680: one annunciator and one ledger layout. shell.css holds the whole lamp (a page sets only `--cells`, its column
count) and the ledger's desktop rules, with the browser's auto table layout and no colgroup. `markup.cells(spec)` draws
every page's lamps; `cell_grammar` is the markup test each page's own suite runs on what it drew.
"""

from __future__ import annotations

import json
import re
import subprocess
import unittest

from test_v2_tasks import FETCH_JS, LIST_JS, MARKUP_JS, STAND_IN, V2
from test_v2_today import OSASCRIPT

STATIC = V2 / "static"
SHELL_CSS = (STATIC / "shell.css").read_text(encoding="utf-8")
PAGE_CSS = {p.name: p.read_text(encoding="utf-8") for p in STATIC.glob("*.css") if p.name not in ("shell.css", "tokens.css")}
PAGE_JS = {p.name: p.read_text(encoding="utf-8") for p in STATIC.glob("*.js")
           if p.name not in ("shell.js", "markup.js", "sections.js", "read.js")}
# The rules every page that draws an annunciator or a ledger shares; shell.css holds the one copy (sd:2588, sd:2680).
SHARED = (
    ".annunciator { display: grid; grid-template-columns: repeat(var(--cells, 3), minmax(0, 1fr)); gap: 1px; margin: 0 0 var(--space-xl);"
    " padding: 1px; background: var(--color-rule); border-radius: var(--radius-md); list-style: none; }",
    ".annunciator li { display: contents; }",
    ".cell { font: inherit; border: 0; text-align: left; width: 100%; min-width: 0; display: grid; grid-template-rows: auto 1fr;"
    " gap: var(--space-2xs); min-height: 4.5rem; padding: var(--space-xs) var(--space-sm); background: var(--color-panel);"
    " color: var(--color-muted); text-decoration: none; }",
    ".cell .lbl { font: 600 var(--text-2xs)/1.2 var(--font-label); letter-spacing: 0.09em; text-transform: uppercase; display: flex;"
    " align-items: center; justify-content: space-between; gap: var(--space-xs); }",
    ".cell .val { font: 400 var(--text-sm)/1.3 var(--font-data); align-self: end; color: var(--color-ink-2); overflow-wrap: anywhere; }",
    ".cell .val b { font-weight: 500; font-size: var(--text-md); color: var(--color-ink); }",
    # A phrase moves to the next line whole and wraps inside only when longer than the lamp: a nowrap one ran past the edge.
    ".cell .ph { display: inline-block; max-width: 100%; }",
    '.cell[data-state="unknown"] { color: var(--color-ink-2); background: repeating-linear-gradient(135deg, var(--color-panel) 0 6px, var(--color-well) 6px 7px); }',
    ".ledger { width: 100%; border-collapse: collapse; font-size: var(--text-sm); }",
    ".ledger th { text-align: left; font: 600 var(--text-2xs)/1 var(--font-label); letter-spacing: 0.09em; text-transform: uppercase;"
    " color: var(--color-muted); padding: var(--space-xs); border-bottom: var(--rule-hair) solid var(--color-rule); white-space: nowrap; }",
    ".ledger td { padding: var(--space-2xs) var(--space-xs); height: var(--row); border-bottom: var(--rule-hair) solid var(--color-rule-2); vertical-align: middle; }",
    ".ledger .g { text-align: center; font-family: var(--font-data); }",
)
# The lamp and ledger selectors only shell.css may style. A page keeps what is its own: `.annunciator { --cells: N; }`, a
# cell's extra class (`.cell.clock`), what sits inside a cell (`.cell kbd`) and its narrow-screen ledger cards.
SHELL_ONLY = re.compile(r'(?m)^\s*(\.cell(\s*[{,]|:hover|:focus-visible|:disabled|\s+\.(lbl|ph|val)\b|\[data-state|\[aria-pressed)'
                        r'|(a|button)\.cell\b|\.ledger(\s+(th|td)\s*[{,]|\s*[{,]|\s+col\b|\s+tbody tr(:hover td|\[aria-selected="true"\] td)?\s*[{,]))')
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
                for decl in re.findall(r"(?m)^\s*\.annunciator\s*\{([^}]*)\}", css):
                    self.assertRegex(decl.strip(), r"^--cells: \d;$")

    def test_one_ledger_layout_auto_with_no_colgroup(self):
        self.assertNotIn("table-layout", SHELL_CSS)
        for name, text in {**PAGE_CSS, **PAGE_JS, **{p.name: p.read_text(encoding="utf-8") for p in V2.glob("*.html")}}.items():
            with self.subTest(name):
                self.assertFalse("table-layout" in text or "<colgroup" in text)


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
    """sd:2530: a lit lamp keeps its state colour under the pointer."""

    def test_no_state_hover_shifts_brightness(self):
        for name, css in {**PAGE_CSS, "shell.css": SHELL_CSS}.items():
            with self.subTest(name):
                self.assertNotRegex(css, r'data-state="(caution|warning)"\][^{]*:hover[^{]*\{[^}]*filter')

    def test_the_hover_ring_is_drawn_in_the_lamps_ink_token(self):
        for state, ink in (("caution", "--color-caution-lamp-ink"), ("warning", "--color-warning-ink")):
            self.assertIn(f':is(a, button).cell[data-state="{state}"]:not([aria-pressed="true"]):hover {{ box-shadow: inset 0 0 0 1px var({ink}); }}',
                          SHELL_CSS)


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


if __name__ == "__main__":
    unittest.main()

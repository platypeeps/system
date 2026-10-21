"""One declaration per command (sd:3524), and each says what it does (sd:4963).

`static/commands.js` holds the commands more than one page registers: Fix, Retry, Show log, Print, Show assignment, Open
on GitHub for a pull request, and Re-run collector. A page registers them through `window.commands`; it writes none itself. Before this,
Fix was declared three times under three ids, Retry four times, Show assignment three times, and a text change landed on
one page and not the others (review 2026-10-12, A2 F6).

sd:4963: every command a page registers carries `does`, one plain sentence of what pressing it does. The Details' Act bar
shows it under the button and the row's ⋯ menu shows it under the label, in view, not in a hover tip (foundation: no
hover-only affordances).
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

    def test_jobs_retry_reads_run_now_for_an_idle_job_and_a_failed_one(self):
        # The run-now pack (sd:4982) allows a run of an idle job too, so the label cannot say Retry.
        line = SCRIPTS[SHARED].split("id: 'jobs.retry'", 1)[1].split("\n", 1)[0]
        self.assertIn("label: 'Run now'", line)
        self.assertIn("does: 'Starts the job now; after a failure, this reruns it.'", line)

    def test_only_the_shared_module_writes_a_shared_command(self):
        for text in SHARED_TEXT:
            with self.subTest(text=text):
                self.assertEqual([name for name, body in SCRIPTS.items() if text in body], [SHARED])

    def test_a_page_that_registers_a_shared_command_loads_the_module_before_its_script(self):
        users = sorted(name for name, body in SCRIPTS.items() if re.search(r"window\.commands\b", body) and name != SHARED)
        self.assertEqual(users, ["activity.js", "brief-details.js", "briefs.js", "contributions.js", "documents.js", "health.js", "hoa.js", "management.js", "queue.js", "reports.js", "research.js",
                                 "tasks.js", "today-github.js", "today.js"])
        # brief-details.js is a module, not a page (sd:5620): every page that loads it loads the shared module first.
        # today-github.js is Today's GitHub view (sd:5633), checked on today.html.
        pages = {name: re.findall(r'<script src="/ui/([^"]+)"', (V2 / name.replace(".js", ".html")).read_text(encoding="utf-8"))
                 for name in users if name not in ("brief-details.js", "today-github.js")}
        pages["today-github.js"] = pages["today.js"]
        pages.update({f"{page.stem}.html": re.findall(r'<script src="/ui/([^"]+)"', page.read_text(encoding="utf-8"))
                      for page in V2.glob("*.html") if 'src="/ui/brief-details.js"' in page.read_text(encoding="utf-8")})
        self.assertEqual(sorted(p for p in pages if p.endswith(".html")), ["briefs.html", "hoa.html", "home.html", "place.html"])
        for name, scripts in pages.items():
            with self.subTest(page=name):
                self.assertIn(SHARED, scripts)
                self.assertLess(scripts.index(SHARED), scripts.index(name if name.endswith(".js") else "brief-details.js"))


def scan(text, i, stop):
    """The end of the code that starts at text[i]: the first character at depth 0 that `stop` accepts, or the brace that
    closes an object literal opened at text[i]. Strings, comments and template literals, with their ${} nesting, are
    skipped, so a brace, a comma or an apostrophe inside one ends nothing."""
    stack, j = [], i  # '{', '(' or '[' for code, '`' for an open template
    while j < len(text):
        ch = text[j]
        if stack and stack[-1] == "`":
            if ch == "\\":
                j += 2
                continue
            if ch == "`":
                stack.pop()
            elif text.startswith("${", j):
                stack.append("{")
                j += 2
                continue
        elif ch in "'\"":
            j += 1
            while text[j] != ch:
                j += 2 if text[j] == "\\" else 1
        elif text.startswith("//", j):
            j = text.index("\n", j)
            continue
        elif text.startswith("/*", j):
            j = text.index("*/", j) + 2
            continue
        elif not stack and stop(ch):
            return j
        elif ch in "`{([":
            stack.append(ch)
        elif ch in "})]":
            stack.pop()
            if not stack and ch == "}" and text[i] == "{":
                return j + 1
        j += 1
    raise ValueError(f"no end to the code at {i}")


def literal(text, i):
    """The object literal that opens at text[i]."""
    return text[i:scan(text, i, lambda ch: False)]


# A command declaration: an object literal that opens with its id, names the type it is on, and labels its button.
OPENS = re.compile(r"\{\s*id:")
ON = re.compile(r"\bon\s*[:,]")
LABEL = re.compile(r"\blabel:\s*['`]")


def declarations(text):
    """Each command declaration in a script, as (id, literal)."""
    out = []
    for m in OPENS.finditer(text):
        body = literal(text, m.start())
        if ON.search(body) and LABEL.search(body):
            at = re.match(r"\{\s*id:\s*", body).end()
            out.append((body[at:scan(body, at, lambda ch: ch == ",")].strip("'`"), body))
    return out


# The pages whose commands do not say yet what they do: other branches own these scripts now (sd:4963). Each leaves this
# list when its texts land; once it is empty, every page's commands are held to the rule.
NOT_YET = {"hoa.js", "tasks.js"}
# `does: '…'`, or `does: o => …` for a text that differs per row: each text a person reads is about 12 words at most.
DOES = re.compile(r"\bdoes:\s*")
TEXT = re.compile(r"'((?:[^'\\]|\\.)*)'|`([^`]*)`")
WORDS = 14
SHELL = SCRIPTS["shell.js"]


def block(name):
    return re.search(rf"  function {name}\(.*?\n  \}}\n", SHELL, re.S).group(0)


class EveryCommandSaysWhatItDoes(unittest.TestCase):
    def test_every_command_on_a_converted_page_has_does(self):
        missing = [f"{name}: {found}" for name, text in SCRIPTS.items() if name not in NOT_YET
                   for found, body in declarations(text) if "does:" not in body]
        self.assertEqual(missing, [], "give each command a does: one sentence of what pressing it does")

    def test_the_not_yet_list_names_only_pages_that_still_lack_one(self):
        for name in sorted(NOT_YET):
            with self.subTest(page=name):
                self.assertTrue(any("does:" not in body for _, body in declarations(SCRIPTS[name])),
                                f"{name} says what every command does: take it off NOT_YET")

    def test_the_scan_finds_the_shared_and_template_declarations(self):
        found = {i for text in SCRIPTS.values() for i, _ in declarations(text)}
        for known in ("${on}.fix", "jobs.retry", "collector.sync", "${t}.snooze", "item.status.${s}", "piece.publish"):
            with self.subTest(id=known):
                self.assertIn(known, found)

    def test_a_does_text_is_short(self):
        for name, text in SCRIPTS.items():
            for found, body in declarations(text):
                m = DOES.search(body)
                if not m:
                    continue
                value = body[m.end():scan(body, m.end(), lambda ch: ch in ",}")]
                texts = [a or b for a, b in TEXT.findall(value)]
                with self.subTest(command=found):
                    self.assertTrue(texts, "write does as a string, or o => a template")
                    for said in texts:
                        self.assertLessEqual(len(re.sub(r"\$\{[^}]*\}", "X", said).split()), WORDS, said)

    def test_the_act_bar_puts_does_in_the_buttons_tip_and_draws_no_line_under_it(self):
        # sd:5287: the explanation is the hover and focus tip (tipOf leads with does), not a line under each button.
        bar = block("bar")
        self.assertNotIn("class=\"does\"", bar)
        self.assertNotIn("act-row", bar)
        self.assertIn('data-tip="${tipOf(c, o)}"', bar)
        self.assertIn("return html`<div class=\"actions\">${on.map(button)}</div>", bar)
        self.assertIn("const doesOf = (c, o) => (typeof c.does === 'function' ? c.does(o) : c.does) || '';", SHELL)
        self.assertIn("const s = shownCli(c, o), d = doesOf(c, o)", SHELL)
        self.assertIn("document.addEventListener('focusin', e => { const b = e.target.closest?.('.help, [data-tip]');", SHELL)

    def test_a_command_a_field_covers_stays_off_the_act_bar_but_not_off_the_menu(self):
        # sd:5295: `bar: false` marks a command an on-page field or dropdown does; the bar skips it, the ⋯ menu keeps it.
        self.assertIn("const list = cmdsFor(o).filter(c => c.bar !== false);", block("bar"))
        self.assertNotIn("c.bar", block("openMenu"))

    def test_the_row_menu_shows_does_as_the_items_second_line(self):
        menu = block("openMenu")
        self.assertRegex(menu, r'<span>\$\{c\.label\}[^<]*</span><small>\$\{doesOf\(c, o\)\}</small>')


# sd:5457: a row's act shows as its icon. These primary commands keep a text button: no sprite icon says the act alone.
TEXT_ACT = {"command.reconcile", "worktree registrations.prune", "merged branches.delete", "safe cleanups.clean", "repo.managed",
            "wt.prune", "bill.cap"}
SPRITE = set(re.findall(r'<symbol id="i-([a-z0-9-]+)"', SCRIPTS["icons.js"]))
ICON = re.compile(r"\bicon:\s*'([a-z0-9-]+)'")
HAS_ICON = re.compile(r"\bicon:")
PRIMARY = re.compile(r"\bprimary(?:\s*:|,|\s*=)")


class EveryRowActIsAnIcon(unittest.TestCase):
    def test_every_primary_command_declares_an_icon_or_is_named_text_only(self):
        missing = sorted(found for text in SCRIPTS.values() for found, body in declarations(text)
                         if PRIMARY.search(body) and not HAS_ICON.search(body) and found not in TEXT_ACT)
        self.assertEqual(missing, [], "give the command an icon: its row act shows as it (shell.js, rowActions)")

    def test_the_text_only_list_names_only_primary_commands_without_an_icon(self):
        found = {i: body for text in SCRIPTS.values() for i, body in declarations(text)}
        for name in sorted(TEXT_ACT):
            with self.subTest(command=name):
                self.assertIn(name, found)
                self.assertTrue(PRIMARY.search(found[name]) and not HAS_ICON.search(found[name]))

    def test_every_declared_icon_is_in_the_sprite(self):
        # A name the sprite lacks draws nothing: the row's act would be an empty button.
        unknown = sorted(f"{name}: {icon}" for name, text in SCRIPTS.items() for icon in ICON.findall(text) if icon not in SPRITE)
        self.assertEqual(unknown, [])


class OneItemAddress(unittest.TestCase):
    def test_only_the_shell_builds_an_items_address(self):
        """sd:3526 (review 2026-10-12, A2 F12): Reports linked /item/<n>, Management /tasks?row=<n>, and five pages wrote
        the address themselves. Every page links an item through shell.itemHref and opens one through shell.openItem."""
        built = re.compile(r"/item/\$\{|tasks\?row=|\.Tasks\b")
        self.assertEqual(sorted(name for name, body in SCRIPTS.items() if built.search(body)), ["shell.js"])


if __name__ == "__main__":
    unittest.main()

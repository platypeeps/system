"""The design documents: the renderer, and the pin the served policy rests on.

Two things here can go wrong quietly. The markdown subset is hand-written --
nothing on this machine ships python-markdown and the CI job installs no
extras -- so a construct the documents use but the converter does not support
would render as literal asterisks on a published page. And the diagram pages'
script digests are *pinned* in `documents.py` rather than derived per file, so
an archify upgrade silently leaves them inert unless the pin moves.

Nothing here needs archify, a pack checkout, or a network call: the tests that
would depend on one use a fixture instead of skipping, because a skipped test
fails CI in this repository.
"""

from __future__ import annotations

import base64
import hashlib
import re
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import design_docs
from sd_dashboard import documents


class Inline(unittest.TestCase):
    """Spans, and the order they have to be applied in."""

    def test_code_span_is_not_markup(self):
        # The reason code is stashed first: these asterisks are content.
        self.assertEqual(design_docs.inline("`**x**`"), "<code>**x**</code>")

    def test_double_backtick_span_may_hold_backticks(self):
        # The citation-anchor form the repository guide spells out.
        rendered = design_docs.inline("`` `a`, in `b` of `c` ``")
        self.assertEqual(rendered, "<code>`a`, in `b` of `c`</code>")
        self.assertEqual(rendered.count("<code>"), 1)

    def test_emphasis_and_strong(self):
        self.assertEqual(design_docs.inline("**a** and *b*"),
                         "<strong>a</strong> and <em>b</em>")

    def test_link_href_is_escaped_and_text_is_rendered(self):
        self.assertEqual(design_docs.inline("[a b](x.html)"),
                         '<a href="x.html">a b</a>')

    def test_angle_brackets_outside_code_are_escaped(self):
        self.assertIn("&lt;script&gt;", design_docs.inline("<script>"))

    def test_angle_brackets_inside_code_are_escaped_too(self):
        self.assertEqual(design_docs.inline("`<b>`"), "<code>&lt;b&gt;</code>")

    def test_underscores_are_not_emphasis(self):
        # Identifiers carry them; treating them as markup would eat names.
        self.assertEqual(design_docs.inline("JOB_SCHEDULE"), "JOB_SCHEDULE")


class Blocks(unittest.TestCase):
    def render(self, text):
        return design_docs.markdown_to_html(text)

    def test_heading_ids_are_unique(self):
        html, headings = self.render("## One\n\ntext\n\n## One\n")
        self.assertEqual([h["id"] for h in headings], ["one", "one-2"])
        self.assertIn('<h2 id="one-2">', html)

    def test_only_headings_are_reported_with_their_level(self):
        _, headings = self.render("# A\n\n## B\n\n### C\n")
        self.assertEqual([(h["level"], h["name"]) for h in headings],
                         [(1, "A"), (2, "B"), (3, "C")])

    def test_table_head_and_body(self):
        html, _ = self.render("| A | B |\n|---|---|\n| 1 | 2 |\n")
        self.assertIn("<th>A</th>", html)
        self.assertIn("<td>1</td>", html)
        self.assertEqual(html.count("<tr>"), 2)

    def test_a_pipe_line_without_a_separator_is_not_a_table(self):
        html, _ = self.render("| not | a table |\n")
        self.assertNotIn("<table>", html)

    def test_lists(self):
        html, _ = self.render("- a\n- b\n")
        self.assertEqual(html, "<ul><li>a</li><li>b</li></ul>")
        html, _ = self.render("1. a\n2. b\n")
        self.assertEqual(html, "<ol><li>a</li><li>b</li></ol>")

    def test_fenced_code_keeps_its_content_verbatim(self):
        html, _ = self.render("```sh\n# **not** bold\n```\n")
        self.assertIn('<code class="language-sh">', html)
        self.assertIn("# **not** bold", html)

    def test_a_heading_inside_a_fence_is_not_a_heading(self):
        html, headings = self.render("```\n## no\n```\n")
        self.assertEqual(headings, [])
        self.assertNotIn("<h2", html)

    def test_blockquote_and_rule(self):
        html, _ = self.render("> quoted\n\n---\n")
        self.assertIn("<blockquote><p>quoted</p></blockquote>", html)
        self.assertIn("<hr>", html)

    def test_diagram_placeholder_survives_conversion(self):
        # build_page substitutes the figure into this after conversion; a
        # paragraph wrapper around it would put the SVG inside a <p>.
        html, _ = self.render("<!--DIAGRAM:x-->\n")
        self.assertEqual(html, "<!--DIAGRAM:x-->")


class Directives(unittest.TestCase):
    def test_diagram_directive_is_replaced(self):
        notes = []
        body = design_docs.expand_diagrams("@diagram x\n", {"x": "<figure/>"}, notes,
                                           design_docs.repo_root(), [])
        self.assertIn("<!--DIAGRAM:x-->", body)
        self.assertEqual(notes, [])

    def test_a_missing_diagram_is_reported_not_raised(self):
        notes, failures = [], []
        body = design_docs.expand_diagrams("@diagram gone\n", {}, notes,
                                           design_docs.repo_root(), failures)
        self.assertNotIn("<!--DIAGRAM:gone-->", body)
        self.assertEqual(len(notes), 1)
        self.assertIn("gone", notes[0])

    def test_a_diagram_with_no_spec_behind_it_fails_the_build(self):
        # Reported but not fatal was the old shape, and it let a document cite
        # a diagram nobody had written while the build exited 0 (#471).
        notes, failures = [], []
        design_docs.expand_diagrams("@diagram gone\n", {}, notes,
                                    design_docs.repo_root(), failures)
        self.assertEqual(failures, ["gone"])

    def test_a_spec_that_exists_but_did_not_render_is_not_counted_twice(self):
        # It was already counted where it failed, and on a machine with no
        # archify nothing failed at all -- the prose still renders there.
        notes, failures = [], []
        real = next(iter(sorted(
            (design_docs.repo_root() / design_docs.SOURCES / "diagrams").glob("*.json"))))
        design_docs.expand_diagrams(f"@diagram {real.stem}\n", {}, notes,
                                    design_docs.repo_root(), failures)
        self.assertEqual(failures, [])

    def test_a_directive_inside_a_fence_is_left_alone(self):
        # Documentation of the directive must not invoke it.
        notes = []
        body = design_docs.expand_diagrams("```\n@diagram x\n```\n", {"x": "<figure/>"},
                                           notes, design_docs.repo_root(), [])
        self.assertIn("@diagram x", body)
        self.assertNotIn("<!--DIAGRAM:x-->", body)

    def test_jobs_directive_inside_a_fence_is_left_alone(self):
        notes = []
        body = design_docs.expand_jobs("```\n@jobs\n```\n", design_docs.repo_root(), notes)
        self.assertIn("@jobs", body)
        self.assertNotIn("| Job |", body)


class JobsTable(unittest.TestCase):
    """The table is generated from the filesystem, which is the whole point."""

    def setUp(self):
        self.root = design_docs.repo_root()
        self.jobs = sorted((self.root / "local-cron-jobs" / "examples").glob("*.job"))

    def test_every_job_file_gets_a_row(self):
        table = design_docs.jobs_table(self.root)
        for job in self.jobs:
            self.assertIn(f"`{job.stem}`", table,
                          f"{job.stem} has no row; the table is generated, so this "
                          f"means jobs_table stopped reading the directory")
        self.assertEqual(table.count("\n|") - 1, len(self.jobs))

    def test_the_stated_count_matches_the_directory(self):
        table = design_docs.jobs_table(self.root)
        self.assertIn(f"*{len(self.jobs)} example job files,", table)

    def test_each_row_names_its_verb(self):
        table = design_docs.jobs_table(self.root)
        for line in table.splitlines():
            if not line.startswith("| `") or "Schedule" in line:
                continue
            self.assertRegex(line, r"\| (agent|shell) \|",
                             f"a job that sets neither verb would be a load failure "
                             f"at run time, not a blank cell: {line}")

    def test_a_checkout_without_the_jobs_folder_skips(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(design_docs.Skip):
                design_docs.jobs_table(Path(tmp))


class ShellValues(unittest.TestCase):
    """The job table reads shell assignments, and nearly-right parsing showed.

    `strip('"')` published `0 6 * * 6"   # Saturdays` as a schedule, and
    reading a key without its value called every shell job that clears
    `JOB_PROMPT` an agent job (#471, found in review).
    """

    def test_a_comment_after_the_value_is_not_part_of_it(self):
        self.assertEqual(design_docs.shell_value('"0 6 * * 6"   # Saturdays'),
                         "0 6 * * 6")

    def test_an_unquoted_value_ends_at_its_comment(self):
        self.assertEqual(design_docs.shell_value("nightly  # every night"), "nightly")

    def test_an_escaped_quote_stays_inside_the_value(self):
        self.assertEqual(design_docs.shell_value('"\\"a b\\" c"'), '"a b" c')

    def test_an_empty_assignment_is_empty(self):
        self.assertEqual(design_docs.shell_value('""'), "")


class JobVerb(unittest.TestCase):
    def rows(self, text):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "local-cron-jobs" / "examples").mkdir(parents=True)
            (root / "local-cron-jobs" / "examples" / "one.job").write_text(text)
            return [line for line in design_docs.jobs_table(root).splitlines()
                    if line.startswith("| `one`")]

    def test_a_cleared_prompt_does_not_make_a_shell_job_an_agent_job(self):
        # agent-meter's shape: a real command, and JOB_PROMPT set to nothing.
        row = self.rows('JOB_SCHEDULE="0 * * * *"\nJOB_COMMAND="/bin/true"\nJOB_PROMPT=""\n')
        self.assertIn("| shell |", row[0])

    def test_a_prompt_job_reads_as_an_agent_job(self):
        row = self.rows('JOB_SCHEDULE="0 * * * *"\nJOB_PROMPT="/digest"\n')
        self.assertIn("| agent |", row[0])

    def test_the_schedule_carries_no_comment(self):
        row = self.rows('JOB_SCHEDULE="0 6 * * 6"   # Saturdays\nJOB_COMMAND="/bin/true"\n')
        self.assertIn("| `0 6 * * 6` |", row[0])


class SharedInputs(unittest.TestCase):
    """A page is out of date when the code that rendered it changes, too.

    `--check` compared a page against its markdown alone, so a tree built by
    last month's renderer read as current (#471, found in review).
    """

    def test_the_renderer_is_an_input_to_every_page(self):
        self.assertIn(Path(design_docs.__file__).resolve(), design_docs.shared_inputs())

    def test_only_files_that_are_here_are_named(self):
        for path in design_docs.shared_inputs(diagram=True):
            self.assertTrue(path.is_file(), f"{path} is named but absent")

    def test_a_page_depends_on_the_renderer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / design_docs.SOURCES).mkdir(parents=True)
            page = root / design_docs.SOURCES / "one.md"
            page.write_text("---\ntitle: One\n---\n\nprose\n")
            self.assertIn(Path(design_docs.__file__).resolve(),
                          design_docs.dependencies(page, root))

    def test_an_input_outside_the_checkout_is_named_absolutely(self):
        # There is no repo-relative name for the pack, and the record is a
        # build artefact beside the pages, never shared between machines.
        outside = Path("/somewhere/else/tokens.py")
        self.assertEqual(design_docs.label(outside, Path("/repo")), str(outside))
        self.assertEqual(design_docs.label(Path("/repo/a/b.md"), Path("/repo")), "a/b.md")


class Frontmatter(unittest.TestCase):
    def test_keys_and_body_are_separated(self):
        meta, body = design_docs.frontmatter("---\ntitle: A\n---\nbody\n")
        self.assertEqual(meta["title"], "A")
        self.assertEqual(body.strip(), "body")

    def test_a_document_without_frontmatter_keeps_all_of_it(self):
        meta, body = design_docs.frontmatter("body\n")
        self.assertEqual(meta, {})
        self.assertEqual(body.strip(), "body")


class DiagramType(unittest.TestCase):
    def write(self, text):
        path = Path(self.tmp.name) / "d.json"
        path.write_text(text)
        return path

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_a_known_type_is_returned(self):
        self.assertEqual(design_docs.diagram_type(
            self.write('{"diagram_type": "workflow"}')), "workflow")

    def test_an_unknown_type_is_broken_and_names_the_allowed_list(self):
        # `Broken`, because a spec declaring no schema is wrong everywhere --
        # and `Skip` here was the one malformation the build did not fail on.
        with self.assertRaises(design_docs.Broken) as caught:
            design_docs.diagram_type(self.write('{"diagram_type": "chart"}'))
        for kind in design_docs.TYPES:
            self.assertIn(kind, str(caught.exception))

    def test_the_old_key_is_not_accepted(self):
        # archify names it diagram_type; "type" was this renderer's own bug.
        with self.assertRaises(design_docs.Broken):
            design_docs.diagram_type(self.write('{"type": "workflow"}'))


class ServedPolicy(unittest.TestCase):
    """What documents.py sends for a page in docs/dashboard/."""

    def test_the_policy_names_every_pinned_digest(self):
        self.assertTrue(documents.DIAGRAM_SCRIPTS)
        for digest in documents.DIAGRAM_SCRIPTS:
            self.assertIn(f"'{digest}'", documents.POLICY)

    def test_every_pin_is_a_sha256_digest(self):
        for digest in documents.DIAGRAM_SCRIPTS:
            self.assertRegex(digest, r"^sha256-[A-Za-z0-9+/]{43}=$")

    def test_script_is_allowed_by_digest_and_by_nothing_else(self):
        directive = re.search(r"script-src ([^;]+);", documents.POLICY)
        self.assertIsNotNone(directive, "the policy must name a script-src")
        sources = directive.group(1).split()
        self.assertEqual(sorted(sources),
                         sorted(f"'{d}'" for d in documents.DIAGRAM_SCRIPTS),
                         "a script source that is not one of the pinned digests "
                         "would let a served document authorize its own script")

    def test_nothing_else_is_loosened(self):
        self.assertIn("default-src 'none'", documents.POLICY)
        self.assertIn("frame-ancestors 'none'", documents.POLICY)
        self.assertIn("base-uri 'none'", documents.POLICY)
        self.assertIn("form-action 'none'", documents.POLICY)

    def test_images_allow_data_and_blob_only(self):
        # blob: is the diagram's PNG export building an object URL.
        directive = re.search(r"img-src ([^;]+);", documents.POLICY)
        self.assertEqual(sorted(directive.group(1).split()), ["blob:", "data:"])


class Hashes(unittest.TestCase):
    """`--hashes` is what says where to move the pin; it must actually measure."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.out = self.root / design_docs.PUBLISHED
        self.out.mkdir(parents=True)

    def digest(self, body):
        return "sha256-" + base64.b64encode(hashlib.sha256(body.encode()).digest()).decode()

    def page(self, name, *bodies, extra=""):
        (self.out / name).write_text(
            f'<html><script type="application/json">{{"a":1}}</script>'
            + "".join(f"<script>{body}</script>" for body in bodies) + f"{extra}</html>")

    def pinned(self, *bodies):
        """Stand in for `documents.DIAGRAM_SCRIPTS`, which is what `--hashes` answers to."""
        return mock.patch.object(documents, "DIAGRAM_SCRIPTS",
                                 tuple(self.digest(b) for b in bodies))

    def test_the_pinned_scripts_on_every_page_exit_zero(self):
        self.page("diagram-a.html", "let x=1;", "let y=2;")
        self.page("diagram-b.html", "let x=1;", "let y=2;")
        with self.pinned("let x=1;", "let y=2;"), self.captured() as printed:
            code = design_docs.hashes(self.root)
        self.assertEqual(code, 0, printed.text)
        self.assertIn(self.digest("let x=1;"), printed.text)
        self.assertIn("2 distinct script(s) over 2 page(s); 2 shared by all",
                      printed.text)

    def test_the_real_pin_is_what_is_read(self):
        # No stand-in: pages carrying scripts nobody pinned cannot pass.
        self.page("diagram-a.html", "let x=1;", "let y=2;")
        self.page("diagram-b.html", "let x=1;", "let y=2;")
        with self.captured():
            self.assertEqual(design_docs.hashes(self.root), 1)

    def test_a_page_with_its_own_script_fails(self):
        self.page("diagram-a.html", "let x=1;")
        self.page("diagram-b.html", "let x=2;")
        with self.pinned("let x=1;", "let x=2;"), self.captured():
            self.assertEqual(design_docs.hashes(self.root), 1)

    def test_pages_with_no_inline_script_fail(self):
        # Zero digests are trivially "shared by every page"; that exited 0
        # and left the pin pointing at scripts no page carries any more.
        self.page("diagram-a.html")
        self.page("diagram-b.html")
        with self.pinned("let x=1;", "let y=2;"), self.captured() as printed:
            self.assertEqual(design_docs.hashes(self.root), 1)
        self.assertIn("0 distinct script(s)", printed.text)

    def test_one_shared_script_is_not_the_pinned_pair(self):
        # archify dropping one of its two scripts: consistent across the set,
        # and still a pin with a digest in it that nothing matches.
        self.page("diagram-a.html", "let x=1;")
        self.page("diagram-b.html", "let x=1;")
        with self.pinned("let x=1;", "let y=2;"), self.captured() as printed:
            self.assertEqual(design_docs.hashes(self.root), 1)
        self.assertIn(self.digest("let y=2;"), printed.text)

    def test_consistent_scripts_the_pin_does_not_name_fail(self):
        # An archify upgrade: identical on every page, and every page inert
        # under the served policy until the pin moves.
        self.page("diagram-a.html", "let x=3;", "let y=4;")
        self.page("diagram-b.html", "let x=3;", "let y=4;")
        with self.pinned("let x=1;", "let y=2;"), self.captured() as printed:
            self.assertEqual(design_docs.hashes(self.root), 1)
        self.assertIn(self.digest("let x=3;"), printed.text)
        self.assertIn("DIAGRAM_SCRIPTS", printed.text)

    def test_a_script_repeated_on_one_page_is_not_on_every_page(self):
        # Counting occurrences rather than pages read two copies on one page
        # as "2/2 pages" while the other page had none.
        self.page("diagram-a.html", "let x=1;", "let x=1;", "let y=2;")
        self.page("diagram-b.html", "let y=2;")
        with self.pinned("let x=1;", "let y=2;"), self.captured() as printed:
            self.assertEqual(design_docs.hashes(self.root), 1)
        self.assertIn("1/2 pages", printed.text)

    def test_a_json_block_is_never_counted(self):
        self.page("diagram-a.html", "let x=1;")
        with self.captured() as printed:
            design_docs.hashes(self.root)
        self.assertNotIn(self.digest('{"a":1}'), printed.text)

    def test_no_rendered_pages_is_reported_not_raised(self):
        with self.captured():
            self.assertEqual(design_docs.hashes(self.root), 3)

    # -- a tiny stdout capture, so these tests assert on what the verb prints,
    #    which is the whole product of `--hashes`: a line to paste into the pin.
    class _Captured:
        def __init__(self):
            self.text = ""

    def captured(self):
        import contextlib
        import io

        captured = self._Captured()
        outer = self

        class Capture:
            def __enter__(self):
                self.buffer = io.StringIO()
                self.error = io.StringIO()
                self.stack = contextlib.ExitStack()
                self.stack.enter_context(contextlib.redirect_stdout(self.buffer))
                self.stack.enter_context(contextlib.redirect_stderr(self.error))
                return captured

            def __exit__(self, *exception):
                self.stack.close()
                captured.text = self.buffer.getvalue() + self.error.getvalue()
                return False

        del outer
        return Capture()


class Sources(unittest.TestCase):
    """The documents this repository actually ships, against the converter."""

    @classmethod
    def setUpClass(cls):
        cls.root = design_docs.repo_root()
        cls.sources = sorted((cls.root / design_docs.SOURCES).glob("*.md"))

    def test_there_are_documents_to_check(self):
        self.assertTrue(self.sources, "docs/design/ has no documents; every test "
                                      "below would pass over an empty set")

    def test_every_document_has_a_title(self):
        for path in self.sources:
            meta, _ = design_docs.frontmatter(path.read_text())
            self.assertTrue(meta.get("title"), f"{path.name} has no title")

    def test_every_document_ends_with_a_status_section(self):
        for path in self.sources:
            _, body = design_docs.frontmatter(path.read_text())
            self.assertIn("## Status", body,
                          f"{path.name} states nothing about what was verified")

    def test_no_markdown_survives_conversion(self):
        # The failure this guards is a construct the subset does not support
        # reaching a published page as literal punctuation.
        for path in self.sources:
            _, body = design_docs.frontmatter(path.read_text())
            body = design_docs.expand_jobs(body, self.root, [])
            body = design_docs.expand_diagrams(body, {}, [], self.root, [])
            html, _ = design_docs.markdown_to_html(body)
            outside = re.sub(r"<pre><code.*?</code></pre>", "", html, flags=re.S)
            outside = re.sub(r"<code>.*?</code>", "", outside, flags=re.S)
            for pattern, what in ((r"\*\*", "bold markers"),
                                  (r"^\s*[-*] ", "a list marker"),
                                  (r"^\s*\|", "a table row"),
                                  (r"^#{1,6} ", "a heading marker")):
                self.assertIsNone(re.search(pattern, outside, re.M),
                                  f"{path.name} leaks {what} into the rendered page")

    def test_every_diagram_directive_names_a_diagram_that_exists(self):
        diagrams = self.root / design_docs.SOURCES / "diagrams"
        for path in self.sources:
            _, body = design_docs.frontmatter(path.read_text())
            for line in body.split("\n"):
                match = design_docs.DIAGRAM.match(line)
                if match:
                    self.assertTrue((diagrams / f"{match.group(1)}.json").is_file(),
                                    f"{path.name} names diagram {match.group(1)}, "
                                    f"which has no json")

    def test_every_internal_link_names_a_document_in_the_set(self):
        stems = {path.stem for path in self.sources}
        for path in self.sources:
            text = path.read_text()
            for target in re.findall(r"\]\(([A-Za-z0-9._-]+)\.html\)", text):
                self.assertIn(target, stems,
                              f"{path.name} links {target}.html, which no source produces")

    def test_no_line_citation_points_into_code(self):
        # The repository-wide gate bans these in tracked markdown; catching it
        # here names the document rather than the whole tree.
        for path in self.sources:
            for hit in re.findall(r"[\w./-]+\.(?:py|sh|js|yml):\d+", path.read_text()):
                self.fail(f"{path.name} cites {hit}; cite an anchor instead")


class Freshness(unittest.TestCase):
    """What `--check` has to notice, which is more than the markdown changing.

    It compared each page against its own `.md` and nothing else, so editing a
    diagram spec or a job file left the page reporting fresh with a stale
    diagram and a stale schedule inside it -- and a diagram page that was never
    written at all passed too, because nothing looked for it (#471, found in
    review). The dependency set is read from the document's directives rather
    than listed anywhere, so it follows an edit that adds or drops one.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.base = time.time()
        (self.root / design_docs.SOURCES / "diagrams").mkdir(parents=True)
        (self.root / "local-cron-jobs" / "examples").mkdir(parents=True)
        (self.root / design_docs.PUBLISHED).mkdir(parents=True)

    def write(self, rel, text=""):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def source(self, stem, body):
        return self.write(f"{design_docs.SOURCES}/{stem}.md",
                          f"---\ntitle: {stem}\n---\n\n{body}\n")

    def touch(self, path, when):
        """`when` orders the fixtures; the clock keeps them after the renderer.

        Every page now depends on `design_docs.py` as well as its markdown, and
        a fixture timestamped in 1970 is older than the checkout -- which is
        the right answer for a real page and the wrong one for a fixture whose
        point is that it is current.
        """
        import os
        os.utime(path, (self.base + when, self.base + when))

    def test_a_diagram_spec_is_a_dependency_of_the_page_that_names_it(self):
        page = self.source("one", "@diagram shape")
        spec = self.write(f"{design_docs.SOURCES}/diagrams/shape.json", "{}")
        self.assertIn(spec, design_docs.dependencies(page, self.root))

    def test_a_diagram_inside_a_fence_is_not_a_dependency(self):
        # A document explaining the syntax prints it, and printing it is not use.
        page = self.source("one", "```\n@diagram shape\n```")
        spec = self.write(f"{design_docs.SOURCES}/diagrams/shape.json", "{}")
        deps = design_docs.dependencies(page, self.root)
        self.assertIn(page, deps)
        self.assertNotIn(spec, deps)

    def test_the_jobs_directive_depends_on_the_files_it_enumerates(self):
        page = self.source("jobs", "@jobs")
        job = self.write("local-cron-jobs/examples/nightly.job", "JOB_SCHEDULE=1\n")
        deps = design_docs.dependencies(page, self.root)
        self.assertIn(job, deps)

    def test_a_page_whose_diagram_spec_moved_ahead_of_it_is_stale(self):
        page = self.source("one", "@diagram shape")
        spec = self.write(f"{design_docs.SOURCES}/diagrams/shape.json", "{}")
        out = self.write(f"{design_docs.PUBLISHED}/one.html", "<html>")
        self.write(f"{design_docs.PUBLISHED}/diagram-shape.html", "<html>")
        self.touch(page, 1000)
        self.touch(out, 2000)
        self.touch(spec, 3000)
        self.assertEqual(design_docs.render(self.root, check=True), 1)

    def test_a_missing_diagram_page_is_stale_even_when_every_document_is_current(self):
        # The failure the old check could not see: exit 0 with the interactive
        # page the document links to absent from the output entirely.
        page = self.source("one", "@diagram shape")
        spec = self.write(f"{design_docs.SOURCES}/diagrams/shape.json", "{}")
        out = self.write(f"{design_docs.PUBLISHED}/one.html", "<html>")
        self.touch(page, 1000)
        self.touch(spec, 1000)
        self.touch(out, 2000)
        targets = dict(design_docs.outputs(self.root, [page]))
        self.assertIn(self.root / design_docs.PUBLISHED / "diagram-shape.html", targets)
        self.assertEqual(design_docs.render(self.root, check=True), 1)

    def test_everything_current_is_not_stale(self):
        page = self.source("one", "@diagram shape")
        spec = self.write(f"{design_docs.SOURCES}/diagrams/shape.json", "{}")
        self.build([page])
        self.touch(page, 1000)
        self.touch(spec, 1000)
        self.assertEqual(design_docs.render(self.root, check=True), 0)

    def build(self, sources):
        """A rendered state: the outputs the manifest says were built."""
        pairs = design_docs.outputs(self.root, sources)
        for target, _ in pairs:
            self.write(f"{design_docs.PUBLISHED}/{target.name}", "<html>")
        names = {target.name for target, _ in pairs}
        design_docs.write_manifest(self.root, self.root / design_docs.PUBLISHED, sources,
                                   names, names)
        self.assertTrue(all(e["complete"] for e in design_docs.read_manifest(
            self.root / design_docs.PUBLISHED).values()))
        for target, _ in pairs:
            self.touch(target, 9000)

    def test_a_deleted_job_file_is_stale_although_every_survivor_is_older(self):
        # The case no comparison of surviving files can reach: the page still
        # lists a job nobody runs, and nothing on disk is newer than the page.
        page = self.source("jobs", "@jobs")
        job = self.write("local-cron-jobs/examples/nightly.job", "JOB_SCHEDULE=1\n")
        self.write("local-cron-jobs/examples/weekly.job", "JOB_SCHEDULE=2\n")
        self.build([page])
        job.unlink()
        self.assertEqual(design_docs.render(self.root, check=True), 1)

    def test_a_new_job_file_is_stale_too(self):
        page = self.source("jobs", "@jobs")
        self.write("local-cron-jobs/examples/nightly.job", "JOB_SCHEDULE=1\n")
        self.build([page])
        self.touch(self.write("local-cron-jobs/examples/weekly.job", "JOB_SCHEDULE=2\n"), 1000)
        self.assertEqual(design_docs.render(self.root, check=True), 1)

    def test_a_build_with_no_manifest_is_stale_rather_than_assumed_fresh(self):
        page = self.source("one", "prose\n")
        self.touch(page, 1000)
        self.touch(self.write(f"{design_docs.PUBLISHED}/one.html", "<html>"), 2000)
        self.assertEqual(design_docs.render(self.root, check=True), 1)

    def test_an_unreadable_manifest_is_read_as_nothing_not_as_a_crash(self):
        self.write(f"{design_docs.PUBLISHED}/{design_docs.MANIFEST}", "{ not json")
        self.assertEqual(design_docs.read_manifest(self.root / design_docs.PUBLISHED), {})

    def test_the_manifest_cannot_be_served(self):
        # The published folder is served by name, and this file is not a page.
        self.assertIsNone(documents.NAME.fullmatch(design_docs.MANIFEST))

    def test_a_withdrawn_document_leaves_a_stale_orphan(self):
        # It stayed published and served for good: the check walked the pages a
        # build would write, so the one nobody writes any more was never looked
        # at, and a rebuild left it alone.
        page = self.source("one", "prose\n")
        self.source("two", "prose\n")
        self.build([page, self.root / design_docs.SOURCES / "two.md"])
        (self.root / design_docs.SOURCES / "two.md").unlink()
        self.assertEqual(design_docs.render(self.root, check=True), 1)

    def test_a_rebuild_withdraws_the_orphan(self):
        page = self.source("one", "prose\n")
        self.build([page, self.source("two", "prose\n")])
        (self.root / design_docs.SOURCES / "two.md").unlink()
        design_docs.render(self.root)
        self.assertFalse((self.root / design_docs.PUBLISHED / "two.html").exists())
        self.assertTrue((self.root / design_docs.PUBLISHED / "one.html").exists())
        self.assertEqual(design_docs.render(self.root, check=True), 0)

    def test_only_a_page_the_manifest_named_is_ever_removed(self):
        # The folder is somebody's published tree. A build withdraws what it
        # recorded writing, and nothing else in there is its business.
        page = self.source("one", "prose\n")
        self.build([page])
        stranger = self.write(f"{design_docs.PUBLISHED}/by-hand.html", "<html>")
        design_docs.render(self.root)
        self.assertTrue(stranger.exists())

    def test_withdrawing_every_document_withdraws_every_page(self):
        # The one way to leave a page published for good: the run that should
        # have swept it exited first, on "nothing to render".
        page = self.source("one", "prose\n")
        self.build([page])
        page.unlink()
        self.assertEqual(design_docs.render(self.root, check=True), 1)
        design_docs.render(self.root)
        self.assertFalse((self.root / design_docs.PUBLISHED / "one.html").exists())
        self.assertEqual(design_docs.render(self.root, check=True), 3)

    def test_removing_the_source_folder_withdraws_them_too(self):
        page = self.source("one", "prose\n")
        self.build([page])
        import shutil
        shutil.rmtree(self.root / design_docs.SOURCES)
        design_docs.render(self.root)
        self.assertFalse((self.root / design_docs.PUBLISHED / "one.html").exists())

    def test_nothing_to_render_and_nothing_published_is_still_three(self):
        import shutil
        shutil.rmtree(self.root / design_docs.PUBLISHED)
        self.assertEqual(design_docs.render(self.root), 3)
        self.assertEqual(design_docs.render(self.root, check=True), 3)

    def test_a_document_naming_a_diagram_that_is_gone_is_stale(self):
        page = self.source("one", "@diagram shape")
        spec = self.write(f"{design_docs.SOURCES}/diagrams/shape.json", "{}")
        self.build([page])
        self.touch(page, 1000)
        spec.unlink()
        self.assertEqual(design_docs.render(self.root, check=True), 1)


class FailedBuildCannotCertifyItself(unittest.TestCase):
    """A build that exits 1 must not leave a `--check` that exits 0.

    It did: the manifest recorded what the build set out to write, so a
    document published without its diagram had a current record, a file that
    existed, and a timestamp beating its inputs. Exit 1 then exit 0, and the
    gate a job runs is the second one (#471, found in review).
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        (self.root / design_docs.SOURCES / "diagrams").mkdir(parents=True)
        (self.root / design_docs.SOURCES / "one.md").write_text(
            "---\ntitle: One\n---\n\n@diagram shape\n")
        self.spec = self.root / design_docs.SOURCES / "diagrams" / "shape.json"
        self.spec.write_text('{"diagram_type": "workflow"}')

    def archify(self, writes):
        """Stand in for the renderer: `writes` decides whether it leaves a page.

        `has_archify` is stubbed with it, because the two answer one question
        between them: a class that supplies a renderer is a machine that has
        one, and leaving the real probe in made every assertion here depend on
        whether the machine running the suite had archify installed.
        """
        def run(*args):
            if writes:
                Path(args[3]).write_text("<html>no svg here</html>")
        original = design_docs.archify
        design_docs.archify = run
        self.addCleanup(setattr, design_docs, "archify", original)
        probe = design_docs.has_archify
        design_docs.has_archify = lambda: True
        self.addCleanup(setattr, design_docs, "has_archify", probe)

    def test_a_build_that_failed_leaves_a_check_that_fails(self):
        # `lift` rejects the page archify wrote, which is the shape that let
        # both a written file and a fresh timestamp exist for a failed output.
        self.archify(writes=True)
        self.assertEqual(design_docs.render(self.root), 1)
        self.assertEqual(design_docs.render(self.root, check=True), 1)

    def test_the_page_is_still_written_and_says_what_is_missing(self):
        self.archify(writes=True)
        design_docs.render(self.root)
        page = self.root / design_docs.PUBLISHED / "one.html"
        self.assertTrue(page.exists())
        self.assertIn("shape", page.read_text())

    def test_the_rejected_diagram_page_is_not_left_behind(self):
        self.archify(writes=True)
        design_docs.render(self.root)
        self.assertFalse(
            (self.root / design_docs.PUBLISHED / "diagram-shape.html").exists())

    def test_the_partial_page_is_owned_but_not_complete(self):
        # Two facts, kept apart. Owned, so withdrawing the source withdraws the
        # page; not complete, so `--check` refuses to certify it. Recording
        # only completeness lost the first and left a page published for good.
        self.archify(writes=True)
        design_docs.render(self.root)
        recorded = design_docs.read_manifest(self.root / design_docs.PUBLISHED)
        self.assertIn("one.html", recorded)
        self.assertFalse(recorded["one.html"]["complete"])
        self.assertNotIn("diagram-shape.html", recorded)

    def test_withdrawing_the_source_of_a_partial_page_withdraws_the_page(self):
        self.archify(writes=True)
        design_docs.render(self.root)
        (self.root / design_docs.SOURCES / "one.md").unlink()
        (self.root / design_docs.SOURCES / "two.md").write_text(
            "---\ntitle: Two\n---\n\nprose\n")
        design_docs.render(self.root)
        self.assertFalse((self.root / design_docs.PUBLISHED / "one.html").exists())

    def test_an_older_manifest_without_the_flag_still_reads_as_owned(self):
        out = self.root / design_docs.PUBLISHED
        out.mkdir(parents=True, exist_ok=True)
        (out / design_docs.MANIFEST).write_text('{"one.html": ["docs/design/one.md"]}')
        recorded = design_docs.read_manifest(out)
        self.assertEqual(recorded["one.html"]["inputs"], ["docs/design/one.md"])
        self.assertTrue(recorded["one.html"]["complete"])


class BuildStatus(unittest.TestCase):
    """A diagram that cannot be built has to reach the exit status.

    It printed FAILED and exited 0, so a malformed spec published its document
    without its diagram and every gate above it stayed green (#471, found in
    review). `Skip` keeps its old meaning and its old exit code, because a
    machine without archify rendering the prose is a complete answer.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        (self.root / design_docs.SOURCES / "diagrams").mkdir(parents=True)
        (self.root / design_docs.SOURCES / "one.md").write_text(
            "---\ntitle: One\n---\n\nprose\n")

    def diagrams(self, raiser):
        notes, failures = [], []
        original = design_docs.archify
        design_docs.archify = raiser
        self.addCleanup(setattr, design_docs, "archify", original)
        embeds = design_docs.build_diagrams(
            self.root, self.root / design_docs.PUBLISHED, notes, failures)
        return embeds, notes, failures

    def spec(self, text='{"diagram_type": "workflow"}'):
        (self.root / design_docs.SOURCES / "diagrams" / "shape.json").write_text(text)

    def test_a_rejected_spec_is_a_failure(self):
        self.spec()
        _, notes, failures = self.diagrams(
            lambda *a: (_ for _ in ()).throw(design_docs.Broken("archify render failed")))
        self.assertEqual(failures, ["shape"])
        self.assertIn("FAILED", notes[0])

    def test_unparseable_json_is_a_failure(self):
        self.spec("{ not json")
        _, _, failures = self.diagrams(lambda *a: None)
        self.assertEqual(failures, ["shape"])

    def test_a_spec_declaring_no_schema_is_a_failure(self):
        # Valid JSON, wrong contents. It raised `Skip`, so the one
        # malformation the type check exists to catch was the one that
        # published a document without its diagram and exited 0.
        self.spec('{"diagram_type": "typo"}')
        _, _, failures = self.diagrams(lambda *a: None)
        self.assertEqual(failures, ["shape"])

    def test_a_machine_without_archify_is_not_a_failure(self):
        self.spec()
        _, notes, failures = self.diagrams(
            lambda *a: (_ for _ in ()).throw(design_docs.Skip("archify not found")))
        self.assertEqual(failures, [])
        self.assertIn("SKIP", notes[0])

    def test_the_build_exits_nonzero_when_a_diagram_failed(self):
        self.spec("{ not json")
        code = design_docs.render(self.root)
        self.assertEqual(code, 1)

    def test_the_build_exits_zero_when_nothing_failed(self):
        self.assertEqual(design_docs.render(self.root), 0)


class Palette(unittest.TestCase):
    """Which of archify's palette blocks reach the one element being lifted.

    Substring tests read `[data-preset="editorial"][data-theme="light"]` as a
    light block, and archify writes its presets last, so every published
    diagram came out in the editorial palette (#471, found in audit).
    """

    CSS = (':root,[data-theme="dark"]{--bg:#020617}\n'
           '[data-theme="light"]{--bg:#f8fafc}\n'
           '[data-preset="editorial"][data-theme="light"]{--bg:#f2eee5}')

    def light(self, preset):
        return design_docs.palette_of(
            self.CSS, lambda sel: design_docs.governs(sel, preset, "light"))

    def test_a_preset_block_does_not_supply_another_preset_s_palette(self):
        self.assertEqual(self.light("signal-flow"), {"--bg": "#f8fafc"})

    def test_a_preset_block_does_supply_its_own(self):
        self.assertEqual(self.light("editorial"), {"--bg": "#f2eee5"})

    def test_the_dark_palette_is_the_dark_one(self):
        dark = design_docs.palette_of(
            self.CSS, lambda sel: design_docs.governs(sel, "signal-flow", "dark"))
        self.assertEqual(dark, {"--bg": "#020617"})

    def test_a_comma_list_matches_on_any_arm(self):
        self.assertTrue(design_docs.governs(':root,[data-theme="dark"]', "x", "light"))
        self.assertFalse(design_docs.governs('[data-theme="light"]', "x", "dark"))

    def test_a_selector_qualifying_nothing_relevant_does_not_govern(self):
        self.assertFalse(design_docs.governs(".c-mask", "x", "light"))


#: Archify-shaped pages that are tracked, so `lift` has something to work on in
#: CI. `docs/dashboard/` is gitignored build output: a test that reads only it
#: loops over nothing on a clean checkout and passes having checked nothing.
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "archify"


def lift_pages() -> list[Path]:
    """The tracked fixtures, plus any diagram pages this checkout has built.

    The fixtures come first and are required; the built pages are a bonus on a
    machine that has archify, and CI is not one.
    """
    fixtures = sorted(FIXTURES.glob("diagram-*.html"))
    published = design_docs.repo_root() / design_docs.PUBLISHED
    return fixtures + sorted(published.glob("diagram-*.html"))


class LiftFixtures(unittest.TestCase):
    """The fixture set itself: an empty set would make every loop below vacuous."""

    def test_the_tracked_fixtures_are_there(self):
        fixtures = sorted(FIXTURES.glob("diagram-*.html"))
        self.assertGreaterEqual(len(fixtures), 2, f"no archify fixtures in {FIXTURES}")
        self.assertTrue(set(fixtures) <= set(lift_pages()))


class Lift(unittest.TestCase):
    """The path every embedded diagram takes: a page archify wrote, made portable."""

    def figure(self, name):
        return design_docs.lift((FIXTURES / name).read_text())

    def light(self, figure):
        return figure.split("@media (prefers-color-scheme:dark)", 1)[0]

    def test_a_figure_wraps_the_svg_and_its_own_stylesheet(self):
        figure = self.figure("diagram-signal.html")
        self.assertTrue(figure.startswith('<figure class="diagram"><style>'))
        self.assertTrue(figure.endswith("</svg></figure>"))
        self.assertIn('<svg class="diagram-svg" preserveAspectRatio="xMidYMid meet"', figure)
        self.assertEqual(figure.count("<svg"), 1)

    def test_every_variable_is_resolved_to_a_literal(self):
        for name in ("diagram-signal.html", "diagram-editorial.html"):
            self.assertNotIn("var(", self.figure(name), name)

    def test_the_light_palette_is_inline_and_the_dark_one_is_guarded(self):
        figure = self.figure("diagram-signal.html")
        self.assertIn(".diagram .t-primary{fill: #0f172a;}", self.light(figure))
        self.assertIn(':root:not([data-theme="light"]) .diagram .t-primary{fill: #ffffff;}',
                      figure)
        self.assertIn(':root[data-theme="dark"] .diagram .t-primary{fill: #ffffff;}', figure)

    def test_a_chain_and_a_fallback_both_resolve(self):
        # `--label` is `var(--text-muted)`, which the light block overrides;
        # `--font-sans` is defined nowhere, so its fallback is what is left.
        light = self.light(self.figure("diagram-signal.html"))
        self.assertIn(".diagram .t-muted, .diagram .t-label{fill: #475569; font-family: system-ui;}",
                      light)

    def test_the_diagram_s_own_preset_decides_its_palette(self):
        signal = self.light(self.figure("diagram-signal.html"))
        editorial = self.light(self.figure("diagram-editorial.html"))
        self.assertIn(".diagram .a-default{stroke: #0e7490;", signal)
        self.assertNotIn("#2b2118", signal, "another preset's palette reached the diagram")
        self.assertIn(".diagram .t-primary{fill: #2b2118;}", editorial)
        self.assertNotIn("#0e7490", editorial)

    def test_the_print_palette_does_not_reach_the_screen(self):
        figure = self.figure("diagram-signal.html")
        self.assertNotIn("#000001", figure)
        self.assertNotIn("#fffffe", figure)

    def test_rules_for_classes_the_svg_does_not_use_are_left_behind(self):
        figure = self.figure("diagram-signal.html")
        self.assertNotIn("toolbar", figure.split("<svg", 1)[0])
        self.assertNotIn("box-sizing", figure)
        self.assertNotIn("background", figure)

    def test_a_class_with_no_rule_is_refused(self):
        page = (FIXTURES / "diagram-signal.html").read_text().replace(
            'class="c-mask"', 'class="c-mask c-unstyled"')
        with self.assertRaises(design_docs.Broken) as caught:
            design_docs.lift(page)
        self.assertIn("c-unstyled", str(caught.exception))


class Scoping(unittest.TestCase):
    """What `lift` emits has to stay inside `.diagram` and honour both themes."""

    def setUp(self):
        self.pages = lift_pages()
        # Every test below is a loop over these, and a loop over nothing passes.
        self.assertTrue(self.pages, "no diagram pages to lift")

    def test_every_dark_rule_carries_a_guard(self):
        # The prefix used to be prepended to the joined block, so it attached
        # to the first rule of thirty-five and the rest were unconditional.
        for page in self.pages:
            figure = design_docs.lift(page.read_text())
            dark = re.search(r"@media \(prefers-color-scheme:dark\)\{(.*?)\}\n", figure, re.S)
            self.assertIsNotNone(dark, f"{page.name} emits no dark block")
            rules = [r for r in dark.group(1).split("}") if r.strip()]
            for rule in rules:
                self.assertIn(':root:not([data-theme="light"])', rule,
                              f"{page.name}: an unguarded rule in the dark block")

    def test_the_explicit_dark_trigger_is_emitted_too(self):
        for page in self.pages:
            self.assertIn(':root[data-theme="dark"] .diagram',
                          design_docs.lift(page.read_text()),
                          f"{page.name} honours the OS preference only")

    def test_no_rule_escapes_the_diagram_scope(self):
        # `.diagram a, b{...}` scopes `a` and leaves `b` loose in the page.
        for page in self.pages:
            figure = design_docs.lift(page.read_text())
            style = re.search(r"<style>(.*?)</style>", figure, re.S).group(1)
            # `[^{}@]+` stops at an at-rule's prelude, so the media query's
            # own text is not a selector to check.
            for selector in re.findall(r"(?:^|\}|\{)\s*([^{}@]+?)\s*\{", style):
                for arm in selector.split(","):
                    arm = arm.strip()
                    if not arm:
                        continue
                    self.assertIn(".diagram", arm,
                                  f"{page.name}: `{arm}` is not scoped")


class Injection(unittest.TestCase):
    """A source document may not put raw HTML on a published page."""

    def test_a_line_that_merely_starts_and_ends_as_a_comment_is_escaped(self):
        html, _ = design_docs.markdown_to_html(
            "<!-- --><script>alert(1)</script><!-- -->")
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_only_the_renderer_s_own_token_survives(self):
        html, _ = design_docs.markdown_to_html("<!--DIAGRAM:x-->")
        self.assertEqual(html, "<!--DIAGRAM:x-->")

    def test_a_source_cannot_forge_the_token(self):
        # It would have a figure substituted into it, twice for a diagram the
        # page already carries.
        body = design_docs.expand_diagrams("<!--DIAGRAM:x-->\n", {"x": "<figure/>"},
                                           [], design_docs.repo_root(), [])
        self.assertNotIn("<!--DIAGRAM:x-->", body)


class Tables(unittest.TestCase):
    def test_an_escaped_pipe_is_content_and_not_a_separator(self):
        rendered = design_docs.table(["| Job | What |", "|---|---|", "| `x` | a \\| b |"])
        self.assertEqual(rendered.count("<td>"), 2)
        self.assertIn("a | b", rendered)

    def test_a_code_span_may_contain_a_pipe(self):
        rendered = design_docs.table(["| A | B |", "|---|---|", "| `a|b` | c |"])
        self.assertEqual(rendered.count("<td>"), 2)

    def test_a_blurb_is_not_cut_at_a_semicolon(self):
        self.assertEqual(
            design_docs.SENTENCE.split("Advance ideas; then stop. Next.", 1)[0],
            "Advance ideas; then stop.")

    def test_a_link_url_is_escaped_once(self):
        self.assertEqual(design_docs.inline("[q](a.html?x=1&y=2)"),
                         '<a href="a.html?x=1&amp;y=2">q</a>')


class Cycles(unittest.TestCase):
    def test_a_variable_defined_in_terms_of_itself_returns(self):
        # It used to loop for ever, and say nothing while it did.
        out = design_docs.resolve_vars("color:var(--a)",
                                       {"--a": "var(--b)", "--b": "var(--a)"})
        self.assertIn("var(", out)  # unresolved, and `lift` refuses it

    def test_a_real_chain_still_resolves(self):
        self.assertEqual(
            design_docs.resolve_vars("color:var(--a)",
                                     {"--a": "var(--b)", "--b": "#fff"}),
            "color:#fff")


class Ownership(unittest.TestCase):
    """Which published files a build may claim, and which it may not.

    Five of these are the audit's: a case-only rename deleting the page just
    written, a build claiming a file it never wrote, a page recorded complete
    without its job table, a document colliding with a diagram page, and a
    source nobody can read crashing both modes (#471).
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        (self.root / design_docs.SOURCES / "diagrams").mkdir(parents=True)
        (self.root / design_docs.PUBLISHED).mkdir(parents=True)

    def source(self, stem, body="prose\n"):
        path = self.root / design_docs.SOURCES / f"{stem}.md"
        path.write_text(f"---\ntitle: {stem}\n---\n\n{body}\n")
        return path

    def published(self, name):
        return self.root / design_docs.PUBLISHED / name

    def test_a_case_only_rename_does_not_delete_the_page_just_written(self):
        page = self.source("Alpha")
        design_docs.render(self.root)
        self.assertTrue(self.published("Alpha.html").exists())
        page.rename(self.root / design_docs.SOURCES / "alpha.md")
        design_docs.render(self.root)
        written = sorted(p.name for p in (self.root / design_docs.PUBLISHED).glob("*.html"))
        self.assertEqual(len(written), 1, f"published: {written}")
        self.assertEqual(design_docs.render(self.root, check=True), 0)

    def test_a_file_this_build_did_not_write_is_never_claimed(self):
        self.source("one")
        stranger = self.published("diagram-x.html")
        stranger.write_text("<html>somebody else's</html>")
        (self.root / design_docs.SOURCES / "diagrams" / "x.json").write_text("{}")
        design_docs.render(self.root)
        recorded = design_docs.read_manifest(self.root / design_docs.PUBLISHED)
        self.assertNotIn("diagram-x.html", recorded)

    def test_a_page_missing_its_job_table_is_not_recorded_complete(self):
        # There is no `local-cron-jobs` here, so `@jobs` cannot be filled.
        self.source("jobs", "@jobs")
        design_docs.render(self.root)
        recorded = design_docs.read_manifest(self.root / design_docs.PUBLISHED)
        self.assertFalse(recorded["jobs.html"]["complete"])
        self.assertEqual(design_docs.render(self.root, check=True), 1)

    def test_a_document_may_not_claim_a_diagram_page_s_name(self):
        self.source("one")
        self.source(f"{design_docs.RESERVED}foo")
        self.assertEqual(design_docs.render(self.root), 1)
        self.assertFalse(self.published(f"{design_docs.RESERVED}foo.html").exists())

    def test_a_source_that_cannot_be_read_is_a_finding_not_a_traceback(self):
        self.source("one")
        (self.root / design_docs.SOURCES / "broken.md").symlink_to(
            self.root / design_docs.SOURCES / "nowhere.md")
        self.assertEqual(design_docs.render(self.root), 1)
        self.assertTrue(self.published("one.html").exists())
        self.assertEqual(design_docs.render(self.root, check=True), 1)


class WithoutArchify(unittest.TestCase):
    """The build and the check have to agree on a machine that has no archify.

    The build exited 0, as the `Skip` contract says it should, and the check
    exited 1 over the same tree on every run (#471, found in audit).
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        (self.root / design_docs.SOURCES / "diagrams").mkdir(parents=True)
        (self.root / design_docs.SOURCES / "one.md").write_text(
            "---\ntitle: One\n---\n\n@diagram shape\n")
        (self.root / design_docs.SOURCES / "diagrams" / "shape.json").write_text(
            '{"diagram_type": "workflow"}')
        original = design_docs.ARCHIFY
        design_docs.ARCHIFY = self.root / "no-archify-here"
        self.addCleanup(setattr, design_docs, "ARCHIFY", original)

    def test_the_build_still_succeeds(self):
        self.assertEqual(design_docs.render(self.root), 0)

    def test_the_check_says_not_configured_rather_than_stale(self):
        design_docs.render(self.root)
        self.assertEqual(design_docs.render(self.root, check=True), 3)

    def test_has_archify_reports_the_absence(self):
        self.assertFalse(design_docs.has_archify())

    def test_the_note_names_the_real_cause(self):
        # It said the spec was missing while the spec was on disk.
        notes = []
        design_docs.expand_diagrams("@diagram shape\n", {}, notes, self.root, [])
        self.assertIn("did not render", notes[0])


class Siblings(unittest.TestCase):
    """Every page carries the other documents' titles, so they are its inputs.

    `--check` read only a page's own directives, so withdrawing or retitling
    one document left every other page published with navigation naming a page
    that is gone -- and certified (#473, found in review).
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        (self.root / design_docs.SOURCES / "diagrams").mkdir(parents=True)

    def source(self, stem, body="prose"):
        path = self.root / design_docs.SOURCES / f"{stem}.md"
        path.write_text(f"---\ntitle: {stem}\n---\n\n{body}\n")
        return path

    def test_a_document_depends_on_its_siblings(self):
        one, two = self.source("one"), self.source("two")
        deps = design_docs.dependencies(one, self.root)
        self.assertIn(two, deps)
        self.assertEqual(deps.count(one), 1)

    def test_a_document_alone_depends_on_no_sibling(self):
        one = self.source("one")
        self.assertEqual([d for d in design_docs.dependencies(one, self.root)
                          if d.suffix == ".md"], [one])

    def test_a_page_this_renderer_writes_is_not_a_sibling(self):
        # `diagram-*.md` is refused by name, and must not become an input either.
        one = self.source("one")
        (self.root / design_docs.SOURCES / "diagram-shape.md").write_text("x")
        self.assertNotIn(self.root / design_docs.SOURCES / "diagram-shape.md",
                         design_docs.dependencies(one, self.root))

    def test_the_recorded_inputs_of_a_page_name_its_siblings(self):
        # The record is what `--check` compares against, so a sibling that is
        # not in it is a sibling no later run can miss.
        self.source("one"), self.source("two")
        design_docs.render(self.root)
        recorded = design_docs.read_manifest(self.root / design_docs.PUBLISHED)
        self.assertIn(f"{design_docs.SOURCES}/two.md", recorded["one.html"]["inputs"])

    def test_withdrawing_a_sibling_makes_the_survivor_stale(self):
        # A control: the orphan sweep already answered 1 for this one, because
        # the withdrawn document leaves its own page behind. It is here so the
        # two paths to the same verdict stay separable.
        one, two = self.source("one"), self.source("two")
        self.assertEqual(design_docs.render(self.root), 0)
        two.unlink()
        self.assertEqual(design_docs.render(self.root, check=True), 1)


class CheckWithoutArchifyStillJudges(unittest.TestCase):
    """"Not configured here" may not swallow a finding archify cannot cause.

    The first answer to the build/check contradiction returned 3 for any tree
    holding a diagram spec. CI has no archify, so four real findings came back
    as "not configured" and the leg went green over them (#473, found in CI).
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        (self.root / design_docs.SOURCES / "diagrams").mkdir(parents=True)
        (self.root / design_docs.SOURCES / "one.md").write_text(
            "---\ntitle: One\n---\n\n@diagram shape\n")
        (self.root / design_docs.SOURCES / "diagrams" / "shape.json").write_text(
            '{"diagram_type": "workflow"}')
        original = design_docs.ARCHIFY
        design_docs.ARCHIFY = self.root / "no-archify-here"
        self.addCleanup(setattr, design_docs, "ARCHIFY", original)

    def test_an_unrecorded_page_is_a_verdict_and_not_an_excuse(self):
        # The page is on disk with no manifest entry: archify explains neither.
        out = self.root / design_docs.PUBLISHED
        out.mkdir(parents=True)
        (out / "one.html").write_text("<html>")
        self.assertEqual(design_docs.render(self.root, check=True), 1)

    def test_a_document_claiming_a_reserved_name_is_a_verdict(self):
        design_docs.render(self.root)
        (self.root / design_docs.SOURCES / "diagram-shape.md").write_text(
            "---\ntitle: X\n---\n\nprose\n")
        self.assertEqual(design_docs.render(self.root, check=True), 1)

    def test_only_the_diagram_findings_answer_not_configured(self):
        self.assertEqual(design_docs.render(self.root), 0)
        self.assertEqual(design_docs.render(self.root, check=True), 3)


class ABrokenSpecKeepsBothModesRed(unittest.TestCase):
    """The rebuttal to "a malformed spec deletes an existing target".

    It does, and that is the intent: `diagram-*.html` is this renderer's own
    name, refused to any document, and `docs/dashboard/` is untracked output
    rebuilt from source. What matters is that neither mode certifies the tree
    afterwards, which is what this pins.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        (self.root / design_docs.SOURCES / "diagrams").mkdir(parents=True)
        (self.root / design_docs.SOURCES / "one.md").write_text(
            "---\ntitle: One\n---\n\n@diagram shape\n")
        self.spec = self.root / design_docs.SOURCES / "diagrams" / "shape.json"
        self.spec.write_text('{"diagram_type": "workflow"}')
        def run(*args):
            Path(args[3]).write_text("<html><svg></svg></html>")
        original = design_docs.archify
        design_docs.archify = run
        self.addCleanup(setattr, design_docs, "archify", original)
        probe = design_docs.has_archify
        design_docs.has_archify = lambda: True
        self.addCleanup(setattr, design_docs, "has_archify", probe)

    def test_a_spec_that_stops_parsing_fails_the_build_and_the_check(self):
        design_docs.render(self.root)
        self.spec.write_text("{ not json")
        self.assertEqual(design_docs.render(self.root), 1)
        self.assertEqual(design_docs.render(self.root, check=True), 1)


if __name__ == "__main__":
    unittest.main()

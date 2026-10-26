"""This repository's design documents and job catalogue, against the pack's converter (sd:6355).

The dashboard in the command pack renders `docs/design/*.md` and the table of
`local-cron-jobs/examples/*.job`. Its tests ran this checkout's own files until
pack sd:6044 gave them fixtures, so the checks on what this repository ships
live here, run against the converter at `SD_PACK_ROOT` (the gate's pin).

Each check is a function over a root that returns its problems. The real tree
must return none; a planted fault in a temporary tree must return one, so a
check that stopped looking would fail its own case.
"""

import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACK = Path(os.environ.get("SD_PACK_ROOT") or Path.home() / "repos/platypeeps/sd-ai-command-pack")
sys.path.insert(0, str(PACK / "dashboard"))

import design_docs  # noqa: E402  the pack's converter; its path is set above


def sources(root):
    return sorted((root / design_docs.SOURCES).glob("*.md"))


def title_problems(root):
    return [f"{path.name} has no title" for path in sources(root)
            if not design_docs.frontmatter(path.read_text())[0].get("title")]


def status_problems(root):
    return [f"{path.name} states nothing about what was verified"
            for path in sources(root)
            if "## Status" not in design_docs.frontmatter(path.read_text())[1]]


# A construct the converter's subset does not support, reaching a page as literal punctuation.
LEAKS = ((r"\*\*", "bold markers"), (r"^\s*[-*] ", "a list marker"),
         (r"^\s*\|", "a table row"), (r"^#{1,6} ", "a heading marker"))


def leak_problems(root):
    found = []
    for path in sources(root):
        body = design_docs.frontmatter(path.read_text())[1]
        body = design_docs.expand_jobs(body, root, [])
        body = design_docs.expand_diagrams(body, {}, [], root, [])
        html, _ = design_docs.markdown_to_html(body)
        outside = re.sub(r"<pre><code.*?</code></pre>", "", html, flags=re.S)
        outside = re.sub(r"<code>.*?</code>", "", outside, flags=re.S)
        found += [f"{path.name} leaks {what} into the rendered page"
                  for pattern, what in LEAKS if re.search(pattern, outside, re.M)]
    return found


def diagram_problems(root):
    diagrams = root / design_docs.SOURCES / "diagrams"
    found = []
    for path in sources(root):
        for line in design_docs.frontmatter(path.read_text())[1].split("\n"):
            match = design_docs.DIAGRAM.match(line)
            if match and not (diagrams / f"{match.group(1)}.json").is_file():
                found.append(f"{path.name} names diagram {match.group(1)}, which has no json")
    return found


def link_problems(root):
    stems = {path.stem for path in sources(root)}
    return [f"{path.name} links {target}.html, which no source produces"
            for path in sources(root)
            for target in re.findall(r"\]\(([A-Za-z0-9._-]+)\.html\)", path.read_text())
            if target not in stems]


def citation_problems(root):
    return [f"{path.name} cites {hit}; cite an anchor instead"
            for path in sources(root)
            for hit in re.findall(r"[\w./-]+\.(?:py|sh|js|yml):\d+", path.read_text())]


def job_problems(root):
    """The generated table: a row per file, the stated count, and a verb on each row."""
    jobs = sorted((root / "local-cron-jobs" / "examples").glob("*.job"))
    table = design_docs.jobs_table(root)
    found = [f"{job.stem} has no row" for job in jobs if f"`{job.stem}`" not in table]
    if table.count("\n|") - 1 != len(jobs):
        found.append(f"the table has {table.count(chr(10) + '|') - 1} rows for {len(jobs)} files")
    if f"*{len(jobs)} example job files," not in table:
        found.append(f"the table does not state {len(jobs)} example job files")
    found += [f"a row names neither verb: {line}" for line in table.splitlines()
              if line.startswith("| `") and "Schedule" not in line
              and not re.search(r"\| (agent|shell) \|", line)]
    return found


class RealTree(unittest.TestCase):
    """What this repository ships."""

    def test_there_are_documents_and_jobs_to_check(self):
        # An empty set would pass every check below.
        self.assertTrue(sources(ROOT), "docs/design/ has no documents")
        self.assertTrue(list((ROOT / "local-cron-jobs" / "examples").glob("*.job")))

    def test_every_document_has_a_title(self):
        self.assertEqual(title_problems(ROOT), [])

    def test_every_document_ends_with_a_status_section(self):
        self.assertEqual(status_problems(ROOT), [])

    def test_no_markdown_survives_conversion(self):
        self.assertEqual(leak_problems(ROOT), [])

    def test_every_diagram_directive_names_a_diagram_that_exists(self):
        self.assertEqual(diagram_problems(ROOT), [])

    def test_every_internal_link_names_a_document_in_the_set(self):
        self.assertEqual(link_problems(ROOT), [])

    def test_no_line_citation_points_into_code(self):
        self.assertEqual(citation_problems(ROOT), [])

    def test_the_jobs_table_matches_the_examples(self):
        self.assertEqual(job_problems(ROOT), [])


class PlantedFaults(unittest.TestCase):
    """Each check names a fault planted in a temporary tree."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.design = self.root / design_docs.SOURCES
        (self.design / "diagrams").mkdir(parents=True)
        (self.root / "local-cron-jobs" / "examples").mkdir(parents=True)

    def doc(self, name, text):
        (self.design / name).write_text(text)

    def good(self):
        self.doc("good.md", "---\ntitle: Good\n---\n\n# Good\n\nProse.\n\n## Status\n\nChecked.\n")

    def test_a_clean_tree_has_no_problem(self):
        self.good()
        (self.root / "local-cron-jobs/examples/nightly.job").write_text(
            '# Runs nightly.\nJOB_SCHEDULE="0 2 * * *"\nJOB_COMMAND="true"\n')
        for check in (title_problems, status_problems, leak_problems, diagram_problems,
                      link_problems, citation_problems, job_problems):
            with self.subTest(check=check.__name__):
                self.assertEqual(check(self.root), [])

    def test_a_document_without_a_status_section_is_named(self):
        self.doc("bare.md", "---\ntitle: Bare\n---\n\nProse only.\n")
        self.assertEqual(status_problems(self.root), ["bare.md states nothing about what was verified"])

    def test_a_document_without_a_title_is_named(self):
        self.doc("untitled.md", "# Untitled\n\n## Status\n\nChecked.\n")
        self.assertEqual(title_problems(self.root), ["untitled.md has no title"])

    def test_a_diagram_directive_with_no_json_is_named(self):
        self.doc("d.md", "---\ntitle: D\n---\n\n@diagram nowhere\n\n## Status\n")
        self.assertEqual(diagram_problems(self.root), ["d.md names diagram nowhere, which has no json"])

    def test_a_link_to_a_missing_document_is_named(self):
        self.doc("l.md", "---\ntitle: L\n---\n\nSee [it](gone.html).\n\n## Status\n")
        self.assertEqual(link_problems(self.root), ["l.md links gone.html, which no source produces"])

    def test_a_line_citation_into_code_is_named(self):
        self.doc("c.md", "---\ntitle: C\n---\n\nSee tool.sh:42 for it.\n\n## Status\n")
        self.assertEqual(citation_problems(self.root), ["c.md cites tool.sh:42; cite an anchor instead"])

    def test_markdown_the_subset_cannot_render_is_named(self):
        # An unclosed bold marker reaches the page as literal asterisks.
        self.doc("m.md", "---\ntitle: M\n---\n\nA **never closed.\n\n## Status\n")
        self.assertEqual(leak_problems(self.root), ["m.md leaks bold markers into the rendered page"])

    def test_a_job_with_neither_verb_is_named(self):
        (self.root / "local-cron-jobs/examples/empty.job").write_text('JOB_SCHEDULE="0 2 * * *"\n')
        self.assertEqual(len(job_problems(self.root)), 1)
        self.assertIn("neither verb", job_problems(self.root)[0])

    def test_a_missing_examples_folder_is_a_skip_not_a_pass(self):
        with tempfile.TemporaryDirectory() as empty:
            with self.assertRaises(design_docs.Skip):
                design_docs.jobs_table(Path(empty))


if __name__ == "__main__":
    unittest.main()

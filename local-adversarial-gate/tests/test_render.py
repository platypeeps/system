"""`adversarial-gate render`: what a lens tells the reviewer to read (sd:3301).

A content repository may carry its own editorial guardrails in
`profile/guardrails.md`. The `writing-draft` lens names that file, makes a hit
on one of its classes a CERTAIN finding, and asks the reviewer to say which
guardrails file it read, so a report shows whether the guardrails were applied.
"""

import pathlib
import subprocess
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "adversarial-gate.sh"


def render(*args):
    result = subprocess.run(["sh", str(SCRIPT), "render", *args], capture_output=True, text=True,
                            timeout=30, check=False)
    if result.returncode:
        raise AssertionError(result.stderr)
    return result.stdout


class WritingDraftReadsTheGuardrails(unittest.TestCase):
    def setUp(self):
        self.prompt = render("--lens", "writing-draft", "--set", "DRAFT_PATH=content/2026/a/index.md",
                             "--set", "RESEARCH_PATH=content/2026/a/research.md")

    def test_the_lens_names_the_repository_guardrails_file(self):
        self.assertIn("`profile/guardrails.md`", self.prompt)

    def test_a_guardrail_hit_is_a_certain_finding_that_quotes_the_class(self):
        paragraph = next(part for part in self.prompt.split("\n\n") if "profile/guardrails.md" in part)
        self.assertIn("CERTAIN", paragraph)
        self.assertIn("quotes the class", paragraph)

    def test_the_report_says_which_guardrails_file_was_read_or_that_none_exists(self):
        paragraph = next(part for part in self.prompt.split("\n\n") if "profile/guardrails.md" in part)
        self.assertIn("which guardrails file", paragraph)
        self.assertIn("none exists", paragraph)

    def test_the_lens_takes_no_new_placeholder(self):
        self.assertNotRegex(self.prompt, r"\{[A-Z_]+\}")


if __name__ == "__main__":
    unittest.main()

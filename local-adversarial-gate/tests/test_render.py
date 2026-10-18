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


#: The dashboard's four lenses (sd:5064) and the placeholders each one takes.
DASHBOARD_LENSES = {
    "task": ("INPUT_PATH", "DESIGN_PATH"),
    "brief": ("INPUT_PATH",),
    "repo": ("REPO_NAME",),
    "change": ("INPUT_PATH", "BASE", "HEAD"),
}


class TheDashboardLenses(unittest.TestCase):
    def test_each_lens_is_listed(self):
        listed = subprocess.run(["sh", str(SCRIPT), "lenses"], capture_output=True, text=True, timeout=30,
                                check=True).stdout.split()
        for lens in DASHBOARD_LENSES:
            self.assertIn(lens, listed)

    def test_each_lens_fills_every_placeholder_it_takes_and_no_other(self):
        for lens, keys in DASHBOARD_LENSES.items():
            with self.subTest(lens=lens):
                sets = sum((["--set", f"{key}=/fixture/{key.lower()}"] for key in keys), [])
                prompt = render("--lens", lens, *sets)
                self.assertNotRegex(prompt, r"\{[A-Z_]+\}")
                for key in keys:
                    self.assertIn(f"/fixture/{key.lower()}", prompt)

    def test_each_lens_keeps_the_core_contract_and_asks_for_hypotheses(self):
        core = (SCRIPT.parent / "core.md").read_text()
        for lens, keys in DASHBOARD_LENSES.items():
            with self.subTest(lens=lens):
                prompt = render("--lens", lens, *sum((["--set", f"{key}=x"] for key in keys), []))
                self.assertTrue(prompt.startswith(core.splitlines()[0]))
                self.assertIn("hypothes", prompt.lower())
                self.assertIn("## Ending", prompt)

    def test_each_lens_asks_for_a_titled_heading_per_hypothesis(self):
        # The dashboard's note names the first titles, found by rank.py's heading rule; a real run on gpt-6-astra
        # wrote its findings as bullets, with no heading to find (sd:5064).
        for lens, keys in DASHBOARD_LENSES.items():
            with self.subTest(lens=lens):
                prompt = render("--lens", lens, *sum((["--set", f"{key}=x"] for key in keys), []))
                self.assertIn("Give each hypothesis a `###` heading of its own", prompt)

    def test_each_lens_asks_for_the_lines_a_recommendation_is_filed_from(self):
        # The dashboard files each hypothesis as a review question; the approved task takes its title from
        # `Recommendation:`, its evidence from `Evidence:` and its repository from `Repo:` (sd:5064).
        for lens, keys in DASHBOARD_LENSES.items():
            with self.subTest(lens=lens):
                prompt = render("--lens", lens, *sum((["--set", f"{key}=x"] for key in keys), []))
                for tag in ("`Recommendation:`", "`Evidence:`", "`Repo:`"):
                    self.assertIn(tag, prompt)

    def test_the_change_lens_names_the_diff_it_reads(self):
        prompt = render("--lens", "change", "--set", "INPUT_PATH=/fixture/input.md", "--set", "BASE=1111111",
                        "--set", "HEAD=2222222")
        self.assertIn("git diff 1111111...2222222", prompt)


if __name__ == "__main__":
    unittest.main()

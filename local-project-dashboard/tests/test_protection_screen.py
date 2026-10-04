"""The fleet's protection table: three statuses told apart, unprotected first,
unknown with no gap cells, and a settings link only for a github.com remote."""

import json
import re

from sd_db import upsert_repo
from support import ScreenCase

from sd_dashboard.protection_screen import APPLICABLE, FLAGS, GAPS

AT = "2026-09-11T02:45:00Z"
UNPROTECTED_GAPS = [
    {"id": "unprotected", "gap": "main has no branch protection at all: nothing that can push is stopped by anything"},
    {"id": "required_checks", "gap": "no required status checks on main: a red PR still merges"},
    {"id": "reviews", "gap": "no pull-request review is required on main"},
]
BASELINE_CLEAN = [
    {"id": "protection_source", "value": "ruleset", "flagged": False, "gap": "the default branch is protected by ruleset"},
    {"id": "required_check", "value": "ci", "flagged": False, "gap": "`ci` is not a required check"},
]
FLAGS_CLEAN = [
    {"id": "squash_message", "value": "PR_TITLE / PR_BODY", "flagged": False, "gap": "squash commits are built from PR_TITLE / PR_BODY"},
    {"id": "rebase_merge", "value": "disallowed", "flagged": False, "gap": "rebase merging is allowed"},
] + BASELINE_CLEAN
FLAGS_FLAGGED = [
    {"id": "squash_message", "value": "COMMIT_OR_PR_TITLE / COMMIT_MESSAGES", "flagged": True,
     "gap": "squash commits are built from COMMIT_OR_PR_TITLE / COMMIT_MESSAGES, so a carrier branch's `wip:` subjects land"},
    {"id": "rebase_merge", "value": "allowed", "flagged": True, "gap": "rebase merging is allowed, which replays every branch commit"},
] + BASELINE_CLEAN


class ProtectionScreen(ScreenCase):
    def observe(self, path, status, *, gaps=(), flags=FLAGS_CLEAN, reason=None, branch="main",
                observed_at=AT, detail=None):
        self.connection.execute(
            "INSERT INTO repo_protection (repo, observed_at, status, default_branch, reason, body) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (path, observed_at, status, branch, reason,
             json.dumps({"gaps": list(gaps), "detail": detail or {}, "merge_settings": list(flags), "requests": 2})))
        self.connection.commit()

    def fleet(self):
        """Four registered repositories: protected with one gap, unprotected,
        unknown (a 403), and one never observed. Registered in an order that
        is not the display order."""
        upsert_repo(self.connection, "/repos/a-guarded", remote="git@github.com:platypeeps/guarded.git")
        upsert_repo(self.connection, "/repos/b-private", remote="https://github.com/someone/private.git")
        upsert_repo(self.connection, "/repos/c-bare", remote="git@github.com:platypeeps/bare.git")
        upsert_repo(self.connection, "/repos/d-new", remote="git@github.com:platypeeps/new.git")
        self.observe("/repos/a-guarded", "protected", flags=FLAGS_FLAGGED,
                     gaps=[{"id": "strict", "gap": "required checks are not strict: a green check run against a stale base still merges"}],
                     observed_at="2026-09-11T02:46:00Z")
        self.observe("/repos/c-bare", "unprotected", gaps=UNPROTECTED_GAPS)
        self.observe("/repos/b-private", "unknown", reason="API HTTP 403; retry on a later collection", branch=None, flags=[])

    def rows(self, body):
        return re.findall(r"<tr>(.*?)</tr>", body.split("<tbody>", 1)[1].split("</tbody>", 1)[0], re.S)

    def test_the_three_statuses_render_distinctly_and_the_summary_counts_them(self):
        self.fleet()
        body = self.render("/protection")
        self.assertIn('class="status protection-protected">protected<', body)
        self.assertIn('class="status protection-unprotected">unprotected<', body)
        self.assertIn('class="status protection-unknown">unknown<', body)
        self.assertIn("1 protected · 1 unprotected · 2 unknown · last observed 2026-09-11T02:46:00Z", body)
        self.assertIn("<title>Protection — sd</title>", body)

    def test_unprotected_sorts_first_then_unknown_then_protected(self):
        self.fleet()
        rows = self.rows(self.render("/protection"))
        order = [re.search(r"protection-(protected|unprotected|unknown)", row).group(1) for row in rows]
        self.assertEqual(order, ["unprotected", "unknown", "unknown", "protected"])
        # Within a group, by slug: someone/private is the observed unknown,
        # platypeeps/new the never-observed one.
        self.assertIn("platypeeps/new", rows[1])
        self.assertIn("someone/private", rows[2])

    def test_a_second_checkout_shows_its_siblings_row_and_names_the_lender(self):
        """sd:1607. A second checkout of one repository read `not yet observed`."""
        upsert_repo(self.connection, "/repos/widget", remote="git@github.com:example/widget.git")
        upsert_repo(self.connection, "/repos/widget-copy", remote="https://github.com/example/widget")
        self.observe("/repos/widget", "protected")
        body = self.render("/protection")
        self.assertIn("2 protected · 0 unprotected · 0 unknown", body)
        self.assertIn(f"{AT} · borrowed from /repos/widget", body)
        self.assertEqual(body.count("borrowed from"), 1)

    def test_unknown_rows_show_no_gap_cells_and_carry_their_reason(self):
        self.fleet()
        rows = self.rows(self.render("/protection"))
        private = next(row for row in rows if "someone/private" in row)
        self.assertNotIn("protection-ok", private)
        self.assertNotIn("protection-gap", private)
        self.assertNotIn(">ok<", private)
        self.assertNotIn(">GAP<", private)
        # Counted from the screen's own columns, not written here: the number
        # this line used to carry went stale the day a gap column was added.
        self.assertEqual(private.count("protection-na"), 1 + len(GAPS) + len(FLAGS))  # branch, every gap, both flags
        self.assertIn("API HTTP 403; retry on a later collection", private)
        new = next(row for row in rows if "platypeeps/new" in row)
        self.assertIn("not yet observed", new)
        self.assertIn(">never<", new)
        self.assertNotIn("protection-ok", new)

    def test_a_gap_cell_expands_to_the_sentence_and_ok_cells_do_not(self):
        self.fleet()
        rows = self.rows(self.render("/protection"))
        guarded = next(row for row in rows if "platypeeps/guarded" in row)
        self.assertIn("required checks are not strict", guarded)
        self.assertEqual(guarded.count(">GAP<"), 3)  # strict, and both merge flags
        self.assertEqual(guarded.count(">ok<"), len(GAPS) - 1 + len(BASELINE_CLEAN))  # every gap but strict, the clean baseline
        self.assertIn("COMMIT_OR_PR_TITLE / COMMIT_MESSAGES", guarded)
        bare = next(row for row in rows if "platypeeps/bare" in row)
        self.assertIn("no branch protection at all", bare)
        self.assertEqual(bare.count(">GAP<"), 2)  # required_checks and reviews
        self.assertEqual(bare.count(">ok<"), len(FLAGS))  # every flag, each clean
        self.assertEqual(bare.count("protection-na"), len(GAPS) - len(APPLICABLE["unprotected"]))  # the gaps unprotected cannot answer
        # No hover-only affordance: the sentence is in a details element, not a title.
        self.assertNotIn("title=", guarded)

    def test_the_two_baseline_flags_are_columns_and_another_owner_reads_not_applicable(self):
        """sd:1741, S3: one column each, read from `merge_settings`."""
        labels = dict(FLAGS)
        self.assertEqual((labels["protection_source"], labels["required_check"]), ("Rulesets only", "Required check"))
        upsert_repo(self.connection, "/repos/classic", remote="git@github.com:platypeeps/classic.git")
        upsert_repo(self.connection, "/repos/product", remote="git@github.com:example-corp/product.git")
        self.observe("/repos/classic", "protected", flags=FLAGS_CLEAN[:2] + [
            {"id": "protection_source", "value": "classic", "flagged": True,
             "gap": "the default branch is protected by classic, not by rulesets alone"},
            {"id": "required_check", "value": "CI Result", "flagged": True, "gap": "`ci` is not a required check"}])
        self.observe("/repos/product", "protected", flags=FLAGS_CLEAN[:2])
        body = self.render("/protection")
        self.assertIn(">Rulesets only<", body)
        self.assertIn(">Required check<", body)
        rows = self.rows(body)
        classic = next(row for row in rows if "platypeeps/classic" in row)
        self.assertEqual(classic.count(">GAP<"), 2)
        self.assertIn("<code>classic</code> the default branch is protected by classic", classic)
        self.assertIn("<code>CI Result</code>", classic)
        product = next(row for row in rows if "example-corp/product" in row)
        self.assertEqual(product.count("protection-na"), 2)
        self.assertEqual(product.count(">ok<"), len(GAPS) + 2)  # every gap, both merge flags

    def test_the_required_check_cell_names_the_local_gate_for_a_local_ci_repository(self):
        """sd:2509. The column read "Requires ci" over a `repo.ci = local` row,
        whose baseline check is `sd/local-gate`; each cell now names its own."""
        upsert_repo(self.connection, "/repos/gated", remote="git@github.com:platypeeps/gated.git", ci="local")
        upsert_repo(self.connection, "/repos/actions", remote="git@github.com:platypeeps/actions.git", ci="github")
        gated = [{"id": "required_check", "value": "sd/local-gate", "flagged": False, "gap": "`sd/local-gate` is not a required check"}]
        self.observe("/repos/gated", "protected", flags=FLAGS_CLEAN[:3] + gated)
        self.observe("/repos/actions", "protected")
        body = self.render("/protection")
        self.assertNotIn("Requires ci", body)
        rows = self.rows(body)
        self.assertIn("<code>sd/local-gate</code>", next(row for row in rows if "platypeeps/gated" in row))
        actions = next(row for row in rows if "platypeeps/actions" in row)
        self.assertIn("<code>ci</code>", actions)
        self.assertNotIn("sd/local-gate", actions)

    def test_reason_and_gap_text_are_escaped(self):
        upsert_repo(self.connection, "/repos/odd", remote="git@github.com:platypeeps/odd.git")
        self.observe("/repos/odd", "unknown", reason="<script>alert(1)</script> & co", branch=None, flags=[])
        upsert_repo(self.connection, "/repos/odd2", remote="git@github.com:platypeeps/odd2.git")
        self.observe("/repos/odd2", "protected", gaps=[{"id": "reviews", "gap": "0 approvals <b>self-merges</b>"}])
        body = self.render("/protection")
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt; &amp; co", body)
        self.assertNotIn("<script>alert(1)</script>", body)
        self.assertIn("0 approvals &lt;b&gt;self-merges&lt;/b&gt;", body)

    def test_a_settings_link_only_for_a_github_remote(self):
        upsert_repo(self.connection, "/repos/gh", remote="git@github.com:platypeeps/gh.git")
        upsert_repo(self.connection, "/repos/https", remote="https://github.com/platypeeps/https")
        upsert_repo(self.connection, "/repos/local", remote="/srv/git/local.git")
        upsert_repo(self.connection, "/repos/none")
        body = self.render("/protection")
        self.assertIn('href="https://github.com/platypeeps/gh/settings/branches"', body)
        self.assertIn('href="https://github.com/platypeeps/https/settings/branches"', body)
        self.assertIn("<span>/repos/local</span>", body)
        self.assertIn("<span>/repos/none</span>", body)
        self.assertEqual(body.count("settings/branches"), 2)

    def test_the_filter_reads_the_visible_words(self):
        self.fleet()
        body = self.render("/protection", {"q": ["unprotected"]})
        rows = self.rows(body)
        self.assertEqual(len(rows), 1)
        self.assertIn("platypeeps/bare", rows[0])

    def test_nav_names_protection_and_an_empty_registry_says_what_to_do(self):
        body = self.render("/protection")
        self.assertIn('href="/protection"', body)
        self.assertIn(">Protection</a>", body)
        self.assertIn("No repositories registered", body)
        self.assertIn("0 protected · 0 unprotected · 0 unknown · last observed never", body)
        self.assertIn(">Protection</a>", self.render("/classic/today"))

    def test_the_page_writes_nothing(self):
        self.fleet()
        before = tuple(self.connection.iterdump())
        self.render("/protection")
        self.assertEqual(tuple(self.connection.iterdump()), before)

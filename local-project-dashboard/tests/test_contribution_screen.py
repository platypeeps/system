"""Projection order, escaped evidence, and exact acknowledgment form identities."""

import html
import re
from unittest.mock import patch

from sd_dashboard import contribution_screen
from sd_db import contributions
from support import ScreenCase


def contribution(number=1, lane="awaiting_you", **changes):
    key = f"github:https://github.com/example/project/pull/{number}"
    row = {"key": key, "revision": "a" * 64, "item_id": None,
        "url": key.removeprefix("github:"), "repo": "example/project", "title": f"Contribution {number}",
        "local_status": None, "external_state": "OPEN", "lane": lane, "needs_you": True,
        "reasons": ["Maintainer requested a change"], "event_ids": [f"event-{number}"],
        "observed_at": "2026-09-09T12:00:00Z", "freshness": {"status": "current", "reason": "Complete observation"},
        "blocked_on": None, "depends_on": [], "local_clone": None, "local_branch": None,
        "tested_commit": None, "evidence": [], "notification_state": {},
        "attention_sources": [{"key": key, "revision": "a" * 64,
            "event_ids": [f"event-{number}"], "reasons": ["Maintainer requested a change"]}]}
    row.update(changes)
    return row


def seed_registered(connection):
    url = "https://github.com/example/project/pull/14"
    dependency = {"kind": "merge", "url": "https://github.com/example/library/pull/3"}
    item = contributions.capture(connection, title="Ready patch <safe>",
        changes={"pull_url": url, "depends_on": [dependency]}, who="operator")["item"]["id"]
    observation = {"complete": True, "observed_at": "2026-09-09T12:00:00Z",
        "operator": {"id": "1", "login": "author"}, "author": {"id": "1", "login": "author"},
        "repo": "example/project", "title": "Upstream patch", "state": "open", "head": "a" * 40,
        "base": "b" * 40, "draft": False, "mergeable": "mergeable", "ci": "success", "ci_head": "a" * 40,
        "ci_ids": ["check:1"], "why": ["author"], "blocking_labels": [], "labels": [], "reviews": [],
        "events": [{"id": "comment:1", "at": "2026-09-09T11:00:00Z", "actor_id": "2", "kind": "comment",
            "maintainer": True, "mentions_operator": False, "url": url + "#issuecomment-1"}]}
    contributions.observe_pull(connection, url, observation,
        expected_revision=contributions.snapshot(connection, "github:" + url)["revision"])
    contributions.observe_dependencies(connection, item,
        [{"dependency": dependency, "state": "satisfied", "proof": {
            "url": dependency["url"], "merged": True, "merge_commit_sha": "d" * 40}}],
        observed_at="2026-09-09T12:00:00Z", expected_revision=contributions.snapshot(connection, f"item:{item}")["revision"])
    return item


class ContributionScreen(ScreenCase):
    def test_every_lane_has_a_label_and_no_label_names_a_lane_that_is_gone(self):
        # LABELS is a hand-kept inventory of the library's lanes; `closed` was
        # added to LANES without a label until this held them equal.
        self.assertEqual(set(contribution_screen.LABELS), set(contributions.LANES))

    def test_release_without_selected_tag_renders_pending_on_details_page(self):
        for number, tag_value in enumerate(({}, {"tag": None})):
            with self.subTest(tag=tag_value):
                dependency = {"kind": "release", "repo": "example/library",
                    "contains_pull": "https://github.com/example/library/pull/3", **tag_value}
                contributions.capture(self.connection, title=f"Waiting {number}", changes={
                    "pull_url": f"https://github.com/example/project/pull/{number + 20}",
                    "depends_on": [dependency]}, who="operator")
        before = tuple(self.connection.iterdump())
        body = self.render("/classic/contributions")
        self.assertIn("Tag not selected", body)
        self.assertIn("example/library", body)
        self.assertNotIn("Newly unblocked</", body)
        self.assertEqual(tuple(self.connection.iterdump()), before)
        self.assertTrue(all(row["lane"] == "awaiting_them" for row in contributions.projection(self.connection)))

    def test_release_package_without_version_renders_pending_and_escapes_name(self):
        for number, version in enumerate(({}, {"version": None})):
            with self.subTest(version=version):
                dependency = {"kind": "release", "repo": "example/library", "tag": "v1",
                    "contains_pull": "https://github.com/example/library/pull/3",
                    "package": {"name": "library<safe>", **version}}
                contributions.capture(self.connection, title=f"Waiting {number}", changes={
                    "pull_url": f"https://github.com/example/project/pull/{number + 30}",
                    "depends_on": [dependency]}, who="operator")
        before = tuple(self.connection.iterdump())
        body = self.render("/classic/contributions")
        self.assertIn("Version not selected", body)
        self.assertIn("library&lt;safe&gt;", body)
        self.assertNotIn("library<safe>", body)
        self.assertEqual(tuple(self.connection.iterdump()), before)
        self.assertTrue(all(row["lane"] == "awaiting_them" for row in contributions.projection(self.connection)))

    def test_real_registered_pull_and_dependency_join_have_one_visible_row(self):
        item = seed_registered(self.connection)
        rows = contributions.projection(self.connection)
        before = tuple(self.connection.iterdump())
        body = self.render("/classic/contributions")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["lane"], "newly_unblocked")
        self.assertEqual(len(rows[0]["attention_sources"]), 2)
        self.assertEqual(re.findall(r'data-contribution-key="([^"]+)"', body), [rows[0]["key"]])
        self.assertIn(f"Local item #{item}", body)
        self.assertEqual(tuple(self.connection.iterdump()), before)

    def test_shared_order_including_closed_and_unfiled_is_not_reclassified(self):
        rows = [contribution(9, "newly_unblocked", url=None, item_id=19, external_state=None),
                contribution(8, "awaiting_you", external_state="CLOSED"),
                contribution(7, "awaiting_them"), contribution(6, "merged", external_state="MERGED")]
        before = tuple(self.connection.iterdump())
        with patch.object(contribution_screen.contributions, "projection", return_value=rows) as projection:
            body = self.render("/classic/contributions")
        projection.assert_called_once_with(self.connection)
        self.assertEqual(re.findall(r'data-contribution-key="([^"]+)"', body), [row["key"] for row in rows])
        self.assertIn("Unfiled local work", body)
        self.assertIn("CLOSED", body)
        self.assertIn("Newly unblocked", body)
        self.assertEqual(tuple(self.connection.iterdump()), before)

    def test_metadata_is_escaped_and_local_evidence_is_concrete(self):
        payload = '<img src=x onerror="alert(1)">'
        row = contribution(title=payload, local_clone="/tmp/project", local_branch="fix/<branch>",
            tested_commit="c" * 40, blocked_on=payload, depends_on=[{"kind": "merge", "url": payload}],
            evidence=[{"argv": ["python", "-m", payload], "cwd": "/tmp/project", "exit_code": 1,
                "commit": "c" * 40, "artifact": "/tmp/check.log", "sha256": "d" * 64}])
        body = str(contribution_screen.card(row))
        self.assertNotIn("<img", body)
        self.assertIn(html.escape(payload), body)
        for text in ("fix/&lt;branch&gt;", "c" * 40, "d" * 64, "/tmp/check.log", "Exit code: <code>1</code>"):
            self.assertIn(text, body)
        self.assertNotIn('href="/tmp', body)

    def test_untrusted_link_is_not_an_active_url(self):
        body = str(contribution_screen.card(contribution(url="javascript:alert(1)")))
        self.assertNotIn('href="javascript:', body)

    def test_a_filed_issue_is_linked_as_an_issue_and_a_pull_as_a_pull(self):
        # Filed issue rows reach this screen with an issue URL, and every
        # filed row was labelled "Pull request" (PR #308 review).
        issue = "https://github.com/example/project/issues/7"
        body = str(contribution_screen.card(contribution(key="issue:" + issue, url=issue)))
        self.assertIn(f'<a href="{issue}" rel="noopener noreferrer">Issue</a>', body)
        self.assertNotIn("Pull request", body)
        pull = str(contribution_screen.card(contribution()))
        self.assertIn('rel="noopener noreferrer">Pull request</a>', pull)

    def test_an_unfiled_issue_draft_is_labelled_as_a_draft_not_local_work(self):
        # Draft rows carry `draft_path` and no URL; they read as unfiled
        # branch work before (sd:1205).
        draft = {"path": "/tmp/draft.md", "sha256": "e" * 64}
        for verified, label in ((True, "Unfiled issue draft"), (False, "Unfiled issue draft; body unverified")):
            with self.subTest(verified=verified):
                body = str(contribution_screen.card(contribution(url=None, target_repo="example/project",
                    draft_title="Bug", draft_path=draft, draft_verified=verified)))
                self.assertIn(f"· {label}</p>", body)
                self.assertNotIn("Unfiled local work", body)
                self.assertNotIn("Pull request", body)
        body = str(contribution_screen.card(contribution(url=None)))
        self.assertIn("Unfiled local work", body)

    def test_an_issue_dependency_renders_its_link_not_the_release_fields(self):
        # `issue` is a library dependency kind; it fell into the release
        # branch here and raised KeyError on `repo` (sd:1205).
        issue = "https://github.com/example/library/issues/9"
        body = str(contribution_screen.card(contribution(depends_on=[{"kind": "issue", "url": issue}])))
        self.assertIn(f'Resolve issue <a href="{issue}" rel="noopener noreferrer">{issue}</a>', body)
        self.assertNotIn("release", body)

    def test_two_attention_sources_get_distinct_exact_forms(self):
        row = contribution()
        row["attention_sources"].append({"key": "item:14", "revision": "b" * 64,
            "event_ids": ["dep-1", "dep-2"], "reasons": ["Dependency ready"]})
        body = str(contribution_screen.card(row))
        forms = re.findall(r'<form\b.*?</form>', body)
        self.assertEqual(len(forms), 2)
        for source, form in zip(row["attention_sources"], forms, strict=True):
            self.assertIn(f'name="key" value="{source["key"]}"', form)
            self.assertIn(f'name="revision" value="{source["revision"]}"', form)
            self.assertEqual(re.findall(r'name="event_ids" value="([^"]+)"', form), source["event_ids"])

    def test_notification_labels_exclude_internal_claim_tokens(self):
        row = contribution(notification_state={"item:14": {"event-1": {
            "event": {"reason": "Maintainer comment"}, "status": "uncertain", "token": "private-claim-token"}}})
        body = str(contribution_screen.card(row))
        self.assertIn("Maintainer comment: uncertain", body)
        self.assertNotIn("private-claim-token", body)
        self.assertIn("No dependencies recorded", body)

    def test_filter_and_pagination_use_existing_listing_preserving_order(self):
        rows = [contribution(number) for number in range(65, 0, -1)]
        with patch.object(contribution_screen.contributions, "projection", return_value=rows):
            body = self.render("/classic/contributions", {"page": ["2"]})
            filtered = self.render("/classic/contributions", {"q": ["Contribution 65"]})
        self.assertEqual(re.findall(r'data-contribution-key="([^"]+)"', body), [row["key"] for row in rows[50:]])
        self.assertEqual(re.findall(r'data-contribution-key="([^"]+)"', filtered), [rows[0]["key"]])

    def test_today_preview_discloses_its_limit_and_full_destination(self):
        rows = [contribution(number) for number in range(8)]
        with patch.object(contribution_screen.contributions, "projection", return_value=rows):
            body = self.render("/classic/today")
        self.assertEqual(re.findall(r'data-contribution-key="([^"]+)"', body), [row["key"] for row in rows[:5]])
        self.assertIn("Showing 5 of 8 contributions", body)
        self.assertIn('href="/contributions"', body)

"""Canned API contracts: no real network, credentials, or notification channels."""

import copy
import json
import time
import unittest
from unittest.mock import patch

from sd_db.contribution_github import (Client, Response, Unavailable, canonical_issue_url, dependency, issue,
                                       issue_parts, pull)
from sd_db.shadow_sync import _SearchBudget

URL = "https://github.com/example/project/pull/7"
ISSUE_URL = "https://github.com/example/project/issues/9"
ROOT = "/repos/example/project"
HEAD, BASE, MERGED, RELEASE = (char * 40 for char in "abcd")
AT = "2026-09-10T01:00:00Z"
OPERATOR = {"id": "1", "login": "operator"}


class Api:
    def __init__(self):
        self.calls = []
        self.detail = {"user": {"id": 1, "login": "operator"}, "head": {"sha": HEAD},
                       "base": {"sha": BASE}, "state": "open", "merged": False,
                       "draft": False, "title": "Contribution", "updated_at": AT,
                       "mergeable": True, "labels": []}
        self.rows = {
            "/user": {"id": 1, "login": "operator"},
            ROOT + "/pulls/7": self.detail,
            ROOT + "/pulls/7/reviews?per_page=100": [],
            ROOT + "/issues/7/comments?per_page=100": [],
            ROOT + "/pulls/7/comments?per_page=100": [],
            ROOT + f"/commits/{HEAD}/check-runs?filter=latest&per_page=100": {"total_count": 1, "check_runs": [
                {"id": 91, "head_sha": HEAD, "status": "completed", "conclusion": "success"}]},
            ROOT + f"/commits/{HEAD}/status?per_page=100": {"sha": HEAD, "total_count": 0, "statuses": []},
            "graphql": {"data": {"repository": {"pullRequest": {"timelineItems": {
                "nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None}}}}}},
        }

    def __call__(self, path, fields, timeout):
        self.calls.append((path, fields, timeout))
        value = self.rows[path]
        if callable(value):
            value = value()
        if isinstance(value, Response):
            return value
        return Response(200, {}, json.dumps(value))

    def client(self, *, requests=100, seconds=30):
        return Client(_SearchBudget(requests, time.monotonic() + seconds), transport=self)

    def observation(self):
        return pull(self.client(), URL, OPERATOR, observed_at=AT)


class PullCollection(unittest.TestCase):
    def test_full_snapshot_keeps_actor_identity_reviews_and_current_ci(self):
        api = Api()
        api.rows[ROOT + "/pulls/7/reviews?per_page=100"] = [
            {"id": 2, "user": {"id": 2, "login": "maintainer"}, "state": "CHANGES_REQUESTED", "submitted_at": AT,
             "body": "Please fix this", "author_association": "MEMBER"},
            {"id": 3, "user": {"id": 2, "login": "maintainer"}, "state": "APPROVED", "submitted_at": AT}]
        api.rows[ROOT + "/issues/7/comments?per_page=100"] = [
            {"id": 5, "user": {"id": 3, "login": "reviewer"}, "body": "@operator please look", "updated_at": AT,
             "author_association": "NONE"},
            {"id": 6, "user": {"id": 1, "login": "operator"}, "body": "own update", "updated_at": AT}]
        result = api.observation()
        self.assertTrue(result["complete"])
        self.assertEqual([row["state"] for row in result["reviews"]], ["CHANGES_REQUESTED", "APPROVED"])
        self.assertEqual([row["actor_id"] for row in result["events"]], ["2", "3", "1"])
        self.assertTrue(result["events"][0]["maintainer"])
        self.assertTrue(result["events"][1]["mentions_operator"])
        self.assertEqual((result["ci"], result["ci_head"], result["ci_ids"]), ("success", HEAD, ["check-run:91"]))
        self.assertEqual([row[0] for row in api.calls].count(ROOT + "/pulls/7"), 2)

    def test_typed_timeline_uses_originator_not_mentioned_actor(self):
        api = Api()
        api.rows["graphql"]["data"]["repository"]["pullRequest"]["timelineItems"]["nodes"] = [
            {"__typename": "MentionedEvent", "actor": {"databaseId": 1, "login": "operator"}},
            {"__typename": "ConvertToDraftEvent", "id": "draft:1", "actor": {"databaseId": 2, "login": "owner"}, "createdAt": AT},
            {"__typename": "LabeledEvent", "id": "label:1", "actor": {"databaseId": 2, "login": "owner"}, "createdAt": AT, "label": {"name": "needs-author"}}]
        result = api.observation()
        self.assertEqual([row["kind"] for row in result["events"]], ["converted_to_draft", "label_added"])
        self.assertEqual(result["blocking_labels"], [])

    def test_null_actor_refuses_complete_snapshot(self):
        api = Api()
        api.rows[ROOT + "/issues/7/comments?per_page=100"] = [{"id": 5, "user": None, "body": "hello", "updated_at": AT}]
        with self.assertRaisesRegex(Unavailable, "actor identity"):
            api.observation()

    def test_null_mergeability_is_unknown_and_invalid_value_refuses(self):
        api = Api()
        api.detail["mergeable"] = None
        self.assertEqual(api.observation()["mergeable"], "unknown")
        api.detail["mergeable"] = "false"
        with self.assertRaisesRegex(Unavailable, "mergeability"):
            api.observation()

    def test_head_drift_and_old_head_checks_refuse(self):
        api = Api()
        count = []
        def detail():
            count.append(True)
            return api.detail if len(count) == 1 else {**api.detail, "head": {"sha": BASE}}
        api.rows[ROOT + "/pulls/7"] = detail
        with self.assertRaisesRegex(Unavailable, "changed while"):
            api.observation()
        api = Api()
        api.rows[ROOT + f"/commits/{HEAD}/check-runs?filter=latest&per_page=100"]["check_runs"][0]["head_sha"] = BASE
        with self.assertRaisesRegex(Unavailable, "stale head"):
            api.observation()
        api = Api()
        api.rows[ROOT + f"/commits/{HEAD}/status?per_page=100"]["sha"] = BASE
        with self.assertRaisesRegex(Unavailable, "stale head"):
            api.observation()

    def test_pending_empty_ci_and_legacy_failure_are_not_green(self):
        api = Api()
        api.rows[ROOT + f"/commits/{HEAD}/check-runs?filter=latest&per_page=100"] = {"total_count": 0, "check_runs": []}
        self.assertEqual(api.observation()["ci"], "pending")
        api.rows[ROOT + f"/commits/{HEAD}/status?per_page=100"] = {"sha": HEAD, "total_count": 1, "statuses": [{"id": 8, "state": "failure"}]}
        self.assertEqual(api.observation()["ci"], "failure")

    def test_closed_unmerged_is_collected_without_open_filter(self):
        api = Api()
        api.detail["state"] = "closed"
        self.assertEqual(api.observation()["state"], "closed")
        api.detail["merged"] = True
        self.assertEqual(api.observation()["state"], "merged")


class BoundedTransport(unittest.TestCase):
    def test_rest_pagination_follows_all_pages_and_refuses_duplicate(self):
        api = Api()
        api.rows["/rows?per_page=100"] = Response(200, {"Link": '<https://api.github.com/rows?per_page=100&page=2>; rel="next"'}, '[{"id":1}]')
        api.rows["/rows?per_page=100&page=2"] = [{"id": 2}]
        self.assertEqual(len(api.client().pages("/rows")), 2)
        api.rows["/rows?per_page=100&page=2"] = [{"id": 1}]
        with self.assertRaisesRegex(Unavailable, "repeated identities"):
            api.client().pages("/rows")

    def test_truncated_counts_repeated_cursor_and_cross_origin_refuse(self):
        api = Api()
        api.rows["/rows?per_page=100"] = {"total_count": 2, "rows": [{"id": 1}]}
        with self.assertRaisesRegex(Unavailable, "truncated"):
            api.client().pages("/rows", member="rows")
        api.rows["/rows?per_page=100"] = Response(200, {"Link": '<https://evil.example/rows?page=2>; rel="next"'}, '[]')
        with self.assertRaisesRegex(Unavailable, "escaped"):
            api.client().pages("/rows")
        info = api.rows["graphql"]["data"]["repository"]["pullRequest"]["timelineItems"]["pageInfo"]
        info.update(hasNextPage=True, endCursor="again")
        with self.assertRaisesRegex(Unavailable, "incomplete GitHub timeline"):
            api.client().timeline("example", "project", 7)

    def test_http_partial_graphql_bad_json_and_budget_refuse(self):
        api = Api()
        for value in (Response(429, {}, ""), Response(200, {}, "not JSON"), {"errors": [{"message": "partial"}], "data": {}}):
            api.rows["/failure"] = value
            with self.subTest(value=value), self.assertRaises(Unavailable):
                api.client().get("/failure")
        for options in ({"requests": 0}, {"seconds": -1}):
            with self.assertRaisesRegex(Unavailable, "budget"):
                api.client(**options).get("/user")

    def test_gh_fixed_get_argv_parses_headers_without_saving_stderr(self):
        calls = []
        def runner(argv):
            calls.append(argv)
            return 0, 'HTTP/2.0 200 OK\nContent-Type: application/json\n\n{"id":1}', "secret must not be retained"
        client = Client(_SearchBudget(1, time.monotonic() + 30), runner=runner)
        self.assertEqual(client.get("/user"), {"id": 1})
        self.assertEqual(calls, [["gh", "api", "--include", "--method", "GET", "/user"]])


class Dependencies(unittest.TestCase):
    def setUp(self):
        self.api = Api()
        self.api.detail.update(merged=True, merge_commit_sha=MERGED)
        self.dep = {"kind": "release", "repo": "example/project", "tag": "v1.2.3", "contains_pull": URL,
                    "package": {"name": "aiounifi", "version": "1.2.3"}}
        self.api.rows.update({
            ROOT + "/releases/tags/v1.2.3": {"id": 21, "tag_name": "v1.2.3", "draft": False, "published_at": AT},
            ROOT + "/git/ref/tags/v1.2.3": {"object": {"type": "tag", "sha": BASE}},
            ROOT + f"/git/tags/{BASE}": {"object": {"type": "commit", "sha": RELEASE}},
            ROOT + f"/compare/{MERGED}...{RELEASE}": {"status": "ahead", "merge_base_commit": {"sha": MERGED}},
            "https://pypi.org/pypi/aiounifi/1.2.3/json": {"info": {"name": "aiounifi", "version": "1.2.3"}, "urls": [
                {"filename": "aiounifi.whl", "yanked": False, "digests": {"sha256": "e" * 64}, "upload_time_iso_8601": AT}]},
        })

    def test_merge_tag_ancestry_and_exact_package_all_have_separate_proof(self):
        result = dependency(self.api.client(), self.dep)
        self.assertEqual(result["state"], "satisfied")
        self.assertEqual(result["dependency"], self.dep)
        self.assertEqual(result["proof"]["merge_commit_sha"], MERGED)
        self.assertEqual(result["proof"]["tag_commit"], RELEASE)
        self.assertEqual(result["proof"]["package"]["version"], "1.2.3")
        self.assertNotEqual(result["proof"]["merge_commit_sha"], HEAD)

    def test_merge_alone_does_not_require_release(self):
        result = dependency(self.api.client(), {"kind": "merge", "url": URL})
        self.assertEqual(result["state"], "satisfied")
        self.assertEqual(len(self.api.calls), 1)

    def test_unmerged_unreleased_not_contained_unpublished_and_yanked_stay_pending(self):
        for path, replacement in (
            (ROOT + "/pulls/7", {"merged": False}),
            (ROOT + "/releases/tags/v1.2.3", Response(404, {}, "")),
            (ROOT + f"/compare/{MERGED}...{RELEASE}", {"status": "diverged", "merge_base_commit": {"sha": BASE}}),
            ("https://pypi.org/pypi/aiounifi/1.2.3/json", Response(404, {}, "")),
            ("https://pypi.org/pypi/aiounifi/1.2.3/json", {"info": {"name": "aiounifi", "version": "1.2.3"}, "urls": []}),
        ):
            with self.subTest(path=path, replacement=replacement):
                old = self.api.rows[path]
                self.api.rows[path] = replacement
                self.assertEqual(dependency(self.api.client(), self.dep)["state"], "pending")
                self.api.rows[path] = old
        self.api.rows["https://pypi.org/pypi/aiounifi/1.2.3/json"]["urls"][0]["yanked"] = True
        self.assertEqual(dependency(self.api.client(), self.dep)["state"], "pending")

    def test_missing_version_transient_failure_and_changed_tag_are_unknown(self):
        dep = copy.deepcopy(self.dep)
        del dep["package"]["version"]
        self.assertEqual(dependency(self.api.client(), dep)["state"], "unknown")
        self.api.rows[ROOT + "/releases/tags/v1.2.3"] = Response(503, {}, "")
        self.assertEqual(dependency(self.api.client(), self.dep)["state"], "unknown")
        self.setUp()
        calls = []
        def changing_ref():
            calls.append(True)
            return {"object": {"type": "commit", "sha": RELEASE if len(calls) == 1 else HEAD}}
        self.api.rows[ROOT + "/git/ref/tags/v1.2.3"] = changing_ref
        self.assertEqual(dependency(self.api.client(), self.dep)["state"], "unknown")

    def test_tag_peeling_and_response_size_are_bounded(self):
        self.api.rows[ROOT + f"/git/tags/{BASE}"] = {"object": {"type": "tag", "sha": BASE}}
        self.assertEqual(dependency(self.api.client(), self.dep)["state"], "unknown")
        self.assertLessEqual(len(self.api.calls), 11)
        with patch("sd_db.contribution_github.MAX_BYTES", 3), self.assertRaisesRegex(Unavailable, "size limit"):
            self.api.client().get("/user")


class IssueApi(Api):
    """The same transport answering for issue 9: detail, comments and an issue-rooted timeline."""

    def __init__(self):
        super().__init__()
        self.issue = {"user": {"id": 1, "login": "operator"}, "state": "open", "state_reason": None,
                      "title": "Bug report", "updated_at": AT, "labels": []}
        self.timeline = {"nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None}}
        self.rows.update({
            ROOT + "/issues/9": self.issue,
            ROOT + "/issues/9/comments?per_page=100": [],
            "graphql": {"data": {"repository": {"issue": {"timelineItems": self.timeline}}}},
        })

    def observation(self):
        return issue(self.client(), ISSUE_URL, OPERATOR, observed_at=AT)

    def closed(self, reason, id="closed:1"):
        return {"__typename": "ClosedEvent", "id": id, "actor": {"databaseId": 2, "login": "owner"},
                "createdAt": AT, "stateReason": reason}


class IssueUrls(unittest.TestCase):
    def test_parts_lower_case_the_repository_and_drop_the_trailing_slash(self):
        self.assertEqual(issue_parts("https://github.com/Example/Project/issues/9/"), ("example", "project", 9))
        self.assertEqual(canonical_issue_url("https://github.com/Example/Project/issues/9/"), ISSUE_URL)

    def test_pull_urls_zero_numbers_and_suffixes_are_refused(self):
        for url in (URL, "https://github.com/example/project/issues/0", ISSUE_URL + "#issuecomment-1",
                    ISSUE_URL + "?x=1", "http://github.com/example/project/issues/9", ""):
            with self.subTest(url=url), self.assertRaisesRegex(Unavailable, "invalid GitHub issue URL"):
                issue_parts(url)


class IssueCollection(unittest.TestCase):
    def test_snapshot_keeps_comments_labels_and_every_close_reason(self):
        api = IssueApi()
        api.issue["labels"] = [{"name": "bug"}]
        api.rows[ROOT + "/issues/9/comments?per_page=100"] = [
            {"id": 5, "user": {"id": 2, "login": "owner"}, "body": "Thanks", "updated_at": AT, "author_association": "OWNER"},
            {"id": 6, "user": {"id": 3, "login": "passerby"}, "body": "@operator same here", "updated_at": AT,
             "author_association": "NONE"}]
        api.timeline["nodes"] = [
            {"__typename": "MentionedEvent", "actor": {"databaseId": 1, "login": "operator"}},
            {"__typename": "LabeledEvent", "id": "label:1", "actor": {"databaseId": 2, "login": "owner"}, "createdAt": AT,
             "label": {"name": "bug"}},
            {"__typename": "UnlabeledEvent", "id": "label:2", "actor": {"databaseId": 2, "login": "owner"}, "createdAt": AT,
             "label": {"name": "triage"}},
            api.closed("COMPLETED", "closed:1"),
            {"__typename": "ReopenedEvent", "id": "reopened:1", "actor": {"databaseId": 1, "login": "operator"}, "createdAt": AT},
            api.closed("NOT_PLANNED", "closed:2"),
            api.closed("DUPLICATE", "closed:3"),
            api.closed(None, "closed:4")]
        result = api.observation()
        self.assertTrue(result["complete"])
        self.assertEqual((result["repo"], result["title"], result["state"], result["state_reason"], result["labels"]),
                         ("example/project", "Bug report", "open", None, ["bug"]))
        self.assertEqual([row["kind"] for row in result["events"]],
                         ["comment", "comment", "label_added", "label_removed", "closed_completed", "reopened",
                          "closed_not_planned", "closed_not_planned", "closed"])
        self.assertEqual([row["actor_id"] for row in result["events"]], ["2", "3", "2", "2", "2", "1", "2", "2", "2"])
        self.assertTrue(result["events"][0]["maintainer"])
        self.assertTrue(result["events"][1]["mentions_operator"])
        self.assertEqual((result["events"][2]["label"], result["events"][3]["label"]), ("bug", "triage"))
        self.assertEqual(result["events"][4]["id"], "closed:1")
        for key in ("reviews", "ci", "ci_head", "ci_ids", "head", "base", "mergeable", "draft"):
            self.assertNotIn(key, result)
        self.assertEqual([row[0] for row in api.calls].count(ROOT + "/issues/9"), 2)
        self.assertEqual(len(api.calls), 4)

    def test_rest_state_reason_maps_to_the_two_the_core_knows(self):
        api = IssueApi()
        for given, expected in (("completed", "completed"), ("not_planned", "not_planned"), ("duplicate", "not_planned"),
                                ("reopened", None), (None, None), ("something-new", None)):
            with self.subTest(given=given):
                api.issue.update(state="closed" if expected else "open", state_reason=given)
                result = api.observation()
                self.assertEqual((result["state"], result["state_reason"]), (api.issue["state"], expected))
        api.timeline["nodes"] = [api.closed("SOMETHING_NEW")]
        self.assertEqual(api.observation()["events"][0]["kind"], "closed")

    def test_pull_request_at_issue_url_refuses(self):
        api = IssueApi()
        api.issue["pull_request"] = {"url": "https://api.github.com/repos/example/project/pulls/9"}
        with self.assertRaisesRegex(Unavailable, "issue URL names a pull request"):
            api.observation()
        self.assertEqual(len(api.calls), 1)

    def test_change_between_the_two_reads_refuses(self):
        for field, changed in (("updated_at", "2026-09-10T02:00:00Z"), ("state", "closed"), ("state_reason", "completed"),
                               ("labels", [{"name": "bug"}])):
            api = IssueApi()
            count = []
            def detail():
                count.append(True)
                return api.issue if len(count) == 1 else {**api.issue, field: changed}
            api.rows[ROOT + "/issues/9"] = detail
            with self.subTest(field=field), self.assertRaisesRegex(Unavailable, "changed while"):
                api.observation()

    def test_unknown_state_null_actor_and_missing_labels_refuse(self):
        api = IssueApi()
        api.issue["state"] = "merged"
        with self.assertRaisesRegex(Unavailable, "issue state is incomplete"):
            api.observation()
        api = IssueApi()
        api.timeline["nodes"] = [{"__typename": "ReopenedEvent", "id": "reopened:1", "actor": None, "createdAt": AT}]
        with self.assertRaisesRegex(Unavailable, "actor identity"):
            api.observation()
        api = IssueApi()
        api.issue["labels"] = None
        with self.assertRaisesRegex(Unavailable, "labels are unavailable"):
            api.observation()

    def test_issue_timeline_is_rooted_at_the_issue_and_pages_like_the_pull_one(self):
        api = IssueApi()
        pages = []
        def graphql():
            pages.append(True)
            nodes = [{"__typename": "ReopenedEvent", "id": f"reopened:{len(pages)}",
                      "actor": {"databaseId": 2, "login": "owner"}, "createdAt": AT}]
            info = {"hasNextPage": len(pages) == 1, "endCursor": "next" if len(pages) == 1 else None}
            return {"data": {"repository": {"issue": {"timelineItems": {"nodes": nodes, "pageInfo": info}}}}}
        api.rows["graphql"] = graphql
        result = api.observation()
        self.assertEqual([row["id"] for row in result["events"]], ["reopened:1", "reopened:2"])
        queries = [fields for path, fields, _ in api.calls if path == "graphql"]
        self.assertEqual(len(queries), 2)
        self.assertIn("issue(number:$number)", queries[0]["query"])
        self.assertIn("ClosedEvent { id createdAt actor", queries[0]["query"])
        self.assertIn("stateReason", queries[0]["query"])
        self.assertEqual(len(api.calls), 5)
        api = Api()
        api.client().timeline("example", "project", 7)
        self.assertIn("pullRequest(number:$number)", api.calls[0][1]["query"])
        self.assertNotIn("stateReason", api.calls[0][1]["query"])


class IssueDependencies(unittest.TestCase):
    def setUp(self):
        self.api = IssueApi()
        self.dep = {"kind": "issue", "url": ISSUE_URL}

    def references(self, *sources, more=False):
        nodes = [{"__typename": "CrossReferencedEvent", "source": source} for source in sources]
        self.api.rows["graphql"] = {"data": {"repository": {"issue": {"timelineItems": {
            "nodes": nodes, "pageInfo": {"hasNextPage": more, "endCursor": "next" if more else None}}}}}}

    def test_closed_as_completed_is_satisfied_from_one_read(self):
        self.api.issue.update(state="closed", state_reason="completed")
        result = dependency(self.api.client(), self.dep)
        self.assertEqual(result["state"], "satisfied")
        self.assertEqual(result["dependency"], self.dep)
        self.assertEqual(result["proof"], {"url": ISSUE_URL, "closed": True, "state_reason": "completed"})
        self.assertEqual(len(self.api.calls), 1)

    def test_open_issue_with_a_merged_referencing_pull_is_satisfied(self):
        self.references({}, {"url": "https://github.com/example/project/pull/3", "merged": False},
                        {"url": "https://github.com/Example/Project/pull/7", "merged": True})
        result = dependency(self.api.client(), self.dep)
        self.assertEqual(result["state"], "satisfied")
        self.assertEqual(result["proof"], {"url": ISSUE_URL, "closed": False, "state_reason": None, "merged_pull": URL})
        self.assertEqual(len(self.api.calls), 2)

    def test_open_and_closed_not_planned_without_a_merged_pull_stay_pending(self):
        self.references({}, {"url": "https://github.com/example/project/pull/3", "merged": False})
        result = dependency(self.api.client(), self.dep)
        self.assertEqual((result["state"], result["proof"]), ("pending", {"url": ISSUE_URL, "closed": False, "state_reason": None}))
        self.api.issue.update(state="closed", state_reason="not_planned")
        result = dependency(self.api.client(), self.dep)
        self.assertEqual((result["state"], result["proof"]),
                         ("pending", {"url": ISSUE_URL, "closed": True, "state_reason": "not_planned"}))
        self.assertNotIn("reason", result)

    def test_unavailable_malformed_truncated_and_misnamed_are_unknown_with_a_reason(self):
        self.api.rows[ROOT + "/issues/9"] = Response(503, {}, "")
        result = dependency(self.api.client(), self.dep)
        self.assertEqual((result["state"], result["proof"]), ("unknown", {}))
        self.assertIn("HTTP 503", result["reason"])
        self.api = IssueApi()
        self.api.rows["graphql"] = {"data": {"repository": {"issue": None}}}
        self.assertEqual(dependency(self.api.client(), self.dep)["reason"], "incomplete GitHub timeline")
        self.references({"url": "https://github.com/example/project/pull/3", "merged": False}, more=True)
        self.assertEqual(dependency(self.api.client(), self.dep)["reason"], "issue cross-references exceed one page")
        self.references({"url": "not a pull", "merged": True})
        self.assertEqual(dependency(self.api.client(), self.dep)["reason"], "invalid GitHub pull URL")
        self.api.issue["pull_request"] = {}
        self.assertEqual(dependency(self.api.client(), self.dep)["reason"], "issue URL names a pull request")
        for bad in ({"kind": "issue", "url": URL}, {"kind": "issue"}):
            with self.subTest(bad=bad):
                self.assertEqual(dependency(self.api.client(), bad)["reason"], "invalid GitHub issue URL")

    def test_other_kinds_are_still_refused(self):
        self.api.detail.update(merged=True, merge_commit_sha=MERGED)
        result = dependency(self.api.client(), {"kind": "closed", "contains_pull": URL})
        self.assertEqual((result["state"], result["reason"]), ("unknown", "unsupported remote dependency kind"))

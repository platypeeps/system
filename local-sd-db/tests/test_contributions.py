"""Contribution lifecycle uses real temporary stores and no external calls."""

import copy
import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from sd_db import connect, initialise
from sd_db import contributions as c
from sd_db.workflow import StaleItem, WorkflowError, item_state
from sd_db.writes import set_item_fields, transition, upsert_shadow

URL = "https://github.com/upstream/project/pull/12"
ISSUE = "https://github.com/upstream/project/issues/56"
DEPENDENCY = "https://github.com/upstream/library/pull/34"
T0 = "2026-09-10T00:00:00+00:00"
T1 = "2026-09-10T00:01:00+00:00"
T2 = "2026-09-10T00:02:00+00:00"
T3 = "2026-09-10T00:03:00+00:00"


class ContributionCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "sd.db"
        initialise(self.path)
        self.db = connect(self.path)
        self.addCleanup(self.db.close)

    def observation(self, **changes):
        value = {"complete": True, "observed_at": T0, "operator": {"id": "1", "login": "author"},
                 "author": {"id": "1", "login": "author"}, "repo": "upstream/project", "title": "Contribution",
                 "state": "open", "head": "a" * 40, "base": "b" * 40, "draft": False,
                 "mergeable": "mergeable", "ci": "success", "ci_head": "a" * 40, "ci_ids": ["check:1"],
                 "why": ["author"], "blocking_labels": [], "labels": ["blocked"], "reviews": [], "events": []}
        value.update(changes)
        return value

    def observe(self, **changes):
        return c.observe_pull(self.db, URL, self.observation(**changes),
                              expected_revision=c.snapshot(self.db, "github:" + URL)["revision"])

    def event(self, kind="comment", **changes):
        value = {"id": "event:1", "at": T0, "actor_id": "2", "kind": kind,
                 "maintainer": True, "mentions_operator": False, "url": URL + "#issuecomment-1"}
        value.update(changes)
        return value

    def metadata(self):
        clone = self.root / "clone"
        clone.mkdir()
        for argv in (["init", "-q", "-b", "contribution"], ["config", "user.name", "Fixture"],
                     ["config", "user.email", "fixture@example.invalid"], ["commit", "--allow-empty", "-qm", "fixture"]):
            subprocess.run(["git", *argv], cwd=clone, check=True)
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=clone, text=True).strip()
        log = self.root / "acceptance.log"
        log.write_text("Ran 4 tests\nOK\n")
        return {"local_clone": str(clone), "local_branch": "contribution", "tested_commit": commit,
                "evidence": [{"argv": ["never-execute-this-stored-command"], "cwd": str(clone), "exit_code": 0,
                              "commit": commit, "artifact": str(log), "sha256": hashlib.sha256(log.read_bytes()).hexdigest()}],
                "blocked_on": "Waiting for the library release", "depends_on": []}

    def draft(self, **changes):
        body = self.root / "draft.md"
        body.write_text("# Bug\n\nSteps to reproduce.\n")
        value = {"target_repo": "upstream/project", "draft_title": "Project loses the second argument",
                 "draft_path": {"path": str(body), "sha256": hashlib.sha256(body.read_bytes()).hexdigest()}}
        value.update(changes)
        return value

    def issue_observation(self, **changes):
        # The shape Slice B's collector produces: no reviews, ci, head, base, mergeable or draft.
        value = {"complete": True, "observed_at": T0, "operator": {"id": "1", "login": "author"},
                 "author": {"id": "1", "login": "author"}, "repo": "upstream/project", "title": "Issue",
                 "state": "open", "state_reason": None, "why": ["author"], "blocking_labels": [],
                 "labels": [], "events": []}
        value.update(changes)
        return value

    def store_issue(self, url=ISSUE, attention=(), **changes):
        # Slice C's observe_issue stores this; until it lands the checkpoint is written the way it will be.
        state = c.snapshot(self.db, "issue:" + url)
        state["observation"] = self.issue_observation(**changes)
        c._publish_attention(self.db, state, list(attention))
        return c._save(self.db, state)


class MetadataTests(ContributionCase):
    def test_unfiled_capture_preserves_real_evidence_and_never_executes_argv(self):
        values = self.metadata()
        result = c.capture(self.db, title="Unfiled contribution", changes=values, who="operator")
        row = result["item"]
        self.assertEqual(row["kind"], "task")
        self.assertEqual(row["status"], "planning")
        self.assertEqual(json.loads(row["fields"])["contribution"], values)
        self.assertEqual(self.db.execute("SELECT count(*) FROM shadow").fetchone()[0], 0)
        projected = c.projection(self.db)[0]
        self.assertEqual(projected["local_branch"], "contribution")
        self.assertTrue(projected["evidence_verified"])

    def test_invalid_proof_does_not_leave_half_a_capture(self):
        values = self.metadata()
        values["evidence"][0]["sha256"] = "0" * 64
        with self.assertRaises(WorkflowError):
            c.capture(self.db, title="Bad proof", changes=values, who="operator")
        self.assertEqual(self.db.execute("SELECT count(*) FROM item").fetchone()[0], 0)

    def test_edit_preserves_unrelated_fields_and_refuses_stale_revision(self):
        state = c.capture(self.db, title="Filed", changes={"pull_url": URL}, who="operator")
        item = state["item"]["id"]
        fields = json.loads(state["item"]["fields"])
        fields["unrelated"] = {"keep": True}
        set_item_fields(self.db, item, fields=fields)
        before = item_state(self.db, item)
        after = c.configure(self.db, item, {"blocked_on": "maintainer"}, expected_revision=before["revision"], who="operator")
        self.assertEqual(json.loads(after["item"]["fields"])["unrelated"], {"keep": True})
        with self.assertRaises(WorkflowError):
            c.configure(self.db, item, {"blocked_on": "overwrite"}, expected_revision=before["revision"], who="operator")
        self.assertEqual(item_state(self.db, item), after)

    def test_task_dependencies_reject_cycles(self):
        first = c.capture(self.db, title="first", changes={"pull_url": URL}, who="operator")["item"]["id"]
        second = c.capture(self.db, title="second", changes={"pull_url": URL.replace("12", "13")}, who="operator")["item"]["id"]
        c.configure(self.db, first, {"depends_on": [{"kind": "item", "item": second}]}, who="operator")
        with self.assertRaisesRegex(WorkflowError, "cycle"):
            c.configure(self.db, second, {"depends_on": [{"kind": "item", "item": first}]}, who="operator")

    def test_missing_or_failed_test_evidence_is_trackable_but_never_verified(self):
        values = self.metadata()
        values.pop("tested_commit")
        values.pop("blocked_on")
        values["evidence"] = []
        captured = c.capture(self.db, title="Unverified", changes=values, who="operator")
        self.assertFalse(c.projection(self.db)[0]["evidence_verified"])
        self.assertEqual(c.projection(self.db)[0]["lane"], "awaiting_you")
        values["tested_commit"] = subprocess.check_output(["git", "-C", values["local_clone"], "rev-parse", "HEAD"], text=True).strip()
        values["evidence"] = [{"artifact": str(self.root / "acceptance.log"),
                               "sha256": hashlib.sha256((self.root / "acceptance.log").read_bytes()).hexdigest(),
                               "exit_code": None}]
        c.configure(self.db, captured["item"]["id"], values, who="operator")
        self.assertFalse(c.projection(self.db)[0]["evidence_verified"])

    def test_duplicate_registered_url_refuses_without_creating_an_item(self):
        c.capture(self.db, title="Filed", changes={"pull_url": URL}, who="operator")
        with self.assertRaises(WorkflowError):
            c.capture(self.db, title="Duplicate", changes={"pull_url": URL}, who="operator")
        self.assertEqual(self.db.execute("SELECT count(*) FROM item").fetchone()[0], 1)

    def test_github_case_aliases_share_one_identity(self):
        c.capture(self.db, title="Filed", changes={"pull_url": URL.replace("upstream/project", "Upstream/Project")}, who="operator")
        with self.assertRaises(WorkflowError):
            c.capture(self.db, title="Duplicate", changes={"pull_url": URL}, who="operator")
        self.assertEqual(c.snapshot(self.db, "github:" + URL)["revision"],
                         c.snapshot(self.db, "github:" + URL.replace("upstream/project", "Upstream/Project"))["revision"])

    def test_duplicate_local_branch_and_path_alias_refuse_even_when_filed(self):
        metadata = self.metadata()
        c.capture(self.db, title="Local", changes=metadata, who="operator")
        for changes in (metadata, {**metadata, "pull_url": URL},
                        {**metadata, "local_clone": str(Path(metadata["local_clone"]) / ".." / "clone")}):
            with self.assertRaisesRegex(WorkflowError, "clone and branch"):
                c.capture(self.db, title="Duplicate", changes=changes, who="operator")
        self.assertEqual(self.db.execute("SELECT count(*) FROM item").fetchone()[0], 1)


class ActivityTests(ContributionCase):
    def test_changes_requested_is_actionable_without_registering_a_task(self):
        result = self.observe(reviews=[{"id": "review:1", "actor_id": "2", "state": "CHANGES_REQUESTED", "at": T0}])
        self.assertTrue(result["attention"])
        self.assertEqual(self.db.execute("SELECT count(*) FROM item").fetchone()[0], 0)
        self.assertTrue(c.projection(self.db)[0]["needs_you"])

    def test_approval_supersedes_the_same_reviewers_old_changes_request(self):
        reviews = [{"id": "review:1", "actor_id": "2", "state": "CHANGES_REQUESTED", "at": T0}]
        self.observe(reviews=reviews)
        reviews.append({"id": "review:2", "actor_id": "2", "state": "APPROVED", "at": T1})
        result = self.observe(observed_at=T1, reviews=reviews)
        self.assertEqual(result["attention"], [])

    def test_external_comment_mention_label_draft_and_close_trigger(self):
        cases = [(self.event(), {}), (self.event(maintainer=False, mentions_operator=True), {}),
                 (self.event("label_added", label="blocked"), {"blocking_labels": ["blocked"]}),
                 (self.event("converted_to_draft"), {"draft": True}),
                 (self.event("closed"), {"state": "closed"})]
        for index, (event, changes) in enumerate(cases):
            url = URL + str(index)
            result = c.observe_pull(self.db, url, self.observation(events=[event], **changes),
                                    expected_revision=c.snapshot(self.db, "github:" + url)["revision"])
            self.assertTrue(result["attention"], event)

    def test_own_activity_and_unrelated_bot_labels_never_trigger(self):
        events = [self.event(actor_id="1"), self.event("label_added", actor_id="1", label="blocked"),
                  self.event("converted_to_draft", actor_id="1"), self.event("label_added", label="unrelated", actor_type="Bot")]
        result = self.observe(events=events, draft=True)
        self.assertEqual(result["attention"], [])

    def test_green_push_pending_then_new_head_failure_notifies_once(self):
        self.observe()
        pending = self.observe(observed_at=T1, head="c" * 40, ci="pending", ci_head="c" * 40, ci_ids=["check:2"])
        self.assertEqual(pending["attention"], [])
        failed = self.observe(observed_at=T2, head="c" * 40, ci="failure", ci_head="c" * 40, ci_ids=["check:2"])
        self.assertEqual(len(failed["attention"]), 1)
        repeated = self.observe(observed_at=T3, head="c" * 40, ci="failure", ci_head="c" * 40, ci_ids=["check:2"])
        self.assertEqual(failed["attention"], repeated["attention"])
        self.assertEqual(len(repeated["notifications"]), 1)

    def test_stale_head_checks_and_unknown_mergeability_never_invent_activity(self):
        before = self.observe()
        with self.assertRaises(WorkflowError):
            self.observe(observed_at=T1, head="c" * 40, ci="failure", ci_head="a" * 40)
        self.assertEqual(c.snapshot(self.db, "github:" + URL)["revision"], before["revision"])
        self.assertEqual(self.observe(observed_at=T1, mergeable="unknown")["attention"], [])

    def test_confirmed_base_conflict_is_actionable(self):
        self.observe()
        result = self.observe(observed_at=T1, base="d" * 40, mergeable="conflicting")
        self.assertEqual(len(result["attention"]), 1)

    def test_incomplete_and_stale_observations_preserve_attention_and_notifications(self):
        before = self.observe(observed_at=T1, events=[self.event()])
        result = c.observe_pull(self.db, URL, {"complete": False, "reason": "gh timed out", "observed_at": T2},
                                expected_revision=before["revision"])
        for key in ("revision", "observation", "attention", "notifications"):
            self.assertEqual(result[key], before[key])
        with self.assertRaises(WorkflowError):
            c.observe_pull(self.db, URL, self.observation(observed_at=T2), expected_revision="stale")
        with self.assertRaises(WorkflowError):
            self.observe(observed_at=T0)

    def test_label_removal_ready_and_reopen_clear_condition_attention(self):
        events = [self.event("label_added", id="label", label="blocked"),
                  self.event("converted_to_draft", id="draft"), self.event("closed", id="closed")]
        before = self.observe(events=events, draft=True, state="closed", blocking_labels=["blocked"])
        self.assertEqual(len(before["attention"]), 3)
        after = self.observe(observed_at=T1, events=events, labels=[], draft=False, state="open")
        self.assertEqual(after["attention"], [])
        self.assertEqual(after["notifications"], before["notifications"])

    def test_missing_identity_holds_and_does_not_reannounce_previous_event(self):
        before = self.observe(events=[self.event()])
        after = self.observe(observed_at=T1, operator={"id": None})
        self.assertEqual(after, before)
        self.assertEqual(c.projection(self.db)[0]["freshness"]["status"], "unknown")
        again = self.observe(observed_at=T2, events=[self.event()])
        self.assertEqual(again["notifications"], before["notifications"])

    def test_superseded_review_resolves_registered_followup_without_erasing_notice(self):
        c.capture(self.db, title="Filed", changes={"pull_url": URL}, who="operator")
        before = self.observe(reviews=[{"id": "one", "actor_id": "2", "state": "CHANGES_REQUESTED", "at": T0}])
        note = next(iter(before["notifications"].values()))["note_id"]
        after = self.observe(observed_at=T1, reviews=[{"id": "two", "actor_id": "2", "state": "APPROVED", "at": T1}])
        self.assertFalse(after["attention"])
        self.assertIsNotNone(self.db.execute("SELECT resolved_at FROM note WHERE id=?", (note,)).fetchone()[0])
        self.assertEqual(after["notifications"], before["notifications"])

    def test_foreign_authors_activity_is_not_the_operators_attention(self):
        result = self.observe(author={"id": "42"}, why=["watching"], events=[self.event()],
                              reviews=[{"id": "review", "actor_id": "2", "state": "CHANGES_REQUESTED", "at": T0}])
        self.assertEqual(result["attention"], [])
        self.assertFalse(c.projection(self.db)[0]["needs_you"])
        assigned = self.observe(observed_at=T1, author={"id": "42"}, why=["review-requested"])
        self.assertEqual(assigned["attention"][0]["id"], "context:review-requested")

    def test_latest_own_lifecycle_action_cannot_reactivate_historical_external_action(self):
        events = []
        for index, (kind, reset) in enumerate((("label_added", "label_removed"), ("converted_to_draft", "ready_for_review"), ("closed", "reopened"))):
            events += [self.event(kind, id=f"old{index}", label="blocked"),
                       self.event(reset, id=f"reset{index}", label="blocked", at=T1),
                       self.event(kind, id=f"own{index}", label="blocked", actor_id="1", at=T2)]
        result = self.observe(observed_at=T3, events=events, blocking_labels=["blocked"], draft=True, state="closed")
        self.assertEqual(result["attention"], [])

    def test_blocking_policy_change_refuses_both_old_revision_and_old_policy(self):
        item = c.capture(self.db, title="Filed", changes={"pull_url": URL}, who="operator")["item"]["id"]
        before = c.snapshot(self.db, "github:" + URL)
        c.configure(self.db, item, {"blocking_labels": ["blocked"]}, who="operator")
        with self.assertRaises(StaleItem):
            c.observe_pull(self.db, URL, self.observation(), expected_revision=before["revision"])
        with self.assertRaises(StaleItem):
            self.observe()
        result = self.observe(blocking_labels=["blocked"], events=[self.event("label_added", label="blocked")])
        self.assertEqual(len(result["attention"]), 1)

    def test_equal_timestamp_opinions_and_events_follow_source_order_not_lexical_ids(self):
        result = self.observe(reviews=[
            {"id": "review:9", "actor_id": "2", "state": "CHANGES_REQUESTED", "at": T0},
            {"id": "review:10", "actor_id": "2", "state": "APPROVED", "at": T0}],
            events=[self.event("closed", id="event:9"), self.event("closed", id="event:10", actor_id="1")], state="closed")
        self.assertEqual(result["attention"], [])
        opened = self.observe(observed_at=T1, reviews=[
            {"id": "review:9", "actor_id": "2", "state": "CHANGES_REQUESTED", "at": T0},
            {"id": "review:10", "actor_id": "2", "state": "APPROVED", "at": T0}])
        self.assertEqual(opened["attention"], [])

    def test_persistent_conflict_and_head_change_do_not_reannounce_after_ack(self):
        before = self.observe(mergeable="conflicting")
        key = "github:" + URL
        after = c.acknowledge(self.db, key, [before["attention"][0]["id"]], expected_revision=before["revision"], who="operator")
        unknown = self.observe(observed_at=T1, mergeable="unknown")
        repeated = self.observe(observed_at=T2, mergeable="conflicting", head="c" * 40, ci_head="c" * 40)
        self.assertEqual(unknown["attention"], [])
        self.assertEqual(repeated["attention"], [])
        self.assertEqual(repeated["notifications"], after["notifications"])


class DependencyTests(ContributionCase):
    def test_merge_and_published_release_with_package_are_separate_conditions(self):
        merge = {"kind": "merge", "url": DEPENDENCY}
        release = {"kind": "release", "repo": "upstream/library", "tag": "v1.2.3", "contains_pull": DEPENDENCY,
                   "package": {"name": "library", "version": "1.2.3"}}
        task = c.capture(self.db, title="Blocked", changes={"pull_url": URL, "depends_on": [merge, release]}, who="operator")
        item = task["item"]["id"]
        key = f"item:{item}"
        merged = {"dependency": merge, "state": "satisfied", "proof": {"url": DEPENDENCY, "merged": True,
                                                                                 "merge_commit_sha": "d" * 40}}
        waiting = {"dependency": release, "state": "pending", "proof": {}}
        first = c.observe_dependencies(self.db, item, [merged, waiting], observed_at=T0,
                                        expected_revision=c.snapshot(self.db, key)["revision"])
        self.assertEqual(first["attention"], [])
        ready = copy.deepcopy(waiting)
        ready.update(state="satisfied", proof={"repo": "upstream/library", "tag": "v1.2.3", "published": True, "merged": True,
            "tag_commit": "e" * 40, "merge_commit_sha": "d" * 40, "contains_pull": DEPENDENCY,
            "ancestor": True, "package": {"name": "library", "version": "1.2.3", "files": [{"filename": "library.whl", "sha256": "1" * 64, "yanked": False}]}})
        second = c.observe_dependencies(self.db, item, [merged, ready], observed_at=T1, expected_revision=first["revision"])
        self.assertEqual(len(second["attention"]), 1)
        self.assertEqual(c.projection(self.db)[0]["lane"], "newly_unblocked")
        self.assertEqual(item_state(self.db, item)["item"]["status"], "planning")
        self.assertEqual(self.db.execute("SELECT count(*) FROM note WHERE kind='followup'").fetchone()[0], 1)
        repeated = c.observe_dependencies(self.db, item, [merged, ready], observed_at=T2, expected_revision=second["revision"])
        self.assertEqual(second["notifications"], repeated["notifications"])

    def test_unknown_dependency_does_not_clear_a_known_state(self):
        task = c.capture(self.db, title="Blocked", changes={"pull_url": URL, "depends_on": [{"kind": "merge", "url": DEPENDENCY}]}, who="operator")
        item = task["item"]["id"]
        key = f"item:{item}"
        before = c.snapshot(self.db, key)
        result = c.observe_dependencies(self.db, item, [{"dependency": {"kind": "merge", "url": DEPENDENCY}, "state": "unknown"}],
                                        observed_at=T0, expected_revision=before["revision"])
        self.assertEqual(result["revision"], before["revision"])

    def test_item_dependency_uses_actual_status_and_ignores_supplied_success(self):
        prerequisite = c.capture(self.db, title="First", changes={"pull_url": DEPENDENCY}, who="operator")["item"]["id"]
        dependency = {"kind": "item", "item": prerequisite}
        item = c.capture(self.db, title="Second", changes={"pull_url": URL, "depends_on": [dependency]}, who="operator")["item"]["id"]
        key = f"item:{item}"
        first = c.observe_dependencies(self.db, item, [{"dependency": dependency, "state": "satisfied"}], observed_at=T0,
                                       expected_revision=c.snapshot(self.db, key)["revision"])
        self.assertFalse(first["observation"]["satisfied"])
        transition(self.db, prerequisite, "done", who="fixture")
        second = c.observe_dependencies(self.db, item, [], observed_at=T1, expected_revision=first["revision"])
        self.assertTrue(second["observation"]["satisfied"])
        self.assertEqual(c.projection(self.db)[0]["lane"], "newly_unblocked")

    def test_unproven_release_and_wrong_package_cannot_unblock(self):
        dependency = {"kind": "release", "repo": "upstream/library", "tag": "v1", "contains_pull": DEPENDENCY,
                      "package": {"name": "library", "version": "1"}}
        item = c.capture(self.db, title="Waiting", changes={"pull_url": URL, "depends_on": [dependency]}, who="operator")["item"]["id"]
        key = f"item:{item}"
        before = c.snapshot(self.db, key)
        proof = {"repo": "upstream/library", "tag": "v1", "contains_pull": DEPENDENCY, "published": True,
                 "ancestor": True, "merged": True, "merge_commit_sha": "d" * 40, "tag_commit": "e" * 40,
                 "package": {"name": "library", "version": "1", "files": [{"filename": "library.whl", "sha256": "f" * 64, "yanked": False}]}}
        for change in ({"ancestor": False}, {"merged": False}, {"tag": "v2"},
                       {"package": {"name": "library", "version": "2", "files": proof["package"]["files"]}}):
            result = c.observe_dependencies(self.db, item, [{"dependency": dependency, "state": "satisfied", "proof": {**proof, **change}}],
                                            observed_at=T0, expected_revision=before["revision"])
            self.assertEqual(result["revision"], before["revision"])

    def test_configuration_change_invalidates_inflight_dependency_proof(self):
        dependency = {"kind": "merge", "url": DEPENDENCY}
        item = c.capture(self.db, title="Waiting", changes={"pull_url": URL, "depends_on": [dependency]}, who="operator")["item"]["id"]
        before = c.snapshot(self.db, f"item:{item}")
        c.configure(self.db, item, {"depends_on": []}, who="operator")
        with self.assertRaises(StaleItem):
            c.observe_dependencies(self.db, item, [], observed_at=T0, expected_revision=before["revision"])

    def test_dependency_reconfiguration_resolves_the_old_unblock_followup(self):
        dependency = {"kind": "merge", "url": DEPENDENCY}
        item = c.capture(self.db, title="Waiting", changes={"pull_url": URL, "depends_on": [dependency]}, who="operator")["item"]["id"]
        key = f"item:{item}"
        ready = c.observe_dependencies(self.db, item, [{"dependency": dependency, "state": "satisfied",
                    "proof": {"url": DEPENDENCY, "merged": True, "merge_commit_sha": "a" * 40}}],
                    observed_at=T0, expected_revision=c.snapshot(self.db, key)["revision"])
        note = next(iter(ready["notifications"].values()))["note_id"]
        c.configure(self.db, item, {"depends_on": [{"kind": "merge", "url": DEPENDENCY.replace("34", "35")}]}, who="operator")
        self.assertFalse(c.snapshot(self.db, key)["attention"])
        self.assertIsNotNone(self.db.execute("SELECT resolved_at FROM note WHERE id=?", (note,)).fetchone()[0])

    def test_package_filename_must_be_a_nonempty_string(self):
        dependency = {"kind": "release", "repo": "upstream/library", "tag": "v1", "contains_pull": DEPENDENCY,
                      "package": {"name": "library", "version": "1"}}
        item = c.capture(self.db, title="Waiting", changes={"pull_url": URL, "depends_on": [dependency]}, who="operator")["item"]["id"]
        before = c.snapshot(self.db, f"item:{item}")
        proof = {"repo": "upstream/library", "tag": "v1", "contains_pull": DEPENDENCY, "published": True,
                 "merged": True, "ancestor": True, "tag_commit": "e" * 40, "merge_commit_sha": "d" * 40,
                 "package": {"name": "library", "version": "1", "files": [{"filename": True, "sha256": "f" * 64, "yanked": False}]}}
        result = c.observe_dependencies(self.db, item, [{"dependency": dependency, "state": "satisfied", "proof": proof}],
                                        observed_at=T0, expected_revision=before["revision"])
        self.assertEqual(result["revision"], before["revision"])


class NotificationTests(ContributionCase):
    def test_returning_context_has_a_new_episode_after_sent_or_acknowledged(self):
        for index, (why, author, acknowledged) in enumerate(
                (why, author, acknowledged) for why in ("assigned", "review-requested")
                for author in ("1", "2") for acknowledged in (False, True)):
            with self.subTest(why=why, author=author, acknowledged=acknowledged):
                url = URL + str(index)
                item = c.capture(self.db, title="Filed", changes={"pull_url": url}, who="operator")["item"]["id"]
                key = "github:" + url

                def observe(*, url=url, author=author, key=key, **changes):
                    return c.observe_pull(self.db, url, self.observation(
                        author={"id": author, "login": "author"}, **changes),
                        expected_revision=c.snapshot(self.db, key)["revision"])

                first = observe(why=[why])
                old_id = first["attention"][0]["id"]
                old_note = first["notifications"][old_id]["note_id"]
                claim = c.claim_notification(self.db, key, old_id, owner="first")
                sent = c.finish_notification(self.db, key, old_id, claim["token"], outcome="sent")
                if acknowledged:
                    sent = c.acknowledge(self.db, key, [old_id], expected_revision=sent["revision"], who="operator")
                repeated = observe(why=[why])
                self.assertEqual(repeated["notifications"], sent["notifications"])
                self.assertIsNone(c.claim_notification(self.db, key, old_id, owner="duplicate"))
                self.assertEqual(observe(why=[], observed_at=T1)["attention"], [])
                returned = observe(why=[why], observed_at=T2)
                self.assertEqual(len(returned["attention"]), 1)
                new_id = returned["attention"][0]["id"]
                self.assertNotEqual(new_id, old_id)
                self.assertEqual(returned["notifications"][old_id], sent["notifications"][old_id])
                new_note = returned["notifications"][new_id]["note_id"]
                self.assertNotEqual(new_note, old_note)
                notes = dict(self.db.execute("SELECT id,resolved_at FROM note WHERE item=? AND kind='followup'", (item,)))
                self.assertIsNotNone(notes[old_note])
                self.assertIsNone(notes[new_note])
                claim = c.claim_notification(self.db, key, new_id, owner="second")
                self.assertIsNotNone(claim)
                c.finish_notification(self.db, key, new_id, claim["token"], outcome="sent")
                unchanged = observe(why=[why], observed_at=T3)
                self.assertEqual(unchanged["attention"], returned["attention"])
                self.assertEqual(len(unchanged["notifications"]), 2)
                self.assertIsNone(c.claim_notification(self.db, key, new_id, owner="duplicate"))

    def test_legacy_acknowledged_context_keeps_identity_until_confirmed_absence(self):
        from sd_db.writes import record_state
        key = "github:" + URL
        first = self.observe(why=["review-requested"])
        event = first["attention"][0]["id"]
        acknowledged = c.acknowledge(self.db, key, [event], expected_revision=first["revision"], who="operator")
        legacy = {k: v for k, v in acknowledged.items() if k not in {"key", "revision", "context_events"}}
        record_state(self.db, "checkpoint", key="contribution:" + key, body=legacy)
        self.assertEqual(self.observe(why=["review-requested"])["attention"], [])
        self.observe(why=[], observed_at=T1)
        returned = self.observe(why=["review-requested"], observed_at=T2)
        self.assertEqual(len(returned["attention"]), 1)
        self.assertNotEqual(returned["attention"][0]["id"], event)
        self.assertEqual(returned["notifications"][event], acknowledged["notifications"][event])

    def test_context_episodes_are_independent(self):
        first = self.observe(why=["assigned", "review-requested"])
        old = {event["reason"]: event["id"] for event in first["attention"]}
        self.observe(why=["assigned"], observed_at=T1)
        returned = self.observe(why=["assigned", "review-requested"], observed_at=T2)
        current = {event["reason"]: event["id"] for event in returned["attention"]}
        self.assertEqual(current["Assigned"], old["Assigned"])
        self.assertNotEqual(current["Review requested"], old["Review requested"])

    def test_incomplete_context_observation_does_not_restart_acknowledged_episode(self):
        first = self.observe(why=["review-requested"])
        key = "github:" + URL
        event = first["attention"][0]["id"]
        acknowledged = c.acknowledge(self.db, key, [event], expected_revision=first["revision"], who="operator")
        held = c.observe_pull(self.db, URL, {"complete": False, "why": []},
                              expected_revision=acknowledged["revision"])
        self.assertEqual(held["revision"], acknowledged["revision"])
        repeated = self.observe(why=["review-requested"], observed_at=T1)
        self.assertEqual(repeated["attention"], [])
        self.assertEqual(repeated["notifications"], acknowledged["notifications"])

    def test_repeated_collect_and_concurrent_claims_send_at_most_once(self):
        before = self.observe(events=[self.event()])
        event = before["attention"][0]["id"]
        key = "github:" + URL
        claim = c.claim_notification(self.db, key, event, owner="worker-one")
        second = connect(self.path)
        try:
            self.assertIsNone(c.claim_notification(second, key, event, owner="worker-two"))
        finally:
            second.close()
        c.finish_notification(self.db, key, event, claim["token"], outcome="sent")
        self.observe(observed_at=T1, events=[self.event()])
        self.assertIsNone(c.claim_notification(self.db, key, event, owner="worker-two"))

    def test_ambiguous_delivery_is_held_and_ack_is_separate(self):
        current = self.observe(events=[self.event()])
        event = current["attention"][0]["id"]
        key = "github:" + URL
        claim = c.claim_notification(self.db, key, event, owner="worker")
        current = c.finish_notification(self.db, key, event, claim["token"], outcome="uncertain")
        self.assertIsNone(c.claim_notification(self.db, key, event, owner="retry"))
        self.assertTrue(current["attention"])
        acknowledged = c.acknowledge(self.db, key, [event], expected_revision=current["revision"], who="operator")
        self.assertEqual(acknowledged["attention"], [])
        repeated = self.observe(observed_at=T1, events=[self.event()])
        self.assertEqual(repeated["attention"], [])
        self.assertEqual(repeated["notifications"][event]["status"], "uncertain")

    def test_registered_pr_is_one_row_and_closed_unmerged_remains_visible(self):
        task = c.capture(self.db, title="Filed", changes={"pull_url": URL}, who="operator")
        upsert_shadow(self.db, tracker="github", url=URL, repo="upstream/project", kind="pull", state="closed")
        self.observe(state="closed", events=[self.event("closed")])
        rows = c.projection(self.db, repo="upstream/project")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["item_id"], task["item"]["id"])
        self.assertEqual(rows[0]["lane"], "awaiting_you")
        self.assertTrue(rows[0]["needs_you"])

    def test_unstarted_notification_can_retry_but_wrong_token_cannot_finish(self):
        state = self.observe(events=[self.event()])
        event = state["attention"][0]["id"]
        key = "github:" + URL
        claim = c.claim_notification(self.db, key, event, owner="first")
        with self.assertRaises(WorkflowError):
            c.finish_notification(self.db, key, event, "wrong", outcome="sent")
        c.finish_notification(self.db, key, event, claim["token"], outcome="not_started")
        self.assertIsNotNone(c.claim_notification(self.db, key, event, owner="second"))

    def test_ack_revision_refuses_stale_and_resolves_only_rendered_source(self):
        first = self.observe(events=[self.event()])
        current = self.observe(observed_at=T1, events=[self.event(), self.event(id="two")])
        with self.assertRaises(StaleItem):
            c.acknowledge(self.db, "github:" + URL, [first["attention"][0]["id"]], expected_revision=first["revision"], who="operator")
        result = c.acknowledge(self.db, "github:" + URL, [first["attention"][0]["id"]], expected_revision=current["revision"], who="operator")
        self.assertEqual(len(result["attention"]), 1)


class ProjectionTests(ContributionCase):
    def test_projection_repo_identities_keep_local_work_in_shared_order(self):
        metadata = self.metadata()
        metadata.pop("blocked_on")
        c.capture(self.db, title="Local", changes=metadata, who="operator")
        self.observe(state="merged")
        result = c.projection(self.db, repo=[metadata["local_clone"], "upstream/project"])
        self.assertEqual([r["lane"] for r in result], ["awaiting_you", "merged"])

    def test_unfiled_explicit_dependency_stays_awaiting_them_until_proven_unblock(self):
        metadata = self.metadata()
        dependency = {"kind": "merge", "url": DEPENDENCY}
        metadata["depends_on"] = [dependency]
        item = c.capture(self.db, title="Local", changes=metadata, who="operator")["item"]["id"]
        self.assertEqual(c.projection(self.db)[0]["lane"], "awaiting_them")
        key = f"item:{item}"
        c.observe_dependencies(self.db, item, [{"dependency": dependency, "state": "satisfied",
                    "proof": {"url": DEPENDENCY, "merged": True, "merge_commit_sha": "a" * 40}}],
                    observed_at=T0, expected_revision=c.snapshot(self.db, key)["revision"])
        self.assertEqual(c.projection(self.db)[0]["lane"], "newly_unblocked")

    def test_closed_unmerged_pull_takes_the_closed_lane(self):
        # The operator closed their own pull request: no attention, nobody left to wait on.
        self.observe(state="closed", events=[self.event("closed", actor_id="1")])
        [observed] = c.projection(self.db)
        self.assertIsNone(observed["item_id"])
        self.assertEqual((observed["lane"], observed["external_state"], observed["needs_you"]), ("closed", "closed", False))
        # A registered pull request a maintainer closed asks for the operator first, then closes.
        filed = URL.replace("12", "13")
        item = c.capture(self.db, title="Filed pull", changes={"pull_url": filed}, who="operator")["item"]["id"]
        key = "github:" + filed
        closed = c.observe_pull(self.db, filed, self.observation(state="closed", events=[self.event("closed")]),
                                expected_revision=c.snapshot(self.db, key)["revision"])
        by_url = {row["url"]: row for row in c.projection(self.db)}
        self.assertEqual((by_url[filed]["item_id"], by_url[filed]["lane"]), (item, "awaiting_you"))
        c.acknowledge(self.db, key, [e["id"] for e in closed["attention"]], who="operator", expected_revision=closed["revision"])
        by_url = {row["url"]: row for row in c.projection(self.db)}
        self.assertEqual({url: row["lane"] for url, row in by_url.items()}, {URL: "closed", filed: "closed"})
        # A reopen is live work again.
        self.observe(observed_at=T1, state="open", events=[self.event("reopened", actor_id="1", at=T1)])
        self.assertEqual({row["url"]: row["lane"] for row in c.projection(self.db)}, {URL: "awaiting_them", filed: "closed"})

    def test_tracker_context_preserves_observed_match_reasons_without_task_changes(self):
        from sd_db.progress import tracker_items
        upsert_shadow(self.db, tracker="github", url=URL, repo="upstream/project", kind="pull", state="open")
        self.observe(reviews=[{"id": "review", "actor_id": "2", "state": "CHANGES_REQUESTED", "at": T0}])
        result = tracker_items(self.db)[0]
        self.assertEqual(result["why"], ["author"])
        self.assertTrue(result["needs_you"])
        self.assertEqual(self.db.execute("SELECT count(*) FROM item").fetchone()[0], 0)

    def test_tracker_context_reads_an_observed_issue_by_its_issue_key(self):
        # An issue URL under the `github:` prefix is refused by `_key`, so
        # every tracker read with one observed issue raised (sd:1205).
        from sd_db.progress import tracker_items
        upsert_shadow(self.db, tracker="github", url=ISSUE, repo="upstream/project", kind="issue", state="open")
        self.store_issue(attention=[c._event("event:1", "New comment")])
        result = tracker_items(self.db)[0]
        self.assertEqual(result["why"], ["author"])
        self.assertTrue(result["needs_you"])
        self.assertEqual(result["attention"], ["New comment"])


class IssueTests(ContributionCase):
    def test_issue_url_has_its_own_validator_and_key_prefix(self):
        self.assertEqual(c._issue_url("https://github.com/Upstream/Project/issues/56/"), ISSUE)
        for value in (URL, ISSUE.replace("56", "0"), ISSUE + "x", None, 56):
            with self.assertRaisesRegex(WorkflowError, "issue_url must be a canonical GitHub issue URL"):
                c._issue_url(value)
        self.assertIsNone(c.PULL.fullmatch(ISSUE))
        with self.assertRaisesRegex(WorkflowError, "canonical GitHub pull-request URL"):
            c._url(ISSUE)
        self.assertEqual(c._key("issue:" + ISSUE.replace("upstream/project", "Upstream/Project")), "issue:" + ISSUE)
        for key in ("issues:" + ISSUE, "item:0", ISSUE):
            with self.assertRaisesRegex(WorkflowError, "item:ID, github:PR-URL or issue:ISSUE-URL"):
                c._key(key)
        with self.assertRaisesRegex(WorkflowError, "canonical GitHub issue URL"):
            c._key("issue:" + URL)
        with self.assertRaisesRegex(WorkflowError, "canonical GitHub pull-request URL"):
            c._key("github:" + ISSUE)

    def test_a_row_carries_exactly_one_identity(self):
        metadata = self.metadata()
        draft = self.draft()
        cases = [({"pull_url": URL, "issue_url": ISSUE}, "pull request or an issue, not both"),
                 ({**draft, **metadata}, "an issue draft has no clone or branch"),
                 ({**draft, "local_branch": "contribution"}, "an issue draft has no clone or branch"),
                 ({"target_repo": "upstream/project"}, "needs target_repo, draft_title and draft_path"),
                 ({**draft, "draft_path": None}, "needs target_repo, draft_title and draft_path"),
                 ({**metadata, "draft_title": "half a draft"}, "needs target_repo, draft_title and draft_path"),
                 ({}, "unfiled work needs local_clone and local_branch")]
        for changes, refusal in cases:
            with self.assertRaisesRegex(WorkflowError, refusal, msg=changes):
                c.capture(self.db, title="Refused", changes=changes, who="operator")
        self.assertEqual(self.db.execute("SELECT count(*) FROM item").fetchone()[0], 0)
        for changes in ({"pull_url": URL}, {"issue_url": ISSUE.replace("56", "57")}, draft, metadata):
            c.capture(self.db, title="Accepted", changes=changes, who="operator")
        self.assertEqual(self.db.execute("SELECT count(*) FROM item").fetchone()[0], 4)

    def test_filing_a_draft_keeps_its_item_and_draft_fields(self):
        draft = self.draft()
        item = c.capture(self.db, title="Draft", changes=draft, who="operator")["item"]["id"]
        before = c.projection(self.db)[0]
        self.assertIsNone(before["url"])
        self.assertEqual(before["repo"], "upstream/project")
        self.assertEqual(before["reasons"], ["Issue draft has not been filed"])
        self.assertEqual(before["lane"], "awaiting_you")
        after = c.configure(self.db, item, {"issue_url": ISSUE.replace("upstream/project", "Upstream/Project")}, who="operator")
        self.assertEqual(after["item"]["id"], item)
        stored = json.loads(after["item"]["fields"])["contribution"]
        self.assertEqual(stored, {**draft, "issue_url": ISSUE})
        rows = c.projection(self.db)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["url"], ISSUE)
        self.assertEqual(rows[0]["issue_url"], ISSUE)
        self.assertEqual(rows[0]["draft_title"], draft["draft_title"])
        self.assertEqual(rows[0]["draft_path"], draft["draft_path"])
        self.assertTrue(rows[0]["draft_verified"])
        self.assertEqual(rows[0]["reasons"], [])
        self.assertEqual(rows[0]["lane"], "awaiting_them")
        self.assertEqual(self.db.execute("SELECT count(*) FROM item").fetchone()[0], 1)

    def test_duplicate_issue_and_draft_refuse_without_creating_an_item(self):
        c.capture(self.db, title="Filed", changes={"issue_url": ISSUE}, who="operator")
        with self.assertRaisesRegex(WorkflowError, "this issue already belongs to another contribution"):
            c.capture(self.db, title="Duplicate", changes={"issue_url": ISSUE.replace("upstream/project", "Upstream/Project")}, who="operator")
        draft = self.draft()
        c.capture(self.db, title="Draft", changes=draft, who="operator")
        alias = {**draft, "draft_path": {**draft["draft_path"], "path": str(self.root / ".." / self.root.name / "draft.md")}}
        for changes in (draft, {**draft, "target_repo": "Upstream/Project"}, alias,
                        {**draft, "issue_url": ISSUE.replace("56", "57")}):
            with self.assertRaisesRegex(WorkflowError, "this issue draft already belongs to another contribution"):
                c.capture(self.db, title="Duplicate", changes=changes, who="operator")
        self.assertEqual(self.db.execute("SELECT count(*) FROM item").fetchone()[0], 2)
        c.capture(self.db, title="Same body, other project", changes={**draft, "target_repo": "upstream/library"}, who="operator")
        self.assertEqual(self.db.execute("SELECT count(*) FROM item").fetchone()[0], 3)

    def test_target_repo_must_be_a_repo_and_agree_with_issue_url(self):
        for changes, refusal in (({"issue_url": ISSUE, "target_repo": "other/repo"}, "target_repo does not match issue_url"),
                                 ({"issue_url": ISSUE, "target_repo": "not a repo"}, "target_repo must be owner/repo"),
                                 ({**self.draft(), "target_repo": "upstream"}, "target_repo must be owner/repo")):
            with self.assertRaisesRegex(WorkflowError, refusal):
                c.capture(self.db, title="Refused", changes=changes, who="operator")
        state = c.capture(self.db, title="Filed", changes={"issue_url": ISSUE, "target_repo": "Upstream/Project"}, who="operator")
        self.assertEqual(json.loads(state["item"]["fields"])["contribution"]["target_repo"], "Upstream/Project")
        self.assertEqual(c.projection(self.db)[0]["target_repo"], "Upstream/Project")

    def test_draft_digest_is_verified_on_write_and_rechecked_on_read(self):
        draft = self.draft()
        path = draft["draft_path"]["path"]
        missing = str(self.root / "missing.md")
        cases = [({**draft["draft_path"], "sha256": "0" * 64}, "issue draft hash differs from the recorded digest"),
                 ({**draft["draft_path"], "path": missing}, "issue draft could not be read"),
                 ({**draft["draft_path"], "path": str(self.root)}, "issue draft could not be read"),
                 ({**draft["draft_path"], "path": "draft.md"}, "absolute file path with a SHA256 digest"),
                 ({**draft["draft_path"], "sha256": "abc"}, "absolute file path with a SHA256 digest"),
                 ({**draft["draft_path"], "body": "inline"}, "absolute file path with a SHA256 digest"),
                 (path, "absolute file path with a SHA256 digest")]
        for value, refusal in cases:
            with self.assertRaisesRegex(WorkflowError, refusal, msg=value):
                c.capture(self.db, title="Refused", changes={**draft, "draft_path": value}, who="operator")
        self.assertEqual(self.db.execute("SELECT count(*) FROM item").fetchone()[0], 0)
        item = c.capture(self.db, title="Draft", changes=draft, who="operator")["item"]["id"]
        row = c.projection(self.db)[0]
        self.assertTrue(row["draft_verified"])
        self.assertEqual(row["draft_path"], draft["draft_path"])
        self.assertEqual(row["reasons"], ["Issue draft has not been filed"])
        Path(path).write_text("# Bug\n\nEdited after review.\n")
        row = c.projection(self.db)[0]
        self.assertEqual(row["item_id"], item)
        self.assertFalse(row["draft_verified"])
        self.assertEqual(row["reasons"], ["Issue draft has not been filed; draft body is unverified"])
        self.assertTrue(row["needs_you"])
        with self.assertRaisesRegex(WorkflowError, "hash differs"):
            c.configure(self.db, item, {"draft_title": "Retitled"}, who="operator")
        Path(path).unlink()
        self.assertFalse(c.projection(self.db)[0]["draft_verified"])

    def test_a_nul_in_a_draft_or_evidence_path_is_a_refusal_not_a_crash(self):
        # `Path.stat` raises ValueError, not OSError, on an embedded NUL (sd:1205).
        draft = self.draft()
        nul = {**draft["draft_path"], "path": str(self.root / "draft\0.md")}
        with self.assertRaisesRegex(WorkflowError, "issue draft could not be read"):
            c.capture(self.db, title="Refused", changes={**draft, "draft_path": nul}, who="operator")
        metadata = self.metadata()
        metadata["evidence"][0]["artifact"] = str(self.root / "acceptance\0.log")
        with self.assertRaisesRegex(WorkflowError, "evidence artifact could not be read"):
            c.capture(self.db, title="Refused", changes=metadata, who="operator")
        self.assertEqual(self.db.execute("SELECT count(*) FROM item").fetchone()[0], 0)
        # A stored NUL path, written before this check, reads as unverified.
        item = c.capture(self.db, title="Draft", changes=draft, who="operator")["item"]["id"]
        fields = json.loads(item_state(self.db, item)["item"]["fields"])
        fields["contribution"]["draft_path"] = nul
        set_item_fields(self.db, item, fields=fields)
        row = c.projection(self.db)[0]
        self.assertFalse(row["draft_verified"])
        self.assertEqual(row["reasons"], ["Issue draft has not been filed; draft body is unverified"])

    def test_a_present_identity_field_of_the_wrong_type_is_refused_even_when_falsy(self):
        # Truthiness let `draft_path=[]` and friends through unvalidated (sd:1205).
        draft = self.draft()
        cases = [({"issue_url": ISSUE, "draft_path": []}, "absolute file path with a SHA256 digest"),
                 ({"pull_url": URL, "draft_path": {}}, "absolute file path with a SHA256 digest"),
                 ({"pull_url": URL, "target_repo": ""}, "target_repo must be owner/repo"),
                 ({"issue_url": ISSUE, "target_repo": 0}, "target_repo must be owner/repo"),
                 ({"pull_url": URL, "draft_title": ""}, "draft_title must not be blank"),
                 ({"issue_url": ISSUE, "draft_title": False}, "draft_title must be text"),
                 ({**draft, "pull_url": ""}, "canonical GitHub pull-request URL"),
                 ({**draft, "issue_url": []}, "canonical GitHub issue URL"),
                 ({"pull_url": URL, "issue_url": ""}, "canonical GitHub issue URL")]
        for changes, refusal in cases:
            with self.assertRaisesRegex(WorkflowError, refusal, msg=changes):
                c.capture(self.db, title="Refused", changes=changes, who="operator")
        self.assertEqual(self.db.execute("SELECT count(*) FROM item").fetchone()[0], 0)
        # None stays the absent value.
        c.capture(self.db, title="Filed", changes={"pull_url": URL, "issue_url": None, "draft_path": None}, who="operator")
        self.assertEqual(self.db.execute("SELECT count(*) FROM item").fetchone()[0], 1)

    def test_closed_issue_takes_the_terminal_lane_after_every_open_row(self):
        self.assertNotIn(c.LANES["closed"], {c.LANES["merged"], c.LANES["awaiting_them"]})
        self.assertGreater(c.LANES["closed"], max(v for k, v in c.LANES.items() if k not in {"merged", "closed"}))
        registered = c.capture(self.db, title="Filed issue", changes={"issue_url": ISSUE}, who="operator")["item"]["id"]
        self.store_issue(state="closed", state_reason="completed")
        self.store_issue(url=ISSUE.replace("56", "57"), state="closed", state_reason="not_planned")
        self.store_issue(url=ISSUE.replace("56", "58"))
        c.capture(self.db, title="Filed pull", changes={"pull_url": URL}, who="operator")
        for suffix, changes in (("1", {"state": "merged"}), ("2", {"state": "closed", "events": [self.event("closed")]}),
                                ("3", {"reviews": [{"id": "review:1", "actor_id": "2", "state": "CHANGES_REQUESTED", "at": T0}]})):
            url = URL + suffix
            c.observe_pull(self.db, url, self.observation(**changes), expected_revision=c.snapshot(self.db, "github:" + url)["revision"])
        metadata = self.metadata()
        dependency = {"kind": "merge", "url": DEPENDENCY}
        metadata.update(blocked_on="", depends_on=[dependency])
        unblocked = c.capture(self.db, title="Local", changes=metadata, who="operator")["item"]["id"]
        c.observe_dependencies(self.db, unblocked, [{"dependency": dependency, "state": "satisfied",
                    "proof": {"url": DEPENDENCY, "merged": True, "merge_commit_sha": "a" * 40}}],
                    observed_at=T0, expected_revision=c.snapshot(self.db, f"item:{unblocked}")["revision"])
        rows = c.projection(self.db)
        lanes = [row["lane"] for row in rows]
        self.assertEqual(sorted(lanes, key=c.LANES.__getitem__), lanes)
        self.assertEqual(lanes.count("closed"), 2)
        self.assertEqual(lanes.count("merged"), 1)
        last_open = max(i for i, lane in enumerate(lanes) if lane not in {"merged", "closed"})
        self.assertTrue(all(i > last_open for i, lane in enumerate(lanes) if lane == "closed"))
        by_url = {row["url"]: row for row in rows}
        self.assertEqual(by_url[ISSUE]["item_id"], registered)
        self.assertEqual(by_url[ISSUE]["lane"], "closed")
        self.assertEqual(by_url[ISSUE]["external_state"], "closed")
        self.assertFalse(by_url[ISSUE]["needs_you"])
        self.assertEqual(by_url[ISSUE.replace("56", "57")]["lane"], "closed")
        self.assertEqual(by_url[ISSUE.replace("56", "58")]["lane"], "awaiting_them")
        self.assertEqual(by_url[URL + "2"]["lane"], "awaiting_you")
        self.assertEqual(by_url[URL + "1"]["lane"], "merged")
        self.assertEqual(lanes[0], "newly_unblocked")
        self.assertEqual(len(rows), len(by_url))

    def test_unregistered_issue_checkpoint_is_listed_once_and_registered_one_not_twice(self):
        self.store_issue(title="Observed issue", labels=["bug"])
        rows = c.projection(self.db)
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["item_id"])
        self.assertEqual(rows[0]["url"], ISSUE)
        self.assertEqual(rows[0]["key"], "issue:" + ISSUE)
        self.assertEqual(rows[0]["title"], "Observed issue")
        self.assertEqual(rows[0]["repo"], "upstream/project")
        self.assertEqual(rows[0]["lane"], "awaiting_them")
        self.assertEqual(rows[0]["observed_at"], T0)
        self.assertEqual(c.projection(self.db, repo="upstream/project"), rows)
        self.assertEqual(c.projection(self.db, repo="other/repo"), [])
        item = c.capture(self.db, title="Registered", changes={"issue_url": ISSUE}, who="operator")["item"]["id"]
        rows = c.projection(self.db)
        self.assertEqual([row["item_id"] for row in rows], [item])
        self.assertEqual(rows[0]["key"], "issue:" + ISSUE)
        self.assertEqual(rows[0]["title"], "Registered")
        self.assertIn("issue:" + ISSUE, rows[0]["notification_state"])

    def test_issue_attention_claims_fall_back_to_the_issue_url(self):
        state = self.store_issue(attention=[c._event("event:1", "New comment")])
        event = state["attention"][0]["id"]
        claim = c.claim_notification(self.db, "issue:" + ISSUE, event, owner="worker")
        self.assertEqual(claim["url"], ISSUE)
        self.assertEqual(claim["key"], "issue:" + ISSUE)
        row = c.projection(self.db)[0]
        self.assertEqual(row["lane"], "awaiting_you")
        self.assertEqual(row["reasons"], ["New comment"])
        finished = c.finish_notification(self.db, "issue:" + ISSUE, event, claim["token"], outcome="sent")
        acknowledged = c.acknowledge(self.db, "issue:" + ISSUE, [event], expected_revision=finished["revision"], who="operator")
        self.assertEqual(acknowledged["attention"], [])

    def test_blocking_label_change_invalidates_the_issue_snapshot(self):
        item = c.capture(self.db, title="Filed", changes={"issue_url": ISSUE}, who="operator")["item"]["id"]
        before = c.snapshot(self.db, "issue:" + ISSUE)
        c.configure(self.db, item, {"blocking_labels": ["blocked"]}, who="operator")
        self.assertNotEqual(c.snapshot(self.db, "issue:" + ISSUE)["revision"], before["revision"])
        self.assertEqual(self.db.execute("SELECT count(*) FROM state WHERE key LIKE 'contribution:github:%'").fetchone()[0], 0)
        draft = c.capture(self.db, title="Draft", changes=self.draft(), who="operator")["item"]["id"]
        c.configure(self.db, draft, {"blocking_labels": ["blocked"]}, who="operator")
        self.assertEqual(self.db.execute("SELECT count(*) FROM state WHERE key LIKE 'contribution:issue:%'").fetchone()[0], 1)


class IssueActivityTests(ContributionCase):
    def observe_issue(self, url=ISSUE, **changes):
        return c.observe_issue(self.db, url, self.issue_observation(**changes),
                               expected_revision=c.snapshot(self.db, "issue:" + url)["revision"])

    def issue_event(self, kind="comment", **changes):
        # A plain comment: neither a maintainer's nor a mention, which raises nothing on a pull.
        return self.event(kind, **{"maintainer": False, "mentions_operator": False, "url": ISSUE + "#issuecomment-1", **changes})

    def test_every_issue_event_kind_states_its_own_reason(self):
        cases = [(self.issue_event(maintainer=True), {}, "Maintainer comment"),
                 (self.issue_event(mentions_operator=True), {}, "Mentioned you"),
                 (self.issue_event(), {}, "New comment"),
                 (self.issue_event("label_added", label="blocked"), {"blocking_labels": ["blocked"], "labels": ["blocked"]}, "Blocking label: blocked"),
                 (self.issue_event("closed_completed"), {"state": "closed", "state_reason": "completed"}, "Closed as completed"),
                 (self.issue_event("closed_not_planned"), {"state": "closed", "state_reason": "not_planned"}, "Closed as not planned"),
                 (self.issue_event("closed"), {"state": "closed"}, "Closed"),
                 (self.issue_event("reopened"), {}, "Reopened")]
        for index, (event, changes, reason) in enumerate(cases):
            url = ISSUE.replace("56", str(60 + index))
            result = self.observe_issue(url, events=[event], **changes)
            self.assertEqual([(e["reason"], e["url"]) for e in result["attention"]], [(reason, event["url"])], event)
            self.assertNotIn("merge", reason)
            self.assertEqual(c.snapshot(self.db, "issue:" + url)["observation"]["events"], [event])
        self.assertEqual(self.db.execute("SELECT count(*) FROM item").fetchone()[0], 0)
        rows = c.projection(self.db)
        self.assertEqual(len(rows), len(cases))
        self.assertTrue(all(row["needs_you"] and row["lane"] == "awaiting_you" for row in rows))
        self.assertNotIn("Closed without merge", [reason for row in rows for reason in row["reasons"]])

    def test_own_actions_removed_labels_and_unblocking_labels_never_fire(self):
        events = [self.issue_event(actor_id="1"), self.issue_event(actor_id="1", maintainer=True, id="two"),
                  self.issue_event("closed_completed", actor_id="1", id="closed"),
                  self.issue_event("label_added", id="label", label="unrelated"),
                  self.issue_event("label_added", id="stale", label="blocked"),
                  self.issue_event("label_removed", id="removed", label="blocked")]
        result = self.observe_issue(events=events, state="closed", state_reason="completed",
                                    blocking_labels=["blocked"], labels=["unrelated"])
        self.assertEqual(result["attention"], [])
        self.assertEqual(result["notifications"], {})

    def test_lifecycle_families_collapse_to_the_latest_event(self):
        reopened = [self.issue_event("closed_completed", id="closed"), self.issue_event("reopened", id="reopened", actor_id="1", at=T1)]
        self.assertEqual(self.observe_issue(events=reopened)["attention"], [])
        reclosed = [self.issue_event("closed_not_planned", id="first"), self.issue_event("closed_completed", id="second", at=T1)]
        result = self.observe_issue(observed_at=T2, events=reclosed, state="closed", state_reason="completed")
        self.assertEqual([e["reason"] for e in result["attention"]], ["Closed as completed"])
        labels = [self.issue_event("label_added", id="added", label="blocked"),
                  self.issue_event("label_removed", id="removed", label="blocked", at=T1)]
        self.assertEqual(self.observe_issue(observed_at=T3, events=labels, blocking_labels=["blocked"], labels=["blocked"])["attention"], [])

    def test_closed_completed_issue_takes_the_closed_lane_once_acknowledged(self):
        item = c.capture(self.db, title="Filed", changes={"issue_url": ISSUE}, who="operator")["item"]["id"]
        result = self.observe_issue(events=[self.issue_event("closed_completed")], state="closed", state_reason="completed")
        row = c.projection(self.db)[0]
        self.assertEqual((row["item_id"], row["lane"], row["reasons"]), (item, "awaiting_you", ["Closed as completed"]))
        note = next(iter(result["notifications"].values()))["note_id"]
        self.assertEqual(self.db.execute("SELECT item, kind, resolved_at FROM note WHERE id=?", (note,)).fetchone()[:3], (item, "followup", None))
        c.acknowledge(self.db, "issue:" + ISSUE, [result["attention"][0]["id"]], expected_revision=result["revision"], who="operator")
        row = c.projection(self.db)[0]
        self.assertEqual((row["lane"], row["external_state"], row["needs_you"], row["reasons"]), ("closed", "closed", False, []))
        self.assertEqual(item_state(self.db, item)["item"]["status"], "planning")

    def test_unchanged_recollection_never_announces_twice(self):
        first = self.observe_issue(events=[self.issue_event()])
        second = self.observe_issue(observed_at=T1, events=[self.issue_event()])
        self.assertEqual(second["attention"], first["attention"])
        self.assertEqual(second["notifications"], first["notifications"])
        self.assertEqual(len(second["notifications"]), 1)
        held = c.observe_issue(self.db, ISSUE, {"complete": False, "reason": "gh timed out", "observed_at": T2},
                               expected_revision=second["revision"])
        self.assertEqual(held["revision"], second["revision"])
        self.assertEqual(c.projection(self.db)[0]["freshness"]["reason"], "Incomplete GitHub observation: gh timed out")
        with self.assertRaises(StaleItem):
            c.observe_issue(self.db, ISSUE, self.issue_observation(observed_at=T2), expected_revision="stale")
        with self.assertRaisesRegex(WorkflowError, "older than the last complete"):
            self.observe_issue(observed_at=T0)
        unknown = self.observe_issue(observed_at=T3, operator={"id": None})
        self.assertEqual(unknown["revision"], second["revision"])

    def test_issue_observation_refuses_pull_shapes_and_the_pull_vocabulary(self):
        before = c.snapshot(self.db, "issue:" + ISSUE)
        for changes, refusal in ((self.observation(), "pull-request fields"), ({"reviews": []}, "pull-request fields"),
                                 ({"state": "merged"}, "invalid issue lifecycle"), ({"state_reason": "duplicate"}, "invalid issue lifecycle"),
                                 ({"repo": "other/project"}, "differs from issue URL"), ({"events": None}, "events must be a list"),
                                 ({"events": [self.issue_event("converted_to_draft")]}, "unsupported contribution event kind"),
                                 ({"events": [self.issue_event("ready_for_review")]}, "unsupported contribution event kind")):
            with self.subTest(changes=changes), self.assertRaisesRegex(WorkflowError, refusal):
                self.observe_issue(**changes)
        with self.assertRaisesRegex(WorkflowError, "canonical GitHub issue URL"):
            c.observe_issue(self.db, URL, self.issue_observation(), expected_revision=before["revision"])
        self.assertEqual(c.snapshot(self.db, "issue:" + ISSUE), before)
        self.assertEqual(c.projection(self.db), [])
        # The pull path gains the two close reasons in its vocabulary and stays otherwise unchanged.
        result = self.observe(events=[self.event("closed_completed"), self.event("closed")], state="closed")
        self.assertEqual([e["reason"] for e in result["attention"]], ["Closed without merge"])

    def test_blocking_policy_change_refuses_the_old_issue_observation(self):
        item = c.capture(self.db, title="Filed", changes={"issue_url": ISSUE}, who="operator")["item"]["id"]
        before = c.snapshot(self.db, "issue:" + ISSUE)
        c.configure(self.db, item, {"blocking_labels": ["blocked"]}, who="operator")
        with self.assertRaises(StaleItem):
            c.observe_issue(self.db, ISSUE, self.issue_observation(), expected_revision=before["revision"])
        with self.assertRaises(StaleItem):
            self.observe_issue()
        result = self.observe_issue(blocking_labels=["blocked"], labels=["blocked"],
                                    events=[self.issue_event("label_added", label="blocked")])
        self.assertEqual([e["reason"] for e in result["attention"]], ["Blocking label: blocked"])


class IssueDependencyTests(ContributionCase):
    def test_issue_dependency_is_validated_and_a_cycle_through_it_is_still_refused(self):
        dependency = {"kind": "issue", "url": ISSUE}
        self.assertEqual(c._dependency(dependency), dependency)
        for bad in ({"kind": "issue", "url": URL}, {"kind": "issue"}, {"kind": "issue", "url": ISSUE, "tag": "v1"}):
            with self.subTest(bad=bad), self.assertRaises(WorkflowError):
                c._dependency(bad)
        first = c.capture(self.db, title="first", changes={"pull_url": URL, "depends_on": [dependency]}, who="operator")["item"]["id"]
        second = c.capture(self.db, title="second", changes={"pull_url": URL.replace("12", "13"),
                                                              "depends_on": [{"kind": "item", "item": first}]}, who="operator")["item"]["id"]
        with self.assertRaisesRegex(WorkflowError, "cycle"):
            c.configure(self.db, first, {"depends_on": [dependency, {"kind": "item", "item": second}]}, who="operator")
        stored = json.loads(item_state(self.db, first)["item"]["fields"])["contribution"]["depends_on"]
        self.assertEqual(stored, [dependency])
        self.assertEqual(c.projection(self.db)[0]["lane"], "awaiting_them")

    def test_issue_is_satisfied_by_a_completed_close_or_a_merged_pull_not_by_not_planned(self):
        dependency = {"kind": "issue", "url": ISSUE}
        completed = {"url": ISSUE, "closed": True, "state_reason": "completed"}
        merged = {"url": ISSUE, "closed": False, "state_reason": None, "merged_pull": DEPENDENCY}
        self.assertTrue(c._satisfied(dependency, completed))
        self.assertTrue(c._satisfied(dependency, merged))
        self.assertTrue(c._satisfied(dependency, {**completed, "merged_pull": DEPENDENCY}))
        for proof in ({"url": ISSUE, "closed": True, "state_reason": "not_planned"},
                      {"url": ISSUE, "closed": True, "state_reason": None},
                      {"url": ISSUE, "closed": False, "state_reason": "completed"},
                      {**completed, "url": ISSUE.replace("56", "57")},
                      {**merged, "merged_pull": DEPENDENCY.replace("pull", "issues")},
                      {**merged, "merged_pull": DEPENDENCY.replace("upstream", "Upstream")},
                      {**merged, "merged_pull": True}, {}, None):
            self.assertFalse(c._satisfied(dependency, proof), proof)
        item = c.capture(self.db, title="Waiting", changes={"pull_url": URL, "depends_on": [dependency]}, who="operator")["item"]["id"]
        key = f"item:{item}"
        before = c.snapshot(self.db, key)
        for row in ({"dependency": dependency, "state": "unknown"},
                    {"dependency": dependency, "state": "satisfied", "proof": {"url": ISSUE, "closed": True, "state_reason": "not_planned"}}):
            self.assertEqual(c.observe_dependencies(self.db, item, [row], observed_at=T0,
                                                    expected_revision=before["revision"])["revision"], before["revision"])
        pending = c.observe_dependencies(self.db, item, [{"dependency": dependency, "state": "pending", "proof": merged | {"merged_pull": None}}],
                                         observed_at=T0, expected_revision=before["revision"])
        self.assertFalse(pending["observation"]["satisfied"])
        self.assertEqual(c.projection(self.db)[0]["lane"], "awaiting_them")
        ready = c.observe_dependencies(self.db, item, [{"dependency": dependency, "state": "satisfied", "proof": merged}],
                                       observed_at=T1, expected_revision=pending["revision"])
        self.assertEqual([e["kind"] for e in ready["attention"]], ["newly_unblocked"])
        self.assertEqual(c.projection(self.db)[0]["lane"], "newly_unblocked")
        # An issue depending on an issue: the row's own identity does not matter to the kind.
        filed = c.capture(self.db, title="Filed issue", changes={"issue_url": ISSUE.replace("56", "57"), "depends_on": [dependency]}, who="operator")["item"]["id"]
        self.assertEqual([row["lane"] for row in c.projection(self.db) if row["item_id"] == filed], ["awaiting_them"])

    def test_issue_dependency_neither_triggers_nor_breaks_the_release_cross_proof(self):
        merge = {"kind": "merge", "url": DEPENDENCY}
        release = {"kind": "release", "repo": "upstream/library", "tag": "v1", "contains_pull": DEPENDENCY}
        issue = {"kind": "issue", "url": ISSUE}
        item = c.capture(self.db, title="Blocked", changes={"pull_url": URL, "depends_on": [merge, release, issue]}, who="operator")["item"]["id"]
        key = f"item:{item}"
        merged = {"dependency": merge, "state": "satisfied", "proof": {"url": DEPENDENCY, "merged": True, "merge_commit_sha": "d" * 40}}
        released = {"dependency": release, "state": "satisfied", "proof": {
            "repo": "upstream/library", "tag": "v1", "contains_pull": DEPENDENCY, "published": True, "ancestor": True,
            "merged": True, "merge_commit_sha": "d" * 40, "tag_commit": "e" * 40}}
        closed = {"dependency": issue, "state": "satisfied", "proof": {"url": ISSUE, "closed": True, "state_reason": "completed"}}
        before = c.snapshot(self.db, key)
        other = copy.deepcopy(released)
        other["proof"]["merge_commit_sha"] = "f" * 40
        held = c.observe_dependencies(self.db, item, [merged, other, closed], observed_at=T0, expected_revision=before["revision"])
        self.assertEqual(held["revision"], before["revision"])
        self.assertEqual(c.projection(self.db)[0]["freshness"]["reason"], "Release proof does not contain the configured merged commit")
        ready = c.observe_dependencies(self.db, item, [merged, released, closed], observed_at=T1, expected_revision=before["revision"])
        self.assertTrue(ready["observation"]["satisfied"])
        self.assertEqual(c.projection(self.db)[0]["lane"], "newly_unblocked")
